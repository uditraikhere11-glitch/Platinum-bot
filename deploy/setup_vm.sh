#!/usr/bin/env bash
# Convenience wrapper: set up one or both bots on the VM.
# Each bot also has its own setup script:
#   bash deploy/setup_telegram_listener.sh   (Telegram listener)
#   bash client_agent/deploy/setup.sh     (equity bot)   see client_agent/DEPLOY.md
#   bash fno_agent/deploy/setup.sh        (options bot)  see fno_agent/DEPLOY.md
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_DIR/deploy/lib.sh"
require_not_root deploy/setup_vm.sh

bold "Set up the Darvas Telegram listener and trading workers on this VM."
status=0; any=0
if yes_no "Set up the Telegram listener and shared SQLite queue?" y; then
    any=1; echo; bash "$REPO_DIR/deploy/setup_telegram_listener.sh" || status=1; echo
fi
if yes_no "Set up the EQUITY (shares) bot?" y; then
    any=1; echo; bash "$(setup_of equity)" || status=1; echo
fi
if yes_no "Set up the OPTIONS (FnO) bot?" "$(is_installed fno && echo y || echo n)"; then
    any=1; echo; bash "$(setup_of fno)" || status=1; echo
fi
[ $any = 1 ] || warn "Nothing selected."
[ $any = 1 ] && command -v bot >/dev/null && bot status
exit $status
