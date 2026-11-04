"""Walk-forward backtest of the prediction model on real Sportmonks results.

For every finished league match, the model only sees matches played *before* it
(same inputs the live bot uses: each team's last 12 matches within 240 days, plus H2H).
It then compares predicted probabilities with what actually happened.

Usage:
    python -m tools.backtest                     # leagues on your plan, since 2024-07-01
    python -m tools.backtest --start 2025-07-01 --leagues 271,501
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv

from spotybot.model import predict
from spotybot.sportmonks import BASE_URL, Match, parse_finished_match

WARMUP_MATCHES = 5  # each team needs this many prior matches before we score a prediction


async def fetch_results(token: str, start: date, end: date, leagues: list[int]) -> list[Match]:
    out: dict[int, Match] = {}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=90), end)
            page = 1
            while True:
                params = {"api_token": token, "include": "participants;scores", "per_page": 50, "page": page}
                if leagues:
                    params["filters"] = "fixtureLeagues:" + ",".join(map(str, leagues))
                r = await http.get(f"/fixtures/between/{cur}/{chunk_end}", params=params)
                r.raise_for_status()
                body = r.json()
                for f in body.get("data") or []:
                    m = parse_finished_match(f)
                    if m:
                        out[m.fixture_id] = m
                if not (body.get("pagination") or {}).get("has_more"):
                    break
                page += 1
            cur = chunk_end + timedelta(days=1)
    return sorted(out.values(), key=lambda m: m.kickoff)


def run(matches: list[Match]) -> None:
    history: dict[int, list[Match]] = defaultdict(list)  # newest last
    rows = []
    for m in matches:
        day = datetime.fromisoformat(m.kickoff.replace(" ", "T")).date()
        cutoff = (day - timedelta(days=240)).isoformat()
        hm = [x for x in reversed(history[m.home_id]) if x.kickoff >= cutoff][:12]
        am = [x for x in reversed(history[m.away_id]) if x.kickoff >= cutoff][:12]
        if len(hm) >= WARMUP_MATCHES and len(am) >= WARMUP_MATCHES:
            h2h = [x for x in reversed(history[m.home_id]) if {x.home_id, x.away_id} == {m.home_id, m.away_id}][:8]
            p = predict({"id": m.home_id, "name": m.home_name}, {"id": m.away_id, "name": m.away_name}, hm, am, h2h=h2h)
            rows.append((m, p))
        history[m.home_id].append(m)
        history[m.away_id].append(m)

    n = len(rows)
    if not n:
        print("Not enough matches to evaluate.")
        return

    def outcome(m: Match) -> int:
        return 0 if m.home_goals > m.away_goals else 1 if m.home_goals == m.away_goals else 2

    # Baseline: always predict the historical home/draw/away frequencies.
    freq = [0, 0, 0]
    for m, _ in rows:
        freq[outcome(m)] += 1
    base = [f / n for f in freq]

    hits = ll = ll_base = brier = brier_base = 0.0
    for m, p in rows:
        probs = [p.p_home, p.p_draw, p.p_away]
        o = outcome(m)
        hits += probs.index(max(probs)) == o
        ll += -math.log(max(probs[o], 1e-9))
        ll_base += -math.log(base[o])
        brier += sum((probs[i] - (i == o)) ** 2 for i in range(3))
        brier_base += sum((base[i] - (i == o)) ** 2 for i in range(3))

    print(f"Matches evaluated: {n}  ({rows[0][0].kickoff[:10]} → {rows[-1][0].kickoff[:10]})")
    print(f"Actual results: home {base[0]:.0%}, draw {base[1]:.0%}, away {base[2]:.0%}\n")
    print("1X2 RESULT")
    print(f"  Pick correct:      {hits / n:.1%}   (always picking 'home' would be {base[0]:.1%})")
    print(f"  Log loss:          {ll / n:.3f}   (baseline {ll_base / n:.3f}; lower is better)")
    print(f"  Brier score:       {brier / n:.3f}   (baseline {brier_base / n:.3f}; lower is better)")

    def calib(title: str, pairs: list[tuple[float, bool]], edges: list[float]) -> None:
        print(f"\n{title}")
        print("  predicted     matches   predicted avg   actually happened")
        for lo, hi in zip(edges, edges[1:]):
            sel = [(q, y) for q, y in pairs if lo <= q < hi]
            if len(sel) < 5:
                continue
            avg = sum(q for q, _ in sel) / len(sel)
            act = sum(y for _, y in sel) / len(sel)
            print(f"  {lo:>4.0%}–{hi:<4.0%}   {len(sel):>7}   {avg:>13.0%}   {act:>17.0%}")

    calib("CONFIDENCE OF THE MAIN PICK (calibration)",
          [(max(p.p_home, p.p_draw, p.p_away),
            [p.p_home, p.p_draw, p.p_away].index(max(p.p_home, p.p_draw, p.p_away)) == outcome(m)) for m, p in rows],
          [0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 1.01])
    calib("OVER 2.5 GOALS (calibration)",
          [(p.over_25, m.home_goals + m.away_goals > 2.5) for m, p in rows],
          [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01])
    calib("OVER 1.5 GOALS (calibration)",
          [(p.over_15, m.home_goals + m.away_goals > 1.5) for m, p in rows],
          [0.0, 0.6, 0.7, 0.8, 0.9, 1.01])
    calib("BOTH TEAMS TO SCORE (calibration)",
          [(p.btts, m.home_goals > 0 and m.away_goals > 0) for m, p in rows],
          [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01])
    calib("DOUBLE CHANCE (best option) (calibration)",
          [(max(p.double_chance.values()),
            {"1X": outcome(m) in (0, 1), "12": outcome(m) in (0, 2), "X2": outcome(m) in (1, 2)}[
                max(p.double_chance, key=p.double_chance.get)]) for m, p in rows],
          [0.5, 0.65, 0.75, 0.85, 0.9, 1.01])

    avg_goals = sum(m.home_goals + m.away_goals for m, _ in rows) / n
    avg_xg = sum(p.xg_home + p.xg_away for _, p in rows) / n
    print(f"\nGoals per match: actual {avg_goals:.2f}, model expected {avg_xg:.2f}")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--leagues", default="", help="comma-separated league IDs (default: all on your plan)")
    a = ap.parse_args()
    leagues = [int(x) for x in a.leagues.split(",") if x.strip()]
    matches = asyncio.run(fetch_results(os.environ["SPORTMONKS_API_TOKEN"], date.fromisoformat(a.start),
                                        date.fromisoformat(a.end), leagues))
    print(f"Fetched {len(matches)} finished matches\n")
    run(matches)


if __name__ == "__main__":
    main()
