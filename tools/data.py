"""Shared historical data loader for backtests: results + corners/cards/referee, and over/under odds.

All pages for finished date ranges are cached under .cache/ (see tools/cache.py).
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from spotybot.history import FullMatch, parse_full_match
from spotybot.sportmonks import BASE_URL
from tools.cache import cached_get

__all__ = ["FullMatch", "fetch_full_matches", "fetch_ou_odds"]


async def _pages(http, token, start, end, include, filters):
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=90), end)
        page = 1
        while True:
            body = await cached_get(http, f"/fixtures/between/{cur}/{chunk_end}", {
                "api_token": token, "include": include, "filters": filters, "per_page": 50, "page": page}, chunk_end)
            for f in body.get("data") or []:
                yield f
            if not (body.get("pagination") or {}).get("has_more"):
                break
            page += 1
        cur = chunk_end + timedelta(days=1)


async def fetch_full_matches(token: str, start: date, end: date, leagues: list[int]) -> list[FullMatch]:
    filters = "fixtureLeagues:" + ",".join(map(str, leagues))
    out: dict[int, FullMatch] = {}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as http:
        async for f in _pages(http, token, start, end, "participants;statistics;referees", filters):
            m = parse_full_match(f)
            if m:
                out[m.fixture_id] = m
    return sorted(out.values(), key=lambda m: m.kickoff)


async def fetch_ou_odds(token: str, start: date, end: date, leagues: list[int], market_ids: list[int]
                        ) -> dict[int, dict[int, dict[float, dict[str, list[float]]]]]:
    """market_id -> fixture_id -> line -> {'over': [odds per bookmaker], 'under': [...]}.

    Only half lines (x.5) and whole lines are kept; quarter (Asian) lines are skipped.
    """
    filters = "markets:" + ",".join(map(str, market_ids)) + ";fixtureLeagues:" + ",".join(map(str, leagues))
    out: dict[int, dict[int, dict[float, dict[str, list[float]]]]] = {m: {} for m in market_ids}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as http:
        async for f in _pages(http, token, start, end, "odds", filters):
            books: dict[tuple[int, float, int], dict[str, float]] = {}
            for o in f.get("odds") or []:
                mid = o.get("market_id")
                label = str(o.get("label", "")).strip().lower()
                if mid not in out or label not in ("over", "under"):
                    continue
                try:
                    line, val = float(o.get("total")), float(o["value"])
                except (TypeError, ValueError, KeyError):
                    continue
                if (line * 2) != int(line * 2) or val <= 1.0:  # skip quarter lines
                    continue
                books.setdefault((mid, line, o["bookmaker_id"]), {})[label] = val
            for (mid, line, _), b in books.items():
                if len(b) == 2:
                    d = out[mid].setdefault(f["id"], {}).setdefault(line, {"over": [], "under": []})
                    d["over"].append(b["over"])
                    d["under"].append(b["under"])
    return out
