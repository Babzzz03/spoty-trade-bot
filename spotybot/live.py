"""Live match updates for followed fixtures (and automatically for corners leads).

Every POLL_SECONDS, for followed fixtures between 10 minutes before kick-off and 3 hours after,
fetch the fixture (1 request each) and message the followers about:
  kick-off, goals (with scorer when available), red cards, half-time and full-time
  (with the corner and card counts).
At most MAX_LIVE_FIXTURES are polled at once so the bot stays inside the Sportmonks free-plan
limit (180 requests per hour): 3 matches x 30 polls/hour = 90 requests.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from .history import STAT_CORNERS, STAT_GOALS, STAT_RED, STAT_YELLOW
from .sportmonks import FINISHED_STATE_IDS, SportmonksError, fixture_teams

log = logging.getLogger(__name__)

POLL_SECONDS = 120
MAX_LIVE_FIXTURES = 3
BEFORE = timedelta(minutes=10)
AFTER = timedelta(hours=3)
STALE = timedelta(hours=6)

LIVE_STATES = {2, 3, 4, 6, 9, 22}   # 1st half, HT, break, extra time, penalties, 2nd half
HALF_TIME = 3
GOAL_EVENTS = {14, 15, 16}          # goal, own goal, penalty
RED_EVENTS = {20, 21}               # red card, second yellow


@dataclass
class Snapshot:
    state_id: int | None
    hg: int
    ag: int
    corners: tuple[int, int] | None
    cards: int | None
    reds: tuple[int, int]
    events: dict[int, dict] = field(default_factory=dict)  # event id -> event

    @property
    def score(self) -> str:
        return f"{self.hg}–{self.ag}"


def snapshot(f: dict) -> Snapshot:
    stats: dict[tuple[int, str], int] = {}
    for s in f.get("statistics") or []:
        v = (s.get("data") or {}).get("value")
        if isinstance(v, (int, float)):
            stats[(s.get("type_id"), s.get("location"))] = int(v)
    goals = {"home": 0, "away": 0}
    for sc in f.get("scores") or []:
        if sc.get("description") == "CURRENT":
            part = (sc.get("score") or {}).get("participant")
            if part in goals:
                goals[part] = int((sc.get("score") or {}).get("goals") or 0)
    hg = stats.get((STAT_GOALS, "home"), goals["home"])
    ag = stats.get((STAT_GOALS, "away"), goals["away"])
    corners = None
    if (STAT_CORNERS, "home") in stats or (STAT_CORNERS, "away") in stats:
        corners = (stats.get((STAT_CORNERS, "home"), 0), stats.get((STAT_CORNERS, "away"), 0))
    has_cards = any(k[0] in (STAT_YELLOW, STAT_RED) for k in stats)
    cards = sum(v for (t, _), v in stats.items() if t in (STAT_YELLOW, STAT_RED)) if has_cards else None
    reds = (stats.get((STAT_RED, "home"), 0), stats.get((STAT_RED, "away"), 0))
    events = {e["id"]: e for e in f.get("events") or [] if "id" in e}
    return Snapshot(f.get("state_id"), hg, ag, corners, cards, reds, events)


def _extras(s: Snapshot) -> str:
    bits = []
    if s.corners is not None:
        bits.append(f"corners {sum(s.corners)} ({s.corners[0]}–{s.corners[1]})")
    if s.cards is not None:
        bits.append(f"cards {s.cards}")
    return " · ".join(bits)


def diff_messages(prev: Snapshot | None, cur: Snapshot, home: str, away: str) -> list[str]:
    """Human-readable updates between two snapshots of the same fixture."""
    h, a = escape(home), escape(away)
    line = f"<b>{h} {cur.score} {a}</b>"
    msgs: list[str] = []
    if prev is None:
        if cur.state_id in FINISHED_STATE_IDS:
            return [f"🏁 Full-time: {line}" + (f"\n{_extras(cur)}" if _extras(cur) else "")]
        if cur.state_id in LIVE_STATES:
            return [f"📺 Live now: {line}" + (f"\n{_extras(cur)}" if _extras(cur) else "")]
        return []
    if prev.state_id not in LIVE_STATES and prev.state_id not in FINISHED_STATE_IDS and cur.state_id in LIVE_STATES:
        msgs.append(f"🟢 Kick-off: {h} vs {a}")
    new_events = [e for eid, e in cur.events.items() if eid not in prev.events]
    if (cur.hg, cur.ag) != (prev.hg, prev.ag):
        scorers = [e for e in new_events if e.get("type_id") in GOAL_EVENTS]
        who = ""
        if scorers:
            e = scorers[-1]
            who = f" — {escape(str(e.get('player_name') or ''))}" + (f" {e['minute']}'" if e.get("minute") else "")
        msgs.append(f"⚽ GOAL! {line}{who}")
    if sum(cur.reds) > sum(prev.reds) or any(e.get("type_id") in RED_EVENTS for e in new_events):
        reds = [e for e in new_events if e.get("type_id") in RED_EVENTS]
        who = f" — {escape(str(reds[-1].get('player_name') or ''))}" if reds else ""
        msgs.append(f"🟥 Red card{who} · {line}")
    if cur.state_id == HALF_TIME and prev.state_id != HALF_TIME:
        msgs.append(f"⏸ Half-time: {line}" + (f"\n{_extras(cur)}" if _extras(cur) else ""))
    if cur.state_id in FINISHED_STATE_IDS and prev.state_id not in FINISHED_STATE_IDS:
        msgs.append(f"🏁 Full-time: {line}" + (f"\n{_extras(cur)}" if _extras(cur) else ""))
    return msgs


def _utc(kickoff: str) -> datetime:
    return datetime.fromisoformat(kickoff.replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)


async def poll(app: Application) -> None:
    storage, client = app.bot_data["storage"], app.bot_data["client"]
    state: dict[int, Snapshot] = app.bot_data.setdefault("live_state", {})
    now = datetime.now(timezone.utc)
    chats: dict[int, list[int]] = defaultdict(list)
    info: dict[int, tuple[str, str, str]] = {}
    for fl in await storage.active_follows():
        ko = _utc(fl.kickoff)
        if now > ko + STALE:
            await storage.finish_follows(fl.fixture_id)
            continue
        if ko - BEFORE <= now <= ko + AFTER:
            chats[fl.fixture_id].append(fl.chat_id)
            info[fl.fixture_id] = (fl.kickoff, fl.home, fl.away)
    for fixture_id in sorted(chats, key=lambda fid: info[fid][0])[:MAX_LIVE_FIXTURES]:
        try:
            f = await client.live_fixture(fixture_id)
        except SportmonksError as e:
            log.warning("live poll %s failed: %s", fixture_id, e)
            return
        try:
            home, away = (t.get("name", "?") for t in fixture_teams(f))
        except Exception:
            _, home, away = info[fixture_id]
        cur = snapshot(f)
        msgs = diff_messages(state.get(fixture_id), cur, home, away)
        state[fixture_id] = cur
        for chat_id in chats[fixture_id]:
            for m in msgs:
                try:
                    await app.bot.send_message(chat_id, m, parse_mode=ParseMode.HTML)
                except TelegramError:
                    log.exception("live message to %s failed", chat_id)
        if cur.state_id in FINISHED_STATE_IDS:
            await storage.finish_follows(fixture_id)
            state.pop(fixture_id, None)


async def _poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await poll(ctx.application)


def start_live(app: Application) -> None:
    app.job_queue.run_repeating(_poll_job, interval=POLL_SECONDS, first=20, name="live-poll")
