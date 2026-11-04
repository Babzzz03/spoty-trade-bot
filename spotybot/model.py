"""Match outcome model: recency-weighted team strengths + Dixon-Coles adjusted Poisson.

The approach:
  1. From each team's recent finished matches, estimate attack and defence strength
     relative to a baseline goal rate, with exponential recency decay and shrinkage
     toward average (so small samples don't produce extreme numbers).
  2. Expected goals: lambda_home = base_home * att_home * def_away (and vice versa).
  3. Build a score-probability matrix with independent Poisson goals, corrected for
     low-scoring draws using the Dixon-Coles tau adjustment.
  4. Derive 1X2, double chance, over/under, BTTS and most likely scorelines.
  5. Optionally blend with Sportmonks' own prediction probabilities when available.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sportmonks import Match

BASE_HOME_GOALS = 1.50  # typical top-flight home goals per game
BASE_AWAY_GOALS = 1.20  # typical top-flight away goals per game
DECAY = 0.85            # weight multiplier per match further in the past
SHRINK_MATCHES = 4.0    # pseudo-matches of "average" added to each estimate
RHO = -0.08             # Dixon-Coles low-score dependence
MAX_GOALS = 10


@dataclass
class TeamForm:
    team_id: int
    matches: int
    attack: float
    defence: float  # >1 means concedes more than average (weaker)
    form: str       # e.g. "WWDLW" most recent first
    goals_for_avg: float
    goals_against_avg: float


@dataclass
class Prediction:
    home: str
    away: str
    xg_home: float
    xg_away: float
    p_home: float
    p_draw: float
    p_away: float
    over_15: float
    over_25: float
    over_35: float
    btts: float
    top_scores: list[tuple[str, float]]
    home_form: TeamForm
    away_form: TeamForm
    h2h: list[Match] = field(default_factory=list)
    blended_with_sportmonks: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def pick(self) -> tuple[str, float]:
        options = {"1 (Home win)": self.p_home, "X (Draw)": self.p_draw, "2 (Away win)": self.p_away}
        label = max(options, key=options.get)
        return label, options[label]

    @property
    def double_chance(self) -> dict[str, float]:
        return {"1X": self.p_home + self.p_draw, "12": self.p_home + self.p_away, "X2": self.p_draw + self.p_away}

    @property
    def confidence(self) -> str:
        """Heuristic confidence from the pick's probability and the data sample size."""
        _, p = self.pick
        sample = min(self.home_form.matches, self.away_form.matches)
        if sample < 4:
            return "Low (limited data)"
        if p >= 0.60:
            return "High"
        if p >= 0.48:
            return "Medium"
        return "Low"


def team_form(team_id: int, matches: list[Match]) -> TeamForm:
    """Recency-weighted attack/defence strengths for a team from its recent matches (newest first)."""
    w_sum = gf_w = ga_w = exp_gf_w = exp_ga_w = 0.0
    letters = []
    gf_tot = ga_tot = 0
    for i, m in enumerate(matches):
        is_home = m.home_id == team_id
        gf, ga = (m.home_goals, m.away_goals) if is_home else (m.away_goals, m.home_goals)
        exp_gf, exp_ga = (BASE_HOME_GOALS, BASE_AWAY_GOALS) if is_home else (BASE_AWAY_GOALS, BASE_HOME_GOALS)
        w = DECAY ** i
        w_sum += w
        gf_w += w * gf
        ga_w += w * ga
        exp_gf_w += w * exp_gf
        exp_ga_w += w * exp_ga
        gf_tot += gf
        ga_tot += ga
        if i < 5:
            letters.append("W" if gf > ga else "D" if gf == ga else "L")

    # Shrink toward league average using pseudo-matches at neutral expectation.
    neutral = (BASE_HOME_GOALS + BASE_AWAY_GOALS) / 2
    attack = (gf_w + SHRINK_MATCHES * neutral) / (exp_gf_w + SHRINK_MATCHES * neutral)
    defence = (ga_w + SHRINK_MATCHES * neutral) / (exp_ga_w + SHRINK_MATCHES * neutral)
    n = len(matches)
    return TeamForm(
        team_id=team_id,
        matches=n,
        attack=attack,
        defence=defence,
        form="".join(letters) or "-",
        goals_for_avg=gf_tot / n if n else 0.0,
        goals_against_avg=ga_tot / n if n else 0.0,
    )


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _tau(h: int, a: int, lh: float, la: float, rho: float) -> float:
    if h == 0 and a == 0:
        return 1 - lh * la * rho
    if h == 0 and a == 1:
        return 1 + lh * rho
    if h == 1 and a == 0:
        return 1 + la * rho
    if h == 1 and a == 1:
        return 1 - rho
    return 1.0


def score_matrix(lh: float, la: float, rho: float = RHO, max_goals: int = MAX_GOALS) -> list[list[float]]:
    m = [[_poisson(h, lh) * _poisson(a, la) * _tau(h, a, lh, la, rho) for a in range(max_goals + 1)]
         for h in range(max_goals + 1)]
    total = sum(map(sum, m))
    return [[v / total for v in row] for row in m]


def h2h_adjustment(h2h: list[Match], home_id: int) -> float:
    """Small multiplicative nudge (±5% max) to home xG from head-to-head goal difference."""
    if not h2h:
        return 1.0
    gd = 0.0
    for m in h2h[:6]:
        diff = m.home_goals - m.away_goals
        gd += diff if m.home_id == home_id else -diff
    gd /= min(len(h2h), 6)
    return 1.0 + max(-0.05, min(0.05, gd * 0.025))


def predict(
    home: dict,
    away: dict,
    home_matches: list[Match],
    away_matches: list[Match],
    h2h: list[Match] | None = None,
    sportmonks_probs: dict[str, float] | None = None,
    blend_weight: float = 0.35,
) -> Prediction:
    hf = team_form(home["id"], home_matches)
    af = team_form(away["id"], away_matches)

    adj = h2h_adjustment(h2h or [], home["id"])
    lh = max(0.15, BASE_HOME_GOALS * hf.attack * af.defence * adj)
    la = max(0.15, BASE_AWAY_GOALS * af.attack * hf.defence / adj)

    mat = score_matrix(lh, la)
    n = len(mat)
    p_home = sum(mat[h][a] for h in range(n) for a in range(n) if h > a)
    p_draw = sum(mat[i][i] for i in range(n))
    p_away = 1 - p_home - p_draw

    def over(line: float) -> float:
        return sum(mat[h][a] for h in range(n) for a in range(n) if h + a > line)

    btts = sum(mat[h][a] for h in range(1, n) for a in range(1, n))
    scores = sorted(((f"{h}-{a}", mat[h][a]) for h in range(n) for a in range(n)), key=lambda x: -x[1])[:3]

    notes = []
    blended = False
    if sportmonks_probs and all(k in sportmonks_probs for k in ("home", "draw", "away")):
        s = sum(sportmonks_probs[k] for k in ("home", "draw", "away"))
        if s > 0:
            w = blend_weight
            p_home = (1 - w) * p_home + w * sportmonks_probs["home"] / s
            p_draw = (1 - w) * p_draw + w * sportmonks_probs["draw"] / s
            p_away = (1 - w) * p_away + w * sportmonks_probs["away"] / s
            blended = True
    if min(hf.matches, af.matches) < 5:
        notes.append("Few recent matches found for at least one team; treat with caution.")

    return Prediction(
        home=home.get("name", "Home"),
        away=away.get("name", "Away"),
        xg_home=lh,
        xg_away=la,
        p_home=p_home,
        p_draw=p_draw,
        p_away=p_away,
        over_15=over(1.5),
        over_25=over(2.5),
        over_35=over(3.5),
        btts=btts,
        top_scores=scores,
        home_form=hf,
        away_form=af,
        h2h=h2h or [],
        blended_with_sportmonks=blended,
        notes=notes,
    )


def extract_sportmonks_1x2(fixture: dict) -> dict[str, float] | None:
    """Pull Sportmonks' fulltime-result probabilities from an included `predictions.type` list."""
    for p in fixture.get("predictions") or []:
        code = ((p.get("type") or {}).get("developer_name") or "").upper()
        if code in ("FULLTIME_RESULT_PROBABILITY", "FULLTIME_RESULT"):
            pred = p.get("predictions") or {}
            try:
                return {k: float(pred[k]) / 100.0 for k in ("home", "draw", "away")}
            except (KeyError, TypeError, ValueError):
                return None
    return None
