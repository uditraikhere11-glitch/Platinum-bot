"""Environment-driven settings for the FnO agent."""

import os
from dataclasses import dataclass, field
from typing import List

from signal_queue import default_database_path
from services.telegram_log_service import telegram_log_credentials

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


@dataclass
class Settings:
    broker: str = "dhan"
    dry_run: bool = True

    signal_db_path: str = field(default_factory=default_database_path)
    queue_poll_seconds: float = 0.25
    max_concurrent_signals: int = 1
    max_signal_age_sec: int = 120
    allowed_channels: List[str] = field(default_factory=list)

    # Sizing
    capital_per_trade: float = 0.0
    max_lots: int = 0  # 0 = no cap

    # Execution
    chase_pct: float = 3.0  # skip if LTP > entry_max by more than this %
    entry_timeout_sec: int = 300
    poll_interval_sec: float = 2.0
    min_days_to_expiry: int = 1
    sl_limit_buffer_pct: float = 5.0  # Kite GTT SL leg: limit price this % below trigger
    sl_target_enabled: bool = True  # place SL/target at the broker (FNO_SL_TARGET_ENABLED)
    sl_pct: float = 3.0  # stop-loss this % below the entry price (FNO_SL_PCT)
    target_pct: float = 3.0  # single target this % above the entry price (FNO_TARGET_PCT)
    enforce_market_hours: bool = True

    # Dhan
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_pin: str = ""
    dhan_totp_secret: str = ""

    # Kite
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""
    kite_token_file: str = os.path.join(AGENT_DIR, ".kite_token")

    # Notifications
    tg_bot_token: str = ""
    tg_chat_id: str = ""

    journal_path: str = os.path.join(AGENT_DIR, "fno_journal.db")
    holidays_file: str = os.path.join(AGENT_DIR, "holidays.json")
    log_dir: str = os.path.join(AGENT_DIR, "logs")

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        dry_run = _bool("DRY_RUN", "true")
        log_mode = "test" if dry_run else "live"
        shared_tg_token, shared_tg_chat = telegram_log_credentials(log_mode)
        return cls(
            broker=e("BROKER", "dhan").strip().lower(),
            dry_run=dry_run,
            signal_db_path=e("DARVAS_SIGNAL_DB", default_database_path()),
            queue_poll_seconds=max(0.1, float(e("SIGNAL_QUEUE_POLL_SECONDS", "0.25"))),
            max_concurrent_signals=max(1, int(e("FNO_MAX_CONCURRENT_SIGNALS", "1"))),
            max_signal_age_sec=max(1, int(e("FNO_MAX_SIGNAL_AGE_SEC", "120"))),
            allowed_channels=[c.strip().lower() for c in e("ALLOWED_CHANNELS", "").split(",") if c.strip()],
            capital_per_trade=float(e("FNO_CAPITAL_PER_TRADE", "0")),
            max_lots=int(e("FNO_MAX_LOTS", "0")),
            chase_pct=float(e("FNO_CHASE_PCT", "3")),
            entry_timeout_sec=int(e("FNO_ENTRY_TIMEOUT_SEC", "300")),
            min_days_to_expiry=int(e("FNO_MIN_DAYS_TO_EXPIRY", "1")),
            sl_limit_buffer_pct=float(e("FNO_SL_LIMIT_BUFFER_PCT", "5")),
            sl_target_enabled=_bool("FNO_SL_TARGET_ENABLED", "true"),
            sl_pct=float(e("FNO_SL_PCT", "3") or 3),
            target_pct=float(e("FNO_TARGET_PCT", "3") or 3),
            enforce_market_hours=_bool("FNO_ENFORCE_MARKET_HOURS", "true"),
            dhan_client_id=e("DHAN_CLIENT_ID", ""),
            dhan_access_token=e("DHAN_ACCESS_TOKEN", ""),
            dhan_pin=e("DHAN_PIN", ""),
            dhan_totp_secret=e("DHAN_TOTP_SECRET", ""),
            kite_api_key=e("KITE_API_KEY", ""),
            kite_api_secret=e("KITE_API_SECRET", ""),
            kite_access_token=e("KITE_ACCESS_TOKEN", ""),
            kite_token_file=e("KITE_TOKEN_FILE", os.path.join(AGENT_DIR, ".kite_token")),
            tg_bot_token=shared_tg_token or e("FNO_TG_BOT_TOKEN", ""),
            tg_chat_id=shared_tg_chat or e("FNO_TG_CHAT_ID", ""),
            journal_path=e("FNO_JOURNAL_PATH", os.path.join(AGENT_DIR, "fno_journal.db")),
        )
