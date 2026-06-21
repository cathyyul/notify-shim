#!/bin/zsh
# weekly_offers_deliver.sh — run the weekly CardPointers standard-offers review
# and notify via the shims. Goes to Yuting's DM (notify-dm) AND the couple group
# (notify-group-couple), each fanning out to Telegram + LINE.
# Run weekly by com.openclaw.weekly-standard-offers.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DELIVER=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-deliver) DELIVER=false; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
REVIEW="${WEEKLY_OFFERS_REVIEW:-$SCRIPT_DIR/weekly_standard_offers_review.py}"
NOTIFY_DM="${NOTIFY_DM_BIN:-$SCRIPT_DIR/notify-dm}"
NOTIFY_GROUP="${NOTIFY_GROUP_COUPLE_BIN:-$SCRIPT_DIR/notify-group-couple}"

# if! (not a bare assignment) so set -e doesn't abort before we can report.
if ! raw_output="$("$PYTHON_BIN" "$REVIEW" 2>&1)"; then
  echo "weekly_standard_offers_review.py failed: $raw_output" >&2
  exit 1
fi

# Format full list for delivery
msg="$("$PYTHON_BIN" - <<'PY' "$raw_output"
import sys

raw = sys.argv[1].strip()
lines = raw.splitlines()

header_lines = []
item_lines = []
for line in lines:
    if line.startswith("- "):
        item_lines.append(line)
    else:
        header_lines.append(line)

parts = ["📋 Weekly Standard Offers Review"]
parts.extend(header_lines)
parts.append("")
if item_lines:
    parts.append("本季到期的未使用 standard offers：")
    parts.extend(item_lines)
else:
    parts.append("✅ 目前沒有本季到期的未使用 standard offers")
print("\n".join(parts))
PY
)"

if [[ "$DELIVER" == "true" ]]; then
  "$NOTIFY_DM" "$msg"
  "$NOTIFY_GROUP" "$msg"
else
  echo "$msg"
fi
