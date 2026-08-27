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
CAUSE_BLIND = "cannot_observe"


@dataclass
class LogEvent:
    kind: str  # "spawn" | "confirm" | "cleared" | "stale"
    ts: Optional[dt.datetime]
    task: Optional[str] = None


@dataclass
class LogRead:
    """What one attempt to read the log actually yielded.

    ``cold`` is the single definition of "no usable cursor" in this program —
    both the sibling bootstrap below and ``main``'s age filter read it from here
    rather than re-deriving it, because two independent answers to that question
    is precisely what let a run go blind while reporting healthy.
    """

    lines: "list[str]"
    cursor: "Optional[dict[str, Any]]"  # None ⇒ nothing usable; persist nothing
    cold: bool
    blocked_reason: Optional[str] = None


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


def read_new_lines(log_path: Path, log_state: "Optional[dict[str, Any]]",
                   bootstrap_since: Optional[dt.datetime] = None,
                   ) -> LogRead:
    stored_ino = (log_state or {}).get("inode")
    stored_off = (log_state or {}).get("offset", 0)
    cold = stored_ino is None
    try:
        return _read_log(log_path, stored_ino, stored_off, cold, bootstrap_since)
    except OSError as exc:
        # stat, open or read — of the main log or of a rotated file we needed.
        # Any of them means we did not observe, so say so and return no cursor:
        # absence has to stay absence, because a placeholder cursor reads as a
        # warm resume on the next run and quietly disables the bootstrap.
        return LogRead([], None, cold, f"無法讀取 {log_path}：{exc}")


def _rotation_index(path: Path, stem: str, suffix: str) -> Optional[int]:
    """main1.log -> 1, main12.log -> 12, anything else -> None."""
    name = path.name
    middle = name[len(stem):len(name) - len(suffix)]
    return int(middle) if middle.isdigit() else None


def _drain_rotation_chain(log_path: Path, stored_ino: Optional[int], stored_off: int,
                          lines: "list[str]") -> Optional[str]:
    """Read every generation between the stored cursor and the live file.

    Returns a blocking reason when continuity cannot be proven. Draining only the
    file that matches the stored inode and then jumping straight to the live log
    skips whole generations whenever two rotations happen between checks — and
    the run would report healthy having never parsed them.
    """
    pattern = f"{log_path.stem}[0-9]*{log_path.suffix}"
    siblings: "list[tuple[int, Path, Any]]" = []
    for sibling in log_path.parent.glob(pattern):
        try:
            sib_st = sibling.stat()
        except OSError:
            continue  # a glob race, not an observation failure
        index = _rotation_index(sibling, log_path.stem, log_path.suffix)
        if index is not None:
            siblings.append((index, sibling, sib_st))

    resume = next((s for s in siblings if s[2].st_ino == stored_ino), None)
    if resume is None:
        return (f"日誌已輪替，找不到上次讀到的檔案（inode {stored_ino}）"
                "——中間可能有整段紀錄沒被讀到")

    resume_index, resume_path, resume_st = resume
    tail, _ = _read_complete_lines(resume_path, min(stored_off, resume_st.st_size))
    lines.extend(tail)
    # A lower rotation index is a more recent generation, so walk down towards
    # the live file and pick up everything that rotated in between.
    for _index, sibling, _sib_st in sorted(
            (s for s in siblings if s[0] < resume_index),
            key=lambda item: item[0], reverse=True):
        generation, _ = _read_complete_lines(sibling, 0)
        lines.extend(generation)
    return None


def _read_log(log_path: Path, stored_ino: Optional[int], stored_off: int, cold: bool,
              bootstrap_since: Optional[dt.datetime]) -> LogRead:
    lines: "list[str]" = []
    st = log_path.stat()

    if not cold and stored_ino == st.st_ino:
        # Same file. If it shrank it was truncated in place, so start over
        # rather than seeking past the end.
        start = stored_off if stored_off <= st.st_size else 0
    else:
        start = 0
        if not cold:
            blocked = _drain_rotation_chain(log_path, stored_ino, stored_off, lines)
            if blocked is not None:
                return LogRead([], None, cold, blocked)
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
    return LogRead(lines, {"inode": st.st_ino, "offset": new_off}, cold)


def format_alert(incident: "dict[str, Any]") -> str:
    causes = incident.get("causes", [])
    lines = ["⚠️ Claude 排程 watchdog：本機 scheduled-task 排程異常"]
    if CAUSE_BLIND in causes:
        lines.append(
            "原因：watchdog 這段期間無法觀測排程狀態，不知道排程是不是正常"
            f"——{incident.get('blind_reason')}")
        lines.append(
            "修法：確認上述檔案存在且可讀（排程日誌要 Claude desktop app 在跑才會產生；"
            "狀態檔則多半是權限或磁碟問題）。")
    if CAUSE_STALE in causes:
        lines.append(
            "原因：Claude desktop app 登入過期（session_stale_relogin），"
            "排程 session 無法取得 elevated scope，spawn 全部失敗。")
        lines.append(
            "修法：在 Mac mini 上重新登入 Claude desktop app；"
            "登入後排程會自動恢復（watchdog 會另發恢復通知）。")
    elif CAUSE_UNCONFIRMED in causes:
        lines.append("原因：scheduled task spawn 後逾時未見 Confirmed task run（原因未知）。")
        lines.append("修法：查 ~/Library/Logs/Claude/main.log 的 [CCDScheduledTasks] 段。")
    tasks = incident.get("affected_tasks", [])
    if tasks:
        lines.append("受影響 tasks：" + ", ".join(tasks))
    elif causes != [CAUSE_BLIND]:
        # With nothing observable there is no task list to speak of, so only say
        # this when we actually looked.
        lines.append("受影響 tasks：（尚未觀察到具體 task，僅見 oauth 失敗）")
    if incident.get("first_seen_at"):
        lines.append(f"事故起始：{incident['first_seen_at']}")
    return "\n".join(lines)


def format_recovery(incident: "dict[str, Any]", resolver: "dict[str, Any]") -> str:
    when = resolver.get("ts") or "時間不明"
    if resolver.get("kind") == "observation":
        head = f"✅ Claude 排程 watchdog 已恢復觀測：重新讀到排程日誌（{when}）。"
    else:
        head = ("✅ Claude 排程已恢復：偵測到 Confirmed task run for: "
                f"{resolver.get('task')}（{when}）。")
    lines = [head]
    tasks = incident.get("affected_tasks", [])
    if tasks:
        lines.append("事故期間受影響 tasks：" + ", ".join(tasks))
    return "\n".join(lines)


def evaluate(state: "dict[str, Any]", events: "list[LogEvent]", now: dt.datetime,
             pending_timeout_min: int = 15,
             cooldown_hours: int = 12,
             blocked_reason: Optional[str] = None) -> EvalResult:
    """Pure incident logic; mutates ``state`` in place.

    Deciding to alert is *not* recording that one was sent — the cooldown is
    only consumed once ``notify-dm`` confirms delivery, via ``mark_alerted``.

    ``blocked_reason`` says this run could not read the log at all. That is an
    incident cause in its own right, not a quiet healthy result: a watchdog that
    cannot see is the exact failure mode this tool exists to catch.
    """
    pending: "list[dict[str, Any]]" = list(state.get("pending_spawns", []))
    incident: "Optional[dict[str, Any]]" = state.get("active_incident")

    # Carry the unresolved failures in BEFORE walking the events, so the events
    # resolve them in log order. Comparing timestamps instead cannot work: the
    # log's one-second resolution cannot say whether a confirm or a cleared
    # dispatch in the same second came first, and the two orders mean opposite
    # things. Log order is the only ordering the log actually gives us.
    open_failures: "dict[str, Optional[dt.datetime]]" = {}
    stale_open = False
    stale_at: Optional[dt.datetime] = None
    if incident is not None:
        for task, ts_str in (incident.get("open_failures") or {}).items():
            open_failures[task] = parse_ts(ts_str)
        stale_open = bool(incident.get("stale_open"))
        stale_at = parse_ts(incident.get("stale_last_at"))

    window_first_failure: Optional[dt.datetime] = None
    window_last_failure: Optional[dt.datetime] = None
    window_stale_at: Optional[dt.datetime] = None
    newest_confirm_at: Optional[dt.datetime] = None
    last_confirm: Optional[LogEvent] = None

    def note_failure(task: Optional[str], ts: Optional[dt.datetime]) -> None:
        nonlocal window_first_failure, window_last_failure
        if task is not None:
            open_failures[task] = ts
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
            # Task-level accounting (see the limitation noted in the README): a
            # confirm says this task is running again, so it closes whatever was
            # outstanding for it.
            pending = [p for p in pending if p["task"] != ev.task]
            open_failures.pop(ev.task, None)
            if ev.ts is not None and (newest_confirm_at is None or ev.ts > newest_confirm_at):
                newest_confirm_at = ev.ts
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

    if stale_seen:
        stale_open = True
    if window_stale_at is not None and (stale_at is None or window_stale_at > stale_at):
        stale_at = window_stale_at

    # A stale-login latch blocks every spawn, so any confirm after the last
    # stale line proves the latch is gone and the failures it held down were
    # symptoms of it. Anything that broke after the latch stands on its own.
    newest_confirm = newest_confirm_at
    if stale_open and newest_confirm is not None and (stale_at is None
                                                      or newest_confirm > stale_at):
        stale_open = False
        if stale_at is not None:
            open_failures = {task: ts for task, ts in open_failures.items()
                             if ts is not None and ts > stale_at}

    # Being unable to look is a cause, not a clean bill of health. It reflects
    # this run only: if we could read the log, we are no longer blind.
    blind_open = blocked_reason is not None

    if not open_failures and not stale_open and not blind_open:
        if incident is None:
            return EvalResult(None, None, incident_active=False)
        resolver = incident.get("resolved_by")
        if last_confirm is not None and last_confirm.ts is not None:
            resolver = {"kind": "confirm", "task": last_confirm.task,
                        "ts": fmt_ts(last_confirm.ts)}
        elif resolver is None and CAUSE_BLIND in incident.get("causes", []):
            # Sight returning is itself the evidence — no confirm required.
            resolver = {"kind": "observation", "ts": fmt_ts(now)}
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
        return EvalResult(None, format_recovery(incident, resolver),
                          incident_active=False)

    if incident is None:
        incident = {
            "first_seen_at": fmt_ts(window_first_failure) or fmt_ts(now),
            "causes": [],
            "affected_tasks": [],
            "last_alert_at": None,
            "alerted_tasks": [],
            "alerted_causes": [],
            "last_failure_at": None,
        }
    causes = set()
    if stale_open:
        causes.add(CAUSE_STALE)
    if open_failures:
        causes.add(CAUSE_UNCONFIRMED)
    if blind_open:
        causes.add(CAUSE_BLIND)
    incident["causes"] = sorted(causes)
    incident["blind_reason"] = blocked_reason
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
    # A cause she has not been told about changes the remedy she is holding —
    # "check the file permissions" is worse than useless once the real problem
    # is an expired login. The cooldown dedupes repeats, not new diagnoses.
    new_causes = set(incident["causes"]) - set(incident.get("alerted_causes", []))
    cooldown_over = (last_alert is None
                     or (now - last_alert) >= dt.timedelta(hours=cooldown_hours))
    if cooldown_over or new_tasks or new_causes:
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
    incident["alerted_causes"] = incident["causes"]


def mark_recovered(state: "dict[str, Any]") -> None:
    """Close a resolved incident once its recovery notice actually went out.

    The mirror of ``mark_alerted``: a state transition that discharges a
    notification obligation is only committed after the notification lands.
    """
    state.pop("active_incident", None)


def _quarantine_state(path: Path, why: str) -> None:
    """Move an unusable state file aside so the next run starts clean.

    Without this the watchdog dies in load_json every hour and nothing is
    watching the scheduler until a human notices. The worst case of starting
    clean is one duplicate alert, not permanent silence.
    """
    quarantine = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, quarantine)
        print(f"state: unusable state file quarantined to {quarantine} ({why})",
              file=sys.stderr)
    except OSError as move_exc:
        print(f"state: unusable state file could not be quarantined: {move_exc}",
              file=sys.stderr)


def _state_is_usable(state: Any) -> bool:
    """Shape check for the decoded state.

    Only the parts evaluate() iterates or indexes are checked. Valid JSON in the
    wrong shape used to reach evaluate() and raise — ``{"pending_spawns": null}``
    became ``list(None)`` — which main() turned into an opaque exit 2, hourly,
    from a file that never changes on its own.
    """
    # A present key must hold the right type. An explicit ``null`` counts as
    # wrong, not as absent: ``state.get(key, default)`` returns the None rather
    # than the default, which is how ``{"pending_spawns": null}`` reached
    # ``list(None)``.
    def wrong(container: "dict[str, Any]", key: str, kind: type) -> bool:
        return key in container and not isinstance(container[key], kind)

    def optional_text(value: Any) -> bool:
        return value is None or isinstance(value, str)

    if not isinstance(state, dict):
        return False
    if wrong(state, "log", dict) or wrong(state, "pending_spawns", list):
        return False
    log = state.get("log") or {}
    if log.get("inode") is not None and not isinstance(log["inode"], int):
        return False
    if "offset" in log and (not isinstance(log["offset"], int) or log["offset"] < 0):
        return False
    for row in state.get("pending_spawns") or []:
        if not isinstance(row, dict) or not isinstance(row.get("task"), str):
            return False
        if not optional_text(row.get("ts")):
            return False
    incident = state.get("active_incident")
    if "active_incident" in state and incident is not None:
        if not isinstance(incident, dict):
            return False
        for key in ("causes", "affected_tasks", "alerted_tasks", "alerted_causes"):
            if wrong(incident, key, list):
                return False
            if not all(isinstance(item, str) for item in incident.get(key) or []):
                return False
        for key in ("first_seen_at", "last_alert_at", "last_failure_at",
                    "stale_last_at", "blind_reason"):
            if not optional_text(incident.get(key)):
                return False
        for key in ("open_failures", "resolved_by"):
            if key in incident and incident[key] is not None \
                    and not isinstance(incident[key], dict):
                return False
        for task, ts in (incident.get("open_failures") or {}).items():
            if not isinstance(task, str) or not optional_text(ts):
                return False
        for value in (incident.get("resolved_by") or {}).values():
            if not optional_text(value):
                return False
    return True


def load_json(path: Path) -> "dict[str, Any]":
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _quarantine_state(path, str(exc))
        return {}
    if not _state_is_usable(data):
        _quarantine_state(path, "結構不符預期")
        return {}
    return data


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


def _parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def _run(args: argparse.Namespace, state_problem: Optional[str] = None,
         use_state: bool = True) -> int:
    now = dt.datetime.now()
    state: "dict[str, Any]" = {}
    if use_state:
        try:
            state = load_json(args.state_file)
        except OSError as exc:
            # A bad chmod or a transient FS error must not switch the watchdog
            # off. Carry on with empty state and report the blindness — noisy
            # beats silent, which is the whole point of this tool.
            state_problem = f"無法讀取狀態檔 {args.state_file}：{exc}"
    bootstrap_since = now - dt.timedelta(hours=args.bootstrap_window_hours)
    read = read_new_lines(args.log_file, state.get("log"),
                          bootstrap_since=bootstrap_since)
    events = parse_events(read.lines)
    if read.cold:
        # A cold start reads whole files, so bound it by age: report what is
        # happening now, not an incident that was resolved months ago.
        events = [ev for ev in events if ev.ts is None or ev.ts >= bootstrap_since]
    blocked_reason = "；".join(
        reason for reason in (state_problem, read.blocked_reason) if reason) or None
    result = evaluate(state, events, now,
                      pending_timeout_min=args.pending_timeout_min,
                      cooldown_hours=args.cooldown_hours,
                      blocked_reason=blocked_reason)

    notified = False
    if args.notify:
        if result.alert_message:
            # Consume the cooldown only on confirmed delivery; a failed send
            # leaves the incident unalerted so the next run retries.
            if send_notification(result.alert_message, args.notify_bin):
                mark_alerted(state, now)
                notified = True
        if result.recovery_message:
            if send_notification(result.recovery_message, args.notify_bin):
                mark_recovered(state)
                notified = True

    if read.cursor is not None:
        # Only advance the cursor when we actually read something. A run that
        # saw nothing leaves the previous cursor alone rather than replacing it
        # with a placeholder that the next run would mistake for a resume.
        state["log"] = read.cursor
    state["checked_at"] = now.strftime(TS_FORMAT)
    state["last_result"] = {
        "ok": not result.incident_active,
        "scanned_lines": len(read.lines),
        "events": len(events),
        "notified": notified,
    }
    if blocked_reason:
        state["last_result"]["blocked"] = blocked_reason
    try:
        save_json(args.state_file, state)
    except OSError as exc:
        # The alert already went out above; losing the bookkeeping must not turn
        # a reported incident into an opaque exit 2.
        print(f"state: 無法寫入 {args.state_file}：{exc}", file=sys.stderr)

    payload = dict(state["last_result"])
    if result.alert_message:
        payload["alert"] = result.alert_message
    if result.recovery_message:
        payload["recovery"] = result.recovery_message
    if args.json or result.incident_active:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if result.incident_active else 0


def main() -> int:
    args = _parse_args()
    try:
        return _run(args)
    except Exception as exc:
        # The persisted state is the only input we are free to throw away, and a
        # file that never changes on its own would otherwise reproduce this exit
        # 2 every hour in silence — the exact failure this tool exists to catch.
        # _state_is_usable() enumerates the fields we know about, but no
        # enumeration can be proven complete; this net does not depend on it
        # being right. The cost when the fault lies elsewhere is a discarded
        # state file, which a cold start rebuilds.
        if args.state_file.exists():
            _quarantine_state(args.state_file, f"使用時發生例外：{exc}")
            try:
                return _run(args, use_state=False,
                            state_problem=f"狀態檔無法使用、已隔離：{exc}")
            except Exception as retry_exc:
                exc = retry_exc
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
