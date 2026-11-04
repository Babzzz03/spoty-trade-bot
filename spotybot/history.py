"""League history: finished matches with goals, corners, cards and referee, plus fitted ratings.

One download per league every few hours (about 15 API requests per league), instead of several
requests per analysed match. This keeps the bot inside the Sportmonks free-plan limit of
180 requests per hour per entity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from .ratings import Obs, Ratings, fit
from .sportmonks import FINISHED_STATE_IDS, Match, SportmonksClient

log = logging.getLogger(__name__)

STAT_CORNERS, STAT_YELLOW, STAT_RED, STAT_GOALS = 34, 84, 83, 52
MAIN_REFEREE_TYPE = 6
HISTORY_DAYS = 730
REFRESH_SECONDS = 12 * 3600

# Rating settings chosen by the walk-forward backtest (tools/advanced_backtest.py), tuned on 2024/25.
RATING_XI = 0.008       # time decay per day (half-life ~87 days)
RATING_PRIOR = 10.0     # shrinkage toward league average, in pseudo-matches
CORNERS_NB_SIZE = 40.0  # corners are slightly over-dispersed vs Poisson


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
    hc: int | None = None
    ac: int | None = None
    hy: int | None = None
    ay: int | None = None
    hr: int = 0
    ar: int = 0
    referee_id: int | None = None

    @property
    def corners(self) -> int | None:
        return None if self.hc is None or self.ac is None else self.hc + self.ac

    @property
    def cards(self) -> int | None:
        return None if self.hy is None or self.ay is None else self.hy + self.ay + self.hr + self.ar

    def as_match(self) -> Match:
        return Match(self.fixture_id, self.kickoff, self.home_id, self.away_id,
                     self.home_name, self.away_name, self.hg, self.ag)


def parse_full_match(f: dict) -> FullMatch | None:
    if f.get("state_id") not in FINISHED_STATE_IDS:
        return None
    home = away = None
    for p in f.get("participants") or []:
        loc = (p.get("meta") or {}).get("location")
        home = p if loc == "home" else home
        away = p if loc == "away" else away
    if not home or not away:
        return None
    stats: dict[tuple[int, str], int] = {}
    for s in f.get("statistics") or []:
        v = (s.get("data") or {}).get("value")
        if isinstance(v, (int, float)):
            stats[(s["type_id"], s.get("location"))] = int(v)
    if (STAT_GOALS, "home") not in stats or (STAT_GOALS, "away") not in stats:
        return None
    has_yellow = (STAT_YELLOW, "home") in stats or (STAT_YELLOW, "away") in stats
    ref = next((r["referee_id"] for r in f.get("referees") or [] if r.get("type_id") == MAIN_REFEREE_TYPE), None)
    return FullMatch(
        fixture_id=f["id"], kickoff=f.get("starting_at") or "", league_id=f.get("league_id"),
        home_id=home["id"], away_id=away["id"], home_name=home.get("name", "?"), away_name=away.get("name", "?"),
        hg=stats[(STAT_GOALS, "home")], ag=stats[(STAT_GOALS, "away")],
        hc=stats.get((STAT_CORNERS, "home")), ac=stats.get((STAT_CORNERS, "away")),
        hy=stats.get((STAT_YELLOW, "home"), 0) if has_yellow else None,
        ay=stats.get((STAT_YELLOW, "away"), 0) if has_yellow else None,
        hr=stats.get((STAT_RED, "home"), 0), ar=stats.get((STAT_RED, "away"), 0),
        referee_id=ref,
    )


@dataclass
class LeagueModel:
    league_id: int
    matches: list[FullMatch]   # oldest first
    goals: Ratings | None
    corners: Ratings | None
    cards: Ratings | None
    loaded_at: float

    def team_matches(self, team_id: int, before: str, limit: int = 12) -> list[Match]:
        cutoff = (datetime.fromisoformat(before[:10]) - timedelta(days=240)).isoformat()
        ms = [m for m in reversed(self.matches)
              if m.kickoff < before and m.kickoff >= cutoff and team_id in (m.home_id, m.away_id)]
        return [m.as_match() for m in ms[:limit]]

    def head_to_head(self, a: int, b: int, before: str, limit: int = 8) -> list[Match]:
        ms = [m for m in reversed(self.matches) if m.kickoff < before and {m.home_id, m.away_id} == {a, b}]
        return [m.as_match() for m in ms[:limit]]

    def games_played(self, team_id: int) -> int:
        return sum(team_id in (m.home_id, m.away_id) for m in self.matches)


def build_league_model(league_id: int, matches: list[FullMatch], as_of: str) -> LeagueModel:
    def obs(get):
        out = []
        for m in matches:
            yh, ya = get(m)
            if yh is not None and ya is not None:
                out.append(Obs(m.kickoff, m.home_id, m.away_id, yh, ya, ref=m.referee_id))
        return out

    goals = fit(obs(lambda m: (m.hg, m.ag)), as_of, xi=RATING_XI, prior=RATING_PRIOR)
    corners = fit(obs(lambda m: (m.hc, m.ac)), as_of, xi=RATING_XI, prior=RATING_PRIOR)
    cards = fit(obs(lambda m: (None if m.hy is None else m.hy + m.hr, None if m.ay is None else m.ay + m.ar)),
                as_of, xi=RATING_XI, prior=RATING_PRIOR, use_referee=True)
    return LeagueModel(league_id, matches, goals, corners, cards, time.time())


class HistoryStore:
    """Per-league history + ratings, refreshed every REFRESH_SECONDS.

    The parsed history is also saved in the database (if given), so a restart or redeploy
    doesn't spend ~25 API requests per league downloading it again.
    """

    def __init__(self, client: SportmonksClient, storage=None):
        self._client = client
        self._storage = storage
        self._models: dict[int, LeagueModel] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _load_saved(self, league_id: int) -> tuple[list[FullMatch], float] | None:
        if self._storage is None:
            return None
        try:
            got = await self._storage.cache_get(f"league_history:{league_id}")
        except Exception:
            log.warning("couldn't read saved history for league %s", league_id, exc_info=True)
            return None
        if not got:
            return None
        value, fetched_at = got
        fetched = datetime.fromisoformat(fetched_at).replace(tzinfo=timezone.utc).timestamp()
        if time.time() - fetched > REFRESH_SECONDS:
            return None
        return [FullMatch(**m) for m in json.loads(value)], fetched

    async def league(self, league_id: int) -> LeagueModel:
        lock = self._locks.setdefault(league_id, asyncio.Lock())
        async with lock:
            cur = self._models.get(league_id)
            if cur and time.time() - cur.loaded_at < REFRESH_SECONDS:
                return cur
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            saved = await self._load_saved(league_id)
            if saved:
                matches, fetched = saved
                model = await asyncio.to_thread(build_league_model, league_id, matches, now)
                model.loaded_at = fetched
                log.info("Loaded league %s from saved history: %d matches", league_id, len(matches))
            else:
                end = date.today()
                start = end - timedelta(days=HISTORY_DAYS)
                raw = await self._client.league_results(league_id, start, end)
                matches = sorted((m for m in (parse_full_match(f) for f in raw) if m), key=lambda m: m.kickoff)
                model = await asyncio.to_thread(build_league_model, league_id, matches, now)
                log.info("Downloaded league %s: %d matches", league_id, len(matches))
                if self._storage is not None:
                    try:
                        await self._storage.cache_set(f"league_history:{league_id}",
                                                      json.dumps([asdict(m) for m in matches]))
                    except Exception:
                        log.warning("couldn't save history for league %s", league_id, exc_info=True)
            self._models[league_id] = model
            return model
