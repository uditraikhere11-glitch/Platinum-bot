"""Telegram Bot API notifications for trade events (falls back to logging only)."""

import asyncio
import logging

import requests

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", prefix: str = "[FnO]"):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.prefix = prefix

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _post(self, text: str):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:3900], "disable_web_page_preview": True},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("ok", False):
                raise RuntimeError(f"Telegram Bot API returned ok=false: {body}")
        except Exception as e:
            # requests exceptions may include the Bot API URL, which embeds the token.
            logger.error("Telegram notify failed (%s)", type(e).__name__)

    async def send(self, text: str):
        text = f"{self.prefix} {text}"
        logger.info(f"NOTIFY: {text}")
        if self.enabled:
            await asyncio.to_thread(self._post, text)
