"""
Equity SL/target (EQUITY_SL_TARGET_ENABLED): placed only AFTER the buy executes, at a % of the
EXECUTED price Dhan reports, as a Dhan Forever OCO for exactly the executed quantity.
"""

import asyncio
import logging

import pytest

from client_agent import client
from client_agent.equity_protection import ProtectionWatcher, levels


# ---------------------------------------------------------------------------
# Order placement: always a plain buy; SL/target are never sent with it
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(monkeypatch, tmp_path):
    b = client.DhanBroker("1100", "TOKEN", dry_run=False)
    monkeypatch.setattr(b._scrips, "get_equity", lambda s: {"id": "3045", "segment": "NSE_EQ"})
    b.sent = []
    monkeypatch.setattr(b, "_place_dhan_order",
                        lambda payload, is_retry=False, url=None: b.sent.append((url, payload))
                        or {"order_id": "ORD1", "status": "PENDING"})
    monkeypatch.setattr(b, "_fetch_ltp", lambda symbol: 200.0)
    monkeypatch.setattr(client, "_is_amo_window", lambda: False)
    monkeypatch.setattr(client, "TRADE_AMOUNT_INR", 10000)
    b.watcher = ProtectionWatcher(b, 2.0, 1.0, path=str(tmp_path / "pending.json"))
    return b


def test_buy_is_plain_and_tracked_nothing_protective_sent(broker):
    broker.place_order({"stock_symbol": "SBIN", "order_type": "LIMIT", "entry_price": 100.0})
    (url, p), = broker.sent
    assert url is None and p["transactionType"] == "BUY" and p["price"] == 101.0 and p["quantity"] == 100
    assert "stopLossPrice" not in p and "targetPrice" not in p
    assert broker.watcher.pending["ORD1"]["qty"] == 100 and broker.watcher.pending["ORD1"]["protected_qty"] == 0


def test_amo_buy_is_tracked_too(broker, monkeypatch):
    monkeypatch.setattr(client, "_is_amo_window", lambda: True)
    broker.place_order({"stock_symbol": "SBIN", "order_type": "LIMIT", "entry_price": 100.0})
    (_, p), = broker.sent
    assert p["afterMarketOrder"] is True
    assert "ORD1" in broker.watcher.pending  # protected once it executes at the open


def test_rejected_buy_is_not_tracked(broker, monkeypatch):
    monkeypatch.setattr(broker, "_place_dhan_order", lambda *a, **k: {"status": "REJECTED", "response": {}})
    broker.place_order({"stock_symbol": "SBIN", "order_type": "LIMIT", "entry_price": 100.0})
    assert broker.watcher.pending == {}


def test_flag_off_has_no_watcher_and_same_order(monkeypatch):
    b = client.DhanBroker("1100", "TOKEN", dry_run=False)
    monkeypatch.setattr(b._scrips, "get_equity", lambda s: {"id": "3045", "segment": "NSE_EQ"})
    sent = []
    monkeypatch.setattr(b, "_place_dhan_order", lambda payload, **k: sent.append(payload) or {"order_id": "1"})
    monkeypatch.setattr(client, "_is_amo_window", lambda: False)
    monkeypatch.setattr(client, "TRADE_AMOUNT_INR", 10000)
    b.place_order({"stock_symbol": "SBIN", "order_type": "LIMIT", "entry_price": 100.0})
    assert b.watcher is None and sent[0]["price"] == 101.0 and sent[0]["validity"] == "DAY"


def test_flag_on_without_sl_pct_refuses_to_start(monkeypatch):
    monkeypatch.setattr(client, "EQUITY_SL_TARGET_ENABLED", True)
    monkeypatch.setattr(client, "EQUITY_SL_PCT", 0.0)
    monkeypatch.setattr(client, "_create_broker", lambda dry_run: pytest.fail("must not start"))
    with pytest.raises(SystemExit):
        asyncio.run(client.run_worker())


# ---------------------------------------------------------------------------
# Watcher: SL/target only after execution, from the executed price Dhan reports
# ---------------------------------------------------------------------------

class FakeDhan:
    """Scripted Dhan order statuses; records Forever OCOs."""

    def __init__(self, statuses, trades_avg=None, oco_ok=True):
        self.statuses = list(statuses)
        self.trades_avg = trades_avg
        self.oco_ok = oco_ok
        self.ocos = []

    def order_status(self, order_id):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def trades_avg_price(self, order_id):
        return self.trades_avg

    def place_forever_oco(self, rec, qty, target, sl, sl_limit):
        if not self.oco_ok:
            return None
        self.ocos.append((qty, target, sl, sl_limit))
        return {"order_id": f"F{len(self.ocos)}"}


def watcher_for(dhan, tmp_path, now=[1000.0]):
    w = ProtectionWatcher(dhan, sl_pct=2.0, target_pct=1.0, sl_limit_buffer_pct=1.0,
                          path=str(tmp_path / "pending.json"), clock=lambda: now[0])
    w.track("ORD1", "SBIN", "3045", "NSE_EQ", 100)
    return w


def test_levels_from_executed_price():
    # executed 101.30: target +1% = 102.313 -> 102.35 (up); SL -2% = 99.274 -> 99.25 (down); SL limit -1% below
    assert levels(101.3, 2, 1, 1) == (99.25, 98.25, 102.35)
    assert levels(0.5, 2, 1, 1) == (0.45, 0.4, 0.55)  # always at least one tick away


def test_nothing_placed_while_pending_then_placed_from_executed_price(tmp_path):
    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "PENDING", "filledQty": 0},
                     {"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 101.3}])
    w = watcher_for(dhan, tmp_path)
    w.check_once()
    assert dhan.ocos == [] and "ORD1" in w.pending  # not executed yet: nothing placed
    w.check_once()
    assert dhan.ocos == [(100, 102.35, 99.25, 98.25)]  # from Dhan's executed 101.30, not the limit
    assert w.pending == {}


def test_never_executed_places_nothing(tmp_path):
    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "CANCELLED", "filledQty": 0}])
    w = watcher_for(dhan, tmp_path)
    w.check_once()
    assert dhan.ocos == [] and w.pending == {}


def test_partial_fills_are_protected_chunk_by_chunk_at_their_own_price(tmp_path):
    dhan = FakeDhan([
        {"orderId": "ORD1", "orderStatus": "PART_TRADED", "filledQty": 40, "averageTradedPrice": 100.0},
        {"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 100.6},
    ])
    w = watcher_for(dhan, tmp_path)
    w.check_once()
    w.check_once()
    # chunk 1: 40 @ 100.00; chunk 2: 60 @ (100.6*100 - 100*40)/60 = 101.00
    assert dhan.ocos == [(40, 101.0, 98.0, 97.0), (60, 102.05, 98.95, 97.95)]
    assert w.pending == {}


def test_missing_average_uses_trades_then_waits_rather_than_guess(tmp_path):
    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 0}],
                    trades_avg=101.3)
    w = watcher_for(dhan, tmp_path)
    w.check_once()
    assert dhan.ocos == [(100, 102.35, 99.25, 98.25)]

    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 0}])
    (tmp_path / "second").mkdir()
    w = watcher_for(dhan, tmp_path / "second")
    w.check_once()
    assert dhan.ocos == [] and "ORD1" in w.pending  # no executed price from Dhan: nothing guessed


def test_rejected_protection_is_retried_later_and_order_stays_tracked(tmp_path):
    now = [1000.0]
    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 101.3}],
                    oco_ok=False)
    w = watcher_for(dhan, tmp_path, now)
    w.check_once()
    assert "ORD1" in w.pending and w.pending["ORD1"]["protected_qty"] == 0
    dhan.oco_ok = True
    w.check_once()  # still inside the retry delay
    assert dhan.ocos == []
    now[0] += 61
    w.check_once()
    assert dhan.ocos == [(100, 102.35, 99.25, 98.25)] and w.pending == {}


def test_tracked_orders_survive_a_restart(tmp_path):
    dhan = FakeDhan([{"orderId": "ORD1", "orderStatus": "PENDING", "filledQty": 0}])
    watcher_for(dhan, tmp_path).check_once()  # e.g. an AMO placed at night, bot restarts at 08:45
    dhan.statuses = [{"orderId": "ORD1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 101.3}]
    restarted = ProtectionWatcher(dhan, 2.0, 1.0, 1.0, path=str(tmp_path / "pending.json"))
    assert "ORD1" in restarted.pending
    restarted.check_once()
    assert dhan.ocos == [(100, 102.35, 99.25, 98.25)]


def test_dry_run_simulates_fill_and_logs_the_oco(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    w = ProtectionWatcher(FakeDhan([{}]), 2.0, 1.0, 1.0, path=str(tmp_path / "p.json"), dry_run=True)
    w.track("DRY_RUN", "SBIN", "3045", "NSE_EQ", 100, dry_price=101.3)
    w.check_once()
    assert "Would place Forever OCO SELL 100 x SBIN: executed 101.3" in caplog.text and w.pending == {}


def test_forever_oco_payload(monkeypatch):
    b = client.DhanBroker("1100", "TOKEN", dry_run=False)
    sent = []
    monkeypatch.setattr(b, "_api", lambda method, path, body=None, is_retry=False:
                        sent.append((method, path, body)) or (200, {"orderId": "F1", "orderStatus": "PENDING"}))
    rec = {"segment": "NSE_EQ", "security_id": "3045"}
    assert b.place_forever_oco(rec, 100, 102.35, 99.25, 98.25) == {"order_id": "F1", "status": "PENDING"}
    method, path, p = sent[0]
    assert (method, path) == ("POST", "/forever/orders")
    assert (p["orderFlag"], p["transactionType"], p["productType"]) == ("OCO", "SELL", "CNC")
    assert (p["quantity"], p["price"], p["triggerPrice"]) == (100, 102.35, 102.35)  # target leg
    assert (p["quantity1"], p["price1"], p["triggerPrice1"]) == (100, 98.25, 99.25)  # stop-loss leg
