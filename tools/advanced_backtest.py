"""Advanced walk-forward backtest: opponent-adjusted ratings for goals, corners and cards vs bookmaker odds.

Protocol (to avoid fooling ourselves):
  * Ratings for a match are fitted only on matches played before that match's week.
  * Hyper-parameters (time decay, shrinkage, dispersion, model/market blend weight) are tuned
    on the TRAIN period only (default: before 2025-07-01).
  * Betting results are reported on the TEST period only (default: 2025-07-01 onward), at the
    AVERAGE bookmaker price, with a standard error so luck can be told apart from skill.

Usage:
    python -m tools.advanced_backtest --leagues 271,501
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from spotybot.ratings import Obs, dixon_coles_matrix, fit, negbin_pmf, over_under
from tools.data import FullMatch, fetch_full_matches, fetch_ou_odds
from tools.odds_backtest import fetch_odds

WINDOW_DAYS = 730
MIN_TEAM_MATCHES = 5


# ----------------------------------------------------------------- walk-forward expectations

def walk(matches: list[FullMatch], stat: str, xi: float, prior: float, use_ref: bool = False
         ) -> dict[int, tuple[float, float]]:
    """fixture_id -> (expected home count, expected away count), fitted weekly on prior data only."""
    getter = {"goals": lambda m: (m.hg, m.ag), "corners": lambda m: (m.hc, m.ac),
              "cards": lambda m: (None if m.hy is None else m.hy + m.hr, None if m.ay is None else m.ay + m.ar)}[stat]
    out: dict[int, tuple[float, float]] = {}
    by_league: dict[int, list[FullMatch]] = defaultdict(list)
    for m in matches:
        by_league[m.league_id].append(m)
    for league_matches in by_league.values():
        obs_all = [(m, Obs(m.kickoff, m.home_id, m.away_id, *getter(m), ref=m.referee_id)) for m in league_matches]
        obs_all = [(m, o) for m, o in obs_all if o.y_home is not None and o.y_away is not None]
        weeks: dict[str, list[FullMatch]] = defaultdict(list)
        for m in league_matches:
            d = datetime.fromisoformat(m.kickoff[:10]).date()
            weeks[(d - timedelta(days=d.weekday())).isoformat()].append(m)
        for monday, week_matches in sorted(weeks.items()):
            as_of = monday + " 00:00:00"
            start = (datetime.fromisoformat(monday) - timedelta(days=WINDOW_DAYS)).isoformat(sep=" ")
            hist = [o for m, o in obs_all if start <= o.when < as_of]
            counts: dict[int, int] = defaultdict(int)
            for o in hist:
                counts[o.home] += 1
                counts[o.away] += 1
            r = fit(hist, as_of, xi=xi, prior=prior, use_referee=use_ref, iters=25)
            if not r:
                continue
            for m in week_matches:
                if counts[m.home_id] >= MIN_TEAM_MATCHES and counts[m.away_id] >= MIN_TEAM_MATCHES:
                    out[m.fixture_id] = r.expected(m.home_id, m.away_id, m.referee_id if use_ref else None)
    return out


# ----------------------------------------------------------------- helpers

def implied_two_way(o: dict[str, list[float]]) -> float:
    """Market probability of 'over' with the bookmaker margin removed (average across bookmakers)."""
    ov = sum(1 / v for v in o["over"]) / len(o["over"])
    un = sum(1 / v for v in o["under"]) / len(o["under"])
    return ov / (ov + un)


def total_pmf(lh: float, la: float, size: float, kmax: int = 40) -> list[float]:
    return negbin_pmf(lh + la, size, kmax)


def count_loglik(exp: dict[int, tuple[float, float]], matches: list[FullMatch], stat: str, size: float) -> float:
    total = {"goals": lambda m: m.hg + m.ag, "corners": lambda m: m.corners, "cards": lambda m: m.cards}[stat]
    ll, n = 0.0, 0
    for m in matches:
        if m.fixture_id not in exp or total(m) is None:
            continue
        pmf = total_pmf(*exp[m.fixture_id], size, kmax=max(40, total(m) + 1))
        ll += math.log(max(pmf[total(m)], 1e-12))
        n += 1
    return ll / max(n, 1)


def bet_report(label: str, bets: list[tuple[float, float]]) -> None:
    """bets: list of (profit, stake=1). Prints ROI with a standard error."""
    n = len(bets)
    if not n:
        print(f"    {label:<34} no bets")
        return
    profits = [p for p, _ in bets]
    roi = sum(profits) / n
    sd = math.sqrt(sum((p - roi) ** 2 for p in profits) / max(n - 1, 1))
    se = sd / math.sqrt(n)
    wins = sum(p > 0 for p in profits)
    z = roi / se if se else 0.0
    print(f"    {label:<34} {n:>5} bets  win {wins / n:5.1%}  profit {sum(profits):+7.1f}  "
          f"ROI {roi:+6.1%} ± {se:4.1%}  (z={z:+.1f})")


# ----------------------------------------------------------------- totals markets (goals/corners/cards)

def evaluate_totals(name, stat, matches, odds, split, grid, use_ref=False):
    print(f"\n================ {name.upper()} ================")
    total = {"goals": lambda m: m.hg + m.ag, "corners": lambda m: m.corners, "cards": lambda m: m.cards}[stat]
    train = [m for m in matches if m.kickoff < split]
    test = [m for m in matches if m.kickoff >= split]

    # 1) tune xi / prior / dispersion on TRAIN by log-likelihood of the observed totals
    best = None
    cache = {}
    for xi, prior in grid:
        exp = walk(matches, stat, xi, prior, use_ref)
        cache[(xi, prior)] = exp
        for size in (1e9, 40.0, 20.0, 10.0):
            ll = count_loglik(exp, train, stat, size)
            if best is None or ll > best[0]:
                best = (ll, xi, prior, size)
    _, xi, prior, size = best
    exp = cache[(xi, prior)]
    print(f"  tuned on train: decay xi={xi} (half-life {math.log(2) / xi:.0f} days), shrinkage={prior}, "
          f"dispersion={'Poisson' if size > 1e6 else f'NB size {size:g}'}")

    # 2) per-line probabilities
    def rows(ms):
        for m in ms:
            if m.fixture_id not in exp or m.fixture_id not in odds or total(m) is None:
                continue
            pmf = total_pmf(*exp[m.fixture_id], size, kmax=60)
            for line, o in odds[m.fixture_id].items():
                if len(o["over"]) < 3:
                    continue
                po, pu, pp = over_under(pmf, line)
                yield m, line, o, po, pu, pp

    # 3) calibration vs market on TEST: log loss on the market's main lines (decided outcomes only)
    def logloss(ms, w):
        ll = n = 0
        for m, line, o, po, pu, pp in rows(ms):
            t = total(m)
            if t == line:
                continue
            q_model = po / (po + pu) if (po + pu) else 0.5
            q = w * q_model + (1 - w) * implied_two_way(o)
            q = min(max(q, 1e-6), 1 - 1e-6)
            ll += -math.log(q if t > line else 1 - q)
            n += 1
        return ll / max(n, 1), n

    ws = (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0)
    train_ll = {w: logloss(train, w)[0] for w in ws}
    w_best = min(train_ll, key=train_ll.get)
    test_market, n = logloss(test, 0.0)
    test_model, _ = logloss(test, 1.0)
    test_blend, _ = logloss(test, w_best)
    print(f"  TEST log loss over all lines ({n} line-outcomes, lower = better):")
    print(f"    bookmakers {test_market:.4f} | our model {test_model:.4f} | blend {int(w_best * 100)}% model {test_blend:.4f}")

    # 4) betting on TEST: one bet per match = best expected value line/side
    print(f"  TEST betting, 1 unit at AVERAGE bookmaker price, one bet per match (best line/side):")
    for w in sorted({w_best, 1.0}):
        for thr in (0.02, 0.05, 0.10):
            best_bet: dict[int, tuple] = {}
            for m, line, o, po, pu, pp in rows(test):
                q_mkt_over = implied_two_way(o)
                q_mkt_under = 1 - q_mkt_over
                # blend on the decided outcomes, then re-apply push probability for whole lines
                dec = po + pu
                q_over = (w * (po / dec if dec else 0.5) + (1 - w) * q_mkt_over) * (1 - pp)
                q_under = (w * (pu / dec if dec else 0.5) + (1 - w) * q_mkt_under) * (1 - pp)
                for side, q in (("over", q_over), ("under", q_under)):
                    price = sum(o[side]) / len(o[side])
                    ev = q * price + pp - 1
                    if ev > thr and (m.fixture_id not in best_bet or ev > best_bet[m.fixture_id][0]):
                        best_bet[m.fixture_id] = (ev, line, side, price, total(m))
            bets = []
            for ev, line, side, price, t in best_bet.values():
                if t == line:
                    bets.append((0.0, 1))
                elif (t > line) == (side == "over"):
                    bets.append((price - 1, 1))
                else:
                    bets.append((-1.0, 1))
            bet_report(f"{'blend ' + str(int(w * 100)) + '% model' if w < 1 else 'pure model':<14} edge > {thr:.0%}", bets)
    return exp


# ----------------------------------------------------------------- 1X2

def evaluate_1x2(matches, exp_goals, odds1x2, split, train_w_grid=(0.0, 0.1, 0.2, 0.3, 0.5, 1.0)):
    print("\n================ MATCH RESULT (1X2) ================")

    def probs(m):
        lh, la = exp_goals[m.fixture_id]
        mat = dixon_coles_matrix(lh, la)
        n = len(mat)
        ph = sum(mat[h][a] for h in range(n) for a in range(n) if h > a)
        pd = sum(mat[i][i] for i in range(n))
        return [ph, pd, 1 - ph - pd]

    def market(o):
        raw = [sum(1 / v for v in col) / len(col) for col in o]
        s = sum(raw)
        return [r / s for r in raw]

    def outcome(m):
        return 0 if m.hg > m.ag else 1 if m.hg == m.ag else 2

    usable = [m for m in matches if m.fixture_id in exp_goals and m.fixture_id in odds1x2]
    train = [m for m in usable if m.kickoff < split]
    test = [m for m in usable if m.kickoff >= split]

    def ll(ms, w):
        s = 0.0
        for m in ms:
            q = [w * a + (1 - w) * b for a, b in zip(probs(m), market(odds1x2[m.fixture_id]))]
            s += -math.log(max(q[outcome(m)], 1e-9))
        return s / max(len(ms), 1)

    w_best = min(train_w_grid, key=lambda w: ll(train, w))
    hits = lambda ms, f: sum(f(m).index(max(f(m))) == outcome(m) for m in ms) / max(len(ms), 1)
    print(f"  TEST ({len(test)} matches): pick correct  model {hits(test, probs):.1%} | bookmakers "
          f"{hits(test, lambda m: market(odds1x2[m.fixture_id])):.1%}")
    print(f"  TEST log loss: bookmakers {ll(test, 0.0):.4f} | model {ll(test, 1.0):.4f} | "
          f"blend {int(w_best * 100)}% model {ll(test, w_best):.4f}")
    print("  TEST betting, 1 unit at AVERAGE bookmaker price:")
    for w in sorted({w_best, 1.0}):
        for thr in (0.02, 0.05, 0.10):
            bets = []
            for m in test:
                o = odds1x2[m.fixture_id]
                q = [w * a + (1 - w) * b for a, b in zip(probs(m), market(o))]
                price = [sum(c) / len(c) for c in o]
                ev = [q[i] * price[i] - 1 for i in range(3)]
                i = ev.index(max(ev))
                if ev[i] > thr:
                    bets.append(((price[i] - 1) if i == outcome(m) else -1.0, 1))
            bet_report(f"{'blend ' + str(int(w * 100)) + '% model' if w < 1 else 'pure model':<14} edge > {thr:.0%}", bets)


# ----------------------------------------------------------------- main

async def load(token, leagues, periods):
    matches, odds_ou, odds_1x2 = [], {80: {}, 14: {}, 67: {}, 255: {}}, {}
    for s, e in periods:
        matches += await fetch_full_matches(token, s, e, leagues)
        for mids in ([80, 14], [67, 255]):
            got = await fetch_ou_odds(token, s, e, leagues, mids)
            for mid in mids:
                odds_ou[mid].update(got[mid])
        odds_1x2.update(await fetch_odds(token, s, e, leagues))
    uniq = {m.fixture_id: m for m in matches}
    return sorted(uniq.values(), key=lambda m: m.kickoff), odds_ou, odds_1x2


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="271,501")
    ap.add_argument("--split", default="2025-07-01", help="train before / test from this date")
    ap.add_argument("--history-from", default="2023-07-01")
    a = ap.parse_args()
    token = os.environ["SPORTMONKS_API_TOKEN"]
    leagues = [int(x) for x in a.leagues.split(",") if x.strip()]
    hist_start = date.fromisoformat(a.history_from)
    periods = []
    if hist_start < date(2024, 7, 1):
        periods.append((hist_start, date(2024, 6, 30)))
    periods.append((date(2024, 7, 1), date.today()))
    matches, odds_ou, odds_1x2 = asyncio.run(load(token, leagues, periods))
    split = a.split + " 00:00:00"
    print(f"Loaded {len(matches)} finished matches; train < {a.split} <= test "
          f"({sum(m.kickoff >= split for m in matches)} test matches)")

    grid = [(xi, pr) for xi in (0.002, 0.004, 0.008) for pr in (2.0, 5.0, 10.0)]
    exp_goals = evaluate_totals("Goals over/under (all lines)", "goals", matches, odds_ou[80], split, grid)
    evaluate_1x2(matches, exp_goals, odds_1x2, split)
    evaluate_totals("Corners over/under (all lines)", "corners", matches, odds_ou[67], split, grid)
    evaluate_totals("Cards over/under (all lines), with referee factor", "cards", matches, odds_ou[255], split, grid,
                    use_ref=True)


if __name__ == "__main__":
    main()
