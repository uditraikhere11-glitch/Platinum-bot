#!/usr/bin/env bash
# Install/start the dedicated Telegram listener on the same VM as the equity and FnO workers.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_DIR/deploy/lib.sh"
require_not_root deploy/setup_telegram_listener.sh
cd "$REPO_DIR"

bold "=== Darvas Telegram listener setup ==="
system_setup requirements.txt
[ -f "$REPO_DIR/.env" ] || cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
chmod 600 "$REPO_DIR/.env"

DB_PATH="$(get_env DARVAS_SIGNAL_DB "$REPO_DIR/.env")"
DB_PATH="${DB_PATH:-/var/lib/darvas/signals.db}"
ask_required DB_PATH "Absolute shared SQLite queue path (same on all three services)" "$DB_PATH"
while [[ "$DB_PATH" != /* ]]; do warn "Use an absolute path so all services open the same database."; ask_required DB_PATH "Absolute shared SQLite queue path" "/var/lib/darvas/signals.db"; done
set_env DARVAS_SIGNAL_DB "$DB_PATH" "$REPO_DIR/.env"

# Prompt for central log routing so listener and both workers send notifications to the same places.
echo
bold "=== Telegram log destinations ==="
echo "Create a bot with @BotFather, add it to BOTH log groups/channels, and grant permission to post."
echo "Test logs receive test-source messages, parser previews and DRY_RUN outcomes."
echo "Live logs receive live-source decisions and real-execution notifications."
ask_required LOG_BOT_TOKEN "Telegram logging bot token" "$(get_env TELEGRAM_LOG_BOT_TOKEN "$REPO_DIR/.env")" secret
ask_required TEST_LOG_CHAT "TEST log group/channel ID (test input + dry-run executor outcomes)" "$(get_env TELEGRAM_TEST_LOG_CHAT_ID "$REPO_DIR/.env")"
ask_required LIVE_LOG_CHAT "LIVE log group/channel ID (live input + live executor outcomes)" "$(get_env TELEGRAM_LIVE_LOG_CHAT_ID "$REPO_DIR/.env")"
set_env TELEGRAM_LOG_BOT_TOKEN "$LOG_BOT_TOKEN" "$REPO_DIR/.env"
set_env TELEGRAM_TEST_LOG_CHAT_ID "$TEST_LOG_CHAT" "$REPO_DIR/.env"
set_env TELEGRAM_LIVE_LOG_CHAT_ID "$LIVE_LOG_CHAT" "$REPO_DIR/.env"

# Also align the worker queue paths; log credentials are read from the root .env as a fallback.
for worker_env in "$REPO_DIR/client_agent/.env" "$REPO_DIR/fno_agent/.env"; do
    if [ -f "$worker_env" ]; then set_env DARVAS_SIGNAL_DB "$DB_PATH" "$worker_env"; fi
done
sudo install -d -o "$(whoami)" -g "$(id -gn)" -m 750 "$(dirname "$DB_PATH")"

bold "Configure Telegram source channels in $REPO_DIR/.env"
echo "  nano $REPO_DIR/.env"
echo 'Use CHANNEL_MAPPINGS with one object per source channel and mode="test" or mode="live".'
echo 'Keep LIVE_TRADING_ENABLED=false for the first test. Omitted source mode defaults to test.'
echo
read -r -p "Have you saved TG_API_ID, TG_API_HASH, and valid CHANNEL_MAPPINGS in .env? [y/N] " answer
case "$answer" in [Yy]*) ;; *) warn "Not started. Edit .env, then rerun this script."; exit 1 ;; esac

# Validate source mappings before installing or starting the long-running listener.
if ! DARVAS_ENV_FILE="$REPO_DIR/.env" "$REPO_DIR/.venv/bin/python" -c 'import config; assert config.CHANNEL_MAPPINGS, "No CHANNEL_MAPPINGS configured"; print(f"Validated {len(config.CHANNEL_MAPPINGS)} Telegram source channel(s)")'; then
    fail "Source-channel configuration is invalid. Fix $REPO_DIR/.env and rerun this script."
    exit 1
fi

# Stop the old Uvicorn-based broadcaster before starting the separate Telethon listener.
# The old unit can otherwise retain a Telethon session while the new unit tries to connect.
if systemctl cat darvas-broadcaster.service >/dev/null 2>&1; then
    warn "Stopping obsolete darvas-broadcaster.service to avoid a duplicate Telegram session."
    sudo systemctl disable --now darvas-broadcaster.service || true
fi

install_unit deploy/darvas-telegram-listener.service
sudo systemctl daemon-reload
sudo systemctl enable --now darvas-telegram-listener

bold "Waiting for Telegram listener..."
for _ in $(seq 1 30); do
    if sudo systemctl is-active --quiet darvas-telegram-listener; then
        recent_logs=$(sudo journalctl -u darvas-telegram-listener --since "2 minutes ago" --no-pager 2>/dev/null || true)
        if grep -F "Telegram session connected" <<<"$recent_logs" >/dev/null && grep -F "Listening for live Telegram messages" <<<"$recent_logs" >/dev/null; then
            ok "Telegram listener ready; SQLite queue: $DB_PATH"
            echo "Check status: sudo systemctl status darvas-telegram-listener"
            echo "Logs:         sudo journalctl -u darvas-telegram-listener -f"
            exit 0
        fi
    fi
    sleep 1
done
fail "Telegram listener did not become ready. Last logs:"
sudo journalctl -u darvas-telegram-listener -n 70 --no-pager
exit 1
