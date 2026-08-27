from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifiers"))

import claude_scheduler_watchdog as mod  # noqa: E402


# Real lines from ~/Library/Logs/Claude/main1.log, 2026-08-18/19 incident.
STALE_LINES = [
    "2026-08-18 12:05:53 [info] LocalSessions.start:",
    "2026-08-18 12:05:54 [error] OAuth token refresh failed: status=400, "
    'response={"error": "invalid_grant", "error_description": "Refresh token expired"}',
    "2026-08-18 12:05:54 [info] oauth authorize rejected with session_stale_relogin; "
    "sessionKey is valid but too old for the requested scope expansion",
    "2026-08-18 12:05:54 [error] Cannot start session local_de02f1d2-6fdd-4322-bed4-dddf96e9d676: "
    "Unable to start session. Sign in again to continue: session_stale_relogin",
]
SPAWN_LINE = (
    "2026-08-18 12:05:52 [info] [CCDScheduledTasks] Spawning new session for scheduled task "
    "process-replies { cronExpression: '0 12 * * *', fireAt: undefined, "
    "lastRunAt: '2026-08-18T19:05:52.415Z', missed: undefined }"
)
CLEARED_LINE = (
    "2026-08-18 12:15:09 [warn] [CCDScheduledTasks] Cleared stale pending dispatch for: process-replies"
)
CLEARED_LINE_2 = (
    "2026-08-18 23:15:29 [warn] [CCDScheduledTasks] Cleared stale pending dispatch for: daily-memory-sync"
)
NORMAL_SPAWN = (
    "2026-08-20 12:06:18 [info] [CCDScheduledTasks] Spawning new session for scheduled task "
    "process-replies { cronExpression: '0 12 * * *', fireAt: undefined, "
    "lastRunAt: '2026-08-20T19:06:18.415Z', missed: undefined }"
)
NORMAL_CONFIRM = "2026-08-20 12:06:18 [info] [CCDScheduledTasks] Confirmed task run for: process-replies"


def T(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


class TestParseEvents:
    def test_extracts_all_event_kinds(self):
        events = mod.parse_events([SPAWN_LINE, NORMAL_CONFIRM, CLEARED_LINE] + STALE_LINES)
        kinds = [e.kind for e in events]
        assert kinds.count("spawn") == 1
        assert kinds.count("confirm") == 1
        assert kinds.count("cleared") == 1
        assert kinds.count("stale") == 2
        spawn = next(e for e in events if e.kind == "spawn")
        assert spawn.task == "process-replies"
        assert spawn.ts == T("2026-08-18 12:05:52")
        cleared = next(e for e in events if e.kind == "cleared")
        assert cleared.task == "process-replies"
        confirm = next(e for e in events if e.kind == "confirm")
        assert confirm.task == "process-replies"

    def test_continuation_line_without_timestamp_carries_previous_ts(self):
        lines = [
            "2026-08-18 12:05:54 [info] oauth failed: authorize returned 403 {",
            "  error: '{\"type\":\"error\",\"error\":{\"type\":\"permission_error\","
            "\"details\":{\"error_code\":\"session_stale_relogin\"}}}'",
        ]
        events = mod.parse_events(lines)
        assert [e.kind for e in events] == ["stale"]
        assert events[0].ts == T("2026-08-18 12:05:54")

    def test_irrelevant_lines_produce_no_events(self):
        events = mod.parse_events([
            "2026-08-18 12:06:28 [info] [process-memory] trigger=interval tree_rss_sum=369MB",
            "not even a log line",
        ])
        assert events == []


class TestEvaluate:
    def test_normal_day_no_incident_no_alert(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events([NORMAL_SPAWN, NORMAL_CONFIRM]),
                              now=T("2026-08-20 13:00:00"))
        assert result.alert_message is None
        assert result.recovery_message is None
        assert state.get("active_incident") is None
        assert state.get("pending_spawns") == []

    def test_stale_relogin_alerts_on_next_hourly_run(self):
        state = {}
        events = mod.parse_events([SPAWN_LINE] + STALE_LINES)
        result = mod.evaluate(state, events, now=T("2026-08-18 13:00:00"))
        assert result.alert_message is not None
        assert "session_stale_relogin" in result.alert_message
        assert "重新登入" in result.alert_message           # 修法
        assert "process-replies" in result.alert_message    # affected task
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))   # notify-dm accepted it
        incident = state["active_incident"]
        assert incident["last_alert_at"] is not None
        assert incident["affected_tasks"] == ["process-replies"]
        # first_seen_at is the EARLIEST failure evidence in the window (the
        # expired 12:05:52 spawn), not the latest stale line
        assert incident["first_seen_at"] == "2026-08-18 12:05:52"

    def test_cooldown_suppresses_repeat_alert_same_tasks(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        # next hourly run: more stale noise, same task set
        result = mod.evaluate(state, mod.parse_events([CLEARED_LINE, STALE_LINES[2]]),
                              now=T("2026-08-18 14:00:00"))
        assert result.alert_message is None
        assert state["active_incident"] is not None

    def test_new_affected_task_realerts_within_cooldown(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        result = mod.evaluate(state, mod.parse_events([CLEARED_LINE_2]),
                              now=T("2026-08-18 23:59:00"))
        assert result.alert_message is not None
        assert "daily-memory-sync" in result.alert_message
        assert state["active_incident"]["affected_tasks"] == [
            "daily-memory-sync", "process-replies"]

    def test_cooldown_elapsed_realerts_same_tasks(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        result = mod.evaluate(state, mod.parse_events([STALE_LINES[2]]),
                              now=T("2026-08-19 01:30:00"))  # > 12h later
        assert result.alert_message is not None

    def test_recovery_notice_after_confirm(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        recover_line = ("2026-08-19 08:03:12 [info] [CCDScheduledTasks] "
                        "Confirmed task run for: daily-memory-sync")
        result = mod.evaluate(state, mod.parse_events([recover_line]),
                              now=T("2026-08-19 09:00:00"))
        assert result.alert_message is None
        assert result.recovery_message is not None
        assert "恢復" in result.recovery_message
        assert state.get("active_incident") is None

    def test_no_recovery_notice_if_never_alerted(self):
        state = {}
        # incident detected but nothing was ever delivered (no mark_alerted)
        mod.evaluate(state, mod.parse_events(STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        assert state["active_incident"]["last_alert_at"] is None
        result = mod.evaluate(state, mod.parse_events([NORMAL_CONFIRM]),
                              now=T("2026-08-20 13:00:00"))
        assert result.recovery_message is None
        assert state.get("active_incident") is None

    def test_confirm_after_stale_in_same_window_means_recovered_not_alerting(self):
        state = {}
        recover_line = ("2026-08-18 12:45:00 [info] [CCDScheduledTasks] "
                        "Confirmed task run for: process-replies")
        result = mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES + [recover_line]),
                              now=T("2026-08-18 13:00:00"))
        assert result.alert_message is None
        assert state.get("active_incident") is None

    def test_spawn_within_timeout_stays_pending_no_alert(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events([NORMAL_SPAWN]),
                              now=T("2026-08-20 12:07:00"))
        assert result.alert_message is None
        assert [p["task"] for p in state["pending_spawns"]] == ["process-replies"]

    def test_unconfirmed_spawn_past_timeout_alerts(self):
        state = {}
        mod.evaluate(state, mod.parse_events([NORMAL_SPAWN]),
                     now=T("2026-08-20 12:07:00"))
        result = mod.evaluate(state, [], now=T("2026-08-20 13:00:00"))
        assert result.alert_message is not None
        assert "process-replies" in result.alert_message
        assert "main.log" in result.alert_message           # 修法 pointer for unknown cause
        assert state["pending_spawns"] == []

    def test_pending_confirmed_next_window_is_cleared(self):
        state = {}
        mod.evaluate(state, mod.parse_events([NORMAL_SPAWN]),
                     now=T("2026-08-20 12:07:00"))
        result = mod.evaluate(state, mod.parse_events([NORMAL_CONFIRM]),
                              now=T("2026-08-20 12:08:00"))
        assert result.alert_message is None
        assert state["pending_spawns"] == []


class TestReadNewLines:
    def test_incremental_read_only_new_complete_lines(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("line1\nline2\n", encoding="utf-8")
        lines, st = mod.read_new_lines(log, {})
        assert lines == ["line1", "line2"]
        log.write_text("line1\nline2\nline3\npartial", encoding="utf-8")
        lines, st = mod.read_new_lines(log, st)
        assert lines == ["line3"]
        # partial line is not consumed until its newline arrives
        log.write_text("line1\nline2\nline3\npartial done\n", encoding="utf-8")
        lines, st = mod.read_new_lines(log, st)
        assert lines == ["partial done"]

    def test_rotation_reads_tail_of_rotated_then_new_file(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("old1\n", encoding="utf-8")
        _, st = mod.read_new_lines(log, {})
        # rotate: main.log → main1.log, new main.log appears
        with log.open("a", encoding="utf-8") as f:
            f.write("old2\n")
        log.rename(tmp_path / "main1.log")
        log.write_text("new1\n", encoding="utf-8")
        lines, st = mod.read_new_lines(log, st)
        assert lines == ["old2", "new1"]

    def test_missing_log_file_is_not_an_error(self, tmp_path):
        lines, st = mod.read_new_lines(tmp_path / "main.log", {})
        assert lines == []


class TestAlertDeliveryGating:
    """A page that never reached Yuting must not count against the cooldown.

    Round-1 review P1: ``evaluate`` used to stamp ``last_alert_at`` as soon as
    it decided to alert, so a missing/failing ``notify-dm`` silently bought the
    incident 12h of suppression — the exact silence this watchdog exists to break.
    """

    def _detect(self, state, now="2026-08-18 13:00:00"):
        return mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES), now=T(now))

    def test_evaluate_does_not_record_delivery_itself(self):
        state = {}
        assert self._detect(state).alert_message is not None
        assert state["active_incident"]["last_alert_at"] is None
        assert state["active_incident"]["alerted_tasks"] == []

    def test_undelivered_alert_is_retried_next_run(self):
        state = {}
        assert self._detect(state).alert_message is not None
        # delivery failed → main() never calls mark_alerted()
        result = mod.evaluate(state, mod.parse_events([STALE_LINES[2]]),
                              now=T("2026-08-18 14:00:00"))
        assert result.alert_message is not None

    def test_delivered_alert_suppresses_next_run(self):
        state = {}
        assert self._detect(state).alert_message is not None
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        assert state["active_incident"]["alerted_tasks"] == ["process-replies"]
        result = mod.evaluate(state, mod.parse_events([STALE_LINES[2]]),
                              now=T("2026-08-18 14:00:00"))
        assert result.alert_message is None

    def test_mark_alerted_without_incident_is_a_noop(self):
        state = {}
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        assert state == {}


class TestSendNotification:
    def test_missing_shim_reports_failure(self, tmp_path):
        assert mod.send_notification("hi", tmp_path / "absent") is False

    def test_nonzero_exit_reports_failure(self, tmp_path, monkeypatch):
        shim = tmp_path / "notify-dm"
        shim.write_text("", encoding="utf-8")
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"))
        assert mod.send_notification("hi", shim) is False

    def test_raised_exception_reports_failure(self, tmp_path, monkeypatch):
        shim = tmp_path / "notify-dm"
        shim.write_text("", encoding="utf-8")

        def blow_up(*a, **k):
            raise OSError("no exec")

        monkeypatch.setattr(mod.subprocess, "run", blow_up)
        assert mod.send_notification("hi", shim) is False

    def test_successful_send_reports_success(self, tmp_path, monkeypatch):
        shim = tmp_path / "notify-dm"
        shim.write_text("", encoding="utf-8")
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
        assert mod.send_notification("hi", shim) is True


class TestMainDeliveryGating:
    """End-to-end: the persisted state must reflect delivery, not intent."""

    def _run(self, tmp_path, monkeypatch, delivered):
        log = tmp_path / "main.log"
        log.write_text("\n".join([SPAWN_LINE] + STALE_LINES) + "\n", encoding="utf-8")
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(mod, "send_notification", lambda *a, **k: delivered)
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(log), "--state-file", str(state_file), "--notify",
        ])
        rc = mod.main()
        return rc, json.loads(state_file.read_text(encoding="utf-8"))

    def test_failed_delivery_leaves_incident_unalerted(self, tmp_path, monkeypatch):
        rc, state = self._run(tmp_path, monkeypatch, delivered=False)
        assert rc == 1
        assert state["active_incident"]["last_alert_at"] is None
        assert state["last_result"]["notified"] is False

    def test_successful_delivery_records_the_alert(self, tmp_path, monkeypatch):
        rc, state = self._run(tmp_path, monkeypatch, delivered=True)
        assert rc == 1
        assert state["active_incident"]["last_alert_at"] is not None
        assert state["active_incident"]["alerted_tasks"] == ["process-replies"]
        assert state["last_result"]["notified"] is True


class TestFormatting:
    def test_stale_alert_carries_cause_and_fix(self):
        incident = {"causes": ["session_stale_relogin"],
                    "affected_tasks": ["process-replies"],
                    "first_seen_at": "2026-08-18 12:05:54",
                    "last_alert_at": None, "alerted_tasks": [],
                    "last_failure_at": "2026-08-18 12:05:54"}
        msg = mod.format_alert(incident)
        assert "session_stale_relogin" in msg
        assert "重新登入" in msg
        assert "process-replies" in msg
