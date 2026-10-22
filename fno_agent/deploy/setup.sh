#!/usr/bin/env bash
# Set up (or change the settings of) the OPTIONS (FnO) bot on the VM. Safe to run again any time.
#
#   bash fno_agent/deploy/setup.sh        (or: bot setup fno)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO_DIR/deploy/lib.sh"
require_not_root fno_agent/deploy/setup.sh
cd "$REPO_DIR"

if ! is_installed listener || ! systemctl is-active -q darvas-telegram-listener; then
    fail "Start the Telegram listener first: bash deploy/setup_telegram_listener.sh"
    exit 1
fi

ENV="$(env_of fno)"; OTHER="$(env_of equity)"

bold "=== Options (FnO) bot setup ==="
echo "Press Enter to keep the value shown in [brackets]. Secret answers stay invisible while you type."
echo

system_setup fno_agent/requirements.txt
if [ ! -f "$ENV" ]; then
    cp fno_agent/.env.example "$ENV"
    set_env FNO_MAX_LOTS 1 "$ENV"   # first setup: suggest 1 lot until the first live trade is checked
fi
chmod 600 "$ENV"

echo
echo " Shared SQLite signal queue (must match the Telegram listener and equity worker):"
DB_PATH="$(first_set "$(get_env DARVAS_SIGNAL_DB "$REPO_DIR/.env")" "$(get_env DARVAS_SIGNAL_DB "$OTHER")" "$(get_env DARVAS_SIGNAL_DB "$ENV")" "/var/lib/darvas/signals.db")"
ask_required DB_PATH "Absolute path to the shared queue database on this VM" "$DB_PATH"
while [[ "$DB_PATH" != /* ]]; do warn "Use an absolute path so all services open the same database."; ask_required DB_PATH "Absolute path to the shared queue database on this VM" "/var/lib/darvas/signals.db"; done
set_env DARVAS_SIGNAL_DB "$DB_PATH" "$ENV"
ask POLL_SECONDS "Queue polling interval in seconds (minimum 0.1)" "$(get_env SIGNAL_QUEUE_POLL_SECONDS "$ENV")"
set_env SIGNAL_QUEUE_POLL_SECONDS "${POLL_SECONDS:-0.25}" "$ENV"
sudo install -d -o "$(whoami)" -g "$(id -gn)" -m 750 "$(dirname "$DB_PATH")"

echo
ask_dhan "$ENV" "$OTHER"

echo
ask_required AMOUNT "Rupees to use per option trade (the bot buys as many whole lots as fit)" "$(get_env FNO_CAPITAL_PER_TRADE "$ENV")"
ask MAXLOTS "Maximum lots per trade (0 = no limit; 1 is safest while testing)" "$(x=$(get_env FNO_MAX_LOTS "$ENV"); echo "${x:-0}")"
echo "  Optional: trade alerts on Telegram (press Enter to skip)."
ask TG_TOKEN "Telegram bot token (from @BotFather)" "$(get_env FNO_TG_BOT_TOKEN "$ENV")" secret
ask TG_CHAT  "Telegram chat id (from @userinfobot)"  "$(get_env FNO_TG_CHAT_ID "$ENV")"
set_env FNO_CAPITAL_PER_TRADE "$AMOUNT" "$ENV"
set_env FNO_MAX_LOTS "${MAXLOTS:-0}" "$ENV"
set_env FNO_TG_BOT_TOKEN "$TG_TOKEN" "$ENV"
set_env FNO_TG_CHAT_ID "$TG_CHAT" "$ENV"
current_sltp=$(get_env FNO_SL_TARGET_ENABLED "$ENV" | tr '[:upper:]' '[:lower:]')
if yes_no "Place a stop-loss and target at Dhan automatically? (recommended)" "$([ "$current_sltp" = false ] && echo n || echo y)"; then
    set_env FNO_SL_TARGET_ENABLED true "$ENV"
    ask_pct FNO_SL "Stop-loss: % below the executed price" "$(x=$(get_env FNO_SL_PCT "$ENV"); echo "${x:-3}")"
    ask_pct FNO_TGT "Target: % above the executed price" "$(x=$(get_env FNO_TARGET_PCT "$ENV"); echo "${x:-3}")"
    set_env FNO_SL_PCT "$FNO_SL" "$ENV"
    set_env FNO_TARGET_PCT "$FNO_TGT" "$ENV"
else
    set_env FNO_SL_TARGET_ENABLED false "$ENV"
    warn "Options trades will have NO stop-loss/target at Dhan. Manage exits yourself."
fi
[ -n "$(get_env DRY_RUN "$ENV")" ] || set_env DRY_RUN true "$ENV"
ok "Settings saved to fno_agent/.env"

echo
bold "Installing the options bot as a 24/7 service..."
install_unit fno_agent/deploy/fno-agent.service
sudo systemctl daemon-reload
sudo systemctl enable -q fno-agent
ok "Starts on boot, restarts if it crashes, refreshes its Dhan login and contract list every morning"

echo
bold "Starting the options worker and checking its SQLite queue (downloads the contract list first, up to 2 min)..."
if start_and_check fno 120; then
    echo
    bold "Done! Next steps:"
    echo "  bot today fno        see signals and orders (TEST mode shows what it WOULD buy)"
    echo "  bot live fno         when you're happy, start placing REAL option orders"
else
    echo
    bold "Something needs attention (see above). Check that darvas-telegram-listener is running and DARVAS_SIGNAL_DB matches in every service:"
    echo "  bot setup fno        re-enter the settings"
    exit 1
fi
