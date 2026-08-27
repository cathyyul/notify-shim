#!/usr/bin/env python3
"""Watch Claude desktop scheduled-task health via ~/Library/Logs/Claude/main.log.

Background (2026-08-18/19 incident): when the Claude desktop login gets too old,
spawning local scheduled-task sessions is rejected server-side with
``session_stale_relogin`` and every local routine fails silently — the routines
themselves send the completion/failure DMs, and here they die before the session
even starts. This watchdog is the external observer that tells Yuting.

Usage:
  python3 notifiers/claude_scheduler_watchdog.py            # check only
  python3 notifiers/claude_scheduler_watchdog.py --notify   # alert via notify-dm

Exit codes:
  0 = healthy (no active incident)
  1 = active incident
  2 = watchdog configuration or execution error
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

WORKSPACE = Path.home() / ".openclaw" / "workspace"
DEFAULT_LOG_FILE = Path.home() / "Library" / "Logs" / "Claude" / "main.log"
DEFAULT_STATE_FILE = WORKSPACE / "data" / "health" / "claude-scheduler-watchdog.json"
DEFAULT_NOTIFY_DM_BIN = WORKSPACE / "scripts" / "notify-dm"

MARKER_SPAWN = "[CCDScheduledTasks] Spawning new session for scheduled task "
MARKER_CONFIRM = "[CCDScheduledTasks] Confirmed task run for: "
MARKER_CLEARED = "[CCDScheduledTasks] Cleared stale pending dispatch for: "
MARKER_STALE = "session_stale_relogin"

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

CAUSE_STALE = "session_stale_relogin"
CAUSE_UNCONFIRMED = "unconfirmed_spawn"


@dataclass
class LogEvent:
    kind: str  # "spawn" | "confirm" | "cleared" | "stale"
    ts: Optional[dt.datetime]
    task: Optional[str] = None


@dataclass
class EvalResult:
    alert_message: Optional[str]
    recovery_message: Optional[str]
    incident_active: bool


def parse_line_ts(line: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(line[:19], TS_FORMAT)
    except ValueError:
        return None


def fmt_ts(ts: Optional[dt.datetime]) -> Optional[str]:
    return ts.strftime(TS_FORMAT) if ts else None


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None


def parse_events(lines: "list[str]") -> "list[LogEvent]":
    events: "list[LogEvent]" = []
    last_ts: Optional[dt.datetime] = None
    for line in lines:
        ts = parse_line_ts(line)
        if ts is not None:
            last_ts = ts
        if MARKER_SPAWN in line:
            rest = line.split(MARKER_SPAWN, 1)[1]
            task = rest.split(" ", 1)[0].strip().rstrip("{")
            if task:
                events.append(LogEvent("spawn", last_ts, task))
        elif MARKER_CONFIRM in line:
            task = line.split(MARKER_CONFIRM, 1)[1].strip()
            if task:
                events.append(LogEvent("confirm", last_ts, task))
        elif MARKER_CLEARED in line:
            task = line.split(MARKER_CLEARED, 1)[1].strip()
            if task:
                events.append(LogEvent("cleared", last_ts, task))
        elif MARKER_STALE in line:
            events.append(LogEvent("stale", last_ts))
    return events


def _read_complete_lines(path: Path, start: int) -> "tuple[list[str], int]":
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n")
    if cut == -1:
        return [], start
    consumed = data[:cut + 1]
    return consumed.decode("utf-8", errors="replace").splitlines(), start + len(consumed)


def read_new_lines(log_path: Path, log_state: "dict[str, Any]",
                   bootstrap_since: Optional[dt.datetime] = None,
                   ) -> "tuple[list[str], dict[str, Any]]":
    stored_ino = log_state.get("inode")
    stored_off = log_state.get("offset", 0)
    lines: "list[str]" = []
    try:
        st = log_path.stat()
    except FileNotFoundError:
        return [], {"inode": None, "offset": 0}

    if stored_ino == st.st_ino and stored_off <= st.st_size:
        start = stored_off
    else:
        start = 0
        if stored_ino is not None:
            # The previous main.log was rotated away; find it by inode among
            # the rotated siblings (main1.log, main2.log, ...) and drain its tail.
            pattern = f"{log_path.stem}[0-9]*{log_path.suffix}"
            for sibling in sorted(log_path.parent.glob(pattern)):
                try:
                    sib_st = sibling.stat()
                except OSError:
                    continue
                if sib_st.st_ino == stored_ino and stored_off <= sib_st.st_size:
                    tail, _ = _read_complete_lines(sibling, stored_off)
                    lines.extend(tail)
                    break
        elif bootstrap_since is not None:
            # Cold start — a fresh deploy, or state we just quarantined as
            # corrupt. An incident already under way may have left its evidence
            # in a file that has since rotated, so read the siblings that are
            # still recent enough to matter. Oldest first, so the merged stream
            # stays in chronological order.
            pattern = f"{log_path.stem}[0-9]*{log_path.suffix}"
            recent: "list[tuple[float, Path]]" = []
            for sibling in log_path.parent.glob(pattern):
                try:
                    sib_st = sibling.stat()
                except OSError:
                    continue
                if dt.datetime.fromtimestamp(sib_st.st_mtime) >= bootstrap_since:
                    recent.append((sib_st.st_mtime, sibling))
            for _, sibling in sorted(recent, key=lambda item: item[0]):
                sib_lines, _ = _read_complete_lines(sibling, 0)
                lines.extend(sib_lines)

    new_lines, new_off = _read_complete_lines(log_path, start)
    lines.extend(new_lines)
    return lines, {"inode": st.st_ino, "offset": new_off}


def format_alert(incident: "dict[str, Any]") -> str:
    lines = ["⚠️ Claude 排程 watchdog：本機 scheduled-task 排程異常"]
    if CAUSE_STALE in incident.get("causes", []):
        lines.append(
            "原因：Claude desktop app 登入過期（session_stale_relogin），"
            "排程 session 無法取得 elevated scope，spawn 全部失敗。")
        lines.append(
            "修法：在 Mac mini 上重新登入 Claude desktop app；"
            "登入後排程會自動恢復（watchdog 會另發恢復通知）。")
    else:
        lines.append("原因：scheduled task spawn 後逾時未見 Confirmed task run（原因未知）。")
        lines.append("修法：查 ~/Library/Logs/Claude/main.log 的 [CCDScheduledTasks] 段。")
    tasks = incident.get("affected_tasks", [])
    if tasks:
        lines.append("受影響 tasks：" + ", ".join(tasks))
    else:
        lines.append("受影響 tasks：（尚未觀察到具體 task，僅見 oauth 失敗）")
    if incident.get("first_seen_at"):
        lines.append(f"事故起始：{incident['first_seen_at']}")
    return "\n".join(lines)


def format_recovery(incident: "dict[str, Any]", confirm: LogEvent) -> str:
    lines = [
        "✅ Claude 排程已恢復：偵測到 Confirmed task run for: "
        f"{confirm.task}（{fmt_ts(confirm.ts) or '時間不明'}）。"
    ]
    tasks = incident.get("affected_tasks", [])
    if tasks:
        lines.append("事故期間受影響 tasks：" + ", ".join(tasks))
    return "\n".join(lines)


def evaluate(state: "dict[str, Any]", events: "list[LogEvent]", now: dt.datetime,
             pending_timeout_min: int = 15,
             cooldown_hours: int = 12) -> EvalResult:
    """Pure incident logic; mutates ``state`` in place.

    Deciding to alert is *not* recording that one was sent — the cooldown is
    only consumed once ``notify-dm`` confirms delivery, via ``mark_alerted``.
    """
    pending: "list[dict[str, Any]]" = list(state.get("pending_spawns", []))
    incident: "Optional[dict[str, Any]]" = state.get("active_incident")

    failed: "dict[str, Optional[dt.datetime]]" = {}
    confirms: "dict[str, dt.datetime]" = {}
    window_first_failure: Optional[dt.datetime] = None
    window_last_failure: Optional[dt.datetime] = None
    window_stale_at: Optional[dt.datetime] = None
    last_confirm: Optional[LogEvent] = None

    def note_failure(task: Optional[str], ts: Optional[dt.datetime]) -> None:
        nonlocal window_first_failure, window_last_failure
        if task is not None and task not in failed:
            failed[task] = ts
        if ts is not None:
            if window_first_failure is None or ts < window_first_failure:
                window_first_failure = ts
            if window_last_failure is None or ts > window_last_failure:
                window_last_failure = ts

    stale_seen = False
    for ev in events:
        if ev.kind == "spawn":
            pending.append({"task": ev.task, "ts": fmt_ts(ev.ts)})
        elif ev.kind == "confirm":
            pending = [p for p in pending if p["task"] != ev.task]
            if ev.ts is not None and (ev.task not in confirms or ev.ts > confirms[ev.task]):
                confirms[ev.task] = ev.ts
            last_confirm = ev
        elif ev.kind == "cleared":
            pending = [p for p in pending if p["task"] != ev.task]
            note_failure(ev.task, ev.ts)
        elif ev.kind == "stale":
            stale_seen = True
            if ev.ts is not None and (window_stale_at is None or ev.ts > window_stale_at):
                window_stale_at = ev.ts
            note_failure(None, ev.ts)

    still_pending: "list[dict[str, Any]]" = []
    for p in pending:
        ts = parse_ts(p.get("ts"))
        if ts is not None and (now - ts) >= dt.timedelta(minutes=pending_timeout_min):
            note_failure(p["task"], ts)
        else:
            still_pending.append(p)
    state["pending_spawns"] = still_pending

    # Carry the unresolved failures forward, then resolve them against this
    # window's confirms. A confirm is evidence about the task it names and
    # nothing else, so a healthy task can no longer bury another task's timeout
    # or fake a recovery for it.
    open_failures: "dict[str, Optional[dt.datetime]]" = {}
    stale_open = False
    stale_at: Optional[dt.datetime] = None
    if incident is not None:
        for task, ts_str in (incident.get("open_failures") or {}).items():
            open_failures[task] = parse_ts(ts_str)
        stale_open = bool(incident.get("stale_open"))
        stale_at = parse_ts(incident.get("stale_last_at"))
    for task, ts in failed.items():
        prev = open_failures.get(task)
        if task not in open_failures or (ts is not None and (prev is None or ts > prev)):
            open_failures[task] = ts
    if stale_seen:
        stale_open = True
    if window_stale_at is not None and (stale_at is None or window_stale_at > stale_at):
        stale_at = window_stale_at

    for task in list(open_failures):
        confirmed_at = confirms.get(task)
        failed_at = open_failures[task]
        if confirmed_at is not None and (failed_at is None or confirmed_at > failed_at):
            del open_failures[task]

    # A stale-login latch blocks every spawn, so any confirm after the last
    # stale line proves the latch is gone and the failures it held down were
    # symptoms of it. Anything that broke after the latch stands on its own.
    newest_confirm = max(confirms.values()) if confirms else None
    if stale_open and newest_confirm is not None and (stale_at is None
                                                      or newest_confirm > stale_at):
        stale_open = False
        if stale_at is not None:
            open_failures = {task: ts for task, ts in open_failures.items()
                             if ts is not None and ts > stale_at}

    if not open_failures and not stale_open:
        if incident is None:
            return EvalResult(None, None, incident_active=False)
        resolver = incident.get("resolved_by")
        if last_confirm is not None and last_confirm.ts is not None:
            resolver = {"task": last_confirm.task, "ts": fmt_ts(last_confirm.ts)}
        if not incident.get("last_alert_at") or resolver is None:
            # Nothing was ever paged, so no notice is owed and the incident can
            # simply close.
            state.pop("active_incident", None)
            return EvalResult(None, None, incident_active=False)
        # A notice IS owed. Hold the incident open until a notifying run actually
        # delivers it — otherwise a documented check-only run consumes the only
        # recovery event, advances the log offset, and the notice is lost.
        incident["resolved_by"] = resolver
        incident["open_failures"] = {}
        incident["stale_open"] = False
        state["active_incident"] = incident
        resolving_confirm = LogEvent("confirm", parse_ts(resolver.get("ts")),
                                     resolver.get("task"))
        return EvalResult(None, format_recovery(incident, resolving_confirm),
                          incident_active=False)

    if incident is None:
        incident = {
            "first_seen_at": fmt_ts(window_first_failure) or fmt_ts(now),
            "causes": [],
            "affected_tasks": [],
            "last_alert_at": None,
            "alerted_tasks": [],
            "last_failure_at": None,
        }
    causes = set()
    if stale_open:
        causes.add(CAUSE_STALE)
    if open_failures:
        causes.add(CAUSE_UNCONFIRMED)
    incident["causes"] = sorted(causes)
    incident["affected_tasks"] = sorted(
        set(incident.get("affected_tasks", [])) | set(open_failures))
    incident["open_failures"] = {task: fmt_ts(ts) for task, ts in open_failures.items()}
    incident["stale_open"] = stale_open
    incident["stale_last_at"] = fmt_ts(stale_at)
    # Something is broken again, so any recovery notice still queued is void.
    incident["resolved_by"] = None
    prev_failure = parse_ts(incident.get("last_failure_at"))
    if window_last_failure is not None and (prev_failure is None
                                            or window_last_failure > prev_failure):
        incident["last_failure_at"] = fmt_ts(window_last_failure)
    state["active_incident"] = incident

    alert_message = None
    last_alert = parse_ts(incident.get("last_alert_at"))
    new_tasks = set(incident["affected_tasks"]) - set(incident.get("alerted_tasks", []))
    cooldown_over = (last_alert is None
                     or (now - last_alert) >= dt.timedelta(hours=cooldown_hours))
    if cooldown_over or new_tasks:
        alert_message = format_alert(incident)
    return EvalResult(alert_message, None, incident_active=True)


def mark_alerted(state: "dict[str, Any]", now: dt.datetime) -> None:
    """Record that an alert actually reached Yuting.

    Only called after ``notify-dm`` reports success. An alert that failed to
    send must not consume the cooldown — otherwise a missing or broken shim
    buys a live incident 12 more hours of the silence this watchdog exists to
    break.
    """
    incident = state.get("active_incident")
    if incident is None:
        return
    incident["last_alert_at"] = fmt_ts(now)
    incident["alerted_tasks"] = incident["affected_tasks"]


def mark_recovered(state: "dict[str, Any]") -> None:
    """Close a resolved incident once its recovery notice actually went out.

    The mirror of ``mark_alerted``: a state transition that discharges a
    notification obligation is only committed after the notification lands.
    """
    state.pop("active_incident", None)


def load_json(path: Path) -> "dict[str, Any]":
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A half-written state file must not blind the watchdog forever: without
        # this the next run dies in load_json every hour and nothing is watching
        # the scheduler until a human notices. Quarantine it and start clean —
        # the worst case is one duplicate alert, not permanent silence.
        quarantine = path.with_name(path.name + ".corrupt")
        try:
            os.replace(path, quarantine)
            print(f"state: corrupt state file quarantined to {quarantine} ({exc})",
                  file=sys.stderr)
        except OSError as move_exc:
            print(f"state: corrupt state file could not be quarantined: {move_exc}",
                  file=sys.stderr)
        return {}


def save_json(path: Path, payload: "dict[str, Any]") -> None:
    """Write via temp file + rename so a crash can never truncate the state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def send_notification(message: str, notify_bin: Path) -> bool:
    """Send via the notify-dm shim. Returns True only when it was accepted."""
    try:
        if not notify_bin.exists():
            print(f"notify: shim not found at {notify_bin}", file=sys.stderr)
            return False
        proc = subprocess.run([str(notify_bin), message], timeout=30,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stdout + proc.stderr).strip()
            print(f"notify: notify-dm exited {proc.returncode}: {detail}",
                  file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"notify: failed to invoke notify-dm: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch Claude scheduled-task health via the desktop app main.log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0=healthy, 1=active incident, 2=watchdog error",
    )
    parser.add_argument("--log-file", type=Path,
                        default=Path(os.environ.get("CLAUDE_MAIN_LOG", DEFAULT_LOG_FILE)))
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--notify", action="store_true",
                        help="Send notify-dm alerts (subject to cooldown/new-task dedup)")
    parser.add_argument("--notify-bin", type=Path,
                        default=Path(os.environ.get("NOTIFY_DM_BIN", DEFAULT_NOTIFY_DM_BIN)))
    parser.add_argument("--pending-timeout-min", type=int, default=15,
                        help="Minutes a spawn may stay unconfirmed before it counts as failed")
    parser.add_argument("--bootstrap-window-hours", type=int, default=24,
                        help="On a cold start (no prior state), how far back log "
                             "evidence — including rotated files — still counts")
    parser.add_argument("--cooldown-hours", type=int, default=12,
                        help="Minimum hours between same-cause alerts (new tasks re-alert)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args()

    try:
        now = dt.datetime.now()
        state = load_json(args.state_file)
        cold_start = not state.get("log")
        bootstrap_since = now - dt.timedelta(hours=args.bootstrap_window_hours)
        lines, log_state = read_new_lines(
            args.log_file, state.get("log", {}),
            bootstrap_since=bootstrap_since if cold_start else None)
        events = parse_events(lines)
        if cold_start:
            # A cold start reads whole files, so bound it by age: report what is
            # happening now, not an incident that was resolved months ago.
            events = [ev for ev in events if ev.ts is None or ev.ts >= bootstrap_since]
        result = evaluate(state, events, now,
                          pending_timeout_min=args.pending_timeout_min,
                          cooldown_hours=args.cooldown_hours)

        notified = False
        if args.notify:
            if result.alert_message:
                # Consume the cooldown only on confirmed delivery; a failed
                # send leaves the incident unalerted so the next run retries.
                if send_notification(result.alert_message, args.notify_bin):
                    mark_alerted(state, now)
                    notified = True
            if result.recovery_message:
                if send_notification(result.recovery_message, args.notify_bin):
                    mark_recovered(state)
                    notified = True

        state["log"] = log_state
        state["checked_at"] = now.strftime(TS_FORMAT)
        state["last_result"] = {
            "ok": not result.incident_active,
            "scanned_lines": len(lines),
            "events": len(events),
            "notified": notified,
        }
        save_json(args.state_file, state)

        payload = dict(state["last_result"])
        if result.alert_message:
            payload["alert"] = result.alert_message
        if result.recovery_message:
            payload["recovery"] = result.recovery_message
        if args.json or result.incident_active:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if result.incident_active else 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
