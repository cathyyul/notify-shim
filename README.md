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
`notifiers/*.sh` / `notifiers/*.py` into `~/.openclaw/workspace/scripts/`,
installs bundled LaunchAgent plist files into `~/Library/LaunchAgents/`, and
seeds `~/.openclaw/notify/routes.json` from the example if it doesn't exist
(then fill in real IDs).

Bundled plist files use `__HOME__` and `__OPENCLAW_BIN__` placeholders in the
repo. `deploy.sh` replaces them with the current `$HOME` and detected
`openclaw` binary path while installing to `~/Library/LaunchAgents/`. It also ensures
`~/.openclaw/workspace/logs/` exists before installing the plist, since launchd
requires the stdout/stderr directory to already exist.

Deploy only copies plist files. Load or reload them explicitly after review:

```sh
launchctl unload ~/Library/LaunchAgents/com.openclaw.channel-watchdog.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.openclaw.channel-watchdog.plist
```

## Workspace notifiers (`notifiers/`)

LaunchAgent-driven scripts that have no other sub-project home live here and fan
out through the shims. They install to the same `scripts/` paths the
LaunchAgents already call, so no plist changes are needed.

| Script | Trigger (LaunchAgent) | Route(s) |
|--------|---------|-------|
| `meal-reminder.sh` | `meal-reminder-{breakfast,lunch,dinner}` | `notify-dm` |
| `slickdeals_deliver.sh` | `slickdeals-monitor` | `notify-dm` |
| `weekly_offers_deliver.sh` | `weekly-standard-offers` | `notify-dm` + `notify-group-couple` |
| `openclaw_channel_watchdog.py` | `channel-watchdog` | `notify-dm` on unhealthy channels |

## OpenClaw channel watchdog

Issue [#13](https://github.com/cathyyul/notify-shim/issues/13) tracks the local
mitigation for OpenClaw channel route/session loss. The watchdog can check LINE,
WhatsApp, or both:

```sh
python3 notifiers/openclaw_channel_watchdog.py --channels line --notify
python3 notifiers/openclaw_channel_watchdog.py --channels whatsapp --notify
python3 notifiers/openclaw_channel_watchdog.py --channels line whatsapp --notify --recovery-mode restart
```

LINE checks:
- LINE official webhook test endpoint
- local `POST http://127.0.0.1:18789/line/webhook`, treating `404` as route missing

WhatsApp checks:
- `openclaw channels status --channel whatsapp --probe --json`
- verifies configured, linked, running, connected, and `healthState=healthy`

By default recovery is notify-only. `--recovery-mode restart` runs
`openclaw gateway restart` at most once for the active failing channel set, then
re-checks health. If the same channel remains unhealthy, later runs stop
restarting and only alert through `notify-dm`, guarded by `--cooldown-minutes`
(default 30). State is written to
`~/.openclaw/workspace/data/health/openclaw-channel-watchdog.json`.

Incident reset behavior:
- The active incident is keyed by failing channel set, e.g. `line`,
  `whatsapp`, or `line|whatsapp`.
- A changing failure detail for the same still-unhealthy channel is treated as
  the same incident, so it does not get another restart attempt.
- After any run where all requested channels are healthy, the watchdog removes
  `active_incident`; a later failure is then treated as a fresh incident and may
  restart once again.
- To manually allow another restart before a healthy pass, remove only the
  `active_incident` key from
  `~/.openclaw/workspace/data/health/openclaw-channel-watchdog.json`.

Bundled LaunchAgent:

- `launchagents/com.openclaw.channel-watchdog.plist`
- runs every 5 minutes
- checks LINE + WhatsApp
- uses `--notify --recovery-mode restart --cooldown-minutes 30`
- passes an absolute `--openclaw-bin` path during deploy so launchd's minimal
  PATH cannot hide the CLI
- writes logs to
  `~/.openclaw/workspace/logs/openclaw-channel-watchdog.log`

## Test

```sh
python3 -m pytest tests/ -q
```

Tests mock `subprocess.run`, so no gateway or network is needed.
