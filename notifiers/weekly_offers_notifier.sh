#!/bin/zsh
# weekly_offers_notifier — run the weekly standard-offers review and, if there
# is output, notify Yuting via the notify-dm shim (Telegram + LINE).
set -euo pipefail

WORKSPACE_DIR="/Users/claw/.openclaw/workspace"
PYTHON_BIN="/usr/bin/python3"
SCRIPT_PATH="$WORKSPACE_DIR/scripts/weekly_standard_offers_review.py"
NOTIFY_DM="${NOTIFY_DM_BIN:-$WORKSPACE_DIR/scripts/notify-dm}"

cd "$WORKSPACE_DIR"

# Run review
OUTPUT=$("$PYTHON_BIN" "$SCRIPT_PATH")

if [[ -n "$OUTPUT" ]]; then
  MESSAGE="Weekly Standard Offers Update 💳"$'\n\n'"$OUTPUT"
  "$NOTIFY_DM" "$MESSAGE"
fi
