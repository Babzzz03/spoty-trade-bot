"""Unit tests using hand-built test data (not real match results)."""
import asyncio
from datetime import date

import httpx

from spotybot.analysis import analyse_fixture
from spotybot.formatting import prediction_message
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
    assert "<b>Strong vs Weak</b>" in prediction_message(p)


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


def test_analyse_fixture_end_to_end_with_mock_api():
    hist_home = [raw_fixture(100 + i, 10, 30 + i, 2, 0, when=f"2026-09-{i+1:02d} 15:00:00") for i in range(8)]
    hist_away = [raw_fixture(200 + i, 40 + i, 20, 2, 1, when=f"2026-09-{i+1:02d} 15:00:00") for i in range(8)]
    target = raw_fixture(999, 10, 20, state=1, when="2026-11-08 15:00:00")

    def handler(req: httpx.Request):
        assert req.url.params["api_token"] == "test"
        p = req.url.path
        if p.endswith("/fixtures/999"):
            return httpx.Response(200, json={"data": target})
        if "/between/" in p and p.endswith("/10"):
            return httpx.Response(200, json={"data": hist_home, "pagination": {"has_more": False}})
        if "/between/" in p and p.endswith("/20"):
            return httpx.Response(200, json={"data": hist_away, "pagination": {"has_more": False}})
        if "head-to-head" in p:
            return httpx.Response(200, json={"data": [raw_fixture(5, 10, 20, 1, 0, when="2026-03-01 15:00:00")]})
        return httpx.Response(404, json={"message": "not found"})

    async def run():
        c = SportmonksClient("test")
        c._http = httpx.AsyncClient(base_url="https://api.sportmonks.com/v3/football",
                                    transport=httpx.MockTransport(handler))
        try:
            return await analyse_fixture(c, 999)
        finally:
            await c.close()

    fx, pred = asyncio.run(run())
    assert pred.home_form.matches == 8 and pred.away_form.matches == 8
    assert len(pred.h2h) == 1
    assert pred.p_home > pred.p_away


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
