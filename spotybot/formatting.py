"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .analysis import CORNERS_MIN_EDGE, Analysis, LineRow
from .odds import fair_odds
from .sportmonks import fixture_teams


def pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def _dt(raw: str | None, tz: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")[:19]).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
    except ValueError:
        return None


def kickoff_local(fixture: dict, tz: str) -> str:
    dt = _dt(fixture.get("starting_at"), tz)
    return dt.strftime("%H:%M") if dt else "TBD"


def kickoff_long(fixture: dict, tz: str) -> str:
    dt = _dt(fixture.get("starting_at"), tz)
    return dt.strftime("%a %d %b, %H:%M") if dt else "TBD"


def fixture_line(fixture: dict, tz: str) -> str:
    try:
        home, away = fixture_teams(fixture)
        teams = f"{home.get('name')} vs {away.get('name')}"
    except Exception:
        teams = fixture.get("name", "?")
    return f"{kickoff_local(fixture, tz)}  {teams}"


def _price(side) -> str:
    return f"best {side.best:.2f}" if side else "no odds"


def _nearest(rows: list[LineRow], centre: float | None, k: int) -> list[LineRow]:
    if centre is None or len(rows) <= k:
        return rows[:k]
    return sorted(sorted(rows, key=lambda r: abs(r.line - centre))[:k], key=lambda r: r.line)


VALUE_MIN_EDGE = 0.03  # safety margin over the fair price before odds count as value


def take_at(p: float) -> str:
    """Lowest bookmaker odds worth taking for an outcome with probability p (fair odds + 3%)."""
    return f"{(1 + VALUE_MIN_EDGE) / p:.2f}+" if p > 0.01 else "—"


def _side(label: str, p: float, lean: bool) -> str:
    mark = "👉 " if lean else ""
    return f"{mark}{label} <b>{pct(p)}</b> → take at {take_at(p)}"


def _ou_line(name: str, r: LineRow) -> str:
    p = r.p_over
    over = _side(f"Over {r.line:g}", p, p >= 0.5)
    under = _side(f"Under {r.line:g}", 1 - p, p < 0.5)
    if r.source == "market":
        note = f"  <i>(our model: over {pct(r.model_over)})</i>" if r.model_over is not None else ""
    else:
        note = "  <i>(our model only — unproven)</i>"
    return f"  {over}{note}\n  {under}"


def analysis_message(a: Analysis, tz: str) -> str:
    f = a.fixture
    src = "bookmakers, margin removed" if a.result_source == "market" else "our model — no odds yet"
    L = [
        f"<b>{escape(a.home)} vs {escape(a.away)}</b>",
        escape(" · ".join(x for x in (a.league, kickoff_long(f, tz)) if x)),
        "Line-ups: " + ("confirmed ✅" if a.lineups_confirmed else "not out yet (usually ~1 hour before kick-off)"),
        "",
        f"<b>Match result</b> <i>({src})</i>",
    ]
    names = (f"{a.home} win", "Draw", f"{a.away} win")
    top = max(range(3), key=lambda k: a.result_probs[k])
    for i, p in enumerate(a.result_probs):
        L.append("  " + _side(escape(names[i]), p, i == top))
    h, d, w = a.result_probs
    dc = {"1X": h + d, "X2": d + w, "12": h + w}
    best_dc = max(dc, key=dc.get)
    L.append(f"  Double chance {best_dc}: <b>{pct(dc[best_dc])}</b> → take at {take_at(dc[best_dc])}")
    if a.result_source == "market":
        m = a.model
        L.append(f"  <i>Our model: {pct(m.p_home)} / {pct(m.p_draw)} / {pct(m.p_away)}</i>")

    L += ["", f"<b>Goals</b> <i>(our model expects {a.model.xg_home + a.model.xg_away:.1f})</i>"]
    goal_rows = [r for r in a.goals if r.line in (1.5, 2.5, 3.5)] or a.goals
    L += [_ou_line("Goals", r) for r in goal_rows[:3]]
    if a.btts:
        b = a.btts
        L += ["", "<b>Both teams to score</b>",
              "  " + _side("Yes", b.p_over, b.p_over >= 0.5),
              "  " + _side("No", 1 - b.p_over, b.p_over < 0.5)
              + (" <i>(our model only — unproven)</i>" if b.source == "model" else "")]

    if a.corners:
        L += ["", f"<b>Corners</b> <i>(our model expects {a.corners_expected:.1f})</i>"]
        L += [_ou_line("Corners", r) for r in _nearest(a.corners, a.corners_expected, 2)]
    if a.cards:
        ref = "referee factored in" if a.referee_known else "referee not assigned yet"
        L += ["", f"<b>Cards</b> <i>(our model expects {a.cards_expected:.1f}, {ref})</i>"]
        L += [_ou_line("Cards", r) for r in _nearest(a.cards, a.cards_expected, 1)]

    m = a.model
    L += ["", "<b>Form</b> (last 5, newest first)",
          f"  {escape(a.home)}: {m.home_form.form}   {escape(a.away)}: {m.away_form.form}"]
    if m.h2h:
        L.append("  H2H: " + ", ".join(f"{x.home_goals}-{x.away_goals}" + ("" if x.home_name == a.home else "*")
                                       for x in m.h2h[:4]) + "  <i>(* = played at the other ground)</i>")

    L += ["", "<b>How to choose</b>",
          "  👉 = the more likely side. But likely ≠ worth betting: compare with SportyBet's odds.",
          "  ✅ If SportyBet pays <b>at or above</b> the \"take at\" odds, that side is worth it.",
          "  ❌ If neither side reaches its \"take at\" odds, skip the market.",
          f"  Check instantly: /value {f['id']} over2.5 1.35",
          "", "<b>Verdict</b>"]
    if a.result_source == "market":
        L.append("  Our own models found no edge over bookmakers on result/goals/BTTS (896-match test), so "
                 "value only exists when SportyBet pays more than the take-at odds.")
    else:
        L.append("  No bookmaker odds yet — these numbers come from our model only. Treat them with caution.")
    lead = a.corners_lead
    if lead:
        min_price = (1 + CORNERS_MIN_EDGE) / lead.prob
        L.append(f"  🧪 <b>Experimental corners lead:</b> {lead.side} {lead.line:g} corners — our chance "
                 f"{pct(lead.prob)}, fair odds {lead.fair_price:.2f}. Only at {min_price:.2f} or higher "
                 f"(average now {lead.avg_price:.2f}, best {lead.best_price:.2f}).")
        L.append("  <i>Not proven yet (+11% ± 16% in testing). Paper-trade it — record it, don't stake real money.</i>")
    elif a.corners:
        L.append("  Corners: no lead for this match.")
    model_only = any(r.source == "model" for r in a.goals + a.corners + a.cards) or (a.btts and a.btts.source == "model")
    if model_only and a.result_source == "market":
        L.append("  <i>Lines marked \"our model\" have too few bookmaker prices to check against; our model on its "
                 "own is not proven, so don't treat a gap there as value.</i>")
    if a.data_note:
        L.append(f"  ⚠️ {escape(a.data_note)}")
    L += ["", "<i>Take-at odds = fair odds (1 ÷ probability) + 3% safety margin. Probabilities, not guarantees — "
              "bet only what you can afford to lose.</i>"]
    return "\n".join(L)


def likely_word(p: float) -> str:
    if p >= 0.75:
        return "very likely"
    if p >= 0.62:
        return "likely"
    if p >= 0.56:
        return "slightly more likely"
    if p >= 0.50:
        return "close call — almost 50/50"
    return "most likely, but close"


def safer_goals(a: Analysis, lean_over: bool) -> tuple[str, float] | None:
    """A lower-risk goals line on the same side: Over 1.5 when leaning over, Under 3.5 when leaning under."""
    line = 1.5 if lean_over else 3.5
    r = next((r for r in a.goals if r.line == line), None)
    if r is None:
        return None
    return (f"Over {line:g} goals", r.p_over) if lean_over else (f"Under {line:g} goals", 1 - r.p_over)


def simple_picks(a: Analysis) -> list[tuple[str, str, str, float]]:
    """(icon+market, pick, plain words, probability) for the three main markets."""
    names = (f"{a.home} to win", "Draw", f"{a.away} to win")
    top = max(range(3), key=lambda k: a.result_probs[k])
    p = a.result_probs[top]
    picks = [("🏆 Winner", names[top], likely_word(p) if p >= 0.45 else "too close to call", p)]
    g = next((r for r in a.goals if r.line == 2.5), None)
    if g:
        side, q = ("Over 2.5 goals", g.p_over) if g.p_over >= 0.5 else ("Under 2.5 goals", 1 - g.p_over)
        picks.append(("⚽ Goals", side, likely_word(q), q))
        safer = safer_goals(a, g.p_over >= 0.5)
        if safer:
            picks.append(("🛡️ Safer goals pick", safer[0], likely_word(safer[1]), safer[1]))
    if a.btts:
        side, q = ("Yes", a.btts.p_over) if a.btts.p_over >= 0.5 else ("No", 1 - a.btts.p_over)
        picks.append(("🤝 Both teams score", side, likely_word(q), q))
    return picks


def simple_message(a: Analysis, tz: str) -> str:
    """The default, easy-to-read prediction."""
    picks = simple_picks(a)
    L = [f"⚽ <b>{escape(a.home)} vs {escape(a.away)}</b>",
         escape(" · ".join(x for x in (kickoff_long(a.fixture, tz), a.league) if x)), "",
         "🔮 <b>PREDICTION</b>"]
    for market, pick, words, p in picks:
        L.append(f"{market}: <b>{escape(pick)}</b> — {words} ({pct(p)})")
    L += ["", "💰 <b>ONLY BET IF SPORTYBET'S ODDS ARE AT LEAST</b>"]
    for market, pick, _, p in picks:
        name = f"Both teams score – {pick}" if market.endswith("Both teams score") else pick
        L.append(f"  {escape(name)}: <b>{(1 + VALUE_MIN_EDGE) / p:.2f}</b>")
    L.append("<i>Lower odds than this? Skip it — the price isn't worth the risk.</i>")
    if a.corners_lead:
        ld = a.corners_lead
        L += ["", f"🧪 Corners tip: <b>{ld.side} {ld.line:g} corners</b> ({pct(ld.prob)}) — experimental, "
                  "track it but don't stake real money yet."]
    L += ["", "⚠️ <i>These are chances, not certainties. Tap 📊 for all the numbers.</i>"]
    return "\n".join(L)


def tips_message(items: list[Analysis], tz: str, day=None) -> str:
    title = f"Predictions for {day.strftime('%a %d %b')}" if day else "Today's predictions"
    L = [f"🔮 <b>{title}</b>", ""]
    for a in items:
        L.append(f"⚽ <b>{escape(a.home)} vs {escape(a.away)}</b> · {kickoff_local(a.fixture, tz)}")
        for market, pick, words, p in simple_picks(a):
            L.append(f"   {market}: <b>{escape(pick)}</b> ({pct(p)}) — bet only at {(1 + VALUE_MIN_EDGE) / p:.2f}+")
        if a.corners_lead:
            ld = a.corners_lead
            L.append(f"   🧪 Corners tip: {ld.side} {ld.line:g} (experimental)")
        L += [f"   More: /predict {a.fixture['id']}", ""]
    L.append("<i>\"bet only at\" = the lowest SportyBet odds worth taking. Lower odds → skip.</i>")
    return "\n".join(L)
