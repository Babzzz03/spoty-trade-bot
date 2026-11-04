"""Compare the model with bookmaker odds on real past matches, and simulate value betting.

Uses Sportmonks' stored 1X2 odds (market 1) from every bookmaker available on your plan.
Note: stored odds are the latest pre-match prices, i.e. close to closing odds, which are
the hardest prices to beat.

Usage:
    python -m tools.odds_backtest --leagues 271,501
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

from spotybot.sportmonks import BASE_URL
from tools.cache import cached_get
from tools.backtest import fetch_results, walk_forward

LABELS = {"home": 0, "1": 0, "draw": 1, "x": 1, "away": 2, "2": 2}


async def fetch_odds(token: str, start: date, end: date, leagues: list[int]) -> dict[int, list[list[float]]]:
    """fixture_id -> [[home odds...], [draw odds...], [away odds...]] across bookmakers."""
    out: dict[int, list[list[float]]] = {}
    filters = "markets:1" + (";fixtureLeagues:" + ",".join(map(str, leagues)) if leagues else "")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as http:
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=90), end)
            page = 1
            while True:
                body = await cached_get(http, f"/fixtures/between/{cur}/{chunk_end}", {
                    "api_token": token, "include": "odds", "filters": filters, "per_page": 50, "page": page}, chunk_end)
                for f in body.get("data") or []:
                    by_book: dict[int, dict[int, float]] = {}
                    for o in f.get("odds") or []:
                        if o.get("market_id") != 1:
                            continue
                        idx = LABELS.get(str(o.get("label", "")).strip().lower())
                        try:
                            val = float(o["value"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if idx is not None and val > 1.0:
                            by_book.setdefault(o["bookmaker_id"], {})[idx] = val
                    books = [b for b in by_book.values() if len(b) == 3]
                    if books:
                        out[f["id"]] = [[b[i] for b in books] for i in range(3)]
                if not (body.get("pagination") or {}).get("has_more"):
                    break
                page += 1
            cur = chunk_end + timedelta(days=1)
    return out


def implied(odds: list[list[float]]) -> list[float]:
    """Average bookmaker probability with the margin removed."""
    raw = [sum(1 / o for o in col) / len(col) for col in odds]
    s = sum(raw)
    return [r / s for r in raw]


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--leagues", default="")
    a = ap.parse_args()
    token = os.environ["SPORTMONKS_API_TOKEN"]
    leagues = [int(x) for x in a.leagues.split(",") if x.strip()]
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)

    matches = asyncio.run(fetch_results(token, start, end, leagues))
    odds = asyncio.run(fetch_odds(token, start, end, leagues))
    rows = [(m, p) for m, p in walk_forward(matches) if m.fixture_id in odds]
    n = len(rows)
    print(f"Matches with model prediction AND bookmaker odds: {n}\n")
    if not n:
        return

    def outcome(m) -> int:
        return 0 if m.home_goals > m.away_goals else 1 if m.home_goals == m.away_goals else 2

    def score(get) -> tuple[float, float]:
        hits = ll = 0.0
        for m, p in rows:
            q = get(m, p)
            o = outcome(m)
            hits += q.index(max(q)) == o
            ll += -math.log(max(q[o], 1e-9))
        return hits / n, ll / n

    model = lambda m, p: [p.p_home, p.p_draw, p.p_away]
    book = lambda m, p: implied(odds[m.fixture_id])
    print("                       pick correct   log loss (lower = better)")
    for name, fn in [("Our model", model), ("Bookmakers", book)] + [
        (f"Blend {int(w*100)}% model", (lambda w: lambda m, p: [w * a + (1 - w) * b for a, b in zip(model(m, p), book(m, p))])(w))
        for w in (0.1, 0.2, 0.3, 0.5)
    ]:
        h, l = score(fn)
        print(f"  {name:<20} {h:>10.1%}   {l:>10.4f}")

    for price_name, price_fn in (("AVERAGE bookmaker price (realistic)", lambda c: sum(c) / len(c)),
                                 ("BEST price across all bookmakers (optimistic)", max)):
        simulate(rows, odds, model, book, outcome, price_name, price_fn)


def simulate(rows, odds, model, book, outcome, price_name, price_fn) -> None:
    print(f"\nBETTING SIMULATION: 1 unit per bet at the {price_name}")
    print("  strategy                                   bets   won   profit    ROI")

    def sim(label: str, choose) -> None:
        bets = won = 0
        profit = 0.0
        for m, p in rows:
            pick = choose(m, p)
            if pick is None:
                continue
            price = price_fn(odds[m.fixture_id][pick])
            bets += 1
            if pick == outcome(m):
                won += 1
                profit += price - 1
            else:
                profit -= 1
        if bets:
            print(f"  {label:<42} {bets:>5} {won:>5} {profit:>+8.1f} {profit / bets:>+7.1%}")

    sim("Back the model's pick every match", lambda m, p: model(m, p).index(max(model(m, p))))
    sim("Back the bookmakers' favourite every match", lambda m, p: book(m, p).index(max(book(m, p))))
    for edge in (0.0, 0.05, 0.10, 0.20):
        def value(m, p, edge=edge):
            q = model(m, p)
            best = [price_fn(c) for c in odds[m.fixture_id]]
            ev = [q[i] * best[i] - 1 for i in range(3)]
            i = ev.index(max(ev))
            return i if ev[i] > edge else None
        sim(f"Value bets (model edge > {edge:.0%})", value)


if __name__ == "__main__":
    main()
