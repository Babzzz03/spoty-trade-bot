"""Checks that the rating engine recovers known strengths from simulated data."""
import math
import random

from spotybot.ratings import Obs, dixon_coles_matrix, fit, negbin_pmf, over_under, poisson_pmf


def _poisson(rng, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p < l:
            return k
        k += 1


def test_fit_recovers_known_ratings():
    rng = random.Random(1)
    true_a = {t: a for t, a in zip(range(8), (1.6, 1.3, 1.1, 1.0, 1.0, 0.9, 0.8, 0.6))}
    true_d = {t: d for t, d in zip(range(8), (0.6, 0.8, 0.9, 1.0, 1.0, 1.1, 1.3, 1.5))}
    obs = []
    day = 0
    for _ in range(6):  # 6 double round-robins
        for h in range(8):
            for a in range(8):
                if h != a:
                    day += 1
                    lh = 1.5 * true_a[h] * true_d[a]
                    la = 1.1 * true_a[a] * true_d[h]
                    obs.append(Obs(f"2025-01-01 00:00:00", h, a, _poisson(rng, lh), _poisson(rng, la)))
    r = fit(obs, "2025-02-01 00:00:00", xi=0.0, prior=1.0)
    # ordering of attack strengths is recovered and values are close
    est = [r.attack[t] / math.exp(sum(math.log(r.attack[x]) for x in range(8)) / 8) for t in range(8)]
    assert est[0] > est[3] > est[7]
    assert abs(math.log(est[0] / est[7]) - math.log(1.6 / 0.6)) < 0.35
    lh, la = r.expected(0, 7)
    assert lh > 2.5 * la


def test_distributions_sum_to_one_and_push():
    assert abs(sum(poisson_pmf(2.7, 40)) - 1) < 1e-9
    assert abs(sum(negbin_pmf(10.2, 20.0, 80)) - 1) < 1e-6
    po, pu, pp = over_under(poisson_pmf(10.0, 60), 10.0)
    assert pp > 0.1 and abs(po + pu + pp - 1) < 1e-9
    po, pu, pp = over_under(poisson_pmf(10.0, 60), 9.5)
    assert pp == 0 and abs(po + pu - 1) < 1e-9
    m = dixon_coles_matrix(1.5, 1.1)
    assert abs(sum(map(sum, m)) - 1) < 1e-9
