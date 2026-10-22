#!/usr/bin/env bash
# Backward-compatible name. New setups should use setup_telegram_listener.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/setup_telegram_listener.sh" "$@"
