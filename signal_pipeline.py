"""Telegram message parsing, validation and signal-queue submission.

Test-mode channel messages are parsed and logged to the test Telegram destination,
but are never submitted to the live SQLite execution queue. Live-mode submission
requires both a channel mapping with mode="live" and LIVE_TRADING_ENABLED=true.
"""
from __future__ import annotations

import asyncio
import os
import time
import zoneinfo
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from config import CHANNEL_MAP, LIVE_TRADING_ENABLED
from signal_queue import enqueue_signal
from state_manager import read_checkpoint, write_checkpoint, read_last_date, write_last_date
from services.notion_service import send_to_notion_async
from services.discord_service import send_to_discord_async
from services.gemini_service import extract_trade_async, hybrid_extract_trade
from services.regex_service import filter_message
from services.fno_parser import hybrid_fno_extract, looks_like_option
from services.google_sheets_service import append_to_sheet_async
from services.telegram_log_service import send_telegram_log

logger = logging.getLogger("signal_pipeline")
IST_ZONE = zoneinfo.ZoneInfo("Asia/Kolkata")
FNO_MAX_SIGNAL_AGE_SEC = int(os.environ.get("FNO_MAX_SIGNAL_AGE_SEC", "120"))
EQUITY_MAX_SIGNAL_AGE_SEC = int(os.environ.get("EQUITY_MAX_SIGNAL_AGE_SEC", "120"))
_channel_locks: dict[int, asyncio.Lock] = {}
_uncheckpointed_messages: dict[int, set[int]] = {}
_log_mode: ContextVar[str] = ContextVar("darvas_telegram_log_mode", default="test")
_telegram_client = None


def configure_telegram_client(client):
    """Register the listener's Telethon client for optional log-channel fallback."""
    global _telegram_client
    _telegram_client = client


def _mode(mapping) -> str:
    value = str(getattr(mapping, "mode", "test") or "test").strip().lower()
    return value if value in {"test", "live"} else "test"


def _live_queue_allowed(mapping) -> bool:
    return _mode(mapping) == "live" and LIVE_TRADING_ENABLED


async def send_trade_log(message: str, mode: str | None = None):
    """Send status to the configured test/live Telegram log destination."""
    target_mode = (mode or _log_mode.get() or "test").lower()
    await send_telegram_log(message, target_mode, telethon_client=_telegram_client)


async def _process_telegram_message_inner(message, channel_id):
    """
    Process a single Telegram message:
    1. Dispatch Notion logging (background)
    2. Dispatch Discord forwarding with raw message object (background, media downloaded off critical path)
    3. Run hybrid F&O extraction (regex → Gemini)
    4. If F&O signal found → persist it to the SQLite queue
    5. If equity signal found → persist it to the SQLite queue; run Google Sheets extraction
    6. Return whether the signal path completed safely; the outer serialized handler owns checkpoint commits.
    """
    checkpoint_safe = True
    # Look up the mapping for this channel
    mapping = CHANNEL_MAP.get(channel_id)
    if not mapping:
        logger.warning(f"No mapping found for channel {channel_id}. Skipping.")
        return True

    # Extract the text of the message, or note if it is media without a caption
    if message.text:
        text = message.text
    elif message.media:
        text = "[Media received without caption]"
    else:
        text = "[Empty or unknown message format]"
        
    mode = _mode(mapping)
    logger.info(f"[{mapping.label}] Processing message {message.id} from channel {channel_id} (mode={mode}):\n{text}")
    logger.info("-" * 40)
    if mode == "test":
        asyncio.create_task(send_trade_log(
            f"🧪 TEST MESSAGE | channel={mapping.label} | message_id={message.id}\n"
            f"Time: {message.date.astimezone(IST_ZONE).strftime('%Y-%m-%d %H:%M:%S')} IST\n"
            f"This message will be parsed for testing only; it cannot enter the live execution queue.\n\n{text[:2600]}",
            mode="test",
        ))
    
    # Check if it's a new day in IST
    message_date_ist = message.date.astimezone(IST_ZONE)
    msg_date_str = message_date_ist.strftime("%Y-%m-%d")
    
    last_date_str = read_last_date(channel_id)
    is_new_day = False
    if last_date_str != msg_date_str:
        is_new_day = True
        write_last_date(channel_id, msg_date_str)
    
    # External integrations are production side effects. Test-source messages stay inside
    # the test log route and are not forwarded to production Discord/Notion/Sheets.
    if mode == "live":
        asyncio.create_task(send_to_notion_async(text, message.date, is_new_day))
        asyncio.create_task(
            send_to_discord_async(text, message=message, webhook_url=mapping.discord_webhook_url)
        )
    
    formatted_time = message_date_ist.strftime("%Y-%m-%d %H:%M:%S")
    signal_id = f"{channel_id}:{message.id}"

    message_age_sec = max(0.0, (datetime.now(timezone.utc) - message.date.astimezone(timezone.utc)).total_seconds())

    # --- FnO Signal Extraction & SQLite queue (opt-in per channel) ---
    # Only option-looking messages (strike+CE/PE, CALL/PUT) take the FnO path, and they never fall
    # through to equity. Plain equity messages pay just one ~1µs regex check here.
    fno_handled = False
    if mapping.enable_fno_trading and looks_like_option(text):
        fno_handled = True
        fno_result = await handle_fno_message(text, message, signal_id, mapping)
        if fno_result is None:
            checkpoint_safe = False

    # --- Equity Signal Extraction & SQLite queue (opt-in per channel) ---
    if fno_handled:
        pass
    elif mapping.enable_trading and message_age_sec > EQUITY_MAX_SIGNAL_AGE_SEC:
        logger.warning(f"[{mapping.label}] Stale equity signal candidate ({message_age_sec:.0f}s old); not queued.")
        asyncio.create_task(send_trade_log(f"[⏱️] Stale equity signal skipped ({message_age_sec:.0f}s old): {text[:150]}"))
    elif mapping.enable_trading:
        # Apply message-level filters (index blocker, sell blocker, etc.)
        block_reason = filter_message(text)
        if block_reason:
            logger.info(f"[{mapping.label}] 🛡️ BLOCKED: {block_reason}")
            asyncio.create_task(send_trade_log(f"[🛡️] BLOCKED: {block_reason}\n\nMessage: {text[:100]}"))
        else:
            # Log signal received
            asyncio.create_task(send_trade_log(f"[🚀] Signal Received: {text}"))
            
            t_start = time.perf_counter_ns()
            try:
                trade_signal = await hybrid_extract_trade(text, timestamp=formatted_time)
                t_extract = time.perf_counter_ns()
                extract_ms = (t_extract - t_start) / 1_000_000
                
                if trade_signal:
                    symbol = trade_signal['stock_symbol']
                    price = trade_signal['entry_price']
                    order_type = trade_signal['order_type']
                    source = trade_signal['source']
                    price_display = 'MARKET' if price == 0 else price
                    
                    # Log extraction result
                    if source == 'REGEX':
                        asyncio.create_task(send_trade_log(
                            f"[*] Local Fast-Path Parsed -> Symbol: {symbol} | Price: {price_display}"
                        ))
                    else:
                        asyncio.create_task(send_trade_log(
                            f"[*] AI Extracted -> Symbol: {symbol} | Price: {price_display}"
                        ))
                    
                    # Add stable identity/context and persist before returning from the listener.
                    trade_signal["_latency_ms"] = {"extraction": round(extract_ms, 2), "source": source}
                    trade_signal["channel_label"] = mapping.label
                    trade_signal["asset_class"] = "EQUITY"
                    trade_signal["signal_id"] = signal_id
                    trade_signal["message_ts"] = message.date.isoformat()
                    trade_signal["mode"] = mode

                    queued = False
                    queue_ms = 0.0
                    if _live_queue_allowed(mapping):
                        queued = await asyncio.to_thread(enqueue_signal, trade_signal)
                        t_queued = time.perf_counter_ns()
                        queue_ms = (t_queued - t_extract) / 1_000_000
                    t_done = time.perf_counter_ns()
                    total_ms = (t_done - t_start) / 1_000_000
                    trade_signal["_latency_ms"]["queue_write"] = round(queue_ms, 2)
                    trade_signal["_latency_ms"]["total_server"] = round(total_ms, 2)

                    if mode == "test":
                        queue_status = "TEST ONLY — parsed successfully; NOT queued for execution"
                    elif not LIVE_TRADING_ENABLED:
                        queue_status = "LIVE TRADING DISABLED — set LIVE_TRADING_ENABLED=true to allow queueing"
                    else:
                        queue_status = "inserted" if queued else "duplicate ignored"

                    asyncio.create_task(send_trade_log(
                        f"{'🧪 TEST PARSED' if mode == 'test' else '🚦 LIVE SIGNAL'} | EQUITY\n"
                        f"Symbol: {symbol} | Price: {price_display} | Type: {order_type} | Source: {source}\n"
                        f"Status: {queue_status} | Parse: {extract_ms:.1f}ms | Total: {total_ms:.1f}ms",
                        mode=mode,
                    ))
                    logger.info(
                        "[EQUITY %s] %s @ %s | extract=%.1fms (%s) | queue=%s | total=%.1fms",
                        mode.upper(), symbol, price_display, extract_ms, source, queue_status, total_ms,
                    )
                else:
                    logger.debug(
                        f"[{mapping.label}] No equity signal found ({extract_ms:.1f}ms)"
                    )
            except Exception as e:
                # If parsing or durable enqueue failed, do not advance the Telegram checkpoint.
                # Catch-up will retry the message after a restart; INSERT OR IGNORE prevents
                # duplicate queue records if persistence succeeded before the failure.
                checkpoint_safe = False
                logger.error(f"Equity hybrid extraction/queue failed: {e}", exc_info=True)
                asyncio.create_task(send_trade_log(f"[X] Extraction/queue error: {e}"))
    else:
        logger.debug(f"[{mapping.label}] Trading disabled for this channel. Skipping extraction.")
    
    # --- Equity Trade Extraction → Google Sheets (existing pipeline, preserved) ---
    if mode == "live":
        try:
            trade_data = await extract_trade_async(text)
            if trade_data:
                asyncio.create_task(append_to_sheet_async(trade_data, formatted_time))
        except Exception as e:
            logger.error(f"Equity trade extraction failed: {e}", exc_info=True)
    
    return checkpoint_safe


async def process_telegram_message(message, channel_id):
    """Serialize messages per channel and never checkpoint past a failed message."""
    mapping = CHANNEL_MAP.get(channel_id)
    mode_token = _log_mode.set(_mode(mapping) if mapping else "test")
    lock = _channel_locks.setdefault(channel_id, asyncio.Lock())
    try:
        async with lock:
            try:
                checkpoint_safe = await _process_telegram_message_inner(message, channel_id)
            except Exception as exc:
                checkpoint_safe = False
                logger.error(
                    "Message %s from channel %s failed before durable processing; checkpoint held: %s",
                    message.id, channel_id, exc, exc_info=True,
                )
                asyncio.create_task(send_trade_log(
                    f"🚨 SIGNAL PIPELINE ERROR\nchannel={getattr(mapping, 'label', channel_id)} "
                    f"message_id={message.id}\ncheckpoint held; message should retry after restart.\n"
                    f"error={str(exc)[:1200]}",
                    mode=_mode(mapping) if mapping else "test",
                ))

            pending = _uncheckpointed_messages.setdefault(channel_id, set())
            if checkpoint_safe:
                pending.discard(message.id)
            else:
                pending.add(message.id)

            blocked_by_earlier_failure = any(mid <= message.id for mid in pending)
            previous_checkpoint = read_checkpoint(channel_id)
            if not blocked_by_earlier_failure and (previous_checkpoint is None or message.id > previous_checkpoint):
                write_checkpoint(channel_id, message.id)
            elif blocked_by_earlier_failure:
                logger.warning(
                    "Holding checkpoint for channel %s at %s because message IDs %s need retry",
                    channel_id, previous_checkpoint, sorted(mid for mid in pending if mid <= message.id),
                )


    finally:
        _log_mode.reset(mode_token)

async def handle_fno_message(text: str, message, signal_id: str, mapping) -> bool | None:
    """Parse an option signal and persist it to the SQLite queue.

    Returns True if the message was an FnO signal (so the equity path is skipped).
    """
    t_start = time.perf_counter_ns()
    try:
        fno_signal = await hybrid_fno_extract(text)
    except Exception as e:
        logger.error(f"FnO extraction failed: {e}", exc_info=True)
        asyncio.create_task(send_trade_log(f"[X] FnO extraction error: {e}"))
        return None
    extract_ms = (time.perf_counter_ns() - t_start) / 1_000_000

    if not fno_signal:
        logger.debug(f"[{mapping.label}] No FnO signal found ({extract_ms:.1f}ms)")
        return False

    desc = (
        f"{fno_signal['symbol']} {fno_signal['strike']:g}{fno_signal['option_type']} "
        f"{fno_signal.get('expiry_month') or ''} @ {fno_signal['entry_min']:g}-{fno_signal['entry_max']:g}"
        + (f" SL {fno_signal['stop_loss']:g}" if fno_signal.get('stop_loss') else "")
        + (f" T {','.join(f'{t:g}' for t in fno_signal['targets'])}" if fno_signal.get('targets') else "")
    )

    age_sec = (datetime.now(timezone.utc) - message.date).total_seconds()
    if age_sec > FNO_MAX_SIGNAL_AGE_SEC:
        logger.warning(f"[{mapping.label}] Stale FnO signal ({age_sec:.0f}s old), not broadcasting: {desc}")
        asyncio.create_task(send_trade_log(f"[⏱️] Stale FnO signal skipped ({age_sec:.0f}s old): {desc}"))
        return True

    mode = _mode(mapping)
    fno_signal.update({
        "asset_class": "FNO", "signal_id": signal_id,
        "channel_label": mapping.label, "message_ts": message.date.isoformat(),
        "mode": mode,
        "_latency_ms": {"extraction": round(extract_ms, 2), "source": fno_signal["source"]},
    })
    queued = False
    if _live_queue_allowed(mapping):
        queued = await asyncio.to_thread(enqueue_signal, fno_signal)
    total_ms = (time.perf_counter_ns() - t_start) / 1_000_000
    if mode == "test":
        queue_status = "TEST ONLY — parsed successfully; NOT queued for execution"
    elif not LIVE_TRADING_ENABLED:
        queue_status = "LIVE TRADING DISABLED — set LIVE_TRADING_ENABLED=true to allow queueing"
    else:
        queue_status = "inserted" if queued else "duplicate ignored"
    logger.info("[FNO %s] %s | extract=%.1fms (%s) | queue=%s | total=%.1fms",
                mode.upper(), desc, extract_ms, fno_signal['source'], queue_status, total_ms)
    asyncio.create_task(send_trade_log(
        f"{'🧪 TEST PARSED' if mode == 'test' else '🚦 LIVE SIGNAL'} | FNO ({fno_signal['source']}): {desc}\n"
        f"Status: {queue_status}", mode=mode,
    ))
    return True


