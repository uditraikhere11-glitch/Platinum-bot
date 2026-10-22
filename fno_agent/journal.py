"""
SQLite trade journal: dedup, audit trail and crash recovery.

Signal lifecycle:
    RECEIVED -> REJECTED | ERROR
             -> ENTRY_PLACED -> CANCELLED (nothing filled)
                             -> FILLED -> PROTECTED | UNPROTECTED (protection failed; act manually!)
                                       -> ENTERED (SL/target disabled by FNO_SL_TARGET_ENABLED=false)
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

RECEIVED, REJECTED, ERROR = "RECEIVED", "REJECTED", "ERROR"
ENTRY_PLACED, FILLED, CANCELLED = "ENTRY_PLACED", "FILLED", "CANCELLED"
PROTECTED, UNPROTECTED, ENTERED = "PROTECTED", "UNPROTECTED", "ENTERED"
PENDING_STATES = (ENTRY_PLACED, FILLED)

ENTRY, PROTECTION = "ENTRY", "PROTECTION"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id      TEXT PRIMARY KEY,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT,
    payload        TEXT NOT NULL,
    contract       TEXT,
    planned_qty    INTEGER,
    filled_qty     INTEGER,
    entry_deadline TEXT,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT NOT NULL REFERENCES signals(signal_id),
    kind            TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    price           REAL,
    stop_loss       REAL,
    target          REAL,
    status          TEXT NOT NULL,
    filled_qty      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Migration: executed (average) price reported by the broker
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(signals)")}
        if "avg_price" not in cols:
            self._conn.execute("ALTER TABLE signals ADD COLUMN avg_price REAL")
        self._lock = threading.Lock()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # --- signals ------------------------------------------------------------

    def try_claim(self, signal_id: str, payload: dict) -> bool:
        """Record a new signal. False if it was already seen (duplicate delivery)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO signals (signal_id, received_at, status, payload, updated_at) VALUES (?,?,?,?,?)",
            (signal_id, _now(), RECEIVED, json.dumps(payload, default=str), _now()),
        )
        return cur.rowcount == 1

    def update_signal(self, signal_id: str, status: Optional[str] = None, reason: Optional[str] = None, **fields):
        cols = {"updated_at": _now(), **fields}
        if status:
            cols["status"] = status
        if reason is not None:
            cols["reason"] = reason
        sets = ", ".join(f"{k}=?" for k in cols)
        self._exec(f"UPDATE signals SET {sets} WHERE signal_id=?", (*cols.values(), signal_id))

    def get_signal(self, signal_id: str) -> Optional[sqlite3.Row]:
        return self._exec("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()

    def signals_with_status(self, *statuses: str) -> List[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self._exec(f"SELECT * FROM signals WHERE status IN ({q})", statuses).fetchall()

    def pending_signals(self) -> List[sqlite3.Row]:
        return self.signals_with_status(*PENDING_STATES)

    def count_by_status(self) -> dict:
        rows = self._exec("SELECT status, COUNT(*) n FROM signals GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- orders ---------------------------------------------------------------

    def add_order(self, signal_id: str, kind: str, broker_order_id: str, qty: int,
                  price: Optional[float], stop_loss: float, target: Optional[float], status: str) -> int:
        cur = self._exec(
            "INSERT INTO orders (signal_id, kind, broker_order_id, qty, price, stop_loss, target, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal_id, kind, broker_order_id, qty, price, stop_loss, target, status, _now(), _now()),
        )
        return cur.lastrowid

    def update_order_levels(self, order_row_id: int, stop_loss: float, target: float):
        self._exec("UPDATE orders SET stop_loss=?, target=?, updated_at=? WHERE id=?",
                   (stop_loss, target, _now(), order_row_id))

    def update_order(self, order_row_id: int, status: str, filled_qty: int):
        self._exec("UPDATE orders SET status=?, filled_qty=?, updated_at=? WHERE id=?",
                   (status, filled_qty, _now(), order_row_id))

    def orders_for(self, signal_id: str, kind: Optional[str] = None) -> List[sqlite3.Row]:
        if kind:
            return self._exec("SELECT * FROM orders WHERE signal_id=? AND kind=? ORDER BY id",
                              (signal_id, kind)).fetchall()
        return self._exec("SELECT * FROM orders WHERE signal_id=? ORDER BY id", (signal_id,)).fetchall()
