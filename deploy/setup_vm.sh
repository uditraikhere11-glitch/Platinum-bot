#!/usr/bin/env bash
# Guided, chronological setup for the Darvas Telegram listener + equity + FnO agents.
# Run as the normal VM user (not with sudo): bash deploy/setup_vm.sh
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_DIR/deploy/lib.sh"
require_not_root deploy/setup_vm.sh

bold "DARVAS GUIDED SETUP"
echo "This wizard runs each stage in order and shows which .env file belongs to it."
echo
echo "Stage 1 — Telegram listener + SQLite queue"
echo "  Environment: $REPO_DIR/.env"
echo "  Contains: Telegram API/session, CHANNEL_MAPPINGS, shared DB path, log bot/chat IDs, global live gate."
echo
echo "Stage 2 — Equity executor (optional)"
echo "  Environment: $REPO_DIR/client_agent/.env"
echo "  Contains: broker credentials, equity budget, dry-run mode and protective exits."
echo
echo "Stage 3 — FnO executor (optional)"
echo "  Environment: $REPO_DIR/fno_agent/.env"
echo "  Contains: broker credentials, option capital/lot limits, dry-run mode, expiry and exits."
echo
echo "The three services must share one absolute DARVAS_SIGNAL_DB path."
echo "This wizard refuses to continue if the global live gate is enabled or a selected executor is already set for real orders."
echo

# The installers write files in the checkout and chmod the bot launcher. Detect the
# earlier two-Linux-account ownership problem before running a partial deployment.
if [[ ! -w "$REPO_DIR" || ! -O "$REPO_DIR/bot" ]]; then
    fail "The checkout is not writable/owned by the current Linux account: $REPO_DIR"
    echo "Your repository was previously owned by a different Linux account. Fix it once with:"
    printf '  sudo chown -R %q:%q %q\n' "$(id -un)" "$(id -gn)" "$REPO_DIR"
    echo "Then secure the secrets file with: chmod 600 $REPO_DIR/.env"
    exit 1
fi
for existing_env in "$REPO_DIR/.env" "$REPO_DIR/client_agent/.env" "$REPO_DIR/fno_agent/.env"; do
    if [[ -e "$existing_env" && ! -w "$existing_env" ]]; then
        fail "Cannot write $existing_env as $(id -un). Fix its ownership/permissions before setup."
        echo "No services have been started by this wizard."
        exit 1
    fi
done

# The global queue gate must remain fail-closed during installation.
LIVE_GATE="$(get_env LIVE_TRADING_ENABLED "$REPO_DIR/.env" | tr '[:upper:]' '[:lower:]')"
case "$LIVE_GATE" in
    true|1|yes|on)
        fail "LIVE_TRADING_ENABLED is enabled in $REPO_DIR/.env."
        echo "Set LIVE_TRADING_ENABLED=false before setup. No setup stage was started."
        exit 1
        ;;
esac

DO_LISTENER=n
DO_EQUITY=n
DO_FNO=n
if yes_no "Configure Telegram listener and shared SQLite queue? (uses root .env)" y; then DO_LISTENER=y; fi
if yes_no "Configure EQUITY executor? (uses client_agent/.env)" y; then DO_EQUITY=y; fi
if yes_no "Configure FnO executor? (uses fno_agent/.env)" "$(is_installed fno && echo y || echo n)"; then DO_FNO=y; fi

if [[ "$DO_LISTENER" != y && ( "$DO_EQUITY" = y || "$DO_FNO" = y ) ]]; then
    warn "An executor requires the Telegram listener and shared queue first."
    DO_LISTENER=y
fi
if [[ "$DO_LISTENER" != y && "$DO_EQUITY" != y && "$DO_FNO" != y ]]; then
    warn "Nothing selected; no setup stage was run."
    exit 0
fi

# Never start a selected executor whose current env already permits real broker orders.
# A missing DRY_RUN is okay on first install: each worker installer initializes it to true.
for pair in "equity:$DO_EQUITY:$REPO_DIR/client_agent/.env" "fno:$DO_FNO:$REPO_DIR/fno_agent/.env"; do
    IFS=: read -r agent selected env_file <<< "$pair"
    [[ "$selected" = y && -f "$env_file" ]] || continue
    DRY_VALUE="$(get_env DRY_RUN "$env_file" | tr '[:upper:]' '[:lower:]')"
    case "$DRY_VALUE" in
        false|0|no|off)
            fail "$agent executor is configured for real orders in $env_file (DRY_RUN=false)."
            echo "Set DRY_RUN=true in that file before running this setup wizard."
            echo "No setup stage was started."
            exit 1
            ;;
    esac
done

STATUS=0
if [[ "$DO_LISTENER" = y ]]; then
    echo
    bold "STAGE 1/3 — Telegram listener and shared queue"
    echo "Editing target: $REPO_DIR/.env"
    echo "Before confirming inside the installer, ensure TG_API_ID, TG_API_HASH, TG_SESSION_STRING,"
    echo "and at least one valid CHANNEL_MAPPINGS entry are saved in this file."
    echo "Use LIVE_TRADING_ENABLED=false throughout testing."
    if bash "$REPO_DIR/deploy/setup_telegram_listener.sh"; then
        ok "Stage 1 complete: Telegram listener/SQLite queue installed."
    else
        fail "Stage 1 failed. Equity/FnO setup is skipped because both require the listener."
        exit 1
    fi
fi

if [[ "$DO_EQUITY" = y ]]; then
    echo
    bold "STAGE 2/3 — Equity executor"
    echo "Editing target: $REPO_DIR/client_agent/.env"
    echo "Enter broker details, rupee budget, stop-loss/target preferences, and keep DRY_RUN=true."
    if bash "$REPO_DIR/client_agent/deploy/setup.sh"; then
        ok "Stage 2 complete: equity executor setup finished."
    else
        fail "Equity executor setup failed; see messages above."
        STATUS=1
    fi
fi

if [[ "$DO_FNO" = y ]]; then
    echo
    bold "STAGE 3/3 — FnO executor"
    echo "Editing target: $REPO_DIR/fno_agent/.env"
    echo "Enter broker details, capital per trade, max lots, expiry guard and protective exits."
    echo "Keep DRY_RUN=true and start with a conservative FNO_MAX_LOTS value (typically 1)."
    if bash "$REPO_DIR/fno_agent/deploy/setup.sh"; then
        ok "Stage 3 complete: FnO executor setup finished."
    else
        fail "FnO executor setup failed; see messages above."
        STATUS=1
    fi
fi

echo
bold "FINAL CHECK"
echo "Configured environment files (values are not printed):"
for env_file in "$REPO_DIR/.env" "$REPO_DIR/client_agent/.env" "$REPO_DIR/fno_agent/.env"; do
    [[ -f "$env_file" ]] && echo "  present: $env_file" || echo "  not created: $env_file"
done
echo
echo "Verify shared queue paths (must match):"
for env_file in "$REPO_DIR/.env" "$REPO_DIR/client_agent/.env" "$REPO_DIR/fno_agent/.env"; do
    [[ -f "$env_file" ]] || continue
    db_value="$(get_env DARVAS_SIGNAL_DB "$env_file")"
    printf '  %s -> %s\n' "$env_file" "${db_value:-NOT SET}"
done
echo "Live queue gate must remain disabled until a separate, deliberate live-readiness review."
if command -v bot >/dev/null 2>&1; then
    echo
    bot status || STATUS=1
fi
exit "$STATUS"
