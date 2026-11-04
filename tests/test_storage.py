"""Scorecard storage (SQLite; the same SQL runs on Postgres) and settlement rules."""
import asyncio

from spotybot.alerts import scorecard_text, settle_value
from spotybot.storage import Storage


def test_settle_value_rules():
    assert settle_value("Over", 9.5, 10) == "won"
    assert settle_value("Over", 9.5, 9) == "lost"
    assert settle_value("Under", 9.5, 9) == "won"
    assert settle_value("Under", 10.0, 10) == "push"


def test_storage_roundtrip(tmp_path):
    st = Storage(None, sqlite_path=str(tmp_path / "t.db"))

    async def run():
        await st.init()
        await st.init()  # idempotent
        await st.subscribe(42)
        await st.subscribe(42)
        assert await st.subscribers() == [42]
        kw = dict(fixture_id=1, kickoff="2026-11-08 15:00:00", home="A", away="B", league="L", market="Corners",
                  side="Over", line=9.5, prob=0.58, min_price=1.81, avg_price=1.95, best_price=2.05)
        assert await st.add_lead(**kw) is True
        assert await st.add_lead(**kw) is False  # duplicate ignored
        assert await st.add_lead(**{**kw, "fixture_id": 2, "side": "Under"}) is True
        lead = await st.lead_for(1)
        assert lead.label == "Over 9.5 corners" and lead.status == "open"
        await st.settle(lead.id, "won", 12)
        await st.settle((await st.lead_for(2)).id, "lost", 11)
        settled = await st.all_settled()
        assert sorted(l.status for l in settled) == ["lost", "won"]
        assert abs(sum(l.profit for l in settled) - (0.95 - 1)) < 1e-6
        text = scorecard_text(settled, await st.open_leads(), await st.recent_leads())
        assert "Settled: <b>2</b>" in text and "Too early" in text
        await st.unsubscribe(42)
        assert await st.subscribers() == []

    asyncio.run(run())


def test_scan_alert_and_settle_flow(tmp_path, monkeypatch):
    """New lead -> saved, announced, pre-check scheduled; after the match -> settled and announced."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import spotybot.alerts as alerts
    from spotybot.analysis import Lead
    from spotybot.config import Settings

    ko = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    sent, jobs = [], []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    class JobQueue:
        def get_jobs_by_name(self, name):
            return [j for j in jobs if j == name]

        def run_once(self, cb, when, data, name):
            jobs.append(name)

    class Client:
        async def fixtures_by_date(self, day, leagues=None):
            return [{"id": 5, "state_id": 1}]

        async def fixture_result(self, fid):
            return {"state_id": 5, "statistics": [{"type_id": 34, "location": "home", "data": {"value": 7}},
                                                  {"type_id": 34, "location": "away", "data": {"value": 5}},
                                                  {"type_id": 52, "location": "home", "data": {"value": 2}},
                                                  {"type_id": 52, "location": "away", "data": {"value": 1}}]}

    lead = Lead("Corners", "Over", 9.5, 0.6, 1.95, 2.05, 0.17)
    N = SimpleNamespace
    fake = N(home="Celtic", away="Kilmarnock", league="Premiership", kickoff_utc=ko,
             fixture={"id": 5, "starting_at": ko}, corners_lead=lead, corners_expected=12.8,
             lineups_confirmed=False, result_probs=[0.6, 0.25, 0.15], result_source="market",
             goals=[N(line=1.5, p_over=0.85, source="market"), N(line=2.5, p_over=0.62, source="market"),
                    N(line=3.5, p_over=0.35, source="market")],
             btts=N(p_over=0.55, source="market"))

    async def fake_analyse(client, store, fid):
        return fake

    monkeypatch.setattr(alerts, "analyse_fixture", fake_analyse)
    storage = Storage(None, sqlite_path=str(tmp_path / "flow.db"))
    app = SimpleNamespace(bot=Bot(), job_queue=JobQueue(), bot_data={
        "settings": Settings("x", "y", timezone="Africa/Lagos"), "client": Client(), "store": None,
        "storage": storage})

    async def run():
        await storage.init()
        await storage.subscribe(99)
        assert await alerts.scan(app) == 1          # today: new lead (tomorrow: same fixture -> no duplicate)
        assert await alerts.scan(app) == 0          # nothing new on a second scan
        assert len(sent) == 1 and "Over 9.5 corners" in sent[0][1] and "1.75 or higher" in sent[0][1]
        assert jobs == [f"precheck:{(await storage.lead_for(5)).id}"]
        # today's main predictions were recorded once: Home win, Over 2.5, Under 3.5, BTTS Yes
        assert await storage.has_predictions(5)
        recorded = await storage.open_predictions()
        assert sorted((p.market, p.side, p.line) for p in recorded) == [
            ("BTTS", "Yes", 0.0), ("Goals", "Over", 2.5), ("Goals", "Under", 3.5), ("Result", "Home", 0.0)]
        # the 08:00 run also sends the morning summary
        await alerts.scan(app, digest=True)
        assert "Good morning" in sent[-1][1] and "Celtic vs Kilmarnock" in sent[-1][1]
        assert len(await storage.open_predictions()) == 4  # not recorded twice
        # pretend the match was played a while ago (2-1, 12 corners), then settle
        for table in ("leads", "predictions"):
            await storage._a(storage._run, f"UPDATE {table} SET kickoff = '2020-01-01 15:00:00'")
        assert await alerts.settle(app) == 5        # 1 lead + 4 predictions
        rec = await storage.lead_for(5)
        assert rec.status == "won" and rec.result_value == 12
        assert any("WON" in t for _, t in sent)
        settled = await storage.settled_predictions()
        assert all(p.status == "won" for p in settled) and len(settled) == 4  # 2-1: home, O2.5, U3.5, BTTS
        card = alerts.predictions_scorecard_text(settled)
        assert "Winner: 1/1 right" in card and "All predictions</b>: 4/4" in card

    asyncio.run(run())


def test_gatekeeper_blocks_strangers():
    from types import SimpleNamespace

    import pytest
    from telegram import Update
    from telegram.ext import ApplicationHandlerStop

    from spotybot.bot import gatekeeper
    from spotybot.config import Settings

    replies = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    def update(chat_id):
        u = Update.__new__(Update)
        object.__setattr__(u, "_effective_chat", SimpleNamespace(id=chat_id))
        object.__setattr__(u, "_effective_message", Msg())
        return u

    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": Settings("x", "y", allowed_chat_ids=[1])}))
    asyncio.run(gatekeeper(update(1), ctx))  # owner: passes
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(gatekeeper(update(2), ctx))
    assert replies and "private bot" in replies[0]


def test_league_history_saved_and_reused(tmp_path):
    """A restart (new HistoryStore) loads history from the database instead of the API."""
    from spotybot.history import HistoryStore

    def fx(fid, h, a, hg, ag, when):
        stat = lambda t, loc, v: {"type_id": t, "location": loc, "data": {"value": v}}
        return {"id": fid, "state_id": 5, "starting_at": when, "league_id": 271,
                "participants": [{"id": h, "name": f"T{h}", "meta": {"location": "home"}},
                                 {"id": a, "name": f"T{a}", "meta": {"location": "away"}}],
                "statistics": [stat(52, "home", hg), stat(52, "away", ag), stat(34, "home", 5), stat(34, "away", 4)],
                "referees": []}

    raw = [fx(i, 10 + i % 4, 20 + i % 3, i % 3, 1, f"2026-0{1 + i % 9}-1{i % 9} 15:00:00") for i in range(60)]
    calls = []

    class Client:
        async def league_results(self, league_id, start, end):
            calls.append(league_id)
            return raw

    storage = Storage(None, sqlite_path=str(tmp_path / "h.db"))

    async def run():
        await storage.init()
        m1 = await HistoryStore(Client(), storage).league(271)
        m2 = await HistoryStore(Client(), storage).league(271)  # simulated restart
        assert calls == [271]
        assert len(m1.matches) == len(m2.matches) == 60 and m2.corners is not None

    asyncio.run(run())



def test_split_message_keeps_parts_under_limit():
    from spotybot.alerts import split_message

    text = "\n\n".join(f"block {i} " + "x" * 300 for i in range(30))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("block") == 30
