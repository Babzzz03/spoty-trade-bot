"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .model import Prediction
from .sportmonks import fixture_teams


def pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def kickoff_local(fixture: dict, tz: str) -> str:
    raw = fixture.get("starting_at")
    if not raw:
        return "TBD"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except ValueError:
        return raw


def fixture_line(fixture: dict, tz: str) -> str:
    try:
        home, away = fixture_teams(fixture)
        teams = f"{home.get('name')} vs {away.get('name')}"
    except Exception:
        teams = fixture.get("name", "?")
    return f"{kickoff_local(fixture, tz)}  {teams}"


def prediction_message(pred: Prediction, league: str = "", kickoff: str = "") -> str:
    pick, p = pred.pick
    dc = pred.double_chance
    best_dc = max(dc, key=dc.get)
    lines = [
        f"<b>{escape(pred.home)} vs {escape(pred.away)}</b>",
    ]
    if league or kickoff:
        lines.append(escape(" · ".join(x for x in (league, kickoff) if x)))
    lines += [
        "",
        f"<b>Pick:</b> {pick} — {pct(p)}  (confidence: {pred.confidence})",
        f"<i>Past picks at this level were right {pred.historical_hit_rate} of the time.</i>",
        f"<b>Safer:</b> Double chance {best_dc} — {pct(dc[best_dc])}",
        "",
        "<b>1X2</b>",
        f"  Home {pct(pred.p_home)} · Draw {pct(pred.p_draw)} · Away {pct(pred.p_away)}",
        f"<b>Expected goals:</b> {pred.xg_home:.2f} – {pred.xg_away:.2f}",
        f"<b>Goals:</b> O1.5 {pct(pred.over_15)} · O2.5 {pct(pred.over_25)} · O3.5 {pct(pred.over_35)}",
        f"<b>BTTS:</b> Yes {pct(pred.btts)} · No {pct(1 - pred.btts)}",
        "<i>Goal markets are hard to predict: these stay close to the league average.</i>",
        "<b>Likely scores:</b> " + ", ".join(f"{s} ({pct(q)})" for s, q in pred.top_scores),
        "",
        "<b>Form (last 5, newest first)</b>",
        f"  {escape(pred.home)}: {pred.home_form.form}  "
        f"(GF {pred.home_form.goals_for_avg:.1f} / GA {pred.home_form.goals_against_avg:.1f}, {pred.home_form.matches} games)",
        f"  {escape(pred.away)}: {pred.away_form.form}  "
        f"(GF {pred.away_form.goals_for_avg:.1f} / GA {pred.away_form.goals_against_avg:.1f}, {pred.away_form.matches} games)",
    ]
    if pred.h2h:
        lines += ["", "<b>Head-to-head (recent)</b>"]
        for m in pred.h2h[:5]:
            lines.append(f"  {m.kickoff[:10]}  {escape(m.home_name)} {m.home_goals}-{m.away_goals} {escape(m.away_name)}")
    if pred.blended_with_sportmonks:
        lines += ["", "<i>Blended with Sportmonks prediction probabilities.</i>"]
    for n in pred.notes:
        lines.append(f"<i>⚠️ {escape(n)}</i>")
    lines += ["", "<i>Probabilities, not guarantees. Bet responsibly.</i>"]
    return "\n".join(lines)
