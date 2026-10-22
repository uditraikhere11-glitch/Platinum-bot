"""Durable local SQLite queue shared by the Telegram listener and trade agents.

This queue provides durable *signal hand-off*, not exactly-once broker execution.
Signals whose handler raises after a possible broker submission are marked UNCERTAIN
and are never automatically requeued. Reconcile them against the broker before any
manual replay.

All processes must use the same DARVAS_SIGNAL_DB path on the same local filesystem.
Do not place this SQLite database on NFS or share it across different machines.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

ASSET_CLASSES = {"EQUITY", "FNO"}
STATUSES = {"PENDING", "CLAIMED", "COMPLETED", "UNCERTAIN"}
REPO_ROOT = Path(__file__).resolve().parent
_INITIALIZED_DATABASES: set[str] = set()
_INITIALIZE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_queue (
    signal_id       TEXT PRIMARY KEY,
    asset_class     TEXT NOT NULL CHECK (asset_class IN ('EQUITY', 'FNO')),
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'CLAIMED', 'COMPLETED', 'UNCERTAIN')),
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    claimed_at      TEXT,
    completed_at    TEXT,
    worker_id       TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    result_json     TEXT,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_queue_pending
    ON signal_queue(asset_class, status, received_at);
CREATE INDEX IF NOT EXISTS idx_signal_queue_status
    ON signal_queue(status, received_at);
"""


def default_database_path() -> str:
    """Absolute default shared by all entry points, independent of cwd."""
    return os.environ.get("DARVAS_SIGNAL_DB", str(REPO_ROOT / "runtime" / "signals.db"))


def _connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    db_path = str(path or default_database_path())
    if db_path != ":memory:":
        if not Path(db_path).expanduser().is_absolute():
            raise ValueError("DARVAS_SIGNAL_DB must be an absolute path shared by all services")
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def initialize_queue(path: str | os.PathLike[str] | None = None) -> str:
    """Create/migrate queue tables and return the selected database path."""
    db_path = str(path or default_database_path())
    # A :memory: database is connection-local, so callers should use a temporary file
    # for tests and shared process operation.
    if db_path != ":memory:":
        with _INITIALIZE_LOCK:
            if db_path in _INITIALIZED_DATABASES:
                return db_path
            with closing(_connect(db_path)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
            _INITIALIZED_DATABASES.add(db_path)
    else:
        with closing(_connect(db_path)) as conn:
            conn.executescript(_SCHEMA)
    return db_path


def enqueue_signal(payload: dict[str, Any], path: str | os.PathLike[str] | None = None) -> bool:
    """Durably insert a signal. Returns False for a duplicate signal_id."""
    if not isinstance(payload, dict):
        raise TypeError("Signal payload must be a dictionary")
    signal_id = str(payload.get("signal_id") or "").strip()
    if not signal_id:
        raise ValueError("Signal payload has no signal_id; refusing non-deduplicated enqueue")
    asset_class = str(payload.get("asset_class") or "").upper().strip()
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"Unsupported asset_class {asset_class!r}; expected EQUITY or FNO")

    initialize_queue(path)
    db_path = str(path or default_database_path())
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO signal_queue(signal_id, asset_class, payload_json) VALUES (?, ?, ?)",
            (signal_id, asset_class, serialized),
        )
        return cur.rowcount == 1


def claim_next(
    asset_class: str,
    worker_id: str,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the oldest pending signal for one worker/asset class."""
    asset = str(asset_class).upper().strip()
    if asset not in ASSET_CLASSES:
        raise ValueError(f"Unsupported asset_class: {asset}")
    initialize_queue(path)
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT signal_id, payload_json FROM signal_queue "
            "WHERE asset_class=? AND status='PENDING' ORDER BY received_at, rowid LIMIT 1",
            (asset,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        cur = conn.execute(
            "UPDATE signal_queue SET status='CLAIMED', claimed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "worker_id=?, attempt_count=attempt_count+1, last_error=NULL "
            "WHERE signal_id=? AND status='PENDING'",
            (worker_id, row["signal_id"]),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        conn.execute("COMMIT")
        payload = json.loads(row["payload_json"])
        return payload


def complete_signal(
    signal_id: str,
    result: Any = None,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Mark hand-off processing complete; executor result is recorded verbatim."""
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE signal_queue SET status='COMPLETED', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "result_json=? WHERE signal_id=? AND status='CLAIMED'",
            (json.dumps(result, ensure_ascii=False, default=str), signal_id),
        )
        return cur.rowcount == 1


def mark_uncertain(
    signal_id: str,
    error: str,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Mark a failed/ambiguous hand-off as uncertain; never auto-requeue it."""
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE signal_queue SET status='UNCERTAIN', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "last_error=? WHERE signal_id=? AND status='CLAIMED'",
            (str(error)[:4000], signal_id),
        )
        return cur.rowcount == 1



def resolve_signal(
    signal_id: str,
    target_status: str,
    confirm_broker_reconciled: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Operator resolution for CLAIMED/UNCERTAIN records after checking broker records.

    Requeueing is deliberately gated by an explicit broker-reconciliation confirmation.
    """
    target = str(target_status).upper().strip()
    if target not in {"PENDING", "COMPLETED"}:
        raise ValueError("target_status must be PENDING or COMPLETED")
    if not confirm_broker_reconciled:
        raise ValueError("reconcile the broker order/trade book and pass confirm_broker_reconciled=True")

    initialize_queue(path)
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        if target == "PENDING":
            cur = conn.execute(
                "UPDATE signal_queue SET status='PENDING', claimed_at=NULL, completed_at=NULL, "
                "worker_id=NULL, result_json=NULL, last_error='Operator reset after broker reconciliation' "
                "WHERE signal_id=? AND status IN ('CLAIMED','UNCERTAIN')",
                (signal_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE signal_queue SET status='COMPLETED', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "result_json=?, last_error='Operator marked complete after broker reconciliation' "
                "WHERE signal_id=? AND status IN ('CLAIMED','UNCERTAIN')",
                (json.dumps({"status": "OPERATOR_RESOLVED", "operator_outcome": "COMPLETED"}), signal_id),
            )
        return cur.rowcount == 1

def queue_stats(path: str | os.PathLike[str] | None = None) -> dict[str, int]:
    initialize_queue(path)
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM signal_queue GROUP BY status").fetchall()
    stats = {status: 0 for status in STATUSES}
    stats.update({str(row["status"]): int(row["n"]) for row in rows})
    return stats


def attention_required(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Return claimed/uncertain records for operator reconciliation (no replay)."""
    initialize_queue(path)
    db_path = str(path or default_database_path())
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT signal_id, asset_class, status, received_at, claimed_at, worker_id, "
            "attempt_count, last_error FROM signal_queue WHERE status IN ('CLAIMED','UNCERTAIN') "
            "ORDER BY received_at"
        ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and manage the Darvas SQLite signal queue")
    parser.add_argument("command", choices=("status", "attention", "resolve"), nargs="?", default="status")
    parser.add_argument("--signal-id", dest="signal_id", help="Required for the resolve command; use --signal-id=-100...:MESSAGE_ID for Telegram IDs")
    parser.add_argument("--status", dest="target_status", choices=("pending", "completed"),
                        help="Resolution for a CLAIMED/UNCERTAIN signal")
    parser.add_argument("--confirm-broker-reconciled", action="store_true",
                        help="Required: confirms you checked broker orders/trades before resolving")
    args = parser.parse_args()
    path = initialize_queue()
    print(f"Signal database: {path}")
    if args.command == "status":
        for state, count in queue_stats(path).items():
            print(f"{state:10s} {count}")
    elif args.command == "attention":
        rows = attention_required(path)
        if not rows:
            print("No claimed or uncertain signals require review.")
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, default=str))
    else:
        if not args.signal_id or not args.target_status:
            parser.error("resolve requires SIGNAL_ID and --status pending|completed")
        if not args.confirm_broker_reconciled:
            parser.error("resolve requires --confirm-broker-reconciled after you check broker orders/trades")
        changed = resolve_signal(args.signal_id, args.target_status, True, path)
        if not changed:
            raise SystemExit(f"No CLAIMED/UNCERTAIN signal found with ID {args.signal_id!r}.")
        print(f"Resolved {args.signal_id} as {args.target_status.upper()} after operator confirmation.")


if __name__ == "__main__":
    main()
