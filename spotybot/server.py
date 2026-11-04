"""Web server for hosted (webhook) mode.

Routes:
  GET/HEAD /          -> "Spoty Trade Bot is running"
  GET/HEAD /health    -> {"status": "ok", ...}   (point an uptime pinger such as cron-job.org here)
  POST     /telegram/<secret> -> Telegram webhook updates
"""
from __future__ import annotations

import hashlib
import logging
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from telegram import Update
from telegram.ext import Application

log = logging.getLogger("spotybot.server")


def webhook_secret(telegram_token: str) -> str:
    return hashlib.sha256(telegram_token.encode()).hexdigest()[:32]


def build_web_app(app: Application, secret: str) -> Starlette:
    started = time.time()

    async def root(_: Request) -> Response:
        return PlainTextResponse("Spoty Trade Bot is running")

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "uptime_seconds": int(time.time() - started)})

    async def telegram(request: Request) -> Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    return Starlette(routes=[
        Route("/", root, methods=["GET", "HEAD"]),
        Route("/health", health, methods=["GET", "HEAD"]),
        Route(f"/telegram/{secret}", telegram, methods=["POST"]),
    ])


async def run_webhook_server(app: Application, base_url: str, port: int, telegram_token: str) -> None:
    secret = webhook_secret(telegram_token)
    server = uvicorn.Server(uvicorn.Config(build_web_app(app, secret), host="0.0.0.0", port=port, log_level="warning"))
    async with app:
        await app.bot.set_webhook(
            url=f"{base_url}/telegram/{secret}", secret_token=secret, allowed_updates=Update.ALL_TYPES
        )
        await app.start()
        from .alerts import start_background
        await start_background(app)
        log.info("Webhook set; health check at %s/health", base_url)
        try:
            await server.serve()
        finally:
            await app.stop()
