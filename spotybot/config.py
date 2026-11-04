"""Configuration loaded from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    sportmonks_token: str
    telegram_token: str
    timezone: str = "UTC"
    default_league_ids: list[int] = field(default_factory=list)
    database_url: str = ""
    scan_time: str = "08:00"  # local time for the daily scan, HH:MM


def load_settings() -> Settings:
    sm = os.getenv("SPORTMONKS_API_TOKEN", "").strip()
    tg = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    missing = [n for n, v in (("SPORTMONKS_API_TOKEN", sm), ("TELEGRAM_BOT_TOKEN", tg)) if not v]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}. See .env.example.")
    leagues = [int(x) for x in os.getenv("DEFAULT_LEAGUE_IDS", "").split(",") if x.strip()]
    return Settings(
        sportmonks_token=sm,
        telegram_token=tg,
        timezone=os.getenv("BOT_TIMEZONE", "UTC").strip() or "UTC",
        default_league_ids=leagues,
        database_url=os.getenv("DATABASE_URL", "").strip(),
        scan_time=os.getenv("SCAN_TIME", "08:00").strip() or "08:00",
    )
