"""Automatic lead alerts, 1-hour-before re-checks, and the paper-trading scorecard.

Flow:
  * Daily scan (SCAN_TIME, local) + on startup: analyse today's and tomorrow's upcoming fixtures.
    Every new corners lead is saved to the scorecard and sent to all subscribers.
  * 1 hour before kick-off of each lead: re-analyse (fresh odds, line-ups) and send an update.
  * Every hour: settle finished leads from the real corner count and send the result.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from .analysis import CORNERS_MIN_EDGE, Analysis, analyse_fixture
from .formatting import kickoff_long
from .history import STAT_CORNERS
from .sportmonks import FINISHED_STATE_IDS, SportmonksError
from .storage import LeadRecord, Storage

log = logging.getLogger(__name__)

NOT_STARTED = (None, 1)
SETTLE_AFTER = timedelta(hours=2, minutes=30)
VOID_AFTER = timedelta(days=3)


def _utc(kickoff: str) -> datetime:
    return datetime.fromisoformat(kickoff.replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)


def _storage(app: Application) -> Storage:
    return app.bot_data["storage"]


async def broadcast(app: Application, text: str) -> None:
    storage = _storage(app)
    for chat_id in await storage.subscribers():
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden:
            await storage.unsubscribe(chat_id)  # user blocked the bot
        except TelegramError:
            log.exception("failed to send alert to %s", chat_id)


def lead_text(a: Analysis, tz: str, title: str) -> str:
    ld = a.corners_lead
    min_price = (1 + CORNERS_MIN_EDGE) / ld.prob
    return (
        f"{title}\n\n"
        f"<b>{escape(a.home)} vs {escape(a.away)}</b>\n"
        f"{escape(a.league)} · {kickoff_long(a.fixture, tz)}\n\n"
        f"🎯 <b>{ld.side} {ld.line:g} corners</b>\n"
        f"Our chance: <b>{ld.prob * 100:.0f}%</b> · fair odds {ld.fair_price:.2f}\n"
        f"✅ Take it only at odds of <b>{min_price:.2f} or higher</b> on SportyBet\n"
        f"<i>(average elsewhere {ld.avg_price:.2f}, best {ld.best_price:.2f}; "
        f"our model expects {a.corners_expected:.1f} corners)</i>\n"
        f"Line-ups: {'confirmed ✅' if a.lineups_confirmed else 'not out yet'}\n\n"
        f"🧪 Experimental — paper-trade only until /record proves it.\n"
        f"Full analysis: /predict {a.fixture['id']} · check odds: /value {a.fixture['id']} &lt;odds&gt;"
    )


# ------------------------------------------------------------------ scanning

async def scan(app: Application, announce: bool = True) -> int:
    """Analyse today's + tomorrow's upcoming fixtures; save and announce new leads. Returns new lead count."""
    s = app.bot_data["settings"]
    client, store, storage = app.bot_data["client"], app.bot_data["store"], _storage(app)
    today = datetime.now(ZoneInfo(s.timezone)).date()
    new = 0
    for day in (today, today + timedelta(days=1)):
        try:
            fixtures = await client.fixtures_by_date(day, s.default_league_ids or None)
        except SportmonksError as e:
            log.warning("scan: fixtures for %s failed: %s", day, e)
            continue
        for f in fixtures:
            if f.get("state_id") not in NOT_STARTED or await storage.lead_for(f["id"]):
                continue
            try:
                a = await analyse_fixture(client, store, f["id"])
            except SportmonksError as e:
                log.warning("scan: analysis of %s failed: %s", f["id"], e)
                break  # most likely the hourly limit; try again next run
            except Exception:
                log.exception("scan: analysis of %s crashed", f["id"])
                continue
            ld = a.corners_lead
            if not ld:
                continue
            added = await storage.add_lead(
                fixture_id=f["id"], kickoff=a.kickoff_utc, home=a.home, away=a.away, league=a.league,
                market="Corners", side=ld.side, line=ld.line, prob=ld.prob,
                min_price=(1 + CORNERS_MIN_EDGE) / ld.prob, avg_price=ld.avg_price, best_price=ld.best_price)
            if added:
                new += 1
                lead = await storage.lead_for(f["id"])
                schedule_precheck(app, lead)
                if announce:
                    await broadcast(app, lead_text(a, s.timezone, "🔔 <b>New corners lead</b>"))
    log.info("scan finished: %d new lead(s)", new)
    return new


async def _scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await scan(ctx.application)


# ------------------------------------------------------------------ 1 hour before kick-off

def schedule_precheck(app: Application, lead: LeadRecord) -> None:
    if lead.precheck_sent or lead.status != "open":
        return
    ko = _utc(lead.kickoff)
    now = datetime.now(timezone.utc)
    if ko <= now:
        return
    when = max(ko - timedelta(hours=1), now + timedelta(seconds=10))
    name = f"precheck:{lead.id}"
    if not app.job_queue.get_jobs_by_name(name):
        app.job_queue.run_once(_precheck_job, when=when, data=lead.fixture_id, name=name)


async def _precheck_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    app = ctx.application
    s, storage = app.bot_data["settings"], _storage(app)
    lead = await storage.lead_for(ctx.job.data)
    if not lead or lead.precheck_sent:
        return
    try:
        a = await analyse_fixture(app.bot_data["client"], app.bot_data["store"], lead.fixture_id)
    except SportmonksError as e:
        log.warning("precheck %s failed: %s", lead.fixture_id, e)
        return
    if a.corners_lead and a.corners_lead.side == lead.side and a.corners_lead.line == lead.line:
        text = lead_text(a, s.timezone, "⏰ <b>Kick-off in ~1 hour — lead still valid</b>")
    else:
        text = (f"⏰ <b>Kick-off in ~1 hour — lead has weakened</b>\n\n"
                f"<b>{escape(lead.home)} vs {escape(lead.away)}</b>: {escape(lead.label)}\n"
                f"With the latest odds{' and line-ups' if a.lineups_confirmed else ''}, the edge is gone. "
                f"If you haven't taken it yet, <b>skip it</b>. (It stays on the scorecard as first recorded.)")
    await broadcast(app, text)
    await storage.mark_precheck(lead.id)


# ------------------------------------------------------------------ settling

def settle_value(side: str, line: float, total: int) -> str:
    if total == line:
        return "push"
    return "won" if (total > line) == (side == "Over") else "lost"


async def settle(app: Application) -> int:
    client, storage = app.bot_data["client"], _storage(app)
    now = datetime.now(timezone.utc)
    done = 0
    for lead in await storage.open_leads():
        ko = _utc(lead.kickoff)
        if now < ko + SETTLE_AFTER:
            continue
        try:
            f = await client.fixture_result(lead.fixture_id)
        except SportmonksError as e:
            log.warning("settle %s failed: %s", lead.fixture_id, e)
            break
        corners = [s["data"].get("value") for s in f.get("statistics") or [] if s.get("type_id") == STAT_CORNERS]
        if f.get("state_id") in FINISHED_STATE_IDS and len(corners) == 2 and all(isinstance(c, int) for c in corners):
            total = sum(corners)
            status = settle_value(lead.side, lead.line, total)
            await storage.settle(lead.id, status, total)
            icon = {"won": "✅ WON", "lost": "❌ LOST", "push": "➖ PUSH (stake back)"}[status]
            await broadcast(app, f"📋 <b>Result:</b> {escape(lead.home)} vs {escape(lead.away)} — "
                                 f"{escape(lead.label)}: <b>{icon}</b> ({total} corners)\nScorecard: /record")
            done += 1
        elif now > ko + VOID_AFTER:
            await storage.settle(lead.id, "void", None)
    return done


async def _settle_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await settle(ctx.application)


# ------------------------------------------------------------------ scorecard

def scorecard_text(settled: list[LeadRecord], open_: list[LeadRecord], recent: list[LeadRecord]) -> str:
    n = len(settled)
    won = sum(l.status == "won" for l in settled)
    lost = sum(l.status == "lost" for l in settled)
    push = sum(l.status == "push" for l in settled)
    profit = sum(l.profit for l in settled)
    L = ["📊 <b>Corners leads scorecard</b> (paper trading, 1 unit per lead, at the average price when spotted)", ""]
    if n:
        decided = [l for l in settled if l.status != "push"]
        roi = profit / n
        sd = math.sqrt(sum((l.profit - roi) ** 2 for l in settled) / max(n - 1, 1))
        se = sd / math.sqrt(n)
        L += [f"Settled: <b>{n}</b>  (won {won} · lost {lost} · push {push})",
              f"Win rate: {won / max(len(decided), 1):.0%}",
              f"Profit: <b>{profit:+.2f} units</b>  ·  ROI {roi:+.1%} ± {se:.1%}"]
        if n < 200:
            L.append(f"<i>Too early to judge: luck alone moves ROI by about ±{se:.0%} at this sample size. "
                     f"Aim for 200+ leads.</i>")
        elif roi - 2 * se > 0:
            L.append("<i>Profit is now more than 2 standard errors above zero — real evidence of an edge.</i>")
        else:
            L.append("<i>Not (yet) distinguishable from luck.</i>")
    else:
        L.append("No settled leads yet.")
    L += ["", f"Open leads: {len(open_)}"]
    if recent:
        L += ["", "<b>Latest</b>"]
        icons = {"won": "✅", "lost": "❌", "push": "➖", "open": "⏳", "void": "🚫"}
        for l in recent:
            res = f" ({l.result_value})" if l.result_value is not None else ""
            L.append(f"{icons.get(l.status, '?')} {l.kickoff[:10]} {escape(l.home)}–{escape(l.away)}: "
                     f"{escape(l.label)} @ {l.avg_price:.2f}{res}")
    return "\n".join(L)


# ------------------------------------------------------------------ wiring

async def start_background(app: Application) -> None:
    """Create tables, re-schedule pending pre-checks, and start the daily scan + hourly settling."""
    s, storage = app.bot_data["settings"], _storage(app)
    await storage.init()
    for lead in await storage.open_leads():
        schedule_precheck(app, lead)
    hh, mm = (int(x) for x in s.scan_time.split(":"))
    app.job_queue.run_daily(_scan_job, time=dtime(hh, mm, tzinfo=ZoneInfo(s.timezone)), name="daily-scan")
    app.job_queue.run_once(_scan_job, when=30, name="startup-scan")
    app.job_queue.run_repeating(_settle_job, interval=3600, first=60, name="settle")
    log.info("Background jobs started (storage: %s, daily scan %s %s)", storage.description, s.scan_time, s.timezone)
