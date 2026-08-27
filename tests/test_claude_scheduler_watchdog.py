from __future__ import annotations

import datetime as dt
import json
import os
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

# One task times out while an unrelated one keeps running fine (no stale login).
SPAWN_A = (
    "2026-08-22 09:00:00 [info] [CCDScheduledTasks] Spawning new session for scheduled task "
    "meal-plan-cart { cronExpression: '0 9 * * *', fireAt: undefined }"
)
CONFIRM_A = "2026-08-22 09:40:00 [info] [CCDScheduledTasks] Confirmed task run for: meal-plan-cart"
CONFIRM_B = ("2026-08-22 09:30:00 [info] [CCDScheduledTasks] "
             "Confirmed task run for: travel-concierge-update")


def T(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def window_hours_for(oldest="2026-08-18 12:05:52"):
    """Cold-start window wide enough to reach the historical incident fixture.

    Derived from the fixture timestamp rather than hard-coded, so these tests do
    not quietly start passing for the wrong reason as the fixture ages.
    """
    return int((dt.datetime.now() - T(oldest)).total_seconds() // 3600) + 24


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
        # held open until the notice is delivered
        assert state["active_incident"]["resolved_by"] is not None
        mod.mark_recovered(state)
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
        read = mod.read_new_lines(log, {})
        assert read.lines == ["line1", "line2"]
        log.write_text("line1\nline2\nline3\npartial", encoding="utf-8")
        read = mod.read_new_lines(log, read.cursor)
        assert read.lines == ["line3"]
        # partial line is not consumed until its newline arrives
        log.write_text("line1\nline2\nline3\npartial done\n", encoding="utf-8")
        read = mod.read_new_lines(log, read.cursor)
        assert read.lines == ["partial done"]

    def test_rotation_reads_tail_of_rotated_then_new_file(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("old1\n", encoding="utf-8")
        first = mod.read_new_lines(log, {})
        # rotate: main.log → main1.log, new main.log appears
        with log.open("a", encoding="utf-8") as f:
            f.write("old2\n")
        log.rename(tmp_path / "main1.log")
        log.write_text("new1\n", encoding="utf-8")
        read = mod.read_new_lines(log, first.cursor)
        assert read.lines == ["old2", "new1"]

    def test_missing_log_file_is_reported_as_unobservable(self, tmp_path):
        read = mod.read_new_lines(tmp_path / "main.log", {})
        assert read.lines == []
        assert read.blocked_reason is not None


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
            "--bootstrap-window-hours", str(window_hours_for()),
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


class TestPerTaskRecovery:
    """A confirm only clears the task it names — except for a stale-login latch.

    Round-2 review [high]: recovery and fresh-incident suppression both keyed off
    "any later confirm", so one healthy task could bury another task's timeout
    and even trigger a false recovery notice.
    """

    def _timed_out_task(self, state):
        mod.evaluate(state, mod.parse_events([SPAWN_A]), now=T("2026-08-22 09:01:00"))
        return mod.evaluate(state, [], now=T("2026-08-22 09:20:00"))

    def test_other_task_confirm_does_not_suppress_a_new_failure(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_A]), now=T("2026-08-22 09:01:00"))
        # a different task confirms; meal-plan-cart is still unaccounted for
        result = mod.evaluate(state, mod.parse_events([CONFIRM_B]),
                              now=T("2026-08-22 09:31:00"))
        assert result.alert_message is not None
        assert "meal-plan-cart" in result.alert_message

    def test_other_task_confirm_does_not_recover_a_task_specific_incident(self):
        state = {}
        assert self._timed_out_task(state).alert_message is not None
        mod.mark_alerted(state, T("2026-08-22 09:20:00"))
        result = mod.evaluate(state, mod.parse_events([CONFIRM_B]),
                              now=T("2026-08-22 09:31:00"))
        assert result.recovery_message is None
        assert state.get("active_incident") is not None
        assert state["active_incident"]["affected_tasks"] == ["meal-plan-cart"]

    def test_same_task_confirm_resolves_its_own_failure(self):
        state = {}
        assert self._timed_out_task(state).alert_message is not None
        mod.mark_alerted(state, T("2026-08-22 09:20:00"))
        result = mod.evaluate(state, mod.parse_events([CONFIRM_A]),
                              now=T("2026-08-22 09:41:00"))
        assert result.recovery_message is not None
        mod.mark_recovered(state)
        assert state.get("active_incident") is None

    def test_stale_latch_still_recovers_globally(self):
        # A stale-login latch blocks every spawn, so any confirm after it proves
        # the latch is gone and the failures it caused were symptoms.
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        other_task = ("2026-08-19 08:03:12 [info] [CCDScheduledTasks] "
                      "Confirmed task run for: daily-memory-sync")
        result = mod.evaluate(state, mod.parse_events([other_task]),
                              now=T("2026-08-19 09:00:00"))
        assert result.recovery_message is not None
        mod.mark_recovered(state)
        assert state.get("active_incident") is None

    def test_failure_after_the_latch_survives_global_recovery(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        # login is fixed (a task confirms) but another task broke afterwards
        later_break = ("2026-08-19 08:30:00 [warn] [CCDScheduledTasks] "
                       "Cleared stale pending dispatch for: meal-plan-cart")
        recovered = ("2026-08-19 08:03:12 [info] [CCDScheduledTasks] "
                     "Confirmed task run for: daily-memory-sync")
        result = mod.evaluate(state, mod.parse_events([recovered, later_break]),
                              now=T("2026-08-19 09:00:00"))
        assert result.recovery_message is None
        assert state.get("active_incident") is not None
        assert "meal-plan-cart" in result.alert_message


class TestStatePersistence:
    """The state file must never be able to permanently blind the watchdog."""

    def test_save_json_swaps_in_atomically(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        mod.save_json(path, {"generation": 1})
        seen = []
        real_replace = mod.os.replace

        def spy(src, dst):
            # the live file still holds the previous, parseable payload
            assert json.loads(Path(dst).read_text(encoding="utf-8")) == {"generation": 1}
            seen.append((src, dst))
            real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", spy)
        mod.save_json(path, {"generation": 2})
        assert len(seen) == 1
        assert json.loads(path.read_text(encoding="utf-8")) == {"generation": 2}
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_corrupt_state_is_quarantined_not_fatal(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"log": {"inode": 7, "off', encoding="utf-8")  # truncated write
        assert mod.load_json(path) == {}
        assert (tmp_path / "state.json.corrupt").exists()
        assert not path.exists()

    def test_main_survives_a_corrupt_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text("{not json", encoding="utf-8")
        log = tmp_path / "main.log"
        log.write_text("\n".join([SPAWN_LINE] + STALE_LINES) + "\n", encoding="utf-8")
        monkeypatch.setattr(mod, "send_notification", lambda *a, **k: True)
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(log), "--state-file", str(state_file), "--notify",
            "--bootstrap-window-hours", str(window_hours_for()),
        ])
        assert mod.main() == 1  # incident still detected and alerted
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["active_incident"]["last_alert_at"] is not None


RECOVER_LINE = ("2026-08-19 08:03:12 [info] [CCDScheduledTasks] "
                "Confirmed task run for: daily-memory-sync")


class TestRecoveryDeliveryGating:
    """Clearing an incident discharges a notification obligation.

    Round-3 review [high]: ``main()`` persists state even without ``--notify``,
    and ``evaluate()`` dropped ``active_incident`` the moment it saw a resolving
    confirm. A documented "check only" run therefore ate the only recovery event
    and advanced the log offset, so no notifying run ever had anything to send.
    """

    def _alerted_then_resolved(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_LINE] + STALE_LINES),
                     now=T("2026-08-18 13:00:00"))
        mod.mark_alerted(state, T("2026-08-18 13:00:00"))
        result = mod.evaluate(state, mod.parse_events([RECOVER_LINE]),
                              now=T("2026-08-19 09:00:00"))
        return state, result

    def test_resolved_incident_is_kept_until_the_notice_is_delivered(self):
        state, result = self._alerted_then_resolved()
        assert result.recovery_message is not None
        assert state.get("active_incident") is not None

    def test_pending_recovery_survives_a_run_that_sent_nothing(self):
        state, first = self._alerted_then_resolved()
        again = mod.evaluate(state, [], now=T("2026-08-19 10:00:00"))
        assert again.recovery_message == first.recovery_message

    def test_mark_recovered_closes_it_for_good(self):
        state, _ = self._alerted_then_resolved()
        mod.mark_recovered(state)
        assert state.get("active_incident") is None
        assert mod.evaluate(state, [], now=T("2026-08-19 11:00:00")).recovery_message is None

    def test_a_new_failure_cancels_a_pending_recovery(self):
        state, _ = self._alerted_then_resolved()
        broke_again = ("2026-08-19 10:15:00 [warn] [CCDScheduledTasks] "
                       "Cleared stale pending dispatch for: meal-plan-cart")
        result = mod.evaluate(state, mod.parse_events([broke_again]),
                              now=T("2026-08-19 10:30:00"))
        assert result.recovery_message is None
        assert state["active_incident"].get("resolved_by") is None

    def test_incident_that_was_never_paged_still_closes_silently(self):
        state = {}
        mod.evaluate(state, mod.parse_events(STALE_LINES), now=T("2026-08-18 13:00:00"))
        result = mod.evaluate(state, mod.parse_events([NORMAL_CONFIRM]),
                              now=T("2026-08-20 13:00:00"))
        assert result.recovery_message is None
        assert state.get("active_incident") is None


class TestMainRecoveryGating:
    @staticmethod
    def _argv(log, state_file, notify):
        argv = ["claude_scheduler_watchdog.py",
                "--log-file", str(log), "--state-file", str(state_file),
                "--bootstrap-window-hours", str(window_hours_for())]
        if notify:
            argv.append("--notify")
        return argv

    def test_check_only_run_does_not_consume_the_recovery(self, tmp_path, monkeypatch):
        log = tmp_path / "main.log"
        state_file = tmp_path / "state.json"
        log.write_text("\n".join([SPAWN_LINE] + STALE_LINES) + "\n", encoding="utf-8")
        sent = []

        def capture(message, _notify_bin):
            sent.append(message)
            return True

        monkeypatch.setattr(mod, "send_notification", capture)

        monkeypatch.setattr(sys, "argv", self._argv(log, state_file, notify=True))
        assert mod.main() == 1
        assert len(sent) == 1  # the alert

        with log.open("a", encoding="utf-8") as f:
            f.write(RECOVER_LINE + "\n")

        # documented diagnostic invocation: observes the recovery, sends nothing
        monkeypatch.setattr(sys, "argv", self._argv(log, state_file, notify=False))
        assert mod.main() == 0
        assert len(sent) == 1

        # the next hourly notifying run must still deliver it
        monkeypatch.setattr(sys, "argv", self._argv(log, state_file, notify=True))
        assert mod.main() == 0
        assert len(sent) == 2
        assert "恢復" in sent[1]
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state.get("active_incident") is None


class TestColdStartBootstrap:
    """A fresh or quarantined state must not discard pre-rotation evidence.

    Round-3 review [high]: ``read_new_lines`` only consulted rotated siblings
    when a prior inode was stored, so after a fresh deploy — or the corrupt-state
    quarantine added in round 2 — an incident whose evidence had already rotated
    into ``main1.log`` was reconstructed as healthy.
    """

    @staticmethod
    def _recent(mins_ago, body):
        stamp = dt.datetime.now() - dt.timedelta(minutes=mins_ago)
        return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {body}"

    def test_cold_start_drains_recent_rotated_siblings(self, tmp_path):
        (tmp_path / "main1.log").write_text("rotated line\n", encoding="utf-8")
        main = tmp_path / "main.log"
        main.write_text("current line\n", encoding="utf-8")
        read = mod.read_new_lines(
            main, {}, bootstrap_since=dt.datetime.now() - dt.timedelta(hours=24))
        assert read.lines == ["rotated line", "current line"]
        assert read.cold is True

    def test_cold_start_skips_siblings_older_than_the_window(self, tmp_path):
        rotated = tmp_path / "main1.log"
        rotated.write_text("ancient line\n", encoding="utf-8")
        old = (dt.datetime.now() - dt.timedelta(days=9)).timestamp()
        os.utime(rotated, (old, old))
        main = tmp_path / "main.log"
        main.write_text("current line\n", encoding="utf-8")
        read = mod.read_new_lines(
            main, {}, bootstrap_since=dt.datetime.now() - dt.timedelta(hours=24))
        assert read.lines == ["current line"]

    def test_warm_start_does_not_re_read_siblings(self, tmp_path):
        main = tmp_path / "main.log"
        main.write_text("first\n", encoding="utf-8")
        first = mod.read_new_lines(main, {})
        (tmp_path / "main1.log").write_text("rotated line\n", encoding="utf-8")
        with main.open("a", encoding="utf-8") as f:
            f.write("second\n")
        read = mod.read_new_lines(
            main, first.cursor, bootstrap_since=dt.datetime.now() - dt.timedelta(hours=24))
        assert read.lines == ["second"]
        assert read.cold is False

    def test_main_sees_an_incident_whose_evidence_already_rotated(self, tmp_path, monkeypatch):
        (tmp_path / "main1.log").write_text("\n".join([
            self._recent(120, "[info] [CCDScheduledTasks] Spawning new session for "
                              "scheduled task process-replies { cronExpression: '0 12 * * *' }"),
            self._recent(119, "[error] Cannot start session local_abc: Unable to start "
                              "session. Sign in again to continue: session_stale_relogin"),
        ]) + "\n", encoding="utf-8")
        main = tmp_path / "main.log"
        main.write_text(
            self._recent(60, "[info] [process-memory] trigger=interval tree_rss_sum=369MB") + "\n",
            encoding="utf-8")
        state_file = tmp_path / "state.json"
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(main), "--state-file", str(state_file), "--notify",
        ])
        assert mod.main() == 1
        assert len(sent) == 1
        assert "session_stale_relogin" in sent[0]

    def test_cold_start_ignores_events_older_than_the_window(self, tmp_path, monkeypatch):
        # a months-old incident already in main.log must not page on first run
        main = tmp_path / "main.log"
        main.write_text("\n".join([SPAWN_LINE] + STALE_LINES) + "\n", encoding="utf-8")
        state_file = tmp_path / "state.json"
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(main), "--state-file", str(state_file), "--notify",
        ])
        assert mod.main() == 0
        assert sent == []


class TestCannotObserve:
    """"I could not look" must never be reported as "I looked and all is well".

    Round-4 handoff (Yuting approved the redesign 2026-08-27): three of the four
    rounds' true bugs were variants of the watchdog going blind while reporting
    healthy. Being unable to observe is now an incident cause of its own.
    """

    def test_missing_log_is_an_incident_not_a_clean_bill(self, tmp_path):
        state = {}
        result = mod.evaluate(state, [], now=T("2026-08-22 09:00:00"),
                              blocked_reason="無法讀取 main.log")
        assert result.incident_active is True
        assert result.alert_message is not None
        assert "無法讀取 main.log" in result.alert_message
        assert mod.CAUSE_BLIND in state["active_incident"]["causes"]

    def test_blind_alert_respects_its_cooldown(self):
        state = {}
        mod.evaluate(state, [], now=T("2026-08-22 09:00:00"), blocked_reason="boom")
        mod.mark_alerted(state, T("2026-08-22 09:00:00"))
        again = mod.evaluate(state, [], now=T("2026-08-22 10:00:00"), blocked_reason="boom")
        assert again.alert_message is None
        assert again.incident_active is True

    def test_regaining_sight_resolves_the_blind_incident(self):
        state = {}
        mod.evaluate(state, [], now=T("2026-08-22 09:00:00"), blocked_reason="boom")
        mod.mark_alerted(state, T("2026-08-22 09:00:00"))
        result = mod.evaluate(state, [], now=T("2026-08-22 10:00:00"))
        assert result.incident_active is False
        assert result.recovery_message is not None
        mod.mark_recovered(state)
        assert state.get("active_incident") is None

    def test_blindness_does_not_bury_a_real_failure(self):
        state = {}
        mod.evaluate(state, mod.parse_events([SPAWN_A]), now=T("2026-08-22 09:01:00"))
        mod.evaluate(state, [], now=T("2026-08-22 09:20:00"))
        mod.mark_alerted(state, T("2026-08-22 09:20:00"))
        # the log vanishes; regaining sight must not clear the task failure
        mod.evaluate(state, [], now=T("2026-08-22 09:30:00"), blocked_reason="boom")
        result = mod.evaluate(state, [], now=T("2026-08-22 09:40:00"))
        assert result.incident_active is True
        assert result.recovery_message is None
        assert "meal-plan-cart" in state["active_incident"]["affected_tasks"]

    def test_main_reports_an_unreadable_log_as_an_incident(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(tmp_path / "absent" / "main.log"),
            "--state-file", str(state_file), "--notify",
        ])
        assert mod.main() == 1
        assert len(sent) == 1
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["last_result"]["ok"] is False


class TestCursorIsNeverASentinel:
    """A run that observed nothing must not leave a cursor that looks warm.

    Round-4 review [high], reproduced before the fix: a missing-log run stored
    ``{"inode": None, "offset": 0}``, which is truthy, so the next run took the
    warm path and skipped both the rotated-sibling bootstrap and the age filter.
    """

    @staticmethod
    def _recent(mins_ago, body):
        stamp = dt.datetime.now() - dt.timedelta(minutes=mins_ago)
        return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {body}"

    def test_unreadable_log_persists_no_cursor(self, tmp_path):
        read = mod.read_new_lines(tmp_path / "absent.log", {})
        assert read.cursor is None
        assert read.blocked_reason is not None
        assert read.cold is True

    def test_missing_log_run_leaves_state_still_cold(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(mod, "send_notification", lambda *a, **k: True)
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py",
            "--log-file", str(tmp_path / "main.log"),
            "--state-file", str(state_file), "--notify",
        ])
        mod.main()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert not state.get("log", {}).get("inode")

        # the log reappears, with the decisive evidence already rotated out
        (tmp_path / "main1.log").write_text("\n".join([
            self._recent(120, "[info] [CCDScheduledTasks] Spawning new session for "
                              "scheduled task process-replies { cronExpression: '0 12 * * *' }"),
            self._recent(119, "[error] Cannot start session local_abc: Unable to start "
                              "session. Sign in again to continue: session_stale_relogin"),
        ]) + "\n", encoding="utf-8")
        (tmp_path / "main.log").write_text(
            self._recent(60, "[info] [process-memory] trigger=interval tree_rss_sum=369MB") + "\n",
            encoding="utf-8")

        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        assert mod.main() == 1
        assert any("session_stale_relogin" in message for message in sent)

    def test_read_reports_cold_from_the_cursor_alone(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("first\n", encoding="utf-8")
        first = mod.read_new_lines(log, {})
        assert first.cold is True
        second = mod.read_new_lines(log, first.cursor)
        assert second.cold is False


class TestSameTaskFailureAccounting:
    """Within one scan window a task can fail, recover, and fail again.

    Round-5 review [high]: ``note_failure`` kept only the *first* failure per
    task, so the resolution pass compared a later confirm against that stale
    timestamp and cleared a task that had broken again since.
    """

    FAIL_1000 = ("2026-08-22 10:00:00 [warn] [CCDScheduledTasks] "
                 "Cleared stale pending dispatch for: process-replies")
    CONFIRM_1005 = ("2026-08-22 10:05:00 [info] [CCDScheduledTasks] "
                    "Confirmed task run for: process-replies")
    FAIL_1010 = ("2026-08-22 10:10:00 [warn] [CCDScheduledTasks] "
                 "Cleared stale pending dispatch for: process-replies")
    CONFIRM_1015 = ("2026-08-22 10:15:00 [info] [CCDScheduledTasks] "
                    "Confirmed task run for: process-replies")

    def test_failure_after_a_confirm_in_the_same_window_survives(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events(
            [self.FAIL_1000, self.CONFIRM_1005, self.FAIL_1010]),
            now=T("2026-08-22 10:30:00"))
        assert result.incident_active is True
        assert result.alert_message is not None
        assert "process-replies" in result.alert_message

    def test_confirm_after_the_newest_failure_still_resolves(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events(
            [self.FAIL_1000, self.CONFIRM_1005, self.FAIL_1010, self.CONFIRM_1015]),
            now=T("2026-08-22 10:30:00"))
        assert result.incident_active is False
        assert result.alert_message is None

    def test_repeated_failures_before_a_confirm_all_clear(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events(
            [self.FAIL_1000, self.FAIL_1010, self.CONFIRM_1015]),
            now=T("2026-08-22 10:30:00"))
        assert result.incident_active is False

    def test_confirm_sharing_the_spawn_second_still_resolves_it(self):
        """A spawn and its confirm routinely land in the same second.

        Caught by re-running the real-log regression after switching
        ``note_failure`` to keep the newest failure: a spawn that aged out in one
        window recorded its own timestamp, and the confirm arriving in the next
        window carried that identical second, so a strict ``>`` left the task
        open forever. Real case: process-replies spawn/confirm at 12:06:15 on
        2026-08-19, which stalled the recovery notice for the whole incident.
        """
        state = {}
        mod.evaluate(state, mod.parse_events([NORMAL_SPAWN]), now=T("2026-08-20 13:00:00"))
        assert state["active_incident"]["open_failures"] == {
            "process-replies": "2026-08-20 12:06:18"}
        result = mod.evaluate(state, mod.parse_events([NORMAL_CONFIRM]),
                              now=T("2026-08-20 13:30:00"))
        assert result.incident_active is False

    def test_a_carried_over_failure_is_not_revived_by_an_old_timestamp(self):
        state = {}
        mod.evaluate(state, mod.parse_events([self.FAIL_1000]), now=T("2026-08-22 10:02:00"))
        mod.mark_alerted(state, T("2026-08-22 10:02:00"))
        result = mod.evaluate(state, mod.parse_events([self.CONFIRM_1005]),
                              now=T("2026-08-22 10:30:00"))
        assert result.recovery_message is not None


class TestStateFileFailuresAreLoud:
    """The state file must never be able to switch the watchdog off quietly.

    Round-5 review: the round-2 quarantine only covered decode errors, so a bad
    chmod made ``load_json`` raise straight into ``main``'s catch-all — exit 2,
    stderr into a log nobody reads, no DM. Same silence the tool exists to break,
    so this is treated as P1 rather than the reviewer's [medium].
    """

    @staticmethod
    def _recent(mins_ago, body):
        stamp = dt.datetime.now() - dt.timedelta(minutes=mins_ago)
        return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {body}"

    def _log(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("\n".join([
            self._recent(120, "[info] [CCDScheduledTasks] Spawning new session for "
                              "scheduled task process-replies { cronExpression: '0 12 * * *' }"),
            self._recent(119, "[error] Cannot start session local_abc: Unable to start "
                              "session. Sign in again to continue: session_stale_relogin"),
        ]) + "\n", encoding="utf-8")
        return log

    def test_unreadable_state_file_alerts_instead_of_exiting_two(self, tmp_path, monkeypatch):
        log = self._log(tmp_path)

        def denied(_path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(mod, "load_json", denied)
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py", "--log-file", str(log),
            "--state-file", str(tmp_path / "state.json"), "--notify",
        ])
        assert mod.main() == 1
        assert sent
        assert "狀態檔" in sent[0]
        # the log itself read fine, so the remedy must not point at main.log
        assert "確認 ~/Library/Logs/Claude/main.log" not in sent[0]

    def test_unwritable_state_file_does_not_mask_the_incident(self, tmp_path, monkeypatch):
        log = self._log(tmp_path)

        def denied(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(mod, "save_json", denied)
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py", "--log-file", str(log),
            "--state-file", str(tmp_path / "state.json"), "--notify",
        ])
        assert mod.main() == 1
        assert any("session_stale_relogin" in message for message in sent)


class TestLogReadFailuresAreLoud:
    """stat() succeeding is not the same as being able to read the file.

    Round-6 review [high]: the round-5 OSError guard only wrapped ``stat()``, so a
    log that stats fine but denies open raised inside ``_read_complete_lines``,
    straight into main()'s catch-all — exit 2, no DM.
    """

    @staticmethod
    def _denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    def test_unreadable_log_is_reported_not_raised(self, tmp_path, monkeypatch):
        log = tmp_path / "main.log"
        log.write_text("line\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_read_complete_lines", self._denied)
        read = mod.read_new_lines(log, {})
        assert read.lines == []
        assert read.cursor is None
        assert read.blocked_reason is not None

    def test_unreadable_rotated_sibling_is_reported(self, tmp_path, monkeypatch):
        sibling = tmp_path / "main1.log"
        sibling.write_text("rotated\n", encoding="utf-8")
        main = tmp_path / "main.log"
        main.write_text("current\n", encoding="utf-8")
        real = mod._read_complete_lines

        def selective(path, start):
            if path == sibling:
                raise PermissionError(13, "Permission denied")
            return real(path, start)

        monkeypatch.setattr(mod, "_read_complete_lines", selective)
        read = mod.read_new_lines(
            main, {}, bootstrap_since=dt.datetime.now() - dt.timedelta(hours=24))
        assert read.blocked_reason is not None
        assert read.cursor is None

    def test_main_alerts_instead_of_exiting_two(self, tmp_path, monkeypatch):
        log = tmp_path / "main.log"
        log.write_text("line\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_read_complete_lines", self._denied)
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py", "--log-file", str(log),
            "--state-file", str(tmp_path / "state.json"), "--notify",
        ])
        assert mod.main() == 1
        assert sent
        assert "無法觀測" in sent[0]

    def test_a_vanishing_sibling_is_tolerated(self, tmp_path):
        # glob races are not observation failures; only the main log matters here
        main = tmp_path / "main.log"
        main.write_text("current\n", encoding="utf-8")
        read = mod.read_new_lines(
            main, {}, bootstrap_since=dt.datetime.now() - dt.timedelta(hours=24))
        assert read.lines == ["current"]
        assert read.blocked_reason is None


class TestEventsResolveInLogOrder:
    """Same-second events are ordered by the log, not by their timestamps.

    Round-7 review [high]: resolution compared second-resolution timestamps, so a
    Confirmed followed in the log by a Cleared for the same task within the same
    second looked "confirmed after failure" and the failure was deleted. Seconds
    cannot express that ordering, so resolution now happens while walking the
    events, in the order the log lists them.
    """

    CONFIRM = ("2026-08-22 10:00:00 [info] [CCDScheduledTasks] "
               "Confirmed task run for: process-replies")
    CLEARED = ("2026-08-22 10:00:00 [warn] [CCDScheduledTasks] "
               "Cleared stale pending dispatch for: process-replies")

    def test_failure_after_a_same_second_confirm_survives(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events([self.CONFIRM, self.CLEARED]),
                              now=T("2026-08-22 10:30:00"))
        assert result.incident_active is True
        assert result.alert_message is not None
        assert "process-replies" in result.alert_message

    def test_confirm_after_a_same_second_failure_resolves(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events([self.CLEARED, self.CONFIRM]),
                              now=T("2026-08-22 10:30:00"))
        assert result.incident_active is False
        assert result.alert_message is None

    def test_a_confirm_closes_a_failure_carried_across_windows(self):
        # no timestamp comparison is involved: the confirm simply closes the task
        state = {}
        mod.evaluate(state, mod.parse_events([NORMAL_SPAWN]), now=T("2026-08-20 13:00:00"))
        mod.mark_alerted(state, T("2026-08-20 13:00:00"))
        result = mod.evaluate(state, mod.parse_events([NORMAL_CONFIRM]),
                              now=T("2026-08-20 13:30:00"))
        assert result.incident_active is False
        assert result.recovery_message is not None


class TestTaskLevelAccountingLimitation:
    """Accounting is per task, not per invocation — a deliberate limitation.

    Yuting approved dropping per-invocation tracking (issue #22, 2026-08-27):
    it was never in the acceptance criteria and the log carries no invocation id,
    so four of the PR's bugs came from trying to infer one. This test pins the
    resulting blind spot so nobody "fixes" it by accident; it is documented in
    the README.
    """

    SPAWN_0900 = ("2026-08-22 09:00:00 [info] [CCDScheduledTasks] Spawning new session "
                  "for scheduled task process-replies { cronExpression: '0 9 * * *' }")
    SPAWN_0910 = ("2026-08-22 09:10:00 [info] [CCDScheduledTasks] Spawning new session "
                  "for scheduled task process-replies { cronExpression: '0 9 * * *' }")
    CONFIRM_0912 = ("2026-08-22 09:12:00 [info] [CCDScheduledTasks] "
                    "Confirmed task run for: process-replies")

    def test_a_rerun_inside_the_window_masks_the_earlier_missed_run(self):
        state = {}
        result = mod.evaluate(state, mod.parse_events(
            [self.SPAWN_0900, self.SPAWN_0910, self.CONFIRM_0912]),
            now=T("2026-08-22 09:30:00"))
        # KNOWN LIMITATION: the 09:00 invocation never confirmed, but the task is
        # demonstrably running again, so no alert is raised.
        assert result.incident_active is False
        assert state["pending_spawns"] == []


class TestRotationChain:
    """Every generation between the cursor and the live file must be read.

    Round-7 review [high]: the warm-resume path drained only the sibling matching
    the stored inode and then jumped to the live main.log, so two rotations
    between checks skipped a whole generation — while advancing the cursor and
    reporting healthy.
    """

    def _rotate(self, d):
        """main1.log -> main2.log, main.log -> main1.log, fresh main.log."""
        if (d / "main2.log").exists():
            (d / "main2.log").unlink()
        if (d / "main1.log").exists():
            (d / "main1.log").rename(d / "main2.log")
        (d / "main.log").rename(d / "main1.log")

    def test_two_rotations_lose_nothing(self, tmp_path):
        main = tmp_path / "main.log"
        main.write_text("A1\nA2\n", encoding="utf-8")
        first = mod.read_new_lines(main, {})
        assert first.lines == ["A1", "A2"]

        with main.open("a", encoding="utf-8") as f:
            f.write("A3\n")
        self._rotate(tmp_path)
        main.write_text("B1\nB2\n", encoding="utf-8")
        self._rotate(tmp_path)
        main.write_text("C1\n", encoding="utf-8")

        read = mod.read_new_lines(main, first.cursor)
        assert read.lines == ["A3", "B1", "B2", "C1"]
        assert read.blocked_reason is None

    def test_single_rotation_still_works(self, tmp_path):
        main = tmp_path / "main.log"
        main.write_text("A1\n", encoding="utf-8")
        first = mod.read_new_lines(main, {})
        with main.open("a", encoding="utf-8") as f:
            f.write("A2\n")
        self._rotate(tmp_path)
        main.write_text("B1\n", encoding="utf-8")
        read = mod.read_new_lines(main, first.cursor)
        assert read.lines == ["A2", "B1"]

    def test_aged_out_cursor_blocks_instead_of_skipping(self, tmp_path):
        main = tmp_path / "main.log"
        main.write_text("A1\n", encoding="utf-8")
        first = mod.read_new_lines(main, {})
        # the generation our cursor pointed at is gone entirely
        main.unlink()
        main.write_text("B1\n", encoding="utf-8")
        read = mod.read_new_lines(main, first.cursor)
        assert read.blocked_reason is not None
        assert read.cursor is None
        assert read.lines == []

    def test_truncation_in_place_restarts_rather_than_blocking(self, tmp_path):
        main = tmp_path / "main.log"
        main.write_text("A1\nA2\n", encoding="utf-8")
        first = mod.read_new_lines(main, {})
        with main.open("w", encoding="utf-8") as f:  # same inode, shorter
            f.write("B1\n")
        read = mod.read_new_lines(main, first.cursor)
        assert read.lines == ["B1"]
        assert read.blocked_reason is None


class TestCauseChangeAlerts:
    """A changed diagnosis must reach her even inside the cooldown.

    Round-8 review [high] (and the round-1 P2 that was never actioned): alert
    eligibility looked only at elapsed time and newly affected tasks. So a
    cannot-observe page followed an hour later by a real login expiry left her
    holding the file-permission remedy for another 11 hours.
    """

    STALE = ("2026-08-22 10:00:00 [error] Cannot start session local_y: Unable to "
             "start session. Sign in again to continue: session_stale_relogin")

    def test_a_new_cause_alerts_inside_the_cooldown(self):
        state = {}
        mod.evaluate(state, [], now=T("2026-08-22 09:00:00"), blocked_reason="boom")
        mod.mark_alerted(state, T("2026-08-22 09:00:00"))
        result = mod.evaluate(state, mod.parse_events([self.STALE]),
                              now=T("2026-08-22 10:01:00"))
        assert result.alert_message is not None
        assert "重新登入" in result.alert_message          # the remedy that changed
        assert mod.CAUSE_STALE in state["active_incident"]["causes"]

    def test_an_unchanged_cause_stays_deduped(self):
        state = {}
        mod.evaluate(state, mod.parse_events([self.STALE]), now=T("2026-08-22 10:01:00"))
        mod.mark_alerted(state, T("2026-08-22 10:01:00"))
        result = mod.evaluate(state, mod.parse_events([self.STALE]),
                              now=T("2026-08-22 11:00:00"))
        assert result.alert_message is None

    def test_delivery_records_the_causes_it_covered(self):
        state = {}
        mod.evaluate(state, [], now=T("2026-08-22 09:00:00"), blocked_reason="boom")
        mod.mark_alerted(state, T("2026-08-22 09:00:00"))
        assert state["active_incident"]["alerted_causes"] == [mod.CAUSE_BLIND]

    def test_an_undelivered_alert_does_not_record_causes(self):
        state = {}
        mod.evaluate(state, [], now=T("2026-08-22 09:00:00"), blocked_reason="boom")
        assert state["active_incident"].get("alerted_causes", []) == []


class TestStateShapeValidation:
    """Valid JSON in the wrong shape must not disable the watchdog.

    Round-8 review [high]: ``{"pending_spawns": null}`` decodes fine, then
    ``evaluate`` did ``list(None)`` and main()'s catch-all turned the TypeError
    into exit 2 with no DM — every hour, from an unchanging file.
    """

    def test_wrong_shaped_state_is_quarantined(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"pending_spawns": None}), encoding="utf-8")
        assert mod.load_json(path) == {}
        assert (tmp_path / "state.json.corrupt").exists()
        assert not path.exists()

    def test_non_dict_state_is_quarantined(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps(["not", "a", "state"]), encoding="utf-8")
        assert mod.load_json(path) == {}
        assert (tmp_path / "state.json.corrupt").exists()

    def test_wrong_shaped_incident_is_quarantined(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"active_incident": {"causes": "stale"}}),
                        encoding="utf-8")
        assert mod.load_json(path) == {}

    def test_a_well_formed_state_survives(self, tmp_path):
        path = tmp_path / "state.json"
        payload = {
            "log": {"inode": 7, "offset": 12},
            "pending_spawns": [{"task": "process-replies", "ts": "2026-08-22 09:00:00"}],
            "active_incident": {"causes": ["session_stale_relogin"],
                                "affected_tasks": ["process-replies"],
                                "open_failures": {"process-replies": "2026-08-22 09:00:00"}},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert mod.load_json(path) == payload
        assert not (tmp_path / "state.json.corrupt").exists()

    def test_main_survives_a_wrong_shaped_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"pending_spawns": None}), encoding="utf-8")
        log = tmp_path / "main.log"
        log.write_text("2026-08-22 10:00:00 [info] nothing interesting\n", encoding="utf-8")
        monkeypatch.setattr(mod, "send_notification", lambda *a, **k: True)
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py", "--log-file", str(log),
            "--state-file", str(state_file), "--notify",
        ])
        assert mod.main() == 0  # not the opaque exit 2


class TestMalformedNestedState:
    """No state file may cost the watchdog its voice, whatever is inside it.

    Round-9 review [high]: the round-8 shape check validated container types but
    not the scalars actually consumed, so ``{"log": {"offset": "0"}}`` passed and
    then ``_read_log`` compared a str with an int — exit 2, no DM, hourly, from a
    file that never changes on its own.

    Enumerating fields is necessary but cannot be sufficient — nine rounds have
    shown that a partly applied invariant gets found. So the last test here
    covers the structural net: whatever slips past validation, using the state
    must degrade to a clean start rather than silence.
    """

    @staticmethod
    def _recent(mins_ago, body):
        stamp = dt.datetime.now() - dt.timedelta(minutes=mins_ago)
        return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {body}"

    def _run_with(self, tmp_path, monkeypatch, payload):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(payload), encoding="utf-8")
        log = tmp_path / "main.log"
        log.write_text("\n".join([
            self._recent(120, "[info] [CCDScheduledTasks] Spawning new session for "
                              "scheduled task process-replies { cronExpression: '0 12 * * *' }"),
            self._recent(119, "[error] Cannot start session local_abc: Unable to start "
                              "session. Sign in again to continue: session_stale_relogin"),
        ]) + "\n", encoding="utf-8")
        sent = []
        monkeypatch.setattr(mod, "send_notification",
                            lambda message, _bin: (sent.append(message), True)[1])
        monkeypatch.setattr(sys, "argv", [
            "claude_scheduler_watchdog.py", "--log-file", str(log),
            "--state-file", str(state_file), "--notify",
        ])
        return mod.main(), sent

    def test_string_offset_still_reports(self, tmp_path, monkeypatch):
        rc, sent = self._run_with(tmp_path, monkeypatch, {"log": {"inode": 1, "offset": "0"}})
        assert rc == 1
        assert sent

    def test_numeric_pending_timestamp_still_reports(self, tmp_path, monkeypatch):
        rc, sent = self._run_with(
            tmp_path, monkeypatch,
            {"pending_spawns": [{"task": "process-replies", "ts": 12345}]})
        assert rc == 1
        assert sent

    def test_numeric_open_failure_value_still_reports(self, tmp_path, monkeypatch):
        rc, sent = self._run_with(tmp_path, monkeypatch, {"active_incident": {
            "causes": [], "affected_tasks": [], "open_failures": {"process-replies": 5}}})
        assert rc == 1
        assert sent

    def test_state_that_only_breaks_at_use_is_quarantined_and_retried(
            self, tmp_path, monkeypatch):
        # pretend validation passed: the net, not the enumeration, must save it
        monkeypatch.setattr(mod, "_state_is_usable", lambda _state: True)
        rc, sent = self._run_with(
            tmp_path, monkeypatch,
            {"pending_spawns": [{"task": "process-replies", "ts": 12345}]})
        assert rc == 1
        assert sent
        assert (tmp_path / "state.json.corrupt").exists()

    def test_a_genuine_bug_still_surfaces_as_exit_two(self, tmp_path, monkeypatch):
        # the net must not swallow failures that have nothing to do with state
        monkeypatch.setattr(mod, "parse_events", lambda _lines: 1 / 0)
        rc, _sent = self._run_with(tmp_path, monkeypatch, {})
        assert rc == 2


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
