#!/bin/zsh
# slickdeals_notifier — run the Slickdeals gift-card monitor and, if there are
# matches, notify Yuting via the notify-dm shim (Telegram + LINE).
set -euo pipefail

WORKSPACE_DIR="/Users/claw/.openclaw/workspace"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
SCRIPT_PATH="${SLICKDEALS_MONITOR:-$WORKSPACE_DIR/scripts/slickdeals_monitor.py}"
NOTIFY_DM="${NOTIFY_DM_BIN:-$WORKSPACE_DIR/scripts/notify-dm}"

cd "$WORKSPACE_DIR"

# Run monitor
OUTPUT=$("$PYTHON_BIN" "$SCRIPT_PATH" --max-age-hours 72)

# Check if matches were found (more than just the "Found X candidate deals" line).
# grep -c exits 1 on zero matches; `|| true` keeps that from aborting under set -e.
NUM_MATCHES=$(echo "$OUTPUT" | grep -c "^- \[" || true)

if [[ "$NUM_MATCHES" -gt 0 ]]; then
  MESSAGE="Slickdeals Gift Card Alert 🔔"$'\n\n'"$OUTPUT"
  "$NOTIFY_DM" "$MESSAGE"
fi
