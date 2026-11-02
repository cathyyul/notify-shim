"""Regressions for stale binding state and ambiguous notify timeouts (#61)."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifiers"))
import claude_scheduler_watchdog as mod
import notify_core

SPAWN = "2026-11-01 21:28:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task thursday-draft { }"
START = "2026-11-01 21:28:00 [info] Starting local session local_thursday in /fixture"
TIMING = "2026-11-01 21:28:01 [info] [CCD start-timing] local_thursday preflight=5ms"
ERROR = "2026-11-01 21:28:02 [warn] [CCD CycleHealth] local_thursday api_error (success): You've hit your session limit"


def usage(events):
    return [e.task for e in events if e.kind == "usage_limit"]


def test_legacy_fifo_offset_cannot_misattribute_next_spawn():
    state = {"awaiting": ["process-github-queue", "process-replies", "wednesday-draft"],
             "session_task": {"local_old": "wrong-task"}}
    events = mod.parse_events([SPAWN, START, TIMING, ERROR], state)
    assert usage(events) == ["thursday-draft"]
    assert not state.get("awaiting")
    assert "local_old" not in state["session_task"]


def test_direct_start_binds_before_start_timing_and_across_reads():
    state = {}
    mod.parse_events([SPAWN], state)
    mod.parse_events([START], state)
    assert usage(mod.parse_events([ERROR], state)) == ["thursday-draft"]


def test_spawn_inside_open_window_makes_binding_ambiguous(capsys):
    # A second spawn before the first one's launch line: launch order is unknown,
    # so neither launch may be attributed to a task (never attribute to the wrong one).
    state = {}
    mod.parse_events([SPAWN.replace("thursday-draft", "old-task")], state)
    assert usage(mod.parse_events([SPAWN, START, ERROR], state)) == []
    assert "ambiguous" in capsys.readouterr().err


def _spawn(task, ts="21:28:00"):
    return SPAWN.replace("thursday-draft", task).replace("21:28:00", ts)


def _start(sid, ts="21:28:01"):
    return START.replace("local_thursday", sid).replace("21:28:00", ts)


def _error(sid):
    return ERROR.replace("local_thursday", sid)


def test_interleaved_burst_spawns_never_misattribute():
    # Wake catch-up (main2.log 2026-10-13 13:35:56): spawn A, spawn B, start, start.
    state = {}
    lines = [_spawn("task-a"), _spawn("task-b"), _start("local_1"), _start("local_2"),
             _error("local_1"), _error("local_2")]
    assert usage(mod.parse_events(lines, state)) == []


def test_burst_straggler_after_next_spawn_keeps_window_ambiguous():
    # A burst still owes a launch line when another spawn arrives: the next launches
    # could be the straggler or the new spawn's, so none of them is bound.
    state = {}
    lines = [_spawn("task-a"), _spawn("task-b"), _start("local_1"),
             _spawn("task-c", "21:28:02"), _start("local_2", "21:28:02"),
             _start("local_3", "21:28:03"),
             _error("local_1"), _error("local_2"), _error("local_3")]
    assert usage(mod.parse_events(lines, state)) == []


def test_after_burst_resolves_next_spawn_binds_normally():
    state = {}
    mod.parse_events([_spawn("task-a"), _spawn("task-b"), _start("local_1"), _start("local_2")], state)
    lines = [_spawn("task-c", "21:40:00"), _start("local_3", "21:40:00"), _error("local_3")]
    assert usage(mod.parse_events(lines, state)) == ["task-c"]


@pytest.mark.parametrize("gap", ["lines", "time"])
def test_unmatched_spawn_expires_before_unrelated_start(gap, capsys):
    state = {}
    mod.parse_events([SPAWN], state)
    if gap == "lines":
        mod.parse_events(["2026-11-01 21:28:00 [info] unrelated"] * 21, state)
        start = START
    else:
        start = START.replace("21:28:00", "22:28:00")
    assert usage(mod.parse_events([start, ERROR], state)) == []
    assert "discard" in capsys.readouterr().err


def test_old_start_timing_alone_does_not_rebind_new_spawn():
    assert usage(mod.parse_events([SPAWN, TIMING, ERROR])) == []


@pytest.mark.parametrize("pending", [None, {"task": "task", "ts": 123, "lines_left": 20},
                                     {"task": "task", "ts": "bad", "lines_left": 20},
                                     {"task": "task", "ts": "2026-11-01 21:28:00", "lines_left": -1},
                                     {"task": "", "ts": "2026-11-01 21:28:00", "lines_left": 20,
                                      "outstanding": 0},
                                     {"task": "task", "ts": "2026-11-01 21:28:00", "lines_left": 20,
                                      "outstanding": 2}])
def test_pending_binding_state_is_validated(pending):
    state = {"session_binding": {"version": 2, "session_task": {}, "pending_start": pending}}
    assert mod._state_is_usable(state) is (pending is None)


def test_notification_uses_shim_route_budget(tmp_path, monkeypatch):
    shim = tmp_path / "notify-dm"
    shim.touch()
    budgets, calls = [], []
    monkeypatch.setattr(notify_core, "route_send_budget", lambda route: budgets.append(route) or 163)
    def send(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", send)
    assert mod.send_notification("fixture", shim) is True
    assert budgets == ["dm"] and calls == [163]


def test_unknown_timeout_does_not_repeat_incident_inside_cooldown(tmp_path, monkeypatch):
    shim = tmp_path / "notify-dm"
    shim.touch()
    log = tmp_path / "main.log"
    log.write_text("\n".join([SPAWN, START, ERROR]) + "\n")
    state_file = tmp_path / "state.json"
    args = argparse.Namespace(log_file=log, state_file=state_file, notify=True,
                              notify_bin=shim, bootstrap_window_hours=100000,
                              pending_timeout_min=15, cooldown_hours=12, json=False)
    monkeypatch.setattr(notify_core, "route_send_budget", lambda route: 163)
    # The shim might have delivered one channel before timing out.
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(mod.subprocess, "run", timeout)
    assert mod._run(args) == 1
    first = json.loads(state_file.read_text())
    assert first["active_incident"]["last_alert_at"] is not None
    assert first["last_result"]["alert_delivery_unknown"] is True
    assert first["last_result"]["notified"] is False
    assert mod._run(args) == 1
    assert len(calls) == 1


def test_timeout_reports_unknown_not_confirmed_delivery(tmp_path, monkeypatch):
    shim = tmp_path / "notify-dm"
    shim.touch()
    monkeypatch.setattr(notify_core, "route_send_budget", lambda route: 163)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(mod.subprocess, "run", timeout)
    assert mod.send_notification("fixture", shim) is None
