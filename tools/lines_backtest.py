"""Goals lines 1.5 / 2.5 / 3.5, over and under: win rate vs profit, and 'pick the best line per match'.

Usage:
    python -m tools.lines_backtest --leagues 271,501
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

from spotybot.sportmonks import BASE_URL
from tools.backtest import fetch_results, walk_forward
from tools.cache import cached_get

LINES = ("1.5", "2.5", "3.5")


async def fetch_total_odds(token, start, end, leagues):
    """fixture_id -> line -> {'over': [odds...], 'under': [odds...]} (bookmakers quoting both sides)."""
    out: dict[int, dict[str, dict[str, list[float]]]] = {}
    filters = "markets:80,14" + (";fixtureLeagues:" + ",".join(map(str, leagues)) if leagues else "")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as http:
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=90), end)
            page = 1
            while True:
                body = await cached_get(http, f"/fixtures/between/{cur}/{chunk_end}", {
                    "api_token": token, "include": "odds", "filters": filters, "per_page": 50, "page": page}, chunk_end)
                for f in body.get("data") or []:
                    books: dict[tuple[str, int], dict[str, float]] = {}
                    for o in f.get("odds") or []:
                        line, label = str(o.get("total")), str(o.get("label", "")).lower()
                        if o.get("market_id") != 80 or line not in LINES or label not in ("over", "under"):
                            continue
                        try:
                            v = float(o["value"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if v > 1.0:
                            books.setdefault((line, o["bookmaker_id"]), {})[label] = v
                    for (line, _), b in books.items():
                        if len(b) == 2:
                            d = out.setdefault(f["id"], {}).setdefault(line, {"over": [], "under": []})
                            d["over"].append(b["over"]); d["under"].append(b["under"])
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
    odds = asyncio.run(fetch_total_odds(token, start, end, leagues))
    rows = [(m, p) for m, p in walk_forward(matches) if m.fixture_id in odds]

    avg = lambda xs: sum(xs) / len(xs)
    model_over = {"1.5": lambda p: p.over_15, "2.5": lambda p: p.over_25, "3.5": lambda p: p.over_35}

    print(f"{len(rows)} matches\n")
    print("EVERY LINE, EVERY MATCH (1 unit, average bookmaker price)")
    print(f"  {'bet':<12} {'bets':>5} {'win rate':>9} {'avg odds':>9} {'profit':>8} {'ROI':>7}")
    for line in LINES:
        for side in ("over", "under"):
            bets = won = 0; profit = 0.0; prices = []
            for m, _ in rows:
                o = odds[m.fixture_id].get(line)
                if not o:
                    continue
                price = avg(o[side]); goals = m.home_goals + m.away_goals
                win = goals > float(line) if side == "over" else goals < float(line)
                bets += 1; won += win; profit += (price - 1) if win else -1; prices.append(price)
            print(f"  {side + ' ' + line:<12} {bets:>5} {won / bets:>9.1%} {avg(prices):>9.2f} {profit:>+8.1f} {profit / bets:>+7.1%}")

    print("\nMODEL PICKS THE BEST LINE/SIDE PER MATCH (largest model edge vs price)")
    print(f"  {'min edge':<10} {'bets':>5} {'win rate':>9} {'avg odds':>9} {'profit':>8} {'ROI':>7}")
    for edge in (0.0, 0.05, 0.10, 0.15):
        bets = won = 0; profit = 0.0; prices = []
        for m, p in rows:
            best = None
            for line, o in odds[m.fixture_id].items():
                q = model_over[line](p)
                for side, prob in (("over", q), ("under", 1 - q)):
                    price = avg(o[side]); ev = prob * price - 1
                    if best is None or ev > best[0]:
                        best = (ev, line, side, price)
            if not best or best[0] <= edge:
                continue
            ev, line, side, price = best; goals = m.home_goals + m.away_goals
            win = goals > float(line) if side == "over" else goals < float(line)
            bets += 1; won += win; profit += (price - 1) if win else -1; prices.append(price)
        if bets:
            print(f"  {'> ' + format(edge, '.0%'):<10} {bets:>5} {won / bets:>9.1%} {avg(prices):>9.2f} {profit:>+8.1f} {profit / bets:>+7.1%}")

    print("\nHIGH-CONFIDENCE ONLY: model says over 1.5 is very likely")
    for thr in (0.80, 0.83, 0.86):
        bets = won = 0; profit = 0.0
        for m, p in rows:
            o = odds[m.fixture_id].get("1.5")
            if not o or p.over_15 < thr:
                continue
            price = avg(o["over"]); win = m.home_goals + m.away_goals > 1.5
            bets += 1; won += win; profit += (price - 1) if win else -1
        if bets:
            print(f"  model over-1.5 >= {thr:.0%}: {bets:>4} bets, win rate {won / bets:.1%}, profit {profit:+.1f}, ROI {profit / bets:+.1%}")


if __name__ == "__main__":
    main()
