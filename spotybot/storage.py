"""Persistent storage for alert subscribers and the paper-trading scorecard.

Uses Postgres when DATABASE_URL is set (e.g. a free Supabase database), otherwise a local
SQLite file (fine for local runs; NOT persistent on Render's free plan, which wipes the disk
on every restart/deploy).
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class LeadRecord:
    id: int
    fixture_id: int
    created_at: str
    kickoff: str
    home: str
    away: str
    league: str
    market: str
    side: str
    line: float
    prob: float
    min_price: float
    avg_price: float
    best_price: float
    status: str             # open / won / lost / push / void
    result_value: int | None
    settled_at: str | None
    precheck_sent: int

    @property
    def label(self) -> str:
        return f"{self.side} {self.line:g} {self.market.lower()}"

    @property
    def profit(self) -> float:
        """Profit in units at the recorded average price (1 unit staked)."""
        return {"won": self.avg_price - 1, "lost": -1.0}.get(self.status, 0.0)


_COLUMNS = ("id, fixture_id, created_at, kickoff, home, away, league, market, side, line, prob, min_price, "
            "avg_price, best_price, status, result_value, settled_at, precheck_sent")

_SCHEMA_COMMON = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id BIGINT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id {pk},
    fixture_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    league TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    prob REAL NOT NULL,
    min_price REAL NOT NULL,
    avg_price REAL NOT NULL,
    best_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    result_value INTEGER,
    settled_at TEXT,
    precheck_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (fixture_id, market)
);
CREATE TABLE IF NOT EXISTS value_checks (
    id {pk},
    chat_id BIGINT NOT NULL,
    fixture_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    prob REAL NOT NULL,
    source TEXT NOT NULL,
    odds REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at TEXT,
    UNIQUE (chat_id, fixture_id, market, side, line)
);
CREATE TABLE IF NOT EXISTS predictions (
    id {pk},
    fixture_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    league TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    prob REAL NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at TEXT,
    UNIQUE (fixture_id, market, side, line)
);
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS follows (
    chat_id BIGINT NOT NULL,
    fixture_id BIGINT NOT NULL,
    kickoff TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, fixture_id)
);
"""


@dataclass
class ValueCheck:
    id: int
    chat_id: int
    fixture_id: int
    created_at: str
    kickoff: str
    home: str
    away: str
    market: str
    side: str
    line: float
    prob: float
    source: str
    odds: float
    status: str
    settled_at: str | None

    @property
    def profit(self) -> float:
        return {"won": self.odds - 1, "lost": -1.0}.get(self.status, 0.0)


@dataclass
class Follow:
    chat_id: int
    fixture_id: int
    kickoff: str
    home: str
    away: str
    created_at: str
    done: int


_VC_COLUMNS = ("id, chat_id, fixture_id, created_at, kickoff, home, away, market, side, line, prob, source, odds, "
               "status, settled_at")
_FOLLOW_COLUMNS = "chat_id, fixture_id, kickoff, home, away, created_at, done"


@dataclass
class PredictionRecord:
    id: int
    fixture_id: int
    created_at: str
    kickoff: str
    home: str
    away: str
    league: str
    market: str
    side: str
    line: float
    prob: float
    source: str
    status: str
    settled_at: str | None


_PRED_COLUMNS = ("id, fixture_id, created_at, kickoff, home, away, league, market, side, line, prob, source, "
                 "status, settled_at")


class Storage:
    def __init__(self, database_url: str | None, sqlite_path: str = "data/spotybot.db"):
        self.url = (database_url or "").strip()
        self.is_postgres = self.url.startswith(("postgres://", "postgresql://"))
        self.sqlite_path = sqlite_path

    # ---------------------------------------------------------------- low level (sync, run in a thread)

    def _connect(self):
        if self.is_postgres:
            import psycopg
            return psycopg.connect(self.url, autocommit=True)
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        conn.isolation_level = None
        return conn

    def _sql(self, q: str) -> str:
        return q.replace("?", "%s") if self.is_postgres else q

    def _run(self, q: str, params: tuple = (), fetch: bool = False):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(self._sql(q), params)
            return cur.fetchall() if fetch else cur.rowcount
        finally:
            conn.close()

    def _init(self) -> None:
        pk = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        conn = self._connect()
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA_COMMON.format(pk=pk).split(";"):
                if stmt.strip():
                    cur.execute(stmt)
        finally:
            conn.close()

    async def _a(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ---------------------------------------------------------------- API

    @property
    def description(self) -> str:
        return "Postgres database" if self.is_postgres else f"local file {self.sqlite_path}"

    async def init(self) -> None:
        await self._a(self._init)

    async def subscribe(self, chat_id: int) -> None:
        await self._a(self._run, "INSERT INTO subscribers (chat_id, created_at) VALUES (?, ?) "
                                 "ON CONFLICT (chat_id) DO NOTHING", (chat_id, _now()))

    async def unsubscribe(self, chat_id: int) -> None:
        await self._a(self._run, "DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    async def subscribers(self) -> list[int]:
        rows = await self._a(self._run, "SELECT chat_id FROM subscribers", (), True)
        return [int(r[0]) for r in rows]

    async def add_lead(self, *, fixture_id: int, kickoff: str, home: str, away: str, league: str, market: str,
                       side: str, line: float, prob: float, min_price: float, avg_price: float,
                       best_price: float) -> bool:
        """Insert a lead; returns False if this fixture already has a lead for this market."""
        n = await self._a(self._run,
                          "INSERT INTO leads (fixture_id, created_at, kickoff, home, away, league, market, side, line, "
                          "prob, min_price, avg_price, best_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                          "ON CONFLICT (fixture_id, market) DO NOTHING",
                          (fixture_id, _now(), kickoff, home, away, league, market, side, float(line), float(prob),
                           float(min_price), float(avg_price), float(best_price)))
        return bool(n and n > 0)

    async def _leads(self, where: str, params: tuple = (), order: str = "kickoff") -> list[LeadRecord]:
        rows = await self._a(self._run, f"SELECT {_COLUMNS} FROM leads WHERE {where} ORDER BY {order}", params, True)
        return [LeadRecord(*r) for r in rows]

    async def open_leads(self) -> list[LeadRecord]:
        return await self._leads("status = 'open'")

    async def lead_for(self, fixture_id: int, market: str = "Corners") -> LeadRecord | None:
        got = await self._leads("fixture_id = ? AND market = ?", (fixture_id, market))
        return got[0] if got else None

    async def recent_leads(self, limit: int = 10) -> list[LeadRecord]:
        rows = await self._leads("1 = 1", (), order="kickoff DESC")
        return rows[:limit]

    async def all_settled(self) -> list[LeadRecord]:
        return await self._leads("status IN ('won', 'lost', 'push')")

    async def settle(self, lead_id: int, status: str, value: int | None) -> None:
        await self._a(self._run, "UPDATE leads SET status = ?, result_value = ?, settled_at = ? WHERE id = ?",
                      (status, value, _now(), lead_id))

    async def mark_precheck(self, lead_id: int) -> None:
        await self._a(self._run, "UPDATE leads SET precheck_sent = 1 WHERE id = ?", (lead_id,))

    # ---------------------------------------------------------------- value checks (odds the user saw)

    async def record_value_check(self, *, chat_id: int, fixture_id: int, kickoff: str, home: str, away: str,
                                 market: str, side: str, line: float, prob: float, source: str, odds: float) -> None:
        await self._a(self._run,
                      "INSERT INTO value_checks (chat_id, fixture_id, created_at, kickoff, home, away, market, side, "
                      "line, prob, source, odds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT (chat_id, fixture_id, market, side, line) DO UPDATE SET "
                      "odds = excluded.odds, prob = excluded.prob, source = excluded.source",
                      (chat_id, fixture_id, _now(), kickoff, home, away, market, side, float(line), float(prob),
                       source, float(odds)))

    async def _checks(self, where: str, params: tuple = ()) -> list[ValueCheck]:
        rows = await self._a(self._run, f"SELECT {_VC_COLUMNS} FROM value_checks WHERE {where} ORDER BY kickoff",
                             params, True)
        return [ValueCheck(*r) for r in rows]

    async def open_value_checks(self) -> list[ValueCheck]:
        return await self._checks("status = 'open'")

    async def value_checks_for(self, chat_id: int) -> list[ValueCheck]:
        return await self._checks("chat_id = ?", (chat_id,))

    async def settle_value_check(self, check_id: int, status: str) -> None:
        await self._a(self._run, "UPDATE value_checks SET status = ?, settled_at = ? WHERE id = ?",
                      (status, _now(), check_id))

    # ---------------------------------------------------------------- live follows

    async def follow(self, chat_id: int, fixture_id: int, kickoff: str, home: str, away: str) -> None:
        await self._a(self._run,
                      "INSERT INTO follows (chat_id, fixture_id, kickoff, home, away, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (chat_id, fixture_id) DO UPDATE SET done = 0",
                      (chat_id, fixture_id, kickoff, home, away, _now()))

    async def unfollow(self, chat_id: int, fixture_id: int) -> None:
        await self._a(self._run, "DELETE FROM follows WHERE chat_id = ? AND fixture_id = ?", (chat_id, fixture_id))

    async def active_follows(self) -> list[Follow]:
        rows = await self._a(self._run, f"SELECT {_FOLLOW_COLUMNS} FROM follows WHERE done = 0 ORDER BY kickoff",
                             (), True)
        return [Follow(*r) for r in rows]

    async def finish_follows(self, fixture_id: int) -> None:
        await self._a(self._run, "UPDATE follows SET done = 1 WHERE fixture_id = ?", (fixture_id,))

    # ---------------------------------------------------------------- persistent API cache

    async def cache_get(self, key: str) -> tuple[str, str] | None:
        """(value, fetched_at UTC 'YYYY-MM-DD HH:MM:SS') or None."""
        rows = await self._a(self._run, "SELECT value, fetched_at FROM api_cache WHERE cache_key = ?", (key,), True)
        return (rows[0][0], rows[0][1]) if rows else None

    async def cache_set(self, key: str, value: str) -> None:
        await self._a(self._run,
                      "INSERT INTO api_cache (cache_key, value, fetched_at) VALUES (?, ?, ?) "
                      "ON CONFLICT (cache_key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
                      (key, value, _now()))

    # ---------------------------------------------------------------- prediction accuracy tracker

    async def has_predictions(self, fixture_id: int) -> bool:
        rows = await self._a(self._run, "SELECT 1 FROM predictions WHERE fixture_id = ?", (fixture_id,), True)
        return bool(rows)

    async def record_prediction(self, *, fixture_id: int, kickoff: str, home: str, away: str, league: str,
                                market: str, side: str, line: float, prob: float, source: str) -> None:
        await self._a(self._run,
                      "INSERT INTO predictions (fixture_id, created_at, kickoff, home, away, league, market, side, "
                      "line, prob, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT (fixture_id, market, side, line) DO NOTHING",
                      (fixture_id, _now(), kickoff, home, away, league, market, side, float(line), float(prob), source))

    async def _preds(self, where: str, params: tuple = ()) -> list[PredictionRecord]:
        rows = await self._a(self._run, f"SELECT {_PRED_COLUMNS} FROM predictions WHERE {where} ORDER BY kickoff",
                             params, True)
        return [PredictionRecord(*r) for r in rows]

    async def open_predictions(self) -> list[PredictionRecord]:
        return await self._preds("status = 'open'")

    async def settled_predictions(self) -> list[PredictionRecord]:
        return await self._preds("status IN ('won', 'lost', 'push')")

    async def settle_prediction(self, pred_id: int, status: str) -> None:
        await self._a(self._run, "UPDATE predictions SET status = ?, settled_at = ? WHERE id = ?",
                      (status, _now(), pred_id))
