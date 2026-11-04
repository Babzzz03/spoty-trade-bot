"""Over/Under 2.5 and BTTS: compare the model with bookmaker odds and simulate betting.

Usage:
    python -m tools.goals_backtest --leagues 271,501
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
from tools.backtest import fetch_results, walk_forward
from tools.cache import cached_get

# market_id -> (name, label meaning "yes", label meaning "no", required total)
MARKETS = {80: ("Over/Under 2.5", "over", "under", "2.5"), 14: ("Both teams to score", "yes", "no", None)}


async def fetch_goal_odds(token: str, start: date, end: date, leagues: list[int]) -> dict[int, dict[int, list[list[float]]]]:
    """market_id -> fixture_id -> [[yes odds...], [no odds...]] across bookmakers."""
    out: dict[int, dict[int, list[list[float]]]] = {m: {} for m in MARKETS}
    filters = "markets:" + ",".join(map(str, MARKETS)) + (";fixtureLeagues:" + ",".join(map(str, leagues)) if leagues else "")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as http:
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=90), end)
            page = 1
            while True:
                body = await cached_get(http, f"/fixtures/between/{cur}/{chunk_end}", {
                    "api_token": token, "include": "odds", "filters": filters, "per_page": 50, "page": page}, chunk_end)
                for f in body.get("data") or []:
                    books: dict[int, dict[int, dict[str, float]]] = {}
                    for o in f.get("odds") or []:
                        spec = MARKETS.get(o.get("market_id"))
                        if not spec or (spec[3] and str(o.get("total")) != spec[3]):
                            continue
                        label = str(o.get("label", "")).strip().lower()
                        try:
                            val = float(o["value"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if label in (spec[1], spec[2]) and val > 1.0:
                            books.setdefault(o["market_id"], {}).setdefault(o["bookmaker_id"], {})[label] = val
                    for mid, by_book in books.items():
                        yes, no = MARKETS[mid][1], MARKETS[mid][2]
                        full = [b for b in by_book.values() if yes in b and no in b]
                        if full:
                            out[mid][f["id"]] = [[b[yes] for b in full], [b[no] for b in full]]
                if not (body.get("pagination") or {}).get("has_more"):
                    break
                page += 1
            cur = chunk_end + timedelta(days=1)
    return out


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
    odds = asyncio.run(fetch_goal_odds(token, start, end, leagues))
    preds = walk_forward(matches)

    for mid, (name, yes, no, _) in MARKETS.items():
        happened = (lambda m: m.home_goals + m.away_goals > 2.5) if mid == 80 else (lambda m: m.home_goals > 0 and m.away_goals > 0)
        model_p = (lambda p: p.over_25) if mid == 80 else (lambda p: p.btts)
        rows = [(m, p, odds[mid][m.fixture_id]) for m, p in preds if m.fixture_id in odds[mid]]
        n = len(rows)
        print(f"\n===== {name.upper()}  ({n} matches with odds) =====")
        if not n:
            continue
        rate = sum(happened(m) for m, _, _ in rows) / n
        print(f"Actually happened ('{yes}'): {rate:.1%} of matches")

        def book_p(o):
            y = sum(1 / v for v in o[0]) / len(o[0]); x = sum(1 / v for v in o[1]) / len(o[1])
            return y / (y + x)

        for label, fn in (("Our model", lambda m, p, o: model_p(p)), ("Bookmakers", lambda m, p, o: book_p(o)),
                          (f"Always {rate:.0%} (league rate)", lambda m, p, o: rate)):
            ll = hits = 0.0
            for m, p, o in rows:
                q = min(max(fn(m, p, o), 1e-6), 1 - 1e-6); y = happened(m)
                ll += -math.log(q if y else 1 - q); hits += (q > 0.5) == y
            print(f"  {label:<28} correct side {hits / n:6.1%}   log loss {ll / n:.4f}")

        print(f"  Betting 1 unit at the AVERAGE bookmaker price:")
        print(f"    {'strategy':<40} {'bets':>5} {'won':>5} {'profit':>8} {'ROI':>7}")

        def sim(label, choose):
            bets = won = 0; profit = 0.0
            for m, p, o in rows:
                side = choose(p, o)
                if side is None:
                    continue
                price = sum(o[side]) / len(o[side]); bets += 1
                win = happened(m) if side == 0 else not happened(m)
                won += win; profit += (price - 1) if win else -1
            if bets:
                print(f"    {label:<40} {bets:>5} {won:>5} {profit:>+8.1f} {profit / bets:>+7.1%}")

        sim(f"Always '{yes}'", lambda p, o: 0)
        sim(f"Always '{no}'", lambda p, o: 1)
        sim("Side the model favours", lambda p, o: 0 if model_p(p) > 0.5 else 1)
        for edge in (0.0, 0.05, 0.10):
            def value(p, o, edge=edge):
                q = model_p(p); ev = [q * sum(o[0]) / len(o[0]) - 1, (1 - q) * sum(o[1]) / len(o[1]) - 1]
                s = 0 if ev[0] >= ev[1] else 1
                return s if ev[s] > edge else None
            sim(f"Value bets (model edge > {edge:.0%})", value)


if __name__ == "__main__":
    main()
