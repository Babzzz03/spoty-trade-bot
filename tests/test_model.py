"""Unit tests using hand-built test data (not real match results)."""
import asyncio
from datetime import date

import httpx

from spotybot.model import extract_sportmonks_1x2, predict, score_matrix, team_form
from spotybot.sportmonks import Match, SportmonksClient, parse_finished_match


def raw_fixture(fid, hid, aid, hg=None, ag=None, state=5, when="2026-10-01 15:00:00"):
    f = {
        "id": fid, "state_id": state, "starting_at": when,
        "participants": [
            {"id": hid, "name": f"T{hid}", "meta": {"location": "home"}},
            {"id": aid, "name": f"T{aid}", "meta": {"location": "away"}},
        ],
        "scores": [],
    }
    if hg is not None:
        f["scores"] = [
            {"description": "CURRENT", "score": {"goals": hg, "participant": "home"}},
            {"description": "CURRENT", "score": {"goals": ag, "participant": "away"}},
            {"description": "1ST_HALF", "score": {"goals": 0, "participant": "home"}},
        ]
    return f


def m(h, a, hg, ag, d="2026-10-01"):
    return Match(0, d, h, a, f"T{h}", f"T{a}", hg, ag)


def test_parse_finished_match():
    mt = parse_finished_match(raw_fixture(1, 10, 20, 3, 1))
    assert (mt.home_id, mt.away_id, mt.home_goals, mt.away_goals) == (10, 20, 3, 1)
    assert parse_finished_match(raw_fixture(2, 10, 20, state=1)) is None


def test_score_matrix_sums_to_one():
    mat = score_matrix(1.6, 1.1)
    assert abs(sum(map(sum, mat)) - 1) < 1e-9


def test_strong_team_favoured_and_probs_valid():
    strong = [m(1, 9, 3, 0) if i % 2 else m(9, 1, 0, 2) for i in range(10)]
    weak = [m(2, 9, 0, 2) if i % 2 else m(9, 2, 3, 0) for i in range(10)]
    p = predict({"id": 1, "name": "Strong"}, {"id": 2, "name": "Weak"}, strong, weak)
    assert abs(p.p_home + p.p_draw + p.p_away - 1) < 1e-9
    assert p.p_home > 0.6 and p.p_away < 0.15
    assert p.xg_home > p.xg_away
    assert p.pick[0].startswith("1") and p.confidence == "High"


def test_shrinkage_on_tiny_sample():
    f = team_form(1, [m(1, 9, 5, 0)])
    assert 1 < f.attack < 2 and f.defence < 1


def test_sportmonks_blend_and_extract():
    fx = {"predictions": [{"type": {"developer_name": "FULLTIME_RESULT_PROBABILITY"},
                           "predictions": {"home": 50.0, "draw": 30.0, "away": 20.0}}]}
    probs = extract_sportmonks_1x2(fx)
    assert probs == {"home": 0.5, "draw": 0.3, "away": 0.2}
    even = [m(1, 9, 1, 1)] * 6
    p = predict({"id": 1}, {"id": 9}, even, even, sportmonks_probs=probs)
    assert p.blended_with_sportmonks and abs(p.p_home + p.p_draw + p.p_away - 1) < 1e-9


def _stat(type_id, loc, v):
    return {"type_id": type_id, "location": loc, "data": {"value": v}}


def _hist_fixture(fid, hid, aid, hg, ag, hc, ac, when):
    f = raw_fixture(fid, hid, aid, hg, ag, when=when)
    f["league_id"] = 271
    f["statistics"] = [_stat(52, "home", hg), _stat(52, "away", ag), _stat(34, "home", hc), _stat(34, "away", ac),
                       _stat(84, "home", 2), _stat(84, "away", 1)]
    f["referees"] = [{"type_id": 6, "referee_id": 77}]
    return f


def _odds(market, label, value, book, total=None):
    return {"market_id": market, "label": label, "value": str(value), "bookmaker_id": book,
            "total": None if total is None else str(total)}


def test_analyse_fixture_end_to_end_with_mock_api():
    from spotybot.analysis import analyse_fixture
    from spotybot.formatting import analysis_message
    from spotybot.history import HistoryStore

    teams = [10, 20, 30, 40]
    hist, fid = [], 100
    for rnd in range(8):  # 8 rounds of a 4-team league; team 10 strong, lots of corners
        for h in teams:
            for a in teams:
                if h != a and (h + a + rnd) % 2 == 0:
                    fid += 1
                    hg, ag = (3, 0) if h == 10 else (0, 2) if a == 10 else (1, 1)
                    hc, ac = (8, 3) if h == 10 else (3, 7) if a == 10 else (5, 4)
                    hist.append(_hist_fixture(fid, h, a, hg, ag, hc, ac, f"2026-{3 + rnd:02d}-{1 + fid % 27:02d} 15:00:00"))
    target = raw_fixture(999, 10, 20, state=1, when="2026-11-08 15:00:00")
    target.update(league_id=271, league={"id": 271, "name": "Test League"}, lineups=[],
                  referees=[{"type_id": 6, "referee_id": 77}])
    target["odds"] = (
        [_odds(1, "Home", 1.50, b) for b in (1, 2, 3)] + [_odds(1, "Draw", 4.2, b) for b in (1, 2, 3)]
        + [_odds(1, "Away", 6.5, b) for b in (1, 2, 3)]
        + [_odds(80, "Over", 1.80, b, 2.5) for b in (1, 2, 3)] + [_odds(80, "Under", 2.00, b, 2.5) for b in (1, 2, 3)]
        + [_odds(67, "Over", 1.90, b, 9.5) for b in (1, 2, 3)] + [_odds(67, "Under", 1.90, b, 9.5) for b in (1, 2, 3)]
        + [_odds(67, "Over", 1.70, 1, 8.25)]  # quarter line: ignored
    )

    def handler(req: httpx.Request):
        assert req.url.params["api_token"] == "test"
        p = req.url.path
        if p.endswith("/fixtures/999"):
            assert "odds" in req.url.params["include"]
            return httpx.Response(200, json={"data": target})
        if "/between/" in p:
            return httpx.Response(200, json={"data": hist if req.url.params.get("page") == "1" else [],
                                             "pagination": {"has_more": False}})
        return httpx.Response(404, json={"message": "not found"})

    async def run():
        c = SportmonksClient("test")
        c._http = httpx.AsyncClient(base_url="https://api.sportmonks.com/v3/football",
                                    transport=httpx.MockTransport(handler))
        try:
            return await analyse_fixture(c, HistoryStore(c), 999)
        finally:
            await c.close()

    a = asyncio.run(run())
    # headline 1X2 comes from bookmakers with the margin removed
    assert a.result_source == "market"
    assert abs(sum(a.result_probs) - 1) < 1e-9 and a.result_probs[0] > 0.6
    assert a.model.home_form.matches >= 5
    # corners model sees team 10 as a high-corner team
    assert a.corners_expected and a.corners_expected > 8
    assert 8.25 not in a.odds.corners and 9.5 in a.odds.corners
    assert a.referee_known and not a.lineups_confirmed
    text = analysis_message(a, "Africa/Lagos")
    assert "Verdict" in text and "fair" in text and "Corners" in text


def test_health_and_webhook_routes():
    from starlette.testclient import TestClient
    from telegram.ext import Application
    from spotybot.server import build_web_app

    app = Application.builder().token("123:abc").updater(None).build()
    client = TestClient(build_web_app(app, "s3cret"))
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert client.head("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.post("/telegram/s3cret", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert client.post("/telegram/other", json={}).status_code == 404


def test_power_margin_removal():
    from spotybot.odds import Side, TwoWay, power_normalise

    p = power_normalise([1 / 1.25, 1 / 6.0, 1 / 13.0])
    assert abs(sum(p) - 1) < 1e-9
    prop = [x / sum([1 / 1.25, 1 / 6.0, 1 / 13.0]) for x in (1 / 1.25, 1 / 6.0, 1 / 13.0)]
    assert p[0] > prop[0] and p[2] < prop[2]  # long shot carries more of the margin
    tw = TwoWay(Side([1.02, 1.02]), Side([7.9, 7.9]))
    assert tw.overround > 0.09  # too much margin -> analysis falls back to our model
