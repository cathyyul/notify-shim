#!/bin/zsh
# deploy.sh — install the notify shims into the OpenClaw workspace scripts/
# directory and seed a local routes.json (with real IDs) if one doesn't exist.
#
# The real routes.json lives OUTSIDE this repo (default ~/.openclaw/notify/)
# so personal chat/user/group IDs are never committed to a public repo.
set -euo pipefail

SRC="${0:A:h}"
DEST="${NOTIFY_DEST:-$HOME/.openclaw/workspace/scripts}"
ROUTES_DIR="${NOTIFY_ROUTES_DIR:-$HOME/.openclaw/notify}"

if [[ ! -d "$DEST" ]]; then
  echo "deploy: destination not found: $DEST" >&2
  exit 1
fi

install -m 0755 "$SRC/notify-dm"            "$DEST/notify-dm"
install -m 0755 "$SRC/notify-group-couple"  "$DEST/notify-group-couple"
install -m 0644 "$SRC/notify_core.py"       "$DEST/notify_core.py"
echo "deploy: installed notify-dm, notify-group-couple, notify_core.py -> $DEST"

# Workspace notifier scripts (called by LaunchAgents at their existing paths).
for n in "$SRC"/notifiers/*.sh; do
  install -m 0755 "$n" "$DEST/${n:t}"
  echo "deploy: installed notifiers/${n:t} -> $DEST/${n:t}"
done

mkdir -p "$ROUTES_DIR"
if [[ ! -f "$ROUTES_DIR/routes.json" ]]; then
  cp "$SRC/routes.example.json" "$ROUTES_DIR/routes.json"
  chmod 0600 "$ROUTES_DIR/routes.json"
  echo "deploy: seeded $ROUTES_DIR/routes.json from example — FILL IN real IDs"
else
  echo "deploy: kept existing $ROUTES_DIR/routes.json"
fi
