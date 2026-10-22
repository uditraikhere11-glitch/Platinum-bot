from datetime import datetime, timezone
from types import SimpleNamespace
import asyncio

from config import ChannelMapping
import signal_pipeline as pipeline


def test_test_mode_never_queues_live_signal(monkeypatch):
    queued = []
    logs = []

    async def parse_equity(text, timestamp=""):
        return {"stock_symbol": "SBIN", "entry_price": 805.0, "order_type": "LIMIT", "source": "REGEX"}

    async def fake_log(message, mode=None):
        logs.append((message, mode))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(pipeline, "enqueue_signal", lambda payload: queued.append(payload) or True)
    monkeypatch.setattr(pipeline, "hybrid_extract_trade", parse_equity)
    monkeypatch.setattr(pipeline, "send_trade_log", fake_log)
    monkeypatch.setattr(pipeline, "send_to_notion_async", noop)
    monkeypatch.setattr(pipeline, "send_to_discord_async", noop)
    monkeypatch.setattr(pipeline, "extract_trade_async", noop)
    monkeypatch.setattr(pipeline, "read_last_date", lambda *_: "2026-10-22")
    monkeypatch.setattr(pipeline, "write_checkpoint", lambda *_: None)
    monkeypatch.setattr(pipeline, "write_last_date", lambda *_: None)

    mapping = ChannelMapping(-100123, "", "test-equity", enable_trading=True, mode="test")
    monkeypatch.setitem(pipeline.CHANNEL_MAP, -100123, mapping)
    message = SimpleNamespace(id=19, text="Buy #SBIN @805", media=None, date=datetime.now(timezone.utc))

    async def run():
        await pipeline.process_telegram_message(message, -100123)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert queued == []
    assert any(mode == "test" and "TEST PARSED" in text for text, mode in logs)


def test_live_mode_stays_blocked_until_global_switch_enabled(monkeypatch):
    queued = []

    async def parse_equity(text, timestamp=""):
        return {"stock_symbol": "SBIN", "entry_price": 805.0, "order_type": "LIMIT", "source": "REGEX"}

    async def fake_log(*args, **kwargs):
        return None

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(pipeline, "enqueue_signal", lambda payload: queued.append(payload) or True)
    monkeypatch.setattr(pipeline, "hybrid_extract_trade", parse_equity)
    monkeypatch.setattr(pipeline, "send_trade_log", fake_log)
    monkeypatch.setattr(pipeline, "send_to_notion_async", noop)
    monkeypatch.setattr(pipeline, "send_to_discord_async", noop)
    monkeypatch.setattr(pipeline, "extract_trade_async", noop)
    monkeypatch.setattr(pipeline, "read_last_date", lambda *_: "2026-10-22")
    monkeypatch.setattr(pipeline, "write_checkpoint", lambda *_: None)
    monkeypatch.setattr(pipeline, "write_last_date", lambda *_: None)

    mapping = ChannelMapping(-100124, "", "live-equity", enable_trading=True, mode="live")
    monkeypatch.setitem(pipeline.CHANNEL_MAP, -100124, mapping)
    message = SimpleNamespace(id=20, text="Buy #SBIN @805", media=None, date=datetime.now(timezone.utc))
    asyncio.run(pipeline.process_telegram_message(message, -100124))
    assert queued == []


def test_live_source_routes_status_to_live_log_and_queues_only_when_gate_on(monkeypatch):
    queued = []
    logs = []

    async def parse_equity(text, timestamp=""):
        return {"stock_symbol": "SBIN", "entry_price": 805.0, "order_type": "LIMIT", "source": "REGEX"}

    async def fake_log(message, mode=None):
        logs.append((message, mode))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(pipeline, "enqueue_signal", lambda payload: queued.append(payload) or True)
    monkeypatch.setattr(pipeline, "hybrid_extract_trade", parse_equity)
    monkeypatch.setattr(pipeline, "send_trade_log", fake_log)
    monkeypatch.setattr(pipeline, "send_to_notion_async", noop)
    monkeypatch.setattr(pipeline, "send_to_discord_async", noop)
    monkeypatch.setattr(pipeline, "extract_trade_async", noop)
    monkeypatch.setattr(pipeline, "read_last_date", lambda *_: "2026-10-22")
    monkeypatch.setattr(pipeline, "write_checkpoint", lambda *_: None)
    monkeypatch.setattr(pipeline, "write_last_date", lambda *_: None)

    mapping = ChannelMapping(-100125, "", "live-equity", enable_trading=True, mode="live")
    monkeypatch.setitem(pipeline.CHANNEL_MAP, -100125, mapping)
    message = SimpleNamespace(id=21, text="Buy #SBIN @805", media=None, date=datetime.now(timezone.utc))

    async def run():
        await pipeline.process_telegram_message(message, -100125)
        await asyncio.sleep(0.01)

    asyncio.run(run())

    assert len(queued) == 1
    assert queued[0]["signal_id"] == "-100125:21"
    assert queued[0]["mode"] == "live"
    assert any(mode == "live" and "LIVE SIGNAL | EQUITY" in text for text, mode in logs)


def test_test_fno_signal_is_parsed_but_never_queued(monkeypatch):
    queued = []
    logs = []

    async def parse_fno(_text):
        return {
            "symbol": "NIFTY", "strike": 25000, "option_type": "CE",
            "entry_min": 100, "entry_max": 105, "source": "REGEX", "targets": [],
        }

    async def fake_log(message, mode=None):
        logs.append((message, mode))

    monkeypatch.setattr(pipeline, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(pipeline, "enqueue_signal", lambda payload: queued.append(payload) or True)
    monkeypatch.setattr(pipeline, "hybrid_fno_extract", parse_fno)
    monkeypatch.setattr(pipeline, "send_trade_log", fake_log)
    monkeypatch.setattr(pipeline, "read_last_date", lambda *_: "2026-10-22")
    monkeypatch.setattr(pipeline, "write_checkpoint", lambda *_: None)
    monkeypatch.setattr(pipeline, "write_last_date", lambda *_: None)

    mapping = ChannelMapping(-100126, "", "test-fno", enable_trading=True, enable_fno_trading=True, mode="test")
    monkeypatch.setitem(pipeline.CHANNEL_MAP, -100126, mapping)
    message = SimpleNamespace(
        id=22,
        text="#NIFTY 25000 CE @100-105",
        media=None,
        date=datetime.now(timezone.utc),
    )

    async def run():
        await pipeline.process_telegram_message(message, -100126)
        await asyncio.sleep(0.01)

    asyncio.run(run())

    assert queued == []
    assert any(mode == "test" and "TEST PARSED | FNO" in text for text, mode in logs)
