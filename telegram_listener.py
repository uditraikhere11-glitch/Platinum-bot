"""Dedicated Telethon listener process.

Run as: python telegram_listener.py
This service only ingests Telegram messages and persists approved live signals to SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime

from config import API_ID, API_HASH, SESSION_STRING, SESSION_NAME, CHANNEL_MAPPINGS, CHANNEL_MAP, LIVE_TRADING_ENABLED
from signal_queue import initialize_queue, default_database_path
from services.scrip_service import load_valid_scrips
from services.telegram_log_service import send_telegram_log
from signal_pipeline import process_telegram_message, configure_telegram_client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_listener")


def create_telegram_client():
    from telethon import TelegramClient
    if SESSION_STRING:
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    return TelegramClient(SESSION_NAME, API_ID, API_HASH)


async def run_listener():
    if not API_ID or not API_HASH:
        raise RuntimeError("Telegram API config incomplete: set TG_API_ID and TG_API_HASH in root .env")
    if not CHANNEL_MAPPINGS:
        raise RuntimeError("No channel mappings configured. Set CHANNEL_MAPPINGS in root .env")

    db_path = initialize_queue()
    logger.info("SQLite signal queue initialized: %s", db_path)
    logger.info("LIVE_TRADING_ENABLED=%s (default is false)", LIVE_TRADING_ENABLED)
    for mapping in CHANNEL_MAPPINGS:
        logger.info("Channel [%s] id=%s mode=%s equity=%s FnO=%s Discord=%s",
                    mapping.label, mapping.telegram_channel_id, mapping.mode,
                    mapping.enable_trading, mapping.enable_fno_trading,
                    bool(mapping.discord_webhook_url))

    # Scrip master loading is non-critical to listener boot; keep it off the event loop.
    try:
        await asyncio.to_thread(load_valid_scrips)
    except Exception:
        logger.exception("Scrip master initialization failed; listener will continue")

    from telethon import events
    client = create_telegram_client()
    configure_telegram_client(client)
    channel_ids = [mapping.telegram_channel_id for mapping in CHANNEL_MAPPINGS]

    @client.on(events.NewMessage(chats=channel_ids))
    async def channel_handler(event):
        try:
            await process_telegram_message(event.message, event.chat_id)
        except Exception:
            logger.exception("Message handler failed for channel=%s message=%s", event.chat_id, event.message.id)

    @client.on(events.NewMessage())
    async def unknown_channel_debug(event):
        if event.chat_id not in CHANNEL_MAP:
            logger.debug("Message received from unconfigured chat_id=%s (message content suppressed)", event.chat_id)

    try:
        logger.info("Connecting Telegram session...")
        await client.start()
        logger.info("Telegram session connected")

        # Startup/catch-up should not replay old messages into execution: parser age checks apply.
        for mapping in CHANNEL_MAPPINGS:
            channel_id = mapping.telegram_channel_id
            from state_manager import read_checkpoint, write_checkpoint
            last_id = read_checkpoint(channel_id)
            if last_id is not None:
                missed = []
                async for message in client.iter_messages(channel_id, min_id=last_id, reverse=True):
                    missed.append(message)
                logger.info("[%s] catch-up has %s message(s)", mapping.label, len(missed))
                for message in missed:
                    await process_telegram_message(message, channel_id)
            else:
                latest = await client.get_messages(channel_id, limit=1)
                if latest:
                    write_checkpoint(channel_id, latest[0].id)
                    logger.info("[%s] initialized checkpoint at %s (starting from newest)", mapping.label, latest[0].id)

        modes = sorted({mapping.mode for mapping in CHANNEL_MAPPINGS})
        logger.info("Listening for live Telegram messages; mode(s)=%s", ", ".join(modes))
        test_channels = [m.label for m in CHANNEL_MAPPINGS if m.mode == "test"]
        live_channels = [m.label for m in CHANNEL_MAPPINGS if m.mode == "live"]
        await send_telegram_log(
            "🟢 Darvas Telegram listener started\n"
            f"Test channels: {', '.join(test_channels) or 'none'}\n"
            f"Live channels: {', '.join(live_channels) or 'none'}\n"
            f"Live queue gate: {'ENABLED' if LIVE_TRADING_ENABLED else 'DISABLED (safe default)'}\n"
            f"Queue DB: {db_path}", mode="test", telethon_client=client,
        )
        if live_channels:
            await send_telegram_log(
                "🟢 Darvas LIVE channel listener started\n"
                f"Channels: {', '.join(live_channels)}\n"
                f"LIVE_TRADING_ENABLED={LIVE_TRADING_ENABLED}; executor DRY_RUN must also be reviewed.",
                mode="live", telethon_client=client,
            )
        await client.run_until_disconnected()
    finally:
        logger.info("Disconnecting Telegram session")
        await client.disconnect()
        configure_telegram_client(None)


def main():
    try:
        asyncio.run(run_listener())
    except KeyboardInterrupt:
        logger.info("Listener stopped by operator")


if __name__ == "__main__":
    main()
