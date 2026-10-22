import os
import json
from dataclasses import dataclass
from dotenv import load_dotenv

# Load the broadcaster environment explicitly. Do not parse sys.argv: when started
# through `uvicorn main:app`, argv[1] is `main:app`, not an environment file.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_file = os.environ.get("DARVAS_ENV_FILE", os.path.join(BASE_DIR, ".env"))
load_dotenv(env_file, override=False)
print(f"Loaded configuration from {env_file}")

# Telegram API Credentials
API_ID_STR = os.environ.get('TG_API_ID', '')
API_ID = int(API_ID_STR) if API_ID_STR else 0
API_HASH = os.environ.get('TG_API_HASH', '')

# Target Channel (legacy single-channel config, kept for backward compatibility)
TARGET_CHANNEL_ID_STR = os.environ.get('TG_TARGET_CHANNEL_ID', '')
TARGET_CHANNEL_ID = int(TARGET_CHANNEL_ID_STR) if TARGET_CHANNEL_ID_STR else 0

# String Session
SESSION_STRING = os.environ.get('TG_SESSION_STRING', '')
SESSION_NAME = 'userbot_session'

# Email Configuration
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT_STR = os.environ.get('SMTP_PORT', '587')
SMTP_PORT = int(SMTP_PORT_STR) if SMTP_PORT_STR else 587
SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', SMTP_USERNAME)
EMAIL_TO = os.environ.get('EMAIL_TO', '')

# Notion Configuration
NOTION_API_KEY = os.environ.get('NOTION_API_KEY', '')
NOTION_PAGE_ID = os.environ.get('NOTION_PAGE_ID', '')

# Discord Configuration (legacy single-webhook, kept for backward compatibility)
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')

# Vertex AI Configuration
VERTEX_PROJECT_ID = os.environ.get('VERTEX_PROJECT_ID', '')
VERTEX_LOCATION = os.environ.get('VERTEX_LOCATION', 'us-central1')  # legacy; Gemini uses GEMINI_LOCATION
# Gemini endpoint. 'global' serves every current Flash model (incl. the gemini-3.x fallback, which
# 404s in us-central1) and routes to available capacity. Override only if you need data residency.
GEMINI_LOCATION = os.environ.get('GEMINI_LOCATION', 'global')

# Google Sheets Configuration
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')

# Telegram log destinations. A Bot API token is preferred so all services can log.
# LOG_GROUP_ID is kept as the legacy live-log target for the Telethon user session.
TELEGRAM_LOG_BOT_TOKEN = os.environ.get('TELEGRAM_LOG_BOT_TOKEN', os.environ.get('TG_LOG_BOT_TOKEN', '')).strip()
TEST_LOG_GROUP_ID = os.environ.get('TELEGRAM_TEST_LOG_CHAT_ID', os.environ.get('TEST_LOG_GROUP_ID', '')).strip()
LIVE_LOG_GROUP_ID = os.environ.get('TELEGRAM_LIVE_LOG_CHAT_ID', os.environ.get('LIVE_LOG_GROUP_ID', os.environ.get('LOG_GROUP_ID', ''))).strip()
LOG_GROUP_ID_STR = os.environ.get('LOG_GROUP_ID', '')
try:
    LOG_GROUP_ID = int(LOG_GROUP_ID_STR) if LOG_GROUP_ID_STR else 0
except ValueError:
    LOG_GROUP_ID = LOG_GROUP_ID_STR

# Fail-closed: live signals are never put into the execution queue unless this is true.
LIVE_TRADING_ENABLED = os.environ.get('LIVE_TRADING_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes')

# Inject Google Cloud Credentials for Vertex AI & Google Sheets
# Only set if the file exists AND is non-empty (an empty file causes SDK errors)
if os.path.exists('credentials.json') and os.path.getsize('credentials.json') > 0:
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'

# --- Telegram source-channel policies ---
# CHANNEL_MAPPINGS lists input channels the listener reads. Each source has its own
# test/live mode and optional equity/FnO parsing flags. If absent, the legacy single
# TG_TARGET_CHANNEL_ID is allowed as a TEST-only source for backward compatibility.

def _config_bool(value, field_name: str) -> bool:
    """Parse JSON boolean-like values without Python's bool("false") trap."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError(f"{field_name} must be a boolean (true/false), got {value!r}")


@dataclass
class ChannelMapping:
    telegram_channel_id: int
    discord_webhook_url: str
    label: str
    enable_trading: bool = False  # Opt-in: only broadcast trade signals for this channel
    enable_fno_trading: bool = False  # Opt-in: parse option (FnO) signals for this channel
    mode: str = 'test'  # 'test' parses/logs only; 'live' can queue only with LIVE_TRADING_ENABLED=true

def _parse_channel_mappings():
    """Parse source-channel policy. Invalid explicit configuration fails closed."""
    raw = os.environ.get('CHANNEL_MAPPINGS', '').strip()

    if raw:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CHANNEL_MAPPINGS is not valid JSON: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise ValueError("CHANNEL_MAPPINGS must be a non-empty JSON array")

        mappings = []
        seen_ids = set()
        seen_labels = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"CHANNEL_MAPPINGS[{index}] must be an object")
            try:
                tg_id = int(entry['telegram_channel_id'])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"CHANNEL_MAPPINGS[{index}] needs an integer telegram_channel_id") from exc
            if tg_id == 0:
                raise ValueError(f"CHANNEL_MAPPINGS[{index}].telegram_channel_id cannot be zero")
            if tg_id in seen_ids:
                raise ValueError(f"Duplicate telegram_channel_id in CHANNEL_MAPPINGS: {tg_id}")
            seen_ids.add(tg_id)

            webhook = str(entry.get('discord_webhook_url', '') or '').strip()
            label = str(entry.get('label', str(tg_id)) or str(tg_id)).strip()
            if not label:
                raise ValueError(f"CHANNEL_MAPPINGS[{index}].label cannot be empty")
            label_key = label.casefold()
            if label_key in seen_labels:
                raise ValueError(f"Duplicate channel label in CHANNEL_MAPPINGS: {label}")
            seen_labels.add(label_key)
            enable_trading = _config_bool(entry.get('enable_trading', False), 'enable_trading')
            enable_fno_trading = _config_bool(entry.get('enable_fno_trading', False), 'enable_fno_trading')
            mode = str(entry.get('mode', 'test')).strip().lower()
            if mode not in {'test', 'live'}:
                raise ValueError(f"channel mode must be 'test' or 'live', got {mode!r}")
            mappings.append(ChannelMapping(
                telegram_channel_id=tg_id,
                discord_webhook_url=webhook,
                label=label,
                enable_trading=enable_trading,
                enable_fno_trading=enable_fno_trading,
                mode=mode,
            ))
        return mappings

    # Backwards compatibility for deployments with only the legacy single-source setting.
    if TARGET_CHANNEL_ID:
        return [ChannelMapping(
            telegram_channel_id=TARGET_CHANNEL_ID,
            discord_webhook_url=DISCORD_WEBHOOK_URL,
            label='default',
            mode='test',
        )]
    return []

CHANNEL_MAPPINGS = _parse_channel_mappings()
# O(1) lookup: telegram_channel_id → ChannelMapping
CHANNEL_MAP = {m.telegram_channel_id: m for m in CHANNEL_MAPPINGS}
