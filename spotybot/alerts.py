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
from .formatting import kickoff_long, tips_message
from .live import snapshot, start_live
from .markets import Selection, main_picks, settle as settle_selection
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


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Split on blank lines so each part stays under Telegram's 4096-character limit."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) > limit and cur:
            parts.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


async def broadcast(app: Application, text: str) -> None:
    storage = _storage(app)
    for chat_id in await storage.subscribers():
        try:
            for part in split_message(text):
                await app.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
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
        f"Full analysis: /p_{a.fixture['id']} · check SportyBet odds: /v_{a.fixture['id']}"
    )


# ------------------------------------------------------------------ scanning

async def scan(app: Application, announce: bool = True, digest: bool = False) -> int:
    """Analyse today's + tomorrow's upcoming fixtures.

    * today's matches: record the bot's main predictions once (accuracy tracker)
    * any match: save and announce new corners leads
    * digest=True (the 08:00 job): send subscribers today's predictions
    Returns the number of new corners leads.
    """
    s = app.bot_data["settings"]
    client, store, storage = app.bot_data["client"], app.bot_data["store"], _storage(app)
    today = datetime.now(ZoneInfo(s.timezone)).date()
    new = 0
    today_items: list[Analysis] = []
    for day in (today, today + timedelta(days=1)):
        try:
            fixtures = await client.fixtures_by_date(day, s.default_league_ids or None)
        except SportmonksError as e:
            log.warning("scan: fixtures for %s failed: %s", day, e)
            continue
        is_today = day == today
        for f in sorted(fixtures, key=lambda f: f.get("starting_at") or ""):
            if f.get("state_id") not in NOT_STARTED:
                continue
            have_lead = await storage.lead_for(f["id"]) is not None
            if not is_today and have_lead:
                continue
            try:
                a = await analyse_fixture(client, store, f["id"])
            except SportmonksError as e:
                log.warning("scan: analysis of %s failed: %s", f["id"], e)
                break  # most likely the hourly limit; try again next run
            except Exception:
                log.exception("scan: analysis of %s crashed", f["id"])
                continue
            if is_today:
                today_items.append(a)
                if not await storage.has_predictions(f["id"]):
                    for sel, prob, source in main_picks(a):
                        await storage.record_prediction(
                            fixture_id=f["id"], kickoff=a.kickoff_utc, home=a.home, away=a.away, league=a.league,
                            market=sel.market, side=sel.side, line=sel.line, prob=prob, source=source)
            ld = a.corners_lead
            if not ld or have_lead:
                continue
            added = await storage.add_lead(
                fixture_id=f["id"], kickoff=a.kickoff_utc, home=a.home, away=a.away, league=a.league,
                market="Corners", side=ld.side, line=ld.line, prob=ld.prob,
                min_price=(1 + CORNERS_MIN_EDGE) / ld.prob, avg_price=ld.avg_price, best_price=ld.best_price)
            if added:
                new += 1
                lead = await storage.lead_for(f["id"])
                schedule_precheck(app, lead)
                for chat_id in await storage.subscribers():  # live updates for every lead
                    await storage.follow(chat_id, f["id"], a.kickoff_utc, a.home, a.away)
                if announce:
                    await broadcast(app, lead_text(a, s.timezone, "🔔 <b>New corners lead</b>"))
    if digest and today_items:
        await broadcast(app, "☀️ <b>Good morning!</b> Here are today's predictions.\n\n"
                        + tips_message(today_items, s.timezone, today))
    log.info("scan finished: %d new lead(s), %d match(es) today", new, len(today_items))
    return new


async def _scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await scan(ctx.application, digest=True)


async def _startup_scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    """Settle finished corners leads and the user's recorded value checks."""
    client, storage = app.bot_data["client"], _storage(app)
    now = datetime.now(timezone.utc)
    done = 0
    leads = await storage.open_leads()
    checks = await storage.open_value_checks()
    preds = await storage.open_predictions()
    due = sorted({x.fixture_id for x in leads + checks + preds if now >= _utc(x.kickoff) + SETTLE_AFTER})
    for fid in due:
        try:
            f = await client.fixture_result(fid)
        except SportmonksError as e:
            log.warning("settle %s failed: %s", fid, e)
            break
        finished = f.get("state_id") in FINISHED_STATE_IDS
        snap = snapshot(f)
        corners = sum(snap.corners) if snap.corners is not None else None
        for lead in (l for l in leads if l.fixture_id == fid):
            if finished and corners is not None:
                status = settle_value(lead.side, lead.line, corners)
                await storage.settle(lead.id, status, corners)
                icon = {"won": "✅ WON", "lost": "❌ LOST", "push": "➖ PUSH (stake back)"}[status]
                await broadcast(app, f"📋 <b>Result:</b> {escape(lead.home)} vs {escape(lead.away)} — "
                                     f"{escape(lead.label)}: <b>{icon}</b> ({corners} corners)\nScorecard: /record")
                done += 1
            elif now > _utc(lead.kickoff) + VOID_AFTER:
                await storage.settle(lead.id, "void", None)
        for c in (c for c in checks if c.fixture_id == fid):
            sel = Selection(c.market, c.side, c.line)
            status = settle_selection(sel, snap.hg, snap.ag, corners, snap.cards) if finished else None
            if status:
                await storage.settle_value_check(c.id, status)
                icon = {"won": "✅ WON", "lost": "❌ LOST", "push": "➖ PUSH"}[status]
                try:
                    await app.bot.send_message(
                        c.chat_id, f"📋 Your value check: {escape(c.home)} vs {escape(c.away)} — "
                                   f"{escape(sel.label)} @ {c.odds:.2f}: <b>{icon}</b> (final {snap.score})",
                        parse_mode=ParseMode.HTML)
                except TelegramError:
                    log.exception("value-check result to %s failed", c.chat_id)
                done += 1
            elif now > _utc(c.kickoff) + VOID_AFTER:
                await storage.settle_value_check(c.id, "void")
        for pr in (p for p in preds if p.fixture_id == fid):
            status = settle_selection(Selection(pr.market, pr.side, pr.line), snap.hg, snap.ag, corners,
                                      snap.cards) if finished else None
            if status:
                await storage.settle_prediction(pr.id, status)
                done += 1
            elif now > _utc(pr.kickoff) + VOID_AFTER:
                await storage.settle_prediction(pr.id, "void")
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


def predictions_scorecard_text(preds) -> str:
    """Accuracy of the bot's main predictions, per type, vs what it expected."""
    settled = [p for p in preds if p.status in ("won", "lost")]
    L = ["🎯 <b>Prediction accuracy</b> (every match-day prediction, checked against the real result)"]
    if not settled:
        L.append("Nothing settled yet — predictions are recorded each morning for that day's matches.")
        return "\n".join(L)
    groups = [("🏆 Winner", lambda p: p.market == "Result"),
              ("📈 Goals over", lambda p: p.market == "Goals" and p.side == "Over"),
              ("📉 Goals under", lambda p: p.market == "Goals" and p.side == "Under"),
              ("🤝 Both teams score", lambda p: p.market == "BTTS")]
    for name, pick in groups + [("<b>All predictions</b>", lambda p: True)]:
        g = [p for p in settled if pick(p)]
        if not g:
            continue
        right = sum(p.status == "won" for p in g)
        expected = sum(p.prob for p in g) / len(g)
        L.append(f"{name}: {right}/{len(g)} right (<b>{right / len(g):.0%}</b>) · expected {expected:.0%}")
    n = len(settled)
    if n < 100:
        L.append(f"<i>Small sample ({n}) — percentages will settle down after 100+ predictions.</i>")
    else:
        L.append("<i>'Expected' is the average chance the bot gave. Close to the real % = honest predictions.</i>")
    return "\n".join(L)


def value_scorecard_text(checks) -> str:
    settled = [c for c in checks if c.status in ("won", "lost", "push")]
    open_n = sum(c.status == "open" for c in checks)
    L = ["", "💱 <b>Your ✅ value checks</b> (odds you saw, e.g. on SportyBet; 1 unit each)"]
    if not settled:
        L.append(f"None settled yet. Open: {open_n}. Use /value &lt;fixture_id&gt; &lt;bet&gt; &lt;odds&gt;.")
        return "\n".join(L)
    n = len(settled)
    profit = sum(c.profit for c in settled)
    roi = profit / n
    sd = math.sqrt(sum((c.profit - roi) ** 2 for c in settled) / max(n - 1, 1))
    se = sd / math.sqrt(n)
    won = sum(c.status == "won" for c in settled)
    L.append(f"Settled {n} (won {won}) · open {open_n} · profit <b>{profit:+.2f} units</b> · ROI {roi:+.1%} ± {se:.1%}")
    if n < 200:
        L.append("<i>Too early to judge — aim for 200+.</i>")
    return "\n".join(L)


# ------------------------------------------------------------------ wiring

async def start_background(app: Application) -> None:
    """Create tables, re-schedule pending pre-checks, and start the daily scan + hourly settling."""
    s, storage = app.bot_data["settings"], _storage(app)
    await storage.init()
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("upcoming", "Matches in the next 7 days"),
            BotCommand("tips", "Predictions overview for a day"),
            BotCommand("predict", "Full analysis of a match"),
            BotCommand("value", "Check your bookmaker's odds"),
            BotCommand("follow", "Live updates for a match"),
            BotCommand("alerts", "Turn lead alerts on/off"),
            BotCommand("record", "Paper-trading scorecard"),
            BotCommand("help", "How to use the bot"),
        ])
    except Exception:
        log.warning("couldn't set the command menu", exc_info=True)
    for lead in await storage.open_leads():
        schedule_precheck(app, lead)
    hh, mm = (int(x) for x in s.scan_time.split(":"))
    app.job_queue.run_daily(_scan_job, time=dtime(hh, mm, tzinfo=ZoneInfo(s.timezone)), name="daily-scan")
    app.job_queue.run_once(_startup_scan_job, when=30, name="startup-scan")
    app.job_queue.run_repeating(_settle_job, interval=3600, first=60, name="settle")
    start_live(app)
    log.info("Background jobs started (storage: %s, daily scan %s %s)", storage.description, s.scan_time, s.timezone)
