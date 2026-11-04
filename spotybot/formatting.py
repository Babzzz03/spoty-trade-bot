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


def tips_message(items: list[Analysis], tz: str, day=None) -> str:
    title = f"Predictions for {day.strftime('%a %d %b')}" if day else "Today's overview"
    L = [f"<b>{title}</b>", "<i>👉 more likely side · take at = lowest SportyBet odds worth taking</i>", ""]
    for a in items:
        h, d, w = a.result_probs
        names = (a.home, "Draw", a.away)
        top = max(range(3), key=lambda k: a.result_probs[k])
        block = [f"<b>{escape(a.home)} vs {escape(a.away)}</b> ({kickoff_local(a.fixture, tz)})",
                 f"  Result: {escape(a.home)} {pct(h)} · Draw {pct(d)} · {escape(a.away)} {pct(w)}"
                 f"  👉 {escape(names[top])} (take at {take_at(a.result_probs[top])})"]
        g = next((r for r in a.goals if r.line == 2.5), None)
        if g:
            lean = ("Over", g.p_over) if g.p_over >= 0.5 else ("Under", 1 - g.p_over)
            block.append(f"  Goals 2.5: Over {pct(g.p_over)} · Under {pct(1 - g.p_over)}"
                         f"  👉 {lean[0]} (take at {take_at(lean[1])})")
        if a.btts:
            b = a.btts.p_over
            lean = ("Yes", b) if b >= 0.5 else ("No", 1 - b)
            block.append(f"  BTTS: Yes {pct(b)} · No {pct(1 - b)}  👉 {lean[0]} (take at {take_at(lean[1])})")
        if a.corners_lead:
            ld = a.corners_lead
            block.append(f"  🧪 Corners lead: {ld.side} {ld.line:g} (our chance {pct(ld.prob)}, "
                         f"take at {(1 + CORNERS_MIN_EDGE) / ld.prob:.2f}+)")
        block.append(f"  Full analysis: /predict {a.fixture['id']}")
        L += block + [""]
    L += ["<i>Likely ≠ worth betting. Only bet a side if SportyBet's odds are at or above its take-at odds "
          "(check with /value). If not, skip.</i>"]
    return "\n".join(L)
