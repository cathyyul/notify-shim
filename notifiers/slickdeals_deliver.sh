#!/bin/zsh
# slickdeals_deliver.sh — run the Slickdeals gift-card monitor and notify via
# the notify-dm shim (Telegram + LINE). Run daily by com.openclaw.slickdeals-monitor.
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
MONITOR="${SLICKDEALS_MONITOR:-$SCRIPT_DIR/slickdeals_monitor.py}"
NOTIFY_DM="${NOTIFY_DM_BIN:-$SCRIPT_DIR/notify-dm}"

raw_output="$("$PYTHON_BIN" "$MONITOR" --max-age-hours 72 2>&1)"
exit_code=$?

if [[ $exit_code -ne 0 ]]; then
  echo "slickdeals_monitor.py failed (exit $exit_code): $raw_output" >&2
  exit $exit_code
fi

# Format message for delivery
msg="$("$PYTHON_BIN" - <<'PY' "$raw_output"
import sys

raw = sys.argv[1].strip()
lines = raw.splitlines()

header = ""
items = []
for line in lines:
    if line.startswith("Found"):
        header = line
    elif line.startswith("- "):
        items.append(line[2:])  # strip leading "- "
    elif line.startswith("  http") and items:
        items[-1] = items[-1] + "\n  " + line.strip()

if not items:
    count_part = header or "Found 0 candidate deals"
    print(f"🛒 Slickdeals 監控\n{count_part}\n沒有符合條件的新 deal（72h 內）")
    sys.exit(0)

parts = ["🛒 Slickdeals 監控", header, ""]
for item in items:
    parts.append(f"• {item}")
print("\n".join(parts))
PY
)"

if [[ "$DELIVER" == "true" ]]; then
  "$NOTIFY_DM" "$msg"
else
  echo "$msg"
fi
