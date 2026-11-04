"""Shared historical data loader for backtests: results + corners/cards/referee, and over/under odds.

All pages for finished date ranges are cached under .cache/ (see tools/cache.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from spotybot.sportmonks import BASE_URL, FINISHED_STATE_IDS
from tools.cache import cached_get

STAT_CORNERS, STAT_YELLOW, STAT_RED, STAT_GOALS = 34, 84, 83, 52
MAIN_REFEREE_TYPE = 6


@dataclass
class FullMatch:
    fixture_id: int
    kickoff: str
    league_id: int
    home_id: int
    away_id: int
    home_name: str
    away_name: str
    hg: int
    ag: int
    hc: int | None = None  # corners
    ac: int | None = None
    hy: int | None = None  # yellow cards
    ay: int | None = None
    hr: int = 0            # red cards
    ar: int = 0
    referee_id: int | None = None

    @property
    def corners(self) -> int | None:
        return None if self.hc is None or self.ac is None else self.hc + self.ac

    @property
    def cards(self) -> int | None:
        """Yellow + red cards (each card counts 1)."""
        return None if self.hy is None or self.ay is None else self.hy + self.ay + self.hr + self.ar


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
            if f.get("state_id") not in FINISHED_STATE_IDS:
                continue
            home = away = None
            for p in f.get("participants") or []:
                loc = (p.get("meta") or {}).get("location")
                home = p if loc == "home" else home
                away = p if loc == "away" else away
            if not home or not away:
                continue
            stats: dict[tuple[int, str], int] = {}
            for s in f.get("statistics") or []:
                v = (s.get("data") or {}).get("value")
                if isinstance(v, (int, float)):
                    stats[(s["type_id"], s.get("location"))] = int(v)
            if (STAT_GOALS, "home") not in stats or (STAT_GOALS, "away") not in stats:
                continue
            ref = next((r["referee_id"] for r in f.get("referees") or [] if r.get("type_id") == MAIN_REFEREE_TYPE), None)
            out[f["id"]] = FullMatch(
                fixture_id=f["id"], kickoff=f.get("starting_at") or "", league_id=f.get("league_id"),
                home_id=home["id"], away_id=away["id"], home_name=home.get("name", "?"), away_name=away.get("name", "?"),
                hg=stats[(STAT_GOALS, "home")], ag=stats[(STAT_GOALS, "away")],
                hc=stats.get((STAT_CORNERS, "home")), ac=stats.get((STAT_CORNERS, "away")),
                hy=stats.get((STAT_YELLOW, "home"), 0 if (STAT_YELLOW, "away") in stats else None),
                ay=stats.get((STAT_YELLOW, "away"), 0 if (STAT_YELLOW, "home") in stats else None),
                hr=stats.get((STAT_RED, "home"), 0), ar=stats.get((STAT_RED, "away"), 0),
                referee_id=ref,
            )
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
