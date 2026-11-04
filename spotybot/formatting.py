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


def _ou_line(name: str, r: LineRow) -> str:
    p = r.p_over
    over = f"Over {r.line:g}: <b>{pct(p)}</b> (fair {fair_odds(p)}, {_price(r.odds.yes if r.odds else None)})"
    under = f"Under: {pct(1 - p)} (fair {fair_odds(1 - p)}, {_price(r.odds.no if r.odds else None)})"
    if r.source == "market":
        model = f" · model {pct(r.model_over)}" if r.model_over is not None else ""
    else:
        model = " <i>(our model; too few bookmaker prices)</i>"
    return f"  {over} | {under}{model}"


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
    names = (a.home, "Draw", a.away)
    for i, p in enumerate(a.result_probs):
        side = a.odds.result[i] if a.odds.result else None
        L.append(f"  {escape(names[i])}: <b>{pct(p)}</b> (fair {fair_odds(p)}, {_price(side)})")
    if a.result_source == "market":
        m = a.model
        L.append(f"  <i>Our model: {pct(m.p_home)} / {pct(m.p_draw)} / {pct(m.p_away)}</i>")

    L += ["", f"<b>Goals</b> <i>(our model expects {a.model.xg_home + a.model.xg_away:.1f})</i>"]
    goal_rows = [r for r in a.goals if r.line in (0.5, 1.5, 2.5, 3.5, 4.5)] or a.goals
    L += [_ou_line("Goals", r) for r in goal_rows[:5]]
    if a.btts:
        b = a.btts
        L.append(f"  Both teams score: <b>{pct(b.p_over)}</b> (fair {fair_odds(b.p_over)}, "
                 f"{_price(b.odds.yes if b.odds else None)})")

    if a.corners:
        L += ["", f"<b>Corners</b> <i>(our model expects {a.corners_expected:.1f})</i>"]
        L += [_ou_line("Corners", r) for r in _nearest(a.corners, a.corners_expected, 3)]
    if a.cards:
        ref = "referee factored in" if a.referee_known else "referee not assigned yet"
        L += ["", f"<b>Cards</b> <i>(our model expects {a.cards_expected:.1f}, {ref})</i>"]
        L += [_ou_line("Cards", r) for r in _nearest(a.cards, a.cards_expected, 2)]

    m = a.model
    L += ["", "<b>Form</b> (last 5, newest first)",
          f"  {escape(a.home)}: {m.home_form.form}   {escape(a.away)}: {m.away_form.form}"]
    if m.h2h:
        L.append("  H2H: " + ", ".join(f"{x.home_goals}-{x.away_goals}" + ("" if x.home_name == a.home else "*")
                                       for x in m.h2h[:4]) + "  <i>(* = played at the other ground)</i>")

    L += ["", "<b>Verdict</b>"]
    if a.result_source == "market":
        L.append("  Result / goals / BTTS: <b>No bet.</b> In testing on 896 matches, bookmaker prices "
                 "were more accurate than our models and no rule beat them.")
    else:
        L.append("  No bookmaker odds yet. Use the fair odds above: only consider a bet if a bookmaker "
                 "offers higher odds than the fair odds.")
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
    L += ["", "<i>Fair odds = 1 ÷ probability. A bet only has value if the bookmaker pays MORE than the fair "
              "odds. Probabilities, not guarantees — bet only what you can afford to lose.</i>"]
    return "\n".join(L)


def tips_message(items: list[Analysis], tz: str, day=None) -> str:
    title = f"Predictions for {day.strftime('%a %d %b')}" if day else "Today's overview"
    L = [f"<b>{title}</b>", ""]
    for a in items:
        label, p = a.favourite
        line = f"<b>{escape(a.home)} vs {escape(a.away)}</b> ({kickoff_local(a.fixture, tz)})\n" \
               f"  Most likely: {escape(label)} {pct(p)} (fair {fair_odds(p)})"
        o25 = next((r for r in a.goals if r.line == 2.5), None)
        if o25:
            line += f" · O2.5 {pct(o25.p_over)}"
        if a.corners_lead:
            ld = a.corners_lead
            line += f"\n  🧪 Corners lead: {ld.side} {ld.line:g} (our chance {pct(ld.prob)}, avg odds {ld.avg_price:.2f})"
        line += f"\n  /predict {a.fixture['id']}"
        L.append(line)
    L += ["", "<i>Most-likely outcomes are not bets: in testing no result/goals rule beat bookmaker prices. "
              "Corners leads are experimental — paper-trade only.</i>"]
    return "\n".join(L)
