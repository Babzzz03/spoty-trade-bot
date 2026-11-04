"""Glue: fetch everything needed for a fixture and run the model."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from .model import Prediction, extract_sportmonks_1x2, predict
from .sportmonks import SportmonksClient, SportmonksError, fixture_teams


def _fixture_date(fixture: dict) -> date:
    raw = fixture.get("starting_at")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace(" ", "T")).date()
        except ValueError:
            pass
    return date.today()


async def analyse_fixture(client: SportmonksClient, fixture_id: int) -> tuple[dict, Prediction]:
    fixture = await client.fixture(fixture_id)
    home, away = fixture_teams(fixture)
    day = _fixture_date(fixture)

    home_recent, away_recent, h2h = await asyncio.gather(
        client.team_recent_matches(home["id"], before=day),
        client.team_recent_matches(away["id"], before=day),
        client.head_to_head(home["id"], away["id"]),
        return_exceptions=True,
    )
    for res in (home_recent, away_recent):
        if isinstance(res, Exception):
            raise res
    if isinstance(h2h, Exception):
        h2h = []
    # Only use H2H matches played before this fixture.
    h2h = [m for m in h2h if m.kickoff[:10] < day.isoformat()]

    pred = predict(
        home,
        away,
        home_recent,
        away_recent,
        h2h=h2h,
        sportmonks_probs=extract_sportmonks_1x2(fixture),
    )
    return fixture, pred


async def best_tips(
    client: SportmonksClient, fixtures: list[dict], max_fixtures: int = 20, top: int = 5
) -> list[tuple[dict, Prediction]]:
    """Analyse up to `max_fixtures` not-yet-started fixtures and return the strongest picks."""
    upcoming = [f for f in fixtures if f.get("state_id") in (None, 1)][:max_fixtures]  # 1 = Not Started
    sem = asyncio.Semaphore(4)

    async def one(f: dict):
        async with sem:
            try:
                return await analyse_fixture(client, f["id"])
            except SportmonksError:
                return None

    results = [r for r in await asyncio.gather(*(one(f) for f in upcoming)) if r]
    results = [r for r in results if min(r[1].home_form.matches, r[1].away_form.matches) >= 4]
    results.sort(key=lambda r: r[1].pick[1], reverse=True)
    return results[:top]
