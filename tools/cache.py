"""Tiny on-disk cache for backtest API pages, so re-runs don't spend your Sportmonks rate limit."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path

import httpx

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "sportmonks"


async def cached_get(http: httpx.AsyncClient, path: str, params: dict, chunk_end: date) -> dict:
    """GET with caching. Finished date ranges are cached permanently, ranges reaching today for 6 hours."""
    public = {k: v for k, v in params.items() if k != "api_token"}
    key = hashlib.sha256((path + json.dumps(public, sort_keys=True)).encode()).hexdigest()[:24]
    file = CACHE_DIR / f"{key}.json"
    if file.exists() and (chunk_end < date.today() or time.time() - file.stat().st_mtime < 6 * 3600):
        return json.loads(file.read_text())
    r = await http.get(path, params=params)
    if r.status_code == 429:
        raise SystemExit("Sportmonks rate limit reached. Wait for it to reset (up to an hour) and re-run; "
                         "pages already downloaded are cached and won't be fetched again.")
    r.raise_for_status()
    body = r.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(body))
    return body
