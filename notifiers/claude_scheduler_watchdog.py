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


def read_new_lines(log_path: Path, log_state: "dict[str, Any]") -> "tuple[list[str], dict[str, Any]]":
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
             will_notify: bool, pending_timeout_min: int = 15,
             cooldown_hours: int = 12) -> EvalResult:
    """Pure incident logic; mutates ``state`` in place."""
    pending: "list[dict[str, Any]]" = list(state.get("pending_spawns", []))
    incident: "Optional[dict[str, Any]]" = state.get("active_incident")

    failed: "dict[str, Optional[dt.datetime]]" = {}
    window_first_failure: Optional[dt.datetime] = None
    window_last_failure: Optional[dt.datetime] = None
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
            last_confirm = ev
        elif ev.kind == "cleared":
            pending = [p for p in pending if p["task"] != ev.task]
            note_failure(ev.task, ev.ts)
        elif ev.kind == "stale":
            stale_seen = True
            note_failure(None, ev.ts)

    still_pending: "list[dict[str, Any]]" = []
    for p in pending:
        ts = parse_ts(p.get("ts"))
        if ts is not None and (now - ts) >= dt.timedelta(minutes=pending_timeout_min):
            note_failure(p["task"], ts)
        else:
            still_pending.append(p)
    state["pending_spawns"] = still_pending

    # Recovery: a confirmed run after the newest failure evidence means the
    # scheduler is spawning again (during a stale-login latch nothing confirms).
    recovery_message = None
    failure_now = stale_seen or bool(failed)
    if incident is not None and last_confirm is not None:
        confirm_ts = last_confirm.ts
        recovered = (not failure_now
                     or (confirm_ts is not None and window_last_failure is not None
                         and confirm_ts > window_last_failure))
        if recovered:
            if incident.get("last_alert_at"):
                recovery_message = format_recovery(incident, last_confirm)
            state.pop("active_incident", None)
            return EvalResult(None, recovery_message, incident_active=False)

    if incident is None and failure_now and last_confirm is not None:
        confirm_ts = last_confirm.ts
        if not (confirm_ts is not None and window_last_failure is not None
                and window_last_failure > confirm_ts):
            # Failure evidence followed by a confirm inside the same window:
            # transient, already self-recovered before we ever alerted.
            return EvalResult(None, None, incident_active=False)

    if not failure_now:
        return EvalResult(None, None, incident_active=incident is not None)

    if incident is None:
        incident = {
            "first_seen_at": fmt_ts(window_first_failure) or fmt_ts(now),
            "causes": [],
            "affected_tasks": [],
            "last_alert_at": None,
            "alerted_tasks": [],
            "last_failure_at": None,
        }
    causes = set(incident.get("causes", []))
    if stale_seen:
        causes.add(CAUSE_STALE)
    if any(task is not None for task in failed):
        causes.add(CAUSE_UNCONFIRMED)
    incident["causes"] = sorted(causes)
    incident["affected_tasks"] = sorted(
        set(incident.get("affected_tasks", [])) | set(failed))
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
        if will_notify:
            incident["last_alert_at"] = fmt_ts(now)
            incident["alerted_tasks"] = incident["affected_tasks"]
    return EvalResult(alert_message, None, incident_active=True)


def load_json(path: Path) -> "dict[str, Any]":
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: "dict[str, Any]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def send_notification(message: str, notify_bin: Path) -> None:
    try:
        if not notify_bin.exists():
            print(f"notify: shim not found at {notify_bin}", file=sys.stderr)
            return
        proc = subprocess.run([str(notify_bin), message], timeout=30,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stdout + proc.stderr).strip()
            print(f"notify: notify-dm exited {proc.returncode}: {detail}",
                  file=sys.stderr)
    except Exception as exc:
        print(f"notify: failed to invoke notify-dm: {exc}", file=sys.stderr)


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
    parser.add_argument("--cooldown-hours", type=int, default=12,
                        help="Minimum hours between same-cause alerts (new tasks re-alert)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args()

    try:
        state = load_json(args.state_file)
        lines, log_state = read_new_lines(args.log_file, state.get("log", {}))
        events = parse_events(lines)
        now = dt.datetime.now()
        result = evaluate(state, events, now, will_notify=args.notify,
                          pending_timeout_min=args.pending_timeout_min,
                          cooldown_hours=args.cooldown_hours)

        notified = False
        if args.notify:
            if result.alert_message:
                send_notification(result.alert_message, args.notify_bin)
                notified = True
            if result.recovery_message:
                send_notification(result.recovery_message, args.notify_bin)
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
