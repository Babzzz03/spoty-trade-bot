"""Bet selections, settlement, live-update messages and value-check settling."""
import asyncio
from types import SimpleNamespace

from spotybot.live import Snapshot, diff_messages, snapshot
from spotybot.markets import Selection, parse_selection, settle
from spotybot.storage import Storage


def test_parse_selection():
    assert parse_selection("1") == Selection("Result", "Home")
    assert parse_selection("X2") == Selection("Double chance", "X2")
    assert parse_selection("over2.5") == Selection("Goals", "Over", 2.5)
    assert parse_selection("u3.5") == Selection("Goals", "Under", 3.5)
    assert parse_selection("btts-no") == Selection("BTTS", "No")
    assert parse_selection("corners-over9.5") == Selection("Corners", "Over", 9.5)
    assert parse_selection("ku4.5") == Selection("Cards", "Under", 4.5)
    assert parse_selection("banana") is None


def test_settle_rules():
    assert settle(Selection("Result", "Home"), 2, 1, None, None) == "won"
    assert settle(Selection("Double chance", "X2"), 1, 1, None, None) == "won"
    assert settle(Selection("Double chance", "12"), 0, 0, None, None) == "lost"
    assert settle(Selection("BTTS", "Yes"), 1, 0, None, None) == "lost"
    assert settle(Selection("Goals", "Over", 2.5), 2, 1, None, None) == "won"
    assert settle(Selection("Corners", "Under", 10.0), 0, 0, 10, None) == "push"
    assert settle(Selection("Cards", "Over", 3.5), 0, 0, None, None) is None


def _fx(state, hg, ag, corners=(0, 0), reds=(0, 0), events=()):
    stats = [{"type_id": 52, "location": "home", "data": {"value": hg}},
             {"type_id": 52, "location": "away", "data": {"value": ag}},
             {"type_id": 34, "location": "home", "data": {"value": corners[0]}},
             {"type_id": 34, "location": "away", "data": {"value": corners[1]}},
             {"type_id": 83, "location": "home", "data": {"value": reds[0]}},
             {"type_id": 83, "location": "away", "data": {"value": reds[1]}}]
    return {"state_id": state, "statistics": stats, "events": list(events)}


def test_live_diff_messages():
    pre = snapshot(_fx(1, 0, 0))
    ko = snapshot(_fx(2, 0, 0))
    assert diff_messages(pre, ko, "Celtic", "Kilmarnock") == ["🟢 Kick-off: Celtic vs Kilmarnock"]
    goal = snapshot(_fx(2, 1, 0, corners=(3, 1),
                        events=[{"id": 1, "type_id": 14, "player_name": "Kyogo", "minute": 23}]))
    msgs = diff_messages(ko, goal, "Celtic", "Kilmarnock")
    assert msgs == ["⚽ GOAL! <b>Celtic 1–0 Kilmarnock</b> — Kyogo 23'"]
    red = snapshot(_fx(2, 1, 0, reds=(0, 1), events=[{"id": 1, "type_id": 14}, {"id": 2, "type_id": 20,
                                                                                  "player_name": "Smith"}]))
    assert any("Red card — Smith" in m for m in diff_messages(goal, red, "Celtic", "Kilmarnock"))
    ht = snapshot(_fx(3, 1, 0, corners=(5, 2)))
    assert "corners 7 (5–2)" in diff_messages(red, ht, "Celtic", "Kilmarnock")[0]
    ft = snapshot(_fx(5, 2, 0, corners=(8, 3)))
    msgs = diff_messages(ht, ft, "Celtic", "Kilmarnock")
    assert msgs[0].startswith("⚽ GOAL!") and msgs[-1].startswith("🏁 Full-time") and "corners 11" in msgs[-1]
    # first sight of a match already in play
    assert diff_messages(None, goal, "Celtic", "Kilmarnock")[0].startswith("📺 Live now")
    assert diff_messages(None, pre, "Celtic", "Kilmarnock") == []


def test_value_check_recorded_and_settled(tmp_path):
    import spotybot.alerts as alerts
    from spotybot.config import Settings

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    class Client:
        async def fixture_result(self, fid):
            return _fx(5, 2, 1, corners=(6, 5))

    storage = Storage(None, sqlite_path=str(tmp_path / "v.db"))
    app = SimpleNamespace(bot=Bot(), job_queue=None, bot_data={
        "settings": Settings("x", "y"), "client": Client(), "storage": storage})

    async def run():
        await storage.init()
        kw = dict(chat_id=7, fixture_id=9, kickoff="2020-01-01 15:00:00", home="A", away="B", market="Goals",
                  side="Over", line=2.5, prob=0.6, source="market", odds=1.9)
        await storage.record_value_check(**kw)
        await storage.record_value_check(**{**kw, "odds": 1.95})  # re-check updates, no duplicate
        checks = await storage.value_checks_for(7)
        assert len(checks) == 1 and checks[0].odds == 1.95
        await storage.follow(7, 9, "2020-01-01 15:00:00", "A", "B")
        assert len(await storage.active_follows()) == 1
        assert await alerts.settle(app) == 1
        c = (await storage.value_checks_for(7))[0]
        assert c.status == "won" and abs(c.profit - 0.95) < 1e-9
        assert sent and sent[0][0] == 7 and "WON" in sent[0][1] and "final 2–1" in sent[0][1]
        text = alerts.value_scorecard_text(await storage.value_checks_for(7))
        assert "Settled 1 (won 1)" in text

    asyncio.run(run())
