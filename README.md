# notify-shim

Multi-channel notification **shims** for the OpenClaw workspace.

A script that wants to notify Yuting calls a shim (e.g. `notify-dm`) instead of
talking to Telegram directly. The shim looks up the **route** in a config file
and fans the same message out to every channel on that route. Today that's
Telegram + LINE; adding or removing a channel later is a config edit, not a code
change.

> Origin: [cathyyul/mac-scripts#5](https://github.com/cathyyul/mac-scripts/issues/5)
> asked for all Telegram notifications to be mirrored to LINE. That repo is
> photo-sync; the notification layer lives here instead.

## Shims

| Shim | Route | Channels (current) |
|------|-------|--------------------|
| `notify-dm` | `dm` | Telegram DM + LINE DM |
| `notify-group-couple` | `group-couple` | Telegram `小寶murmur` + LINE `海老群` |

Naming convention: a future group gets its own shim `notify-group-<name>` backed
by a matching route in `routes.json`.

## Usage

```sh
notify-dm "晚餐時間到了 🍽️"
echo "multi-line\nbody" | notify-dm
notify-group-couple "這週要買的東西…"
notify-dm --dry-run "preview, nothing is sent"
```

Exit code is **0 only if every channel succeeded**. If any channel fails the
shim prints a per-channel summary to stderr and exits non-zero — a Telegram
success never hides a LINE failure.

## Config (`routes.json`)

Real chat/user/group IDs are **not** in this repo. They live in a local,
gitignored file (default `~/.openclaw/notify/routes.json`). The repo ships
[`routes.example.json`](routes.example.json) with placeholders.

```json
{
  "dm": {
    "description": "Yuting personal DM",
    "channels": [
      { "channel": "telegram", "target": "<telegram chat id>" },
      { "channel": "line", "target": "<line user id>" }
    ]
  },
  "group-couple": {
    "description": "Couple group — Telegram 小寶murmur / LINE 海老群",
    "channels": [
      { "channel": "telegram", "target": "<telegram group id>" },
      { "channel": "line", "target": "<line group id>" }
    ]
  }
}
```

Resolution order for the config path: `$NOTIFY_ROUTES` →
`~/.openclaw/notify/routes.json`.

**This file is the single switch for where notifications go.** Every notifier
routes through the shims, so changing a route's `channels` here changes delivery
everywhere at once — no script edits.

- **Turn a channel off/on** (non-destructive): set `"enabled": false` on the
  channel (default is `true`). The target id stays so you can flip it back.
  ```json
  { "channel": "telegram", "target": "...", "enabled": false }
  ```
  If every channel on a route is disabled, the shim sends nothing and exits 0.
- **Add a channel** (e.g. WhatsApp when out): add an entry to `channels`. Any
  channel `openclaw message send --channel` supports works (telegram, line,
  whatsapp, signal, imessage, …); `target` is that channel's raw id (E.164 for
  WhatsApp/Signal, chat id for Telegram, userId/groupId for LINE).
- **Go single-channel** (e.g. LINE-only later): disable or remove the others.
- **Add a new group**: add a route, then a `notify-group-<name>` wrapper (copy
  an existing one, change `--route`).

## Delivery

Each channel is delivered with:

```sh
openclaw message send --channel <channel> --target <target> --message <text>
```

so the **OpenClaw gateway must be running**. `--target` is the raw id per
channel (Telegram chat id, LINE userId/groupId — no prefix).

> ⚠️ `openclaw message send --dry-run` is a *preview that still validates the
> target*; it does not deliver. The shim's own `--dry-run` is genuinely safe and
> never invokes openclaw at all.

## Deploy

```sh
./deploy.sh
```

Installs `notify-dm`, `notify-group-couple`, `notify_core.py`, and every
`notifiers/*.sh` into `~/.openclaw/workspace/scripts/`, and seeds
`~/.openclaw/notify/routes.json` from the example if it doesn't exist (then fill
in real IDs).

## Workspace notifiers (`notifiers/`)

LaunchAgent-driven scripts that have no other sub-project home live here and fan
out through the shims. They install to the same `scripts/` paths the
LaunchAgents already call, so no plist changes are needed.

| Script | Trigger | Route |
|--------|---------|-------|
| `meal-reminder.sh` | breakfast/lunch/dinner LaunchAgents | `notify-dm` |
| `slickdeals_notifier.sh` | Slickdeals gift-card monitor | `notify-dm` |
| `weekly_offers_notifier.sh` | weekly standard-offers review | `notify-dm` |

## Test

```sh
python3 -m pytest tests/ -q
```

Tests mock `subprocess.run`, so no gateway or network is needed.
