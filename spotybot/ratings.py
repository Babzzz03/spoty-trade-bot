"""Opponent-adjusted team ratings for count statistics (goals, corners, cards).

Model (per league), for a match between home team i and away team j:

    lambda_home = base_home * A[i] * D[j] * R[ref]
    lambda_away = base_away * A[j] * D[i] * R[ref]

    A[t]  "attack": how many the team produces relative to average (goals scored, corners won, cards received)
    D[t]  "defence": how many the team allows its opponent (goals conceded, corners conceded, cards provoked)
    R[r]  referee factor (used for cards only; 1.0 otherwise)

Fitted by maximum a-posteriori Poisson estimation with:
  * exponential time decay  w = exp(-xi * days_ago)   (recent matches count more)
  * a Gamma(c, c) prior on every factor, i.e. shrinkage toward 1.0 worth `c` pseudo-observations,
    so teams with little data stay close to average.

The multiplicative fixed-point updates below are the exact MAP solution for each factor
given the others (coordinate ascent), and converge in a few dozen iterations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Obs:
    when: str           # ISO kickoff
    home: int
    away: int
    y_home: float       # count for the home side
    y_away: float       # count for the away side
    ref: int | None = None


@dataclass
class Ratings:
    base_home: float
    base_away: float
    attack: dict[int, float] = field(default_factory=dict)
    defence: dict[int, float] = field(default_factory=dict)
    referee: dict[int, float] = field(default_factory=dict)

    def expected(self, home: int, away: int, ref: int | None = None) -> tuple[float, float]:
        r = self.referee.get(ref, 1.0) if ref is not None else 1.0
        a, d = self.attack, self.defence
        return (self.base_home * a.get(home, 1.0) * d.get(away, 1.0) * r,
                self.base_away * a.get(away, 1.0) * d.get(home, 1.0) * r)


def _days(a: str, b: str) -> float:
    da = datetime.fromisoformat(a.replace(" ", "T")[:19])
    db = datetime.fromisoformat(b.replace(" ", "T")[:19])
    return (db - da).total_seconds() / 86400.0


def fit(obs: list[Obs], as_of: str, xi: float = 0.004, prior: float = 3.0, ref_prior: float = 8.0,
        use_referee: bool = False, iters: int = 40) -> Ratings | None:
    """Fit ratings on matches played before `as_of`."""
    data = [(o, math.exp(-xi * max(0.0, _days(o.when, as_of)))) for o in obs if o.when < as_of]
    if len(data) < 20:
        return None
    sw = sum(w for _, w in data)
    base_h = sum(w * o.y_home for o, w in data) / sw
    base_a = sum(w * o.y_away for o, w in data) / sw
    teams = {o.home for o, _ in data} | {o.away for o, _ in data}
    refs = {o.ref for o, _ in data if o.ref is not None} if use_referee else set()
    A = {t: 1.0 for t in teams}
    D = {t: 1.0 for t in teams}
    R = {r: 1.0 for r in refs}

    for _ in range(iters):
        rf = lambda o: R.get(o.ref, 1.0) if use_referee and o.ref is not None else 1.0
        # attack
        num = {t: prior for t in teams}
        den = {t: prior for t in teams}
        for o, w in data:
            r = rf(o)
            num[o.home] += w * o.y_home
            den[o.home] += w * base_h * D[o.away] * r
            num[o.away] += w * o.y_away
            den[o.away] += w * base_a * D[o.home] * r
        A = {t: num[t] / den[t] for t in teams}
        # defence
        num = {t: prior for t in teams}
        den = {t: prior for t in teams}
        for o, w in data:
            r = rf(o)
            num[o.away] += w * o.y_home
            den[o.away] += w * base_h * A[o.home] * r
            num[o.home] += w * o.y_away
            den[o.home] += w * base_a * A[o.away] * r
        D = {t: num[t] / den[t] for t in teams}
        # referee
        if use_referee:
            num = {r: ref_prior for r in refs}
            den = {r: ref_prior for r in refs}
            for o, w in data:
                if o.ref is None:
                    continue
                num[o.ref] += w * (o.y_home + o.y_away)
                den[o.ref] += w * (base_h * A[o.home] * D[o.away] + base_a * A[o.away] * D[o.home])
            R = {r: num[r] / den[r] for r in refs}
        # base rates (keeps the average factor anchored)
        sh = sum(w * A[o.home] * D[o.away] * rf(o) for o, w in data)
        sa = sum(w * A[o.away] * D[o.home] * rf(o) for o, w in data)
        base_h = sum(w * o.y_home for o, w in data) / sh
        base_a = sum(w * o.y_away for o, w in data) / sa

    return Ratings(base_h, base_a, A, D, R)


# ---------------------------------------------------------------- distributions

def poisson_pmf(lam: float, kmax: int) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, kmax + 1):
        p.append(p[-1] * lam / k)
    return p


def negbin_pmf(mean: float, size: float, kmax: int) -> list[float]:
    """Negative binomial with variance = mean + mean^2/size (size -> inf gives Poisson)."""
    if size > 1e6:
        return poisson_pmf(mean, kmax)
    p = size / (size + mean)
    out = [p ** size]
    for k in range(1, kmax + 1):
        out.append(out[-1] * (k - 1 + size) / k * (1 - p))
    return out


def over_under(pmf: list[float], line: float) -> tuple[float, float, float]:
    """(P over, P under, P push) for a total line (push only on whole-number lines)."""
    over = sum(q for k, q in enumerate(pmf) if k > line)
    under = sum(q for k, q in enumerate(pmf) if k < line)
    push = max(0.0, 1.0 - over - under) if line == int(line) else 0.0
    s = over + under + push
    return over / s, under / s, push / s


def dixon_coles_matrix(lh: float, la: float, rho: float = -0.08, kmax: int = 10) -> list[list[float]]:
    ph, pa = poisson_pmf(lh, kmax), poisson_pmf(la, kmax)

    def tau(h: int, a: int) -> float:
        if h == 0 and a == 0:
            return 1 - lh * la * rho
        if h == 0 and a == 1:
            return 1 + lh * rho
        if h == 1 and a == 0:
            return 1 + la * rho
        if h == 1 and a == 1:
            return 1 - rho
        return 1.0

    m = [[ph[h] * pa[a] * tau(h, a) for a in range(kmax + 1)] for h in range(kmax + 1)]
    s = sum(map(sum, m))
    return [[v / s for v in row] for row in m]
