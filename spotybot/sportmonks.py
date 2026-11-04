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

    async def _get(self, path: str, params: dict | None = None, paginate: bool = False) -> Any:
        params = dict(params or {})
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]

        params["api_token"] = self._token
        results: list = []
        page = 1
        while True:
            if paginate:
                params["page"] = page
            r = await self._http.get(path, params=params)
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
            if not pag.get("has_more") or page >= 10:
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

    async def fixture(self, fixture_id: int, with_predictions: bool = True) -> dict:
        includes = "participants;league;state;scores"
        if with_predictions:
            includes += ";predictions.type"
        try:
            return await self._get(f"/fixtures/{fixture_id}", {"include": includes})
        except SportmonksError as e:
            # The Predictions add-on may not be on the plan; retry without it.
            if with_predictions and ("predictions" in str(e).lower() or "403" in str(e)):
                return await self._get(f"/fixtures/{fixture_id}", {"include": "participants;league;state;scores"})
            raise

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
