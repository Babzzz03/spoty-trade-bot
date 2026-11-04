"""Thin async client for the Sportmonks Football API v3, plus parsing helpers.

Docs: https://docs.sportmonks.com/football
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.sportmonks.com/v3/football"
FINISHED_STATE_IDS = {5, 7, 8}  # FT, AET (after extra time), FT_PEN
# Odds markets: 1 = match result, 80 = goals over/under, 14 = both teams to score,
# 67 = corners over/under, 255 = cards over/under
ODDS_MARKETS = (1, 80, 14, 67, 255)


class SportmonksError(RuntimeError):
    pass


@dataclass
class Match:
    """A finished match from one team's perspective-free view."""
    fixture_id: int
    kickoff: str
    home_id: int
    away_id: int
    home_name: str
    away_name: str
    home_goals: int
    away_goals: int


def _participants(fixture: dict) -> tuple[dict | None, dict | None]:
    home = away = None
    for p in fixture.get("participants") or []:
        loc = (p.get("meta") or {}).get("location")
        if loc == "home":
            home = p
        elif loc == "away":
            away = p
    return home, away


def _current_goals(fixture: dict) -> tuple[int, int] | None:
    """Return (home_goals, away_goals) from the 'CURRENT' score entries, or None."""
    home = away = None
    for s in fixture.get("scores") or []:
        if s.get("description") != "CURRENT":
            continue
        score = s.get("score") or {}
        if score.get("participant") == "home":
            home = score.get("goals")
        elif score.get("participant") == "away":
            away = score.get("goals")
    if home is None or away is None:
        return None
    return int(home), int(away)


def parse_finished_match(fixture: dict) -> Match | None:
    """Convert a raw fixture (with participants+scores included) into a Match, if it is finished."""
    state = fixture.get("state_id")
    if state is not None and state not in FINISHED_STATE_IDS:
        return None
    home, away = _participants(fixture)
    goals = _current_goals(fixture)
    if not home or not away or goals is None:
        return None
    return Match(
        fixture_id=fixture["id"],
        kickoff=fixture.get("starting_at") or "",
        home_id=home["id"],
        away_id=away["id"],
        home_name=home.get("name", "?"),
        away_name=away.get("name", "?"),
        home_goals=goals[0],
        away_goals=goals[1],
    )


def fixture_teams(fixture: dict) -> tuple[dict, dict]:
    home, away = _participants(fixture)
    if not home or not away:
        raise SportmonksError(f"Fixture {fixture.get('id')} has no home/away participants")
    return home, away


class SportmonksClient:
    def __init__(self, token: str, timeout: float = 20.0, cache_ttl: int = 600):
        self._token = token
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._ttl = cache_ttl

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None, paginate: bool = False,
                   max_pages: int = 10, ttl: int | None = None) -> Any:
        params = dict(params or {})
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < (ttl if ttl is not None else self._ttl):
            return hit[1]

        params["api_token"] = self._token
        results: list = []
        page = 1
        while True:
            if paginate:
                params["page"] = page
            r = await self._http.get(path, params=params)
            if r.status_code == 429:
                raise SportmonksError("Sportmonks hourly request limit reached (free plan: 180 per hour). "
                                      "Try again later.")
            if r.status_code != 200:
                try:
                    msg = r.json().get("message", r.text)
                except ValueError:
                    msg = r.text
                raise SportmonksError(f"Sportmonks {r.status_code} on {path}: {msg}")
            body = r.json()
            data = body.get("data")
            if not paginate:
                results = data
                break
            results.extend(data or [])
            pag = body.get("pagination") or {}
            if not pag.get("has_more") or page >= max_pages:
                break
            page += 1

        self._cache[key] = (time.monotonic(), results)
        return results

    # ---- endpoints -------------------------------------------------------

    async def fixtures_by_date(self, day: date, league_ids: list[int] | None = None) -> list[dict]:
        params: dict[str, Any] = {"include": "participants;league;state", "per_page": 50}
        if league_ids:
            params["filters"] = "fixtureLeagues:" + ",".join(map(str, league_ids))
        return await self._get(f"/fixtures/date/{day.isoformat()}", params, paginate=True) or []

    async def fixtures_between(self, start: date, end: date, league_ids: list[int] | None = None) -> list[dict]:
        params: dict[str, Any] = {"include": "participants;league;state", "per_page": 50}
        if league_ids:
            params["filters"] = "fixtureLeagues:" + ",".join(map(str, league_ids))
        return await self._get(f"/fixtures/between/{start.isoformat()}/{end.isoformat()}", params, paginate=True) or []

    async def fixture(self, fixture_id: int) -> dict:
        """One request: teams, league, state, odds (1X2, goals O/U, BTTS, corners, cards), line-ups, referee."""
        return await self._get(f"/fixtures/{fixture_id}", {
            "include": "participants;league;state;scores;odds;lineups;referees",
            "filters": "markets:" + ",".join(map(str, ODDS_MARKETS)),
        })

    async def fixture_result(self, fixture_id: int) -> dict:
        """Fixture state, score and statistics (corners, cards) for settling. Cached 5 minutes."""
        return await self._get(f"/fixtures/{fixture_id}", {"include": "participants;state;scores;statistics"},
                               ttl=300)

    async def live_fixture(self, fixture_id: int) -> dict:
        """Score, statistics and events during a match (cached 30 s)."""
        return await self._get(f"/fixtures/{fixture_id}",
                               {"include": "participants;state;scores;statistics;events"}, ttl=30)

    async def league_results(self, league_id: int, start: date, end: date) -> list[dict]:
        """Finished league fixtures with statistics and referees, in <=90-day chunks (cached 6 hours)."""
        out: list[dict] = []
        cur = start
        while cur <= end:
            chunk_end = min(cur + timedelta(days=90), end)
            out += await self._get(
                f"/fixtures/between/{cur.isoformat()}/{chunk_end.isoformat()}",
                {"include": "participants;statistics;referees", "filters": f"fixtureLeagues:{league_id}",
                 "per_page": 50},
                paginate=True, max_pages=30, ttl=6 * 3600,
            ) or []
            cur = chunk_end + timedelta(days=1)
        return out

    async def team_recent_matches(self, team_id: int, before: date, days: int = 240, limit: int = 12) -> list[Match]:
        start = before - timedelta(days=days)
        end = before - timedelta(days=1)
        raw = await self._get(
            f"/fixtures/between/{start.isoformat()}/{end.isoformat()}/{team_id}",
            {"include": "participants;scores", "per_page": 50},
            paginate=True,
        ) or []
        matches = [m for m in (parse_finished_match(f) for f in raw) if m]
        matches.sort(key=lambda m: m.kickoff, reverse=True)
        return matches[:limit]

    async def head_to_head(self, team_a: int, team_b: int, limit: int = 8) -> list[Match]:
        raw = await self._get(
            f"/fixtures/head-to-head/{team_a}/{team_b}", {"include": "participants;scores"}
        ) or []
        matches = [m for m in (parse_finished_match(f) for f in raw) if m]
        matches.sort(key=lambda m: m.kickoff, reverse=True)
        return matches[:limit]

    async def search_teams(self, name: str) -> list[dict]:
        return await self._get(f"/teams/search/{quote(name.strip(), safe='')}") or []
