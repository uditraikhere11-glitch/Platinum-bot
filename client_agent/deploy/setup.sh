#!/usr/bin/env bash
# Set up (or change the settings of) the EQUITY bot on the VM. Safe to run again any time.
#
#   bash client_agent/deploy/setup.sh        (or: bot setup equity)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO_DIR/deploy/lib.sh"
require_not_root client_agent/deploy/setup.sh
cd "$REPO_DIR"

if ! is_installed listener || ! systemctl is-active -q darvas-telegram-listener; then
    fail "Start the Telegram listener first: bash deploy/setup_telegram_listener.sh"
    exit 1
fi

ENV="$(env_of equity)"; OTHER="$(env_of fno)"

bold "=== Equity bot setup ==="
echo "Press Enter to keep the value shown in [brackets]. Secret answers stay invisible while you type."
echo

system_setup client_agent/requirements.txt
[ -f "$ENV" ] || cp client_agent/.env.example "$ENV"
chmod 600 "$ENV"

echo
echo " Shared SQLite signal queue (must match the Telegram listener and FnO worker):"
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
ask_required AMOUNT "Rupees to invest per share trade" "$(get_env TRADE_AMOUNT_INR "$ENV")"
set_env TRADE_AMOUNT_INR "$AMOUNT" "$ENV"

echo
echo " Stop-loss and target (optional): attach both to every live-market buy at Dhan."
current_sltp=$(get_env EQUITY_SL_TARGET_ENABLED "$ENV" | tr '[:upper:]' '[:lower:]')
if yes_no "Place a stop-loss and target automatically?" "$([ "$current_sltp" = true ] && echo y || echo n)"; then
    ask_pct SL_PCT "Stop-loss: % below the executed price (e.g. 2)" "$(get_env EQUITY_SL_PCT "$ENV")"
    ask_pct TARGET_PCT "Target: % above the executed price" "$(x=$(get_env EQUITY_TARGET_PCT "$ENV"); echo "${x:-1}")"
    set_env EQUITY_SL_TARGET_ENABLED true "$ENV"
    set_env EQUITY_SL_PCT "$SL_PCT" "$ENV"
    set_env EQUITY_TARGET_PCT "$TARGET_PCT" "$ENV"
else
    set_env EQUITY_SL_TARGET_ENABLED false "$ENV"
fi
[ -n "$(get_env DRY_RUN "$ENV")" ] || set_env DRY_RUN true "$ENV"
ok "Settings saved to client_agent/.env"

echo
bold "Installing the equity bot as a 24/7 service..."
install_unit client_agent/deploy/trading-client.service
sudo cp client_agent/deploy/trading-client-restart.service client_agent/deploy/trading-client-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q trading-client
sudo systemctl enable -q --now trading-client-restart.timer
ok "Starts on boot, restarts if it crashes, fresh restart every weekday at 08:45 IST"

echo
bold "Starting the equity worker and checking its SQLite queue..."
if start_and_check equity 60; then
    echo
    bold "Done! Next steps:"
    echo "  bot today equity     see signals and orders (TEST mode shows what it WOULD buy)"
    echo "  bot live equity      when you're happy, start placing REAL share orders"
else
    echo
    bold "Something needs attention (see above). Check that darvas-telegram-listener is running and DARVAS_SIGNAL_DB matches in every service:"
    echo "  bot setup equity     re-enter the settings"
    exit 1
fi
