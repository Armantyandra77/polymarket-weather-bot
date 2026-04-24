from __future__ import annotations

import json
import math
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
                CREATE TABLE IF NOT EXISTS forecast_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    forecast_type TEXT,
                    predicted_value REAL,
                    actual_value REAL,
                    predicted_probability REAL,
                    actual_outcome INTEGER,
                    absolute_error REAL,
                    squared_error REAL,
                    brier_score REAL,
                    payload TEXT,
                    created_at TEXT,
                    resolved_at TEXT
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
                CREATE TABLE IF NOT EXISTS telegram_command_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    user_id TEXT,
                    username TEXT,
                    command TEXT,
                    args TEXT,
                    message_text TEXT,
                    reply_text TEXT,
                    status TEXT,
                    raw_update TEXT,
                    created_at TEXT
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

    def save_forecast_outcome(self, payload: Dict[str, Any]):
        def _first_number(*values: Any) -> Optional[float]:
            for value in values:
                if value is None:
                    continue
                try:
                    return float(value)
                except Exception:
                    continue
            return None

        forecast = payload.get('forecast') or {}
        outcome = payload.get('outcome') or {}
        forecast_type = str(payload.get('forecast_type') or outcome.get('type') or 'numeric').strip().lower() or 'numeric'
        predicted_value = _first_number(
            payload.get('predicted_value'),
            forecast.get('expected_temp_c'),
            forecast.get('mean'),
            forecast.get('temperature'),
            forecast.get('value'),
        )
        actual_value = _first_number(
            payload.get('actual_value'),
            outcome.get('actual_value'),
            outcome.get('temperature_c'),
            outcome.get('value'),
        )
        predicted_probability = _first_number(
            payload.get('predicted_probability'),
            forecast.get('predicted_probability'),
            forecast.get('model_prob'),
            forecast.get('probability'),
        )
        if predicted_probability is None and predicted_value is not None and 0.0 <= predicted_value <= 1.0:
            predicted_probability = predicted_value
        actual_outcome_raw = payload.get('actual_outcome')
        if actual_outcome_raw is None:
            actual_outcome_raw = outcome.get('actual_outcome', outcome.get('value'))
        actual_outcome: Optional[int]
        if actual_outcome_raw is None:
            actual_outcome = None
        else:
            try:
                actual_outcome = 1 if float(actual_outcome_raw) >= 0.5 else 0
            except Exception:
                actual_outcome = 1 if bool(actual_outcome_raw) else 0

        absolute_error = None
        squared_error = None
        if predicted_value is not None and actual_value is not None:
            absolute_error = abs(predicted_value - actual_value)
            squared_error = absolute_error ** 2

        brier_score = None
        if predicted_probability is not None and actual_outcome is not None:
            brier_score = (predicted_probability - float(actual_outcome)) ** 2
            forecast_type = 'binary'

        created_at = payload.get('created_at') or datetime.now(timezone.utc).isoformat()
        resolved_at = payload.get('resolved_at') or created_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO forecast_outcomes (
                    market_id, forecast_type, predicted_value, actual_value, predicted_probability,
                    actual_outcome, absolute_error, squared_error, brier_score, payload, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get('market_id') or ''),
                    forecast_type,
                    predicted_value,
                    actual_value,
                    predicted_probability,
                    actual_outcome,
                    absolute_error,
                    squared_error,
                    brier_score,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                    resolved_at,
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

    def save_telegram_command(self, payload: Dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_command_history
                (chat_id, user_id, username, command, args, message_text, reply_text, status, raw_update, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get('chat_id') or ''),
                    str(payload.get('user_id') or ''),
                    str(payload.get('username') or ''),
                    str(payload.get('command') or ''),
                    str(payload.get('args') or ''),
                    str(payload.get('message_text') or ''),
                    str(payload.get('reply_text') or ''),
                    str(payload.get('status') or 'handled'),
                    json.dumps(payload.get('raw_update') or {}, ensure_ascii=False),
                    payload.get('created_at') or datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_telegram_command_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, user_id, username, command, args, message_text, reply_text, status, raw_update, created_at FROM telegram_command_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    raw_update = json.loads(row[9]) if row[9] else {}
                except Exception:
                    raw_update = {'raw': row[9]}
                items.append({
                    'id': row[0],
                    'chat_id': row[1],
                    'user_id': row[2],
                    'username': row[3],
                    'command': row[4],
                    'args': row[5],
                    'message_text': row[6],
                    'reply_text': row[7],
                    'status': row[8],
                    'raw_update': raw_update,
                    'created_at': row[10],
                })
            return items

    def get_telegram_command_counts(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT command, COUNT(*) AS count, MAX(created_at) AS last_seen FROM telegram_command_history GROUP BY command ORDER BY count DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {'command': row[0], 'count': row[1], 'last_seen': row[2]}
                for row in rows
            ]

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

    def get_forecast_outcomes(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, market_id, forecast_type, predicted_value, actual_value, predicted_probability,
                       actual_outcome, absolute_error, squared_error, brier_score, payload, created_at, resolved_at
                FROM forecast_outcomes
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row[10])
                except Exception:
                    payload = {'raw': row[10]}
                record = payload if isinstance(payload, dict) else {'value': payload}
                record = {**record}
                record.setdefault('id', row[0])
                record.setdefault('market_id', row[1])
                record.setdefault('forecast_type', row[2])
                record.setdefault('predicted_value', row[3])
                record.setdefault('actual_value', row[4])
                record.setdefault('predicted_probability', row[5])
                record.setdefault('actual_outcome', row[6])
                record.setdefault('absolute_error', row[7])
                record.setdefault('squared_error', row[8])
                record.setdefault('brier_score', row[9])
                record.setdefault('created_at', row[11])
                record.setdefault('resolved_at', row[12])
                items.append(record)
            return items

    def get_forecast_calibration_summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT forecast_type, predicted_value, actual_value, predicted_probability, actual_outcome,
                       absolute_error, squared_error, brier_score, created_at, resolved_at
                FROM forecast_outcomes
                ORDER BY id ASC
                """
            ).fetchall()

        records_count = len(rows)
        numeric_count = 0
        binary_count = 0
        abs_error_total = 0.0
        sq_error_total = 0.0
        brier_total = 0.0
        accuracy_count = 0
        resolved_count = 0
        last_record_at = None

        for row in rows:
            forecast_type, predicted_value, actual_value, predicted_probability, actual_outcome, absolute_error, squared_error, brier_score, created_at, resolved_at = row
            if created_at:
                last_record_at = created_at
            if resolved_at:
                resolved_count += 1
            ft = str(forecast_type or '').lower()
            if absolute_error is not None or (predicted_value is not None and actual_value is not None):
                numeric_count += 1
                abs_error = float(absolute_error if absolute_error is not None else abs(float(predicted_value) - float(actual_value)))
                sq_error = float(squared_error if squared_error is not None else abs_error ** 2)
                abs_error_total += abs_error
                sq_error_total += sq_error
            if predicted_probability is not None and actual_outcome is not None:
                binary_count += 1
                brier = float(brier_score if brier_score is not None else (float(predicted_probability) - float(actual_outcome)) ** 2)
                brier_total += brier
                predicted_label = 1 if float(predicted_probability) >= 0.5 else 0
                accuracy_count += int(predicted_label == int(actual_outcome))
            elif ft == 'binary' and actual_outcome is not None:
                binary_count += 1
                predicted_label = 1 if float(predicted_probability or 0.0) >= 0.5 else 0
                accuracy_count += int(predicted_label == int(actual_outcome))

        return {
            'records_count': records_count,
            'resolved_records_count': resolved_count,
            'numeric_records_count': numeric_count,
            'binary_records_count': binary_count,
            'mae': round(abs_error_total / numeric_count, 4) if numeric_count else 0.0,
            'rmse': round(math.sqrt(sq_error_total / numeric_count), 4) if numeric_count else 0.0,
            'accuracy': round(accuracy_count / binary_count, 4) if binary_count else 0.0,
            'brier_score': round(brier_total / binary_count, 4) if binary_count else 0.0,
            'last_record_at': last_record_at,
        }

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
