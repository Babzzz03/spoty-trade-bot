"""Bet selections the user can type, their probability from an Analysis, and how they settle.

Accepted selections (case-insensitive):
  1 / home, x / draw, 2 / away            match result
  1x, x2, 12                              double chance
  over2.5 / o2.5, under2.5 / u2.5         total goals
  btts / gg, btts-no / ng                 both teams to score
  corners-over9.5 / co9.5, corners-under9.5 / cu9.5
  cards-over3.5 / ko3.5, cards-under3.5 / ku3.5
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .analysis import Analysis, LineRow


@dataclass(frozen=True)
class Selection:
    market: str          # Result, Double chance, Goals, BTTS, Corners, Cards
    side: str            # Home/Draw/Away, 1X/X2/12, Over/Under, Yes/No
    line: float = 0.0

    @property
    def label(self) -> str:
        if self.market in ("Goals", "Corners", "Cards"):
            return f"{self.side} {self.line:g} {self.market.lower()}"
        if self.market == "BTTS":
            return f"Both teams to score: {self.side}"
        if self.market == "Double chance":
            return f"Double chance {self.side}"
        return {"Home": "Home win", "Draw": "Draw", "Away": "Away win"}[self.side]


_SIMPLE = {
    "1": ("Result", "Home"), "home": ("Result", "Home"), "x": ("Result", "Draw"), "draw": ("Result", "Draw"),
    "2": ("Result", "Away"), "away": ("Result", "Away"),
    "1x": ("Double chance", "1X"), "x2": ("Double chance", "X2"), "12": ("Double chance", "12"),
    "btts": ("BTTS", "Yes"), "gg": ("BTTS", "Yes"), "btts-yes": ("BTTS", "Yes"),
    "btts-no": ("BTTS", "No"), "ng": ("BTTS", "No"),
}
_LINE = re.compile(r"^(corners-|cards-|c|k)?(over|under|o|u)(\d+(?:\.\d)?)$")


def parse_selection(token: str) -> Selection | None:
    t = token.strip().lower().replace(" ", "").replace(",", ".")
    if t in _SIMPLE:
        return Selection(*_SIMPLE[t])
    m = _LINE.match(t)
    if not m:
        return None
    prefix, side, line = m.groups()
    market = {"corners-": "Corners", "c": "Corners", "cards-": "Cards", "k": "Cards"}.get(prefix or "", "Goals")
    return Selection(market, "Over" if side.startswith("o") else "Under", float(line))


@dataclass
class Estimate:
    prob: float          # probability the selection wins (pushes excluded)
    source: str          # "market" or "model"


def estimate(a: Analysis, sel: Selection) -> Estimate | None:
    """Our best probability for a selection, from an Analysis."""
    if sel.market in ("Result", "Double chance"):
        h, d, w = a.result_probs
        p = {"Home": h, "Draw": d, "Away": w, "1X": h + d, "X2": d + w, "12": h + w}[sel.side]
        return Estimate(p, a.result_source)
    if sel.market == "BTTS":
        if not a.btts:
            return None
        return Estimate(a.btts.p_over if sel.side == "Yes" else 1 - a.btts.p_over, a.btts.source)
    rows: list[LineRow] = {"Goals": a.goals, "Corners": a.corners, "Cards": a.cards}[sel.market]
    row = next((r for r in rows if r.line == sel.line), None)
    if row is None:
        return None
    return Estimate(row.p_over if sel.side == "Over" else 1 - row.p_over, row.source)


def settle(sel: Selection, hg: int, ag: int, corners: int | None, cards: int | None) -> str | None:
    """'won' / 'lost' / 'push', or None if the needed statistic is missing."""
    if sel.market == "Result":
        res = "Home" if hg > ag else "Draw" if hg == ag else "Away"
        return "won" if res == sel.side else "lost"
    if sel.market == "Double chance":
        res = "1" if hg > ag else "X" if hg == ag else "2"
        return "won" if res in sel.side else "lost"
    if sel.market == "BTTS":
        yes = hg > 0 and ag > 0
        return "won" if yes == (sel.side == "Yes") else "lost"
    total = {"Goals": hg + ag, "Corners": corners, "Cards": cards}[sel.market]
    if total is None:
        return None
    if total == sel.line:
        return "push"
    return "won" if (total > sel.line) == (sel.side == "Over") else "lost"
