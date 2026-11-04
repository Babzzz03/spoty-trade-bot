"""Telegram bot entry point.  Run with:  python -m spotybot.bot"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .analysis import analyse_fixture, best_tips
from .config import Settings, load_settings
from .formatting import analysis_message, fixture_line, kickoff_local, tips_message
from .history import HistoryStore
from .sportmonks import SportmonksClient, SportmonksError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the API token out of logs
log = logging.getLogger("spotybot")

PAGE_SIZE = 8

HELP = (
    "<b>Spoty Trade Bot</b> — football fixture analysis &amp; predictions\n\n"
    "/today — today's fixtures (tap one to analyse)\n"
    "/fixtures YYYY-MM-DD — fixtures on a date\n"
    "/predict &lt;fixture_id&gt; — full analysis: probabilities, fair odds, bookmaker prices, verdict\n"
    "/tips — overview of today's upcoming fixtures\n"
    "/remind &lt;fixture_id&gt; — fresh analysis 1 hour before kick-off (line-ups usually out)\n"
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
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def _send_fixture_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE, day: date, page: int, edit: bool) -> None:
    s = _settings(ctx)
    try:
        fixtures = await _client(ctx).fixtures_by_date(day, s.default_league_ids or None)
    except SportmonksError as e:
        await update.effective_message.reply_text(f"Couldn't load fixtures: {e}")
        return
    fixtures.sort(key=lambda f: (f.get("starting_at") or "", (f.get("league") or {}).get("name", "")))
    if not fixtures:
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
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Remind me 1 hour before kick-off",
                                                             callback_data=f"r:{fixture_id}")]])
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


async def tips(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = _settings(ctx)
    msg = await update.effective_message.reply_text("Scanning today's fixtures… ⏳ (this can take a moment)")
    try:
        fixtures = await _client(ctx).fixtures_by_date(_today(s.timezone), s.default_league_ids or None)
        items = await best_tips(_client(ctx), _store(ctx), fixtures)
    except SportmonksError as e:
        await msg.edit_text(f"Couldn't build tips: {e}")
        return
    if not items:
        await msg.edit_text("No upcoming fixtures today in the leagues on your plan.")
        return
    await msg.edit_text(tips_message(items, s.timezone), parse_mode=ParseMode.HTML)


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
    app = (builder.updater(None) if webhook else builder.post_shutdown(_shutdown)).build()
    app.bot_data["settings"] = settings
    app.bot_data["client"] = client
    app.bot_data["store"] = HistoryStore(client)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("fixtures", fixtures_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("tips", tips))
    app.add_handler(CommandHandler("team", team_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
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
