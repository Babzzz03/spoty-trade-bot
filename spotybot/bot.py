"""Telegram bot entry point.  Run with:  python -m spotybot.bot"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

from .alerts import scorecard_text, start_background, value_scorecard_text
from .markets import estimate, parse_selection
from .analysis import CORNERS_MIN_EDGE, analyse_fixture, best_tips
from .config import Settings, load_settings
from .formatting import analysis_message, fixture_line, kickoff_local, tips_message
from .history import HistoryStore
from .sportmonks import SportmonksClient, SportmonksError, fixture_teams
from .storage import Storage

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the API token out of logs
log = logging.getLogger("spotybot")

PAGE_SIZE = 8

HELP = (
    "<b>Spoty Trade Bot</b> — football fixture analysis &amp; predictions\n\n"
    "/upcoming — matches in the next 7 days (tap one for its prediction)\n"
    "/today — today's fixtures (tap one to analyse)\n"
    "/fixtures YYYY-MM-DD — fixtures on a date\n"
    "/predict &lt;fixture_id&gt; — full analysis: probabilities, fair odds, bookmaker prices, verdict\n"
    "/tips [YYYY-MM-DD] — predictions overview for a day (default today)\n"
    "/remind &lt;fixture_id&gt; — fresh analysis 1 hour before kick-off (line-ups usually out)\n"
    "/alerts on — get new corners leads automatically (morning scan + 1-hour-before check + results)\n"
    "/alerts off — stop alerts\n"
    "/value &lt;fixture_id&gt; &lt;bet&gt; &lt;odds&gt; — are the odds you see on SportyBet better than fair? "
    "Any market, e.g. /value 19722750 over2.5 1.35 (bets: 1 x 2 1x x2 12 over2.5 under3.5 btts btts-no "
    "corners-over9.5 cards-under4.5)\n"
    "/value &lt;odds&gt; — check odds for the next corners lead\n"
    "/follow &lt;fixture_id&gt; — live updates: goals, red cards, half/full-time with corners &amp; cards\n"
    "/record — paper-trading scorecard of all leads\n"
    "/team &lt;name&gt; — find a team's Sportmonks ID\n\n"
    "<b>How to read it:</b> fair odds = 1 ÷ probability. A bet only has value when a bookmaker pays "
    "MORE than the fair odds. Headline probabilities come from bookmaker prices (they proved most "
    "accurate in testing); our models for goals, corners and cards (with referee) are shown alongside.\n"
    "Probabilities, not guarantees."
)


def _settings(ctx: ContextTypes.DEFAULT_TYPE) -> Settings:
    return ctx.application.bot_data["settings"]


def _client(ctx: ContextTypes.DEFAULT_TYPE) -> SportmonksClient:
    return ctx.application.bot_data["client"]


def _today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    extra = ""
    if not _settings(ctx).allowed_chat_ids:
        extra = (f"\n\n🔓 This bot is open to anyone. Your chat ID is <code>{chat_id}</code> — set "
                 f"ALLOWED_CHAT_IDS={chat_id} on the server to make it private.")
    await update.effective_message.reply_text(HELP + extra, parse_mode=ParseMode.HTML)


async def gatekeeper(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: if ALLOWED_CHAT_IDS is set, ignore everyone else."""
    allowed = ctx.application.bot_data["settings"].allowed_chat_ids
    if not allowed or not isinstance(update, Update) or update.effective_chat is None:
        return
    if update.effective_chat.id not in allowed:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"This is a private bot. (Your chat ID: {update.effective_chat.id})")
        raise ApplicationHandlerStop


async def _send_fixture_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE, day: date, page: int, edit: bool) -> None:
    s = _settings(ctx)
    try:
        fixtures = await _client(ctx).fixtures_by_date(day, s.default_league_ids or None)
    except SportmonksError as e:
        await update.effective_message.reply_text(f"Couldn't load fixtures: {e}")
        return
    fixtures.sort(key=lambda f: (f.get("starting_at") or "", (f.get("league") or {}).get("name", "")))
    if not fixtures:
        if day == _today(s.timezone):
            await update.effective_message.reply_text("No matches today in your leagues — here's what's coming up:")
            await upcoming_cmd(update, ctx)
        else:
            await update.effective_message.reply_text(f"No fixtures found for {day.isoformat()} on your plan.")
        return

    pages = (len(fixtures) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = fixtures[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = [f"<b>Fixtures {day.isoformat()}</b>  (page {page + 1}/{pages}, {len(fixtures)} total)", ""]
    buttons = []
    for f in chunk:
        league = (f.get("league") or {}).get("name", "")
        text.append(f"{escape(fixture_line(f, s.timezone))}  <i>{escape(league)}</i>  [<code>{f['id']}</code>]")
        label = fixture_line(f, s.timezone)
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"p:{f['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"f:{day.isoformat()}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"f:{day.isoformat()}:{page + 1}"))
    if nav:
        buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text("\n".join(text), parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.effective_message.reply_text("\n".join(text), parse_mode=ParseMode.HTML, reply_markup=markup)


async def today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_fixture_page(update, ctx, _today(_settings(ctx).timezone), 0, edit=False)


UPCOMING_DAYS = 7


async def upcoming_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = _settings(ctx)
    start = _today(s.timezone)
    try:
        fixtures = await _client(ctx).fixtures_between(start, start + timedelta(days=UPCOMING_DAYS - 1),
                                                       s.default_league_ids or None)
    except SportmonksError as e:
        await update.effective_message.reply_text(f"Couldn't load fixtures: {e}")
        return
    fixtures = [f for f in fixtures if f.get("state_id") in (None, 1)]
    fixtures.sort(key=lambda f: f.get("starting_at") or "")
    if not fixtures:
        await update.effective_message.reply_text(f"No matches in the next {UPCOMING_DAYS} days in your leagues.")
        return
    tz = ZoneInfo(s.timezone)
    by_day: dict[str, list[dict]] = {}
    for f in fixtures:
        ko = datetime.fromisoformat(f["starting_at"].replace(" ", "T")[:19]).replace(tzinfo=timezone.utc).astimezone(tz)
        by_day.setdefault(ko.date().isoformat(), []).append(f)
    lines = [f"<b>Upcoming matches</b> (next {UPCOMING_DAYS} days) — tap one for its prediction", ""]
    buttons = []
    for d, fs in by_day.items():
        lines.append(f"<b>{datetime.fromisoformat(d).strftime('%a %d %b')}</b>  ·  predictions: /tips_{d.replace('-', '')}")
        for f in fs:
            league = (f.get("league") or {}).get("name", "")
            lines.append(f"  {escape(fixture_line(f, s.timezone))}  <i>{escape(league)}</i>")
            if len(buttons) < 40:
                buttons.append([InlineKeyboardButton(f"{datetime.fromisoformat(d).strftime('%a')} "
                                                     f"{fixture_line(f, s.timezone)}"[:60],
                                                     callback_data=f"p:{f['id']}")])
        lines.append("")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                              reply_markup=InlineKeyboardMarkup(buttons))


async def fixtures_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text("Usage: /fixtures YYYY-MM-DD")
        return
    try:
        day = date.fromisoformat(ctx.args[0])
    except ValueError:
        await update.effective_message.reply_text("Date must look like 2026-11-08")
        return
    await _send_fixture_page(update, ctx, day, 0, edit=False)


def _store(ctx: ContextTypes.DEFAULT_TYPE) -> HistoryStore:
    return ctx.application.bot_data["store"]


async def _send_prediction(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int) -> None:
    msg = await update.effective_message.reply_text("Analysing… ⏳ (the first match of a league takes ~20 s)")
    text, markup = await _analysis_text(ctx, fixture_id)
    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _analysis_text(ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int, with_button: bool = True):
    try:
        a = await analyse_fixture(_client(ctx), _store(ctx), fixture_id)
    except SportmonksError as e:
        return f"Couldn't analyse fixture {fixture_id}: {escape(str(e))}", None
    except Exception:
        log.exception("analysis failed for %s", fixture_id)
        return "Something went wrong analysing that fixture.", None
    markup = None
    if with_button and a.fixture.get("state_id") in (None, 1):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Remind me 1 hour before kick-off", callback_data=f"r:{fixture_id}")],
            [InlineKeyboardButton("📺 Follow live (goals, cards, corners)", callback_data=f"l:{fixture_id}")],
        ])
    return analysis_message(a, _settings(ctx).timezone), markup


async def _reminder_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, fixture_id = ctx.job.data
    text, _ = await _analysis_text(ctx, fixture_id, with_button=False)
    await ctx.bot.send_message(chat_id, "⏰ <b>Kick-off in about 1 hour</b> — updated analysis:\n\n" + text,
                               parse_mode=ParseMode.HTML)


async def _schedule_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int) -> None:
    try:
        fixture = await _client(ctx).fixture(fixture_id)
        ko = datetime.fromisoformat(fixture["starting_at"].replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)
    except (SportmonksError, KeyError, ValueError, TypeError):
        await update.effective_message.reply_text("Couldn't find that fixture's kick-off time.")
        return
    when = ko - timedelta(hours=1)
    now = datetime.now(timezone.utc)
    if ko <= now:
        await update.effective_message.reply_text("That match has already started.")
        return
    chat_id = update.effective_chat.id
    name = f"remind:{chat_id}:{fixture_id}"
    for job in ctx.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    ctx.job_queue.run_once(_reminder_job, when=max(when, now + timedelta(seconds=5)),
                           data=(chat_id, fixture_id), name=name)
    local = when.astimezone(ZoneInfo(_settings(ctx).timezone)).strftime("%a %d %b %H:%M")
    await update.effective_message.reply_text(
        f"🔔 I'll send a fresh analysis (with line-ups if they're out) at {local}.\n"
        "Note: reminders are lost if the server restarts or redeploys.")


async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /remind <fixture_id>")
        return
    await _schedule_reminder(update, ctx, int(ctx.args[0]))


async def predict_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /predict <fixture_id>  (IDs are shown in /today)")
        return
    await _send_prediction(update, ctx, int(ctx.args[0]))


def _tips_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> date | None:
    """Date from '/tips 2026-11-08' or the clickable '/tips_20261108'; default today."""
    text = (update.effective_message.text or "") if update.effective_message else ""
    m = re.search(r"/tips_(\d{8})", text)
    raw = m.group(1) if m else (ctx.args[0] if ctx.args else "")
    if not raw:
        return _today(_settings(ctx).timezone)
    try:
        return date.fromisoformat(raw) if "-" in raw else datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


async def tips(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = _settings(ctx)
    day = _tips_day(update, ctx)
    if day is None:
        await update.effective_message.reply_text("Date must look like 2026-11-08")
        return
    msg = await update.effective_message.reply_text(f"Analysing matches on {day.strftime('%a %d %b')}… ⏳ "
                                                    "(about 20 s the first time)")
    try:
        fixtures = await _client(ctx).fixtures_by_date(day, s.default_league_ids or None)
        items = await best_tips(_client(ctx), _store(ctx), fixtures, top=8)
    except SportmonksError as e:
        await msg.edit_text(f"Couldn't build tips: {e}")
        return
    if not items:
        await msg.edit_text(f"No upcoming matches on {day.isoformat()} in your leagues. Try /upcoming.")
        return
    await msg.edit_text(tips_message(items, s.timezone, day), parse_mode=ParseMode.HTML)


def _storage(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.application.bot_data["storage"]


async def alerts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (ctx.args[0].lower() if ctx.args else "on")
    chat_id = update.effective_chat.id
    if arg == "off":
        await _storage(ctx).unsubscribe(chat_id)
        await update.effective_message.reply_text("🔕 Alerts off. Send /alerts on to turn them back on.")
        return
    await _storage(ctx).subscribe(chat_id)
    s = _settings(ctx)
    await update.effective_message.reply_text(
        f"🔔 Alerts on. Every day at {s.scan_time} ({s.timezone}) I scan today's and tomorrow's matches and "
        "message you any new corners lead, re-check it 1 hour before kick-off, and send the result after the match.\n"
        "Leads are experimental: paper-trade them and watch /record.")


async def record_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = _storage(ctx)
    text = scorecard_text(await st.all_settled(), await st.open_leads(), await st.recent_leads(10))
    text += "\n" + value_scorecard_text(await st.value_checks_for(update.effective_chat.id))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


VALUE_MIN_EDGE = 0.03  # for selections priced off the bookmaker consensus


async def _value_any_market(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int, token: str,
                            odds: float | None) -> None:
    sel = parse_selection(token)
    if sel is None:
        await update.effective_message.reply_text(
            "I don't know that bet. Examples: 1, x, 2, 1x, x2, 12, over2.5, under3.5, btts, btts-no, "
            "corners-over9.5, cards-under4.5")
        return
    if odds is None:
        await update.effective_message.reply_text("Odds must be a number above 1, like 1.85")
        return
    try:
        a = await analyse_fixture(_client(ctx), _store(ctx), fixture_id)
    except SportmonksError as e:
        await update.effective_message.reply_text(f"Couldn't analyse that fixture: {e}")
        return
    if a.fixture.get("state_id") not in (None, 1):
        await update.effective_message.reply_text("That match has already started — pre-match prices only.")
        return
    est = estimate(a, sel)
    if est is None:
        await update.effective_message.reply_text(f"No estimate for {sel.label} in this match. Try another line.")
        return
    p, edge = est.prob, est.prob * odds - 1
    fair = 1 / p
    lines = [f"<b>{escape(a.home)} vs {escape(a.away)}</b>: {escape(sel.label)}",
             f"Chance {p:.0%} · fair odds {fair:.2f} · take only at <b>{(1 + VALUE_MIN_EDGE) / p:.2f}+</b>"]
    if est.source == "model":
        lines.append(f"Your odds {odds:.2f} → {'🟡 above' if edge > 0 else '❌ below'} our model's fair odds "
                     f"({edge:+.0%}), but too few bookmakers price this line to confirm it. "
                     "Our model alone is unproven: treat as no bet.")
    elif edge >= VALUE_MIN_EDGE:
        lines.append(f"Your odds {odds:.2f} → ✅ <b>Value</b>: {edge:+.0%} vs the market's fair price.")
        await _storage(ctx).record_value_check(
            chat_id=update.effective_chat.id, fixture_id=fixture_id, kickoff=a.kickoff_utc, home=a.home,
            away=a.away, market=sel.market, side=sel.side, line=sel.line, prob=p, source=est.source, odds=odds)
        lines.append("<i>Recorded on your /record scorecard — it settles after the match.</i>")
    elif edge > 0:
        lines.append(f"Your odds {odds:.2f} → 🟡 Marginal ({edge:+.0%}): under our {VALUE_MIN_EDGE:.0%} safety margin. Skip.")
    else:
        lines.append(f"Your odds {odds:.2f} → ❌ No value ({edge:+.0%}). Skip.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _parse_odds(raw: str) -> float | None:
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        return None
    return v if v > 1.0 else None


async def value_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    st = _storage(ctx)
    lead = None
    if len(args) == 3 and args[0].isdigit():
        await _value_any_market(update, ctx, int(args[0]), args[1], _parse_odds(args[2]))
        return
    if len(args) == 2 and args[0].isdigit():
        odds = _parse_odds(args[1])
        lead = await st.lead_for(int(args[0]))
        if lead is None:
            # No recorded lead: analyse now
            try:
                a = await analyse_fixture(_client(ctx), _store(ctx), int(args[0]))
            except SportmonksError as e:
                await update.effective_message.reply_text(f"Couldn't analyse that fixture: {e}")
                return
            if not a.corners_lead:
                await update.effective_message.reply_text(
                    "There's no corners lead for that match, so there's no bet to check. "
                    "For other markets use the fair odds in /predict: only odds ABOVE the fair odds have value.")
                return
            ld = a.corners_lead
            label, prob = f"{ld.side} {ld.line:g} corners", ld.prob
            teams = f"{a.home} vs {a.away}"
        else:
            label, prob, teams = lead.label, lead.prob, f"{lead.home} vs {lead.away}"
    elif len(args) == 1:
        odds = _parse_odds(args[0])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        upcoming = [l for l in await st.open_leads() if l.kickoff > now]
        if not upcoming:
            await update.effective_message.reply_text("There's no upcoming lead right now. Use /value <fixture_id> <odds>.")
            return
        lead = upcoming[0]
        label, prob, teams = lead.label, lead.prob, f"{lead.home} vs {lead.away}"
    else:
        await update.effective_message.reply_text(
            "Usage:\n/value <fixture_id> <bet> <odds> — any market, e.g. /value 19722750 over2.5 1.35\n"
            "/value <odds> — the next corners lead\n"
            "Bets: 1, x, 2, 1x, x2, 12, over2.5, under3.5, btts, btts-no, corners-over9.5, cards-under4.5")
        return
    if odds is None:
        await update.effective_message.reply_text("Odds must be a number above 1, like 1.85")
        return
    edge = prob * odds - 1
    min_price = (1 + CORNERS_MIN_EDGE) / prob
    if edge >= CORNERS_MIN_EDGE:
        verdict = f"✅ <b>Good value</b> — {edge:+.0%} expected edge."
    elif edge > 0:
        verdict = f"🟡 <b>Marginal</b> — only {edge:+.0%} edge; below our {CORNERS_MIN_EDGE:.0%} safety margin. Skip."
    else:
        verdict = f"❌ <b>No value</b> — {edge:+.0%}. Skip."
    await update.effective_message.reply_text(
        f"<b>{escape(teams)}</b>: {escape(label)}\n"
        f"Our chance {prob:.0%} · fair odds {1 / prob:.2f} · take only at <b>{min_price:.2f}+</b>\n"
        f"Your odds {odds:.2f} → {verdict}\n<i>Paper-trade only until /record proves the edge.</i>",
        parse_mode=ParseMode.HTML)


async def _follow(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int) -> None:
    try:
        f = await _client(ctx).fixture(fixture_id)
        home, away = (t.get("name", "?") for t in fixture_teams(f))
    except (SportmonksError, Exception):
        await update.effective_message.reply_text("Couldn't find that fixture.")
        return
    if f.get("state_id") in (5, 7, 8):
        await update.effective_message.reply_text("That match has already finished.")
        return
    await _storage(ctx).follow(update.effective_chat.id, fixture_id, f["starting_at"], home, away)
    await update.effective_message.reply_text(
        f"📺 Following {home} vs {away}: kick-off, goals, red cards, half-time and full-time "
        "(with corners and cards). Updates every ~2 minutes. /unfollow " + str(fixture_id) + " to stop.")


async def follow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /follow <fixture_id>")
        return
    await _follow(update, ctx, int(ctx.args[0]))


async def unfollow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /unfollow <fixture_id>")
        return
    await _storage(ctx).unfollow(update.effective_chat.id, int(ctx.args[0]))
    await update.effective_message.reply_text("Stopped following that match.")


async def team_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(ctx.args or []).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /team <name>")
        return
    try:
        teams = await _client(ctx).search_teams(name)
    except SportmonksError as e:
        await update.effective_message.reply_text(f"Search failed: {e}")
        return
    if not teams:
        await update.effective_message.reply_text("No teams found.")
        return
    lines = [f"{escape(t.get('name', '?'))} — ID <code>{t['id']}</code>" for t in teams[:10]]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("p:"):
        await _send_prediction(update, ctx, int(data[2:]))
    elif data.startswith("r:"):
        await _schedule_reminder(update, ctx, int(data[2:]))
    elif data.startswith("l:"):
        await _follow(update, ctx, int(data[2:]))
    elif data.startswith("f:"):
        _, d, page = data.split(":")
        await _send_fixture_page(update, ctx, date.fromisoformat(d), int(page), edit=True)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=ctx.error)


def build_app(settings: Settings, webhook: bool = False) -> Application:
    client = SportmonksClient(settings.sportmonks_token)

    async def _shutdown(app: Application) -> None:
        await client.close()

    builder = Application.builder().token(settings.telegram_token)
    # In webhook mode our own web server feeds updates in, so no built-in Updater is needed.
    if webhook:
        app = builder.updater(None).build()  # server.py starts the background jobs itself
    else:
        app = builder.post_init(start_background).post_shutdown(_shutdown).build()
    app.bot_data["settings"] = settings
    app.bot_data["client"] = client
    app.bot_data["store"] = HistoryStore(client)
    app.bot_data["storage"] = Storage(settings.database_url)
    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("upcoming", upcoming_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"^/tips_\d{8}"), tips))
    app.add_handler(CommandHandler("fixtures", fixtures_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("tips", tips))
    app.add_handler(CommandHandler("team", team_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CommandHandler("record", record_cmd))
    app.add_handler(CommandHandler("value", value_cmd))
    app.add_handler(CommandHandler("follow", follow_cmd))
    app.add_handler(CommandHandler("unfollow", unfollow_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    settings = load_settings()
    # Webhook mode on a web host (Render sets RENDER_EXTERNAL_URL automatically for web services).
    base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if base_url:
        import asyncio

        from .server import run_webhook_server

        port = int(os.getenv("PORT", "10000"))
        log.info("Starting Spoty Trade Bot in webhook mode on port %s (tz=%s)", port, settings.timezone)
        app = build_app(settings, webhook=True)

        async def _run() -> None:
            try:
                await run_webhook_server(app, base_url, port, settings.telegram_token)
            finally:
                await app.bot_data["client"].close()

        asyncio.run(_run())
    else:
        log.info("Starting Spoty Trade Bot in polling mode (tz=%s)", settings.timezone)
        build_app(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
