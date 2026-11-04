"""Glue: one fixture request + cached league history -> full match analysis.

What the backtests (tools/) showed, and therefore how this module decides:
  * Match result, goals and BTTS: bookmaker prices (margin removed) were MORE accurate than our
    models, and no betting rule based on our models made money at average prices.
    -> We show the market's probabilities as the headline numbers, our model as a second opinion,
       and the verdict for these markets is always "no proven edge".
  * Corners: blending 30% of our corners model into the market improved accuracy, and value bets
    with >5% edge returned +11% ± 16% on 126 out-of-sample bets. Promising but NOT statistically
    proven. -> Shown as an "experimental lead" for paper-trading only.
  * Cards: no edge found. -> Information only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .history import CORNERS_NB_SIZE, HistoryStore, LeagueModel
from .model import Prediction, predict
from .odds import FixtureOdds, TwoWay, parse_fixture_odds
from .ratings import negbin_pmf, over_under, poisson_pmf
from .sportmonks import SportmonksClient, SportmonksError, fixture_teams

CORNERS_MODEL_WEIGHT = 0.30   # chosen on 2024/25 data by log loss
CORNERS_MIN_EDGE = 0.05       # expected value threshold at the AVERAGE bookmaker price
MIN_BOOKS_FOR_LEAD = 3
# Use a market probability only when enough bookmakers price it and the margin is sane;
# otherwise (e.g. 2 bookmakers quoting 1.02 / 7.90) fall back to our model.
MIN_BOOKS_FOR_MARKET = 3
MAX_OVERROUND = 0.09
LINEUPS_WINDOW_MIN = 90       # Sportmonks may return predicted XIs days ahead; trust only near kick-off


@dataclass
class LineRow:
    line: float
    p_over: float            # headline probability (market if available, else model)
    source: str              # "market" or "model"
    model_over: float | None = None
    odds: TwoWay | None = None


@dataclass
class Lead:
    market: str
    side: str                # "Over" / "Under"
    line: float
    prob: float              # blended probability of winning
    avg_price: float
    best_price: float
    edge: float              # expected value at the average price

    @property
    def fair_price(self) -> float:
        return 1 / self.prob


@dataclass
class Analysis:
    fixture: dict
    home: str
    away: str
    league: str
    kickoff_utc: str
    model: Prediction
    odds: FixtureOdds
    result_probs: list[float]
    result_source: str
    goals: list[LineRow] = field(default_factory=list)
    btts: LineRow | None = None
    corners_expected: float | None = None
    corners: list[LineRow] = field(default_factory=list)
    cards_expected: float | None = None
    cards: list[LineRow] = field(default_factory=list)
    referee_known: bool = False
    lineups_confirmed: bool = False
    corners_lead: Lead | None = None
    data_note: str | None = None

    @property
    def favourite(self) -> tuple[str, float]:
        labels = (f"{self.home} win", "Draw", f"{self.away} win")
        i = max(range(3), key=lambda k: self.result_probs[k])
        return labels[i], self.result_probs[i]


def _usable(o: TwoWay | None) -> TwoWay | None:
    return o if o and o.books >= MIN_BOOKS_FOR_MARKET and o.overround <= MAX_OVERROUND else None


def _lineups_confirmed(fixture: dict) -> bool:
    starters = [p for p in fixture.get("lineups") or [] if p.get("type_id") == 11]
    try:
        ko = datetime.fromisoformat(fixture["starting_at"].replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, AttributeError):
        return False
    minutes_to_ko = (ko - datetime.now(timezone.utc)).total_seconds() / 60
    return len(starters) >= 20 and minutes_to_ko <= LINEUPS_WINDOW_MIN


def _main_referee(fixture: dict) -> int | None:
    return next((r.get("referee_id") for r in fixture.get("referees") or [] if r.get("type_id") == 6), None)


def _corners_lead(rows: list[LineRow], pmf: list[float]) -> Lead | None:
    best: Lead | None = None
    for row in rows:
        o = row.odds
        if not o or o.books < MIN_BOOKS_FOR_LEAD or row.model_over is None:
            continue
        p_over, p_under, p_push = over_under(pmf, row.line)
        decided = p_over + p_under
        m_over = p_over / decided if decided else 0.5
        mkt = o.prob_yes_proportional  # same method as the backtest that produced this rule
        q_over = (CORNERS_MODEL_WEIGHT * m_over + (1 - CORNERS_MODEL_WEIGHT) * mkt) * (1 - p_push)
        q_under = (CORNERS_MODEL_WEIGHT * (1 - m_over) + (1 - CORNERS_MODEL_WEIGHT) * (1 - mkt)) * (1 - p_push)
        for side, q, prices in (("Over", q_over, o.yes), ("Under", q_under, o.no)):
            ev = q * prices.avg + p_push - 1
            if ev > CORNERS_MIN_EDGE and (best is None or ev > best.edge):
                best = Lead("Corners", side, row.line, q, prices.avg, prices.best, ev)
    return best


async def analyse_fixture(client: SportmonksClient, store: HistoryStore, fixture_id: int) -> Analysis:
    fixture = await client.fixture(fixture_id)
    home, away = fixture_teams(fixture)
    league_id = fixture.get("league_id") or (fixture.get("league") or {}).get("id")
    kickoff = fixture.get("starting_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lm: LeagueModel = await store.league(league_id)
    return build_analysis(fixture, home, away, lm, kickoff)


def build_analysis(fixture: dict, home: dict, away: dict, lm: LeagueModel, kickoff: str) -> Analysis:
    odds = parse_fixture_odds(fixture)
    ref = _main_referee(fixture)

    # Our form model (second opinion) from cached league history
    h_matches = lm.team_matches(home["id"], kickoff)
    a_matches = lm.team_matches(away["id"], kickoff)
    model = predict(home, away, h_matches, a_matches, h2h=lm.head_to_head(home["id"], away["id"], kickoff))

    market_1x2 = odds.result_probs() if odds.result_books >= MIN_BOOKS_FOR_MARKET else None
    a = Analysis(
        fixture=fixture, home=home.get("name", "Home"), away=away.get("name", "Away"),
        league=(fixture.get("league") or {}).get("name", ""), kickoff_utc=kickoff, model=model, odds=odds,
        result_probs=market_1x2 or [model.p_home, model.p_draw, model.p_away],
        result_source="market" if market_1x2 else "model",
        referee_known=ref is not None, lineups_confirmed=_lineups_confirmed(fixture),
    )
    if min(lm.games_played(home["id"]), lm.games_played(away["id"])) < 5:
        a.data_note = "One team has fewer than 5 league games in our history (e.g. newly promoted)."

    # Goals lines
    model_goals = {1.5: model.over_15, 2.5: model.over_25, 3.5: model.over_35}
    for line in sorted(set(odds.goals) | set(model_goals)):
        o, mk = odds.goals.get(line), _usable(odds.goals.get(line))
        if mk is None and line not in model_goals:
            continue
        a.goals.append(LineRow(line, mk.prob_yes if mk else model_goals[line], "market" if mk else "model",
                               model_goals.get(line), o))
    # P(over) must not increase with the line (e.g. O1.5 >= O2.5 >= O3.5). Mixed sources (market on one
    # line, our model on another) can break that, so enforce it, trusting market lines over model lines.
    a.goals.sort(key=lambda r: r.line)
    for i in range(len(a.goals) - 1, 0, -1):
        lo, hi = a.goals[i - 1], a.goals[i]
        if lo.p_over < hi.p_over:
            if lo.source == "model" or hi.source == "market":
                lo.p_over = hi.p_over
            else:
                hi.p_over = lo.p_over
    bt = _usable(odds.btts)
    a.btts = LineRow(0, bt.prob_yes if bt else model.btts, "market" if bt else "model", model.btts, odds.btts)

    # Corners: rating model (+ market)
    if lm.corners:
        eh, ea = lm.corners.expected(home["id"], away["id"])
        a.corners_expected = eh + ea
        pmf = negbin_pmf(eh + ea, CORNERS_NB_SIZE, 60)
        lines = sorted(odds.corners) or [8.5, 9.5, 10.5, 11.5]
        for line in lines:
            po, pu, _ = over_under(pmf, line)
            m_over = po / (po + pu) if po + pu else 0.5
            o, mk = odds.corners.get(line), _usable(odds.corners.get(line))
            a.corners.append(LineRow(line, mk.prob_yes if mk else m_over, "market" if mk else "model", m_over, o))
        a.corners_lead = _corners_lead(a.corners, pmf)

    # Cards: rating model with referee factor (information only)
    if lm.cards:
        eh, ea = lm.cards.expected(home["id"], away["id"], ref)
        a.cards_expected = eh + ea
        pmf = poisson_pmf(eh + ea, 40)
        for line in sorted(odds.cards) or [3.5, 4.5, 5.5]:
            po, pu, _ = over_under(pmf, line)
            m_over = po / (po + pu) if po + pu else 0.5
            o, mk = odds.cards.get(line), _usable(odds.cards.get(line))
            a.cards.append(LineRow(line, mk.prob_yes if mk else m_over, "market" if mk else "model", m_over, o))
    return a


async def best_tips(client: SportmonksClient, store: HistoryStore, fixtures: list[dict],
                    max_fixtures: int = 15, top: int = 5) -> list[Analysis]:
    """Analyse upcoming fixtures (1 request each) and return the clearest favourites + any corners leads."""
    upcoming = [f for f in fixtures if f.get("state_id") in (None, 1)][:max_fixtures]
    sem = asyncio.Semaphore(3)

    async def one(f: dict) -> Analysis | None:
        async with sem:
            try:
                return await analyse_fixture(client, store, f["id"])
            except SportmonksError:
                return None

    results = [r for r in await asyncio.gather(*(one(f) for f in upcoming)) if r]
    results.sort(key=lambda a: (a.corners_lead is not None, a.favourite[1]), reverse=True)
    return results[:top]
