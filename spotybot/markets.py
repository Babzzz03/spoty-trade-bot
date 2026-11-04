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


# ---------------------------------------------------------------- the bot's main picks for a match

GOAL_PICK_MIN = 0.60  # a goals line is only suggested when it is at least this likely


def goal_picks(a: Analysis) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Best Over line = highest line still >= 60% likely; best Under line = lowest line >= 60% likely.

    Returns ((over_line, p), (under_line, p)); either can be None.
    """
    rows = {r.line: r.p_over for r in a.goals}
    over = next(((l, rows[l]) for l in (3.5, 2.5, 1.5, 0.5) if l in rows and rows[l] >= GOAL_PICK_MIN), None)
    under = next(((l, 1 - rows[l]) for l in (1.5, 2.5, 3.5, 4.5, 5.5) if l in rows and 1 - rows[l] >= GOAL_PICK_MIN),
                 None)
    return over, under


def main_picks(a: Analysis) -> list[tuple[Selection, float, str]]:
    """The predictions shown in the simple view, as (selection, probability, source)."""
    picks: list[tuple[Selection, float, str]] = []
    top = max(range(3), key=lambda k: a.result_probs[k])
    picks.append((Selection("Result", ("Home", "Draw", "Away")[top]), a.result_probs[top], a.result_source))
    src = {r.line: r.source for r in a.goals}
    over, under = goal_picks(a)
    if over:
        picks.append((Selection("Goals", "Over", over[0]), over[1], src.get(over[0], "model")))
    if under:
        picks.append((Selection("Goals", "Under", under[0]), under[1], src.get(under[0], "model")))
    if not over and not under:
        g = next((r for r in a.goals if r.line == 2.5), None)
        if g:
            side, q = ("Over", g.p_over) if g.p_over >= 0.5 else ("Under", 1 - g.p_over)
            picks.append((Selection("Goals", side, 2.5), q, g.source))
    if a.btts:
        side, q = ("Yes", a.btts.p_over) if a.btts.p_over >= 0.5 else ("No", 1 - a.btts.p_over)
        picks.append((Selection("BTTS", side), q, a.btts.source))
    return picks
