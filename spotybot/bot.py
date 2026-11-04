"""Telegram bot entry point.  Run with:  python -m spotybot.bot"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .analysis import analyse_fixture, best_tips
from .config import Settings, load_settings
from .formatting import fixture_line, kickoff_local, pct, prediction_message
from .sportmonks import SportmonksClient, SportmonksError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the API token out of logs
log = logging.getLogger("spotybot")

PAGE_SIZE = 8

HELP = (
    "<b>Spoty Trade Bot</b> — football fixture analysis &amp; predictions\n\n"
    "/today — today's fixtures (tap one to analyse)\n"
    "/fixtures YYYY-MM-DD — fixtures on a date\n"
    "/predict &lt;fixture_id&gt; — full analysis of one fixture\n"
    "/tips — strongest picks among today's upcoming fixtures\n"
    "/team &lt;name&gt; — find a team's Sportmonks ID\n\n"
    "Predictions use recency-weighted team form, a Dixon–Coles Poisson goal model, "
    "head-to-head, and Sportmonks predictions when your plan includes them. "
    "They are probabilities, not certainties."
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


async def _send_prediction(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fixture_id: int) -> None:
    msg = await update.effective_message.reply_text("Analysing… ⏳")
    try:
        fixture, pred = await analyse_fixture(_client(ctx), fixture_id)
    except SportmonksError as e:
        await msg.edit_text(f"Couldn't analyse fixture {fixture_id}: {e}")
        return
    except Exception:
        log.exception("analysis failed for %s", fixture_id)
        await msg.edit_text("Something went wrong analysing that fixture.")
        return
    league = (fixture.get("league") or {}).get("name", "")
    ko = f"{fixture.get('starting_at', '')[:10]} {kickoff_local(fixture, _settings(ctx).timezone)}"
    await msg.edit_text(prediction_message(pred, league, ko), parse_mode=ParseMode.HTML)


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
        picks = await best_tips(_client(ctx), fixtures)
    except SportmonksError as e:
        await msg.edit_text(f"Couldn't build tips: {e}")
        return
    if not picks:
        await msg.edit_text("No upcoming fixtures with enough data today.")
        return
    lines = ["<b>Top picks today</b>", ""]
    for f, p in picks:
        pick, prob = p.pick
        lines.append(
            f"<b>{escape(p.home)} vs {escape(p.away)}</b> ({kickoff_local(f, s.timezone)})\n"
            f"  {pick} {pct(prob)} · O2.5 {pct(p.over_25)} · BTTS {pct(p.btts)} · conf: {p.confidence}\n"
            f"  /predict {f['id']}"
        )
    lines += ["", "<i>Probabilities, not guarantees. Bet responsibly.</i>"]
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
    elif data.startswith("f:"):
        _, d, page = data.split(":")
        await _send_fixture_page(update, ctx, date.fromisoformat(d), int(page), edit=True)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=ctx.error)


def build_app(settings: Settings) -> Application:
    client = SportmonksClient(settings.sportmonks_token)

    async def _shutdown(app: Application) -> None:
        await client.close()

    app = Application.builder().token(settings.telegram_token).post_shutdown(_shutdown).build()
    app.bot_data["settings"] = settings
    app.bot_data["client"] = client
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("fixtures", fixtures_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("tips", tips))
    app.add_handler(CommandHandler("team", team_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    settings = load_settings()
    app = build_app(settings)
    # Webhook mode on a web host (Render sets RENDER_EXTERNAL_URL automatically for web services).
    base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if base_url:
        secret = hashlib.sha256(settings.telegram_token.encode()).hexdigest()[:32]
        port = int(os.getenv("PORT", "10000"))
        log.info("Starting Spoty Trade Bot in webhook mode on port %s (tz=%s)", port, settings.timezone)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=f"telegram/{secret}",
            webhook_url=f"{base_url}/telegram/{secret}",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Starting Spoty Trade Bot in polling mode (tz=%s)", settings.timezone)
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
