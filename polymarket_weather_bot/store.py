from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Market, Position, Signal, Trade


class Store:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS markets (
                    id TEXT PRIMARY KEY,
                    question TEXT,
                    slug TEXT,
                    condition_id TEXT,
                    yes_price REAL,
                    no_price REAL,
                    volume REAL,
                    liquidity REAL,
                    active INTEGER,
                    closed INTEGER,
                    end_date TEXT,
                    category TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT PRIMARY KEY,
                    payload TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS market_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS forecast_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS account_order_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS account_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    event_type TEXT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS controls (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                );
                """
            )

    def upsert_markets(self, markets: Iterable[Market]):
        with self._connect() as conn:
            for m in markets:
                conn.execute(
                    """
                    INSERT INTO markets (id, question, slug, condition_id, yes_price, no_price, volume, liquidity, active, closed, end_date, category, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        question=excluded.question,
                        slug=excluded.slug,
                        condition_id=excluded.condition_id,
                        yes_price=excluded.yes_price,
                        no_price=excluded.no_price,
                        volume=excluded.volume,
                        liquidity=excluded.liquidity,
                        active=excluded.active,
                        closed=excluded.closed,
                        end_date=excluded.end_date,
                        category=excluded.category,
                        updated_at=excluded.updated_at
                    """,
                    (
                        m.id, m.question, m.slug, m.condition_id, m.yes_price, m.no_price, m.volume,
                        m.liquidity, int(m.active), int(m.closed), m.end_date, m.category,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

    def save_signal(self, signal: Signal):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO signals (market_id, payload, created_at) VALUES (?, ?, ?)",
                (signal.market_id, json.dumps(asdict(signal), ensure_ascii=False), signal.generated_at),
            )

    def save_position(self, position: Position):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (market_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (position.market_id, json.dumps(asdict(position), ensure_ascii=False), position.updated_at),
            )

    def delete_position(self, market_id: str):
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE market_id = ?", (market_id,))

    def save_trade(self, trade: Trade):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trades (market_id, payload, created_at) VALUES (?, ?, ?)",
                (trade.market_id, json.dumps(asdict(trade), ensure_ascii=False), trade.created_at),
            )

    def save_market_scan(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO market_scans (market_id, payload, created_at) VALUES (?, ?, ?)",
                (
                    str(payload.get('market_id') or payload.get('market', {}).get('id') or ''),
                    json.dumps(payload, ensure_ascii=False),
                    payload.get('created_at') or datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_forecast_snapshot(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO forecast_snapshots (market_id, payload, created_at) VALUES (?, ?, ?)",
                (
                    str(payload.get('market_id') or ''),
                    json.dumps(payload, ensure_ascii=False),
                    payload.get('created_at') or datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_signal_outcome(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO signal_outcomes (market_id, payload, created_at) VALUES (?, ?, ?)",
                (
                    str(payload.get('market_id') or ''),
                    json.dumps(payload, ensure_ascii=False),
                    payload.get('created_at') or datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_account_order_snapshot(self, payload: Dict[str, Any], source: str = 'live'):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO account_order_snapshots (source, payload, created_at) VALUES (?, ?, ?)",
                (
                    source,
                    json.dumps(payload, ensure_ascii=False),
                    payload.get('created_at') or datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_account_order_events(self, events: Iterable[Dict[str, Any]]):
        with self._connect() as conn:
            for event in events:
                conn.execute(
                    "INSERT INTO account_order_events (order_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        str(event.get('order_id') or ''),
                        str(event.get('event_type') or 'updated'),
                        json.dumps(event, ensure_ascii=False),
                        event.get('created_at') or datetime.now(timezone.utc).isoformat(),
                    ),
                )

    def save_snapshot(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO snapshots (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def save_error(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO errors (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def set_control(self, key: str, value: Any):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO controls (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def get_control(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM controls WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row[0])
            except Exception:
                return row[0]

    def get_controls(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM controls ORDER BY key ASC").fetchall()
            data: Dict[str, Any] = {}
            for row in rows:
                try:
                    data[row[0]] = json.loads(row[1])
                except Exception:
                    data[row[0]] = row[1]
            return data

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM positions ORDER BY updated_at DESC").fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_market_scans(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM market_scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_forecast_snapshots(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM forecast_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_signal_outcomes(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM signal_outcomes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_account_order_snapshots(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM account_order_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_account_order_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM account_order_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_markets(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM markets ORDER BY volume DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_last_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None

    def get_last_error(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM errors ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None

    def get_errors(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, payload, created_at FROM errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                payload = json.loads(row[1])
                if isinstance(payload, dict):
                    payload = {**payload}
                else:
                    payload = {"value": payload}
                payload.setdefault("created_at", row[2])
                payload.setdefault("id", row[0])
                items.append(payload)
            return items

    def get_snapshots(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, payload, created_at FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row[1])
                except Exception:
                    payload = {"raw": row[1]}
                if isinstance(payload, dict):
                    record = {**payload}
                else:
                    record = {"value": payload}
                record.setdefault("created_at", row[2])
                record.setdefault("id", row[0])
                items.append(record)
            return items
