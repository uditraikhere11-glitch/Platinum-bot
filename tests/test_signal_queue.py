import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from signal_queue import (
    attention_required,
    claim_next,
    complete_signal,
    enqueue_signal,
    initialize_queue,
    mark_uncertain,
    queue_stats,
    resolve_signal,
)


def signal(signal_id="channel:123", asset_class="EQUITY"):
    return {
        "signal_id": signal_id,
        "asset_class": asset_class,
        "channel_label": "test",
        "stock_symbol": "SBIN" if asset_class == "EQUITY" else None,
        "raw_text": "BUY SBIN @ 805",
    }


def test_enqueue_is_durable_and_deduplicates(tmp_path):
    db = str(tmp_path / "signals.db")
    initialize_queue(db)

    assert enqueue_signal(signal(), db) is True
    assert enqueue_signal(signal(), db) is False
    assert queue_stats(db)["PENDING"] == 1


def test_claim_routes_by_asset_class_and_completion(tmp_path):
    db = str(tmp_path / "signals.db")
    initialize_queue(db)
    enqueue_signal(signal("equity:1", "EQUITY"), db)
    enqueue_signal(signal("fno:2", "FNO"), db)

    equity = claim_next("EQUITY", "equity-test-worker", db)
    assert equity["signal_id"] == "equity:1"
    assert claim_next("EQUITY", "equity-test-worker", db) is None
    assert claim_next("FNO", "fno-test-worker", db)["signal_id"] == "fno:2"

    assert complete_signal("equity:1", {"status": "SIMULATED"}, db) is True
    assert complete_signal("equity:1", {"status": "SIMULATED"}, db) is False
    assert queue_stats(db)["COMPLETED"] == 1


def test_uncertain_signal_is_never_automatically_requeued(tmp_path):
    db = str(tmp_path / "signals.db")
    initialize_queue(db)
    enqueue_signal(signal("equity:uncertain"), db)
    claimed = claim_next("EQUITY", "equity-worker", db)
    assert claimed is not None

    assert mark_uncertain("equity:uncertain", "test timeout after submit", db) is True
    assert claim_next("EQUITY", "equity-worker", db) is None
    rows = attention_required(db)
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "equity:uncertain"
    assert rows[0]["status"] == "UNCERTAIN"
    assert rows[0]["last_error"] == "test timeout after submit"


def test_concurrent_claimers_only_claim_a_signal_once(tmp_path):
    db = str(tmp_path / "signals.db")
    initialize_queue(db)
    enqueue_signal(signal("equity:unique"), db)

    def claim(worker):
        return claim_next("EQUITY", worker, db)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, [f"worker-{n}" for n in range(6)]))

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["signal_id"] == "equity:unique"


def test_enqueue_requires_stable_id_and_supported_asset(tmp_path):
    db = str(tmp_path / "signals.db")
    with pytest.raises(ValueError, match="signal_id"):
        enqueue_signal({"asset_class": "EQUITY"}, db)
    with pytest.raises(ValueError, match="asset_class"):
        enqueue_signal({"signal_id": "unknown:1", "asset_class": "CRYPTO"}, db)


@pytest.mark.asyncio
async def test_equity_worker_marks_missing_broker_outcome_uncertain(tmp_path, monkeypatch):
    """Broker adapters returning None must not be treated as confirmed completion."""
    import asyncio
    import client_agent.client as client

    db = str(tmp_path / "signals.db")
    initialize_queue(db)
    enqueue_signal({
        "signal_id": "equity:ambiguous",
        "asset_class": "EQUITY",
        "channel_label": "test",
        "stock_symbol": "SBIN",
        "entry_price": 805.0,
        "order_type": "LIMIT",
    }, db)
    monkeypatch.setattr(client, "SIGNAL_DB_PATH", db)
    monkeypatch.setattr(client, "QUEUE_POLL_SECONDS", 0.01)

    class AmbiguousBroker:
        def handle_signal(self, payload):
            return None

    task = asyncio.create_task(client.consume_signal_queue(AmbiguousBroker()))
    try:
        for _ in range(100):
            if queue_stats(db)["UNCERTAIN"] == 1:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert queue_stats(db)["UNCERTAIN"] == 1
    assert attention_required(db)[0]["signal_id"] == "equity:ambiguous"


def test_operator_resolution_requires_explicit_broker_reconciliation(tmp_path):
    db = str(tmp_path / "signals.db")
    initialize_queue(db)
    enqueue_signal(signal("equity:review"), db)
    claim_next("EQUITY", "equity-worker", db)
    mark_uncertain("equity:review", "ambiguous response", db)

    with pytest.raises(ValueError, match="reconcile the broker"):
        resolve_signal("equity:review", "PENDING", path=db)

    assert resolve_signal("equity:review", "PENDING", True, db) is True
    assert claim_next("EQUITY", "equity-worker", db)["signal_id"] == "equity:review"
