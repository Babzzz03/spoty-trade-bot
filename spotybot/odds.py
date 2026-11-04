"""Parse Sportmonks pre-match odds into prices and margin-free probabilities.

Bookmaker odds include a margin: 1/odds summed over all outcomes is > 1 (e.g. 1.05).
We remove it proportionally, averaging the implied probabilities across bookmakers.
Fair odds for a probability p are simply 1 / p.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MARKET_RESULT, MARKET_GOALS, MARKET_BTTS, MARKET_CORNERS, MARKET_CARDS = 1, 80, 14, 67, 255


@dataclass
class Side:
    prices: list[float]

    @property
    def avg(self) -> float:
        return sum(self.prices) / len(self.prices)

    @property
    def best(self) -> float:
        return max(self.prices)


@dataclass
class TwoWay:
    yes: Side   # over / yes
    no: Side    # under / no

    @property
    def books(self) -> int:
        return len(self.yes.prices)

    @property
    def prob_yes(self) -> float:
        """Margin-free market probability of over / yes."""
        y = sum(1 / v for v in self.yes.prices) / len(self.yes.prices)
        n = sum(1 / v for v in self.no.prices) / len(self.no.prices)
        return y / (y + n)


@dataclass
class FixtureOdds:
    result: list[Side] | None = None                                   # [home, draw, away]
    goals: dict[float, TwoWay] = field(default_factory=dict)          # line -> over/under
    btts: TwoWay | None = None
    corners: dict[float, TwoWay] = field(default_factory=dict)
    cards: dict[float, TwoWay] = field(default_factory=dict)

    def result_probs(self) -> list[float] | None:
        if not self.result:
            return None
        raw = [sum(1 / v for v in s.prices) / len(s.prices) for s in self.result]
        total = sum(raw)
        return [r / total for r in raw]


_RESULT = {"home": 0, "1": 0, "draw": 1, "x": 1, "away": 2, "2": 2}


def parse_fixture_odds(fixture: dict, min_books: int = 2) -> FixtureOdds:
    per_book: dict[tuple, dict[str, float]] = {}
    for o in fixture.get("odds") or []:
        try:
            val = float(o["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if val <= 1.0:
            continue
        mid, label, book = o.get("market_id"), str(o.get("label", "")).strip().lower(), o.get("bookmaker_id")
        if mid == MARKET_RESULT and label in _RESULT:
            per_book.setdefault((mid, None, book), {})[str(_RESULT[label])] = val
        elif mid == MARKET_BTTS and label in ("yes", "no"):
            per_book.setdefault((mid, None, book), {})[label] = val
        elif mid in (MARKET_GOALS, MARKET_CORNERS, MARKET_CARDS) and label in ("over", "under"):
            try:
                line = float(o.get("total"))
            except (TypeError, ValueError):
                continue
            if line * 2 != int(line * 2):  # skip quarter (Asian) lines
                continue
            per_book.setdefault((mid, line, book), {})[label] = val

    out = FixtureOdds()
    res = [b for (m, _, _), b in per_book.items() if m == MARKET_RESULT and len(b) == 3]
    if len(res) >= min_books:
        out.result = [Side([b[str(i)] for b in res]) for i in range(3)]
    bt = [b for (m, _, _), b in per_book.items() if m == MARKET_BTTS and len(b) == 2]
    if len(bt) >= min_books:
        out.btts = TwoWay(Side([b["yes"] for b in bt]), Side([b["no"] for b in bt]))
    for mid, target in ((MARKET_GOALS, out.goals), (MARKET_CORNERS, out.corners), (MARKET_CARDS, out.cards)):
        lines: dict[float, list[dict[str, float]]] = {}
        for (m, line, _), b in per_book.items():
            if m == mid and len(b) == 2:
                lines.setdefault(line, []).append(b)
        for line, books in lines.items():
            if len(books) >= min_books:
                target[line] = TwoWay(Side([b["over"] for b in books]), Side([b["under"] for b in books]))
    return out


def fair_odds(p: float) -> str:
    return f"{1 / p:.2f}" if p > 0.005 else "—"
