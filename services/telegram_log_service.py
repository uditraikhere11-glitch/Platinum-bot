"""Telegram status/trade logger shared by ingestion and execution services.

Configure TELEGRAM_LOG_BOT_TOKEN plus TELEGRAM_TEST_LOG_CHAT_ID and
TELEGRAM_LIVE_LOG_CHAT_ID. The listener can fall back to its Telethon account
when no Bot API token is configured; execution agents should use the Bot API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from dotenv import dotenv_values

import requests

logger = logging.getLogger(__name__)
_configured_env = (os.environ.get("DARVAS_ENV_FILE") or "").strip()
ROOT_ENV = (Path(_configured_env).expanduser().resolve() if _configured_env
            else Path(__file__).resolve().parents[1] / ".env")


def _setting(*names: str) -> str:
    # Local worker .env values win; blank worker values fall back to the repo root .env.
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        root_values = dotenv_values(ROOT_ENV)
        for name in names:
            value = (root_values.get(name) or "").strip()
            if value:
                return value
    except Exception:
        pass
    return ""


def telegram_log_credentials(mode: str = "live") -> tuple[str, str]:
    """Return (bot token, chat id) for the requested TEST or LIVE log route.

    Invalid modes deliberately return no destination instead of risking cross-routing
    a message into a log chat with a different safety purpose.
    """
    token = _setting("TELEGRAM_LOG_BOT_TOKEN", "TG_LOG_BOT_TOKEN")
    normalized = str(mode or "test").strip().lower()
    if normalized == "test":
        chat = _setting("TELEGRAM_TEST_LOG_CHAT_ID", "TEST_LOG_GROUP_ID")
    elif normalized == "live":
        chat = _setting("TELEGRAM_LIVE_LOG_CHAT_ID", "LIVE_LOG_GROUP_ID", "LOG_GROUP_ID")
    else:
        logger.error("Refusing Telegram log delivery with invalid mode=%r", mode)
        chat = ""
    return token, chat


def _chat_id(mode: str) -> str:
    return telegram_log_credentials(mode)[1]


def send_telegram_log_sync(message: str, mode: str = "live") -> bool:
    """Send a log message through Telegram Bot API; return False if not configured."""
    token, chat_id = telegram_log_credentials(mode)
    if not token or not chat_id:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": str(message)[:3900], "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok", False):
            raise RuntimeError(f"Telegram Bot API returned ok=false: {body}")
        return True
    except Exception as exc:
        # Avoid logging HTTP exception strings: the Bot API URL contains the bot token.
        logger.warning("Telegram %s log delivery failed (%s)", mode, type(exc).__name__)
        return False


async def send_telegram_log(message: str, mode: str = "live", telethon_client: Any = None) -> bool:
    """Send using the Bot API; optionally fall back to Telethon in the listener process."""
    if await asyncio.to_thread(send_telegram_log_sync, message, mode):
        return True
    chat_id = _chat_id(mode)
    if telethon_client is not None and chat_id and telethon_client.is_connected():
        try:
            await telethon_client.send_message(chat_id, str(message)[:3900], link_preview=False)
            return True
        except Exception as exc:
            logger.warning("Telegram Telethon %s log delivery failed: %s", mode, exc)
    logger.info("Telegram %s log (not delivered; destination/token missing): %s", mode, str(message)[:500])
    return False
