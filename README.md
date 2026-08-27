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
`~/Library/LaunchAgents/` and `~/.openclaw/workspace/logs/` exist before
installing the plist, since launchd requires the stdout/stderr directory to
already exist. If `openclaw` cannot be found, deploy fails and asks for
`OPENCLAW_BIN=/absolute/path/to/openclaw` rather than installing a broken agent.

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
| `claude_scheduler_watchdog.py` | `claude-scheduler-watchdog` | `notify-dm` on scheduled-task spawn failures |

## Claude scheduler watchdog

Issue [#22](https://github.com/cathyyul/notify-shim/issues/22): when the Claude
desktop login gets too old, spawning local scheduled-task sessions fails with
`session_stale_relogin` and every local routine dies silently (2026-08-18/19:
all 7 routines down for ~20h). The watchdog tails
`~/Library/Logs/Claude/main.log` (rotation-aware, incremental via a state file)
hourly and alerts through `notify-dm` when it sees `session_stale_relogin` or a
`Spawning new session for scheduled task <X>` with no matching
`Confirmed task run for: <X>` within 15 minutes. Alerts carry the cause, the fix
(re-login to the Claude desktop app on the Mac mini), and the affected task
list; same-cause alerts are deduped for 12h but a newly affected task re-alerts,
and the first confirmed run after an alert sends a recovery notice.

Not being able to look is itself an incident. If the log or the state file
cannot be read, or a rotation leaves a gap the watchdog cannot prove it read
through, it alerts saying so rather than reporting healthy — a watchdog that is
quietly blind is the failure this tool exists to catch. Alerts and recovery
notices are only recorded as sent once `notify-dm` accepts them, so a failed
send is retried on the next run instead of being silenced by the cooldown.

### Known limitations

These are accepted, not overlooked. The watchdog covers the failure that
actually happened (a silent 20h outage) and deliberately stops short of proving
every edge; each was adjudicated on issue #22 rather than hardened further.

- **Accounting is per task, not per invocation.** The log carries no invocation
  id, so if a task spawns twice inside the 15-minute window and only the later
  run confirms, the earlier missed run is not reported. The scheduled routines
  are daily or hourly with a single in-flight invocation, so this needs a manual
  rerun or a catch-up dispatch to overlap the original schedule. Inferring
  invocation identity from second-resolution timestamps was tried and removed:
  it produced more bugs than it caught.
- **A gap in the middle of the rotation chain is not detected.** If the
  generation the cursor points at still exists, the reader walks the newer
  generations it can find. Should an intermediate `mainN.log` be deleted, its
  events are skipped without the run being marked blind. A missing *resume*
  generation is still detected and does block.
- **A stale-login line and a `Confirmed` line in the same second may still
  alert.** Per-task failures resolve in log order, but the global latch check
  compares timestamps, so recovery proven within the same second can page once
  before the next scan clears it.
- **A rotation landing between the `stat()` and the read binds the cursor to
  the wrong file.** The live log is statted once and reopened by pathname, so a
  rotation inside that window returns the new file's offset paired with the old
  file's inode, and the next run resumes at an unrelated position. The window is
  microseconds against a rotation every few days, so this is left as a race
  rather than fixed by re-opening and `fstat`ing.
- **An unwritable state file re-sends alerts every hour.** Delivery happens
  before persistence and a failed `save_json` is only logged, so a run that
  cannot save its bookkeeping repeats the same alert on the next tick. Dedup
  would need an idempotency key that `notify-dm` records independently. Noisy is
  the intended direction to fail in — this watchdog exists because silence is
  the worse outcome.

```sh
python3 notifiers/claude_scheduler_watchdog.py --json      # check only
python3 notifiers/claude_scheduler_watchdog.py --notify    # alert via notify-dm
```

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
  PATH cannot hide the CLI; watchdog subprocesses also prepend Homebrew paths
  so OpenClaw's `env node` launcher can resolve Node under launchd
- writes logs to
  `~/.openclaw/workspace/logs/openclaw-channel-watchdog.log`

## Test

```sh
python3 -m pytest tests/ -q
```

Tests mock `subprocess.run`, so no gateway or network is needed.
