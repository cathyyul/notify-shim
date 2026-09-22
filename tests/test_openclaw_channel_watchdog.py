from __future__ import annotations

import json
import subprocess
import sys

import pytest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifiers"))

import openclaw_channel_watchdog as mod  # noqa: E402


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_line_local_webhook_healthy_when_not_404(monkeypatch):
    monkeypatch.setattr(
        mod,
        "http_post",
        lambda *a, **k: mod.HttpResult(status_code=400, body="bad signature"),
    )

    result = mod.check_line_local_webhook(gateway_port=18789, timeout=1)

    assert result.ok is True
    assert result.status == "line_local_route_present"


def test_line_local_webhook_fails_on_404(monkeypatch):
    monkeypatch.setattr(
        mod,
        "http_post",
        lambda *a, **k: mod.HttpResult(status_code=404, body="not found"),
    )

    result = mod.check_line_local_webhook(gateway_port=18789, timeout=1)

    assert result.ok is False
    assert result.status == "line_local_route_missing"
    assert "Restart OpenClaw gateway" in result.suggested_next_step


def test_http_post_converts_url_error_to_result(monkeypatch):
    def raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", raise_url_error)

    result = mod.http_post("http://127.0.0.1:18789/line/webhook", timeout=1)

    assert result.status_code == 0
    assert "connection refused" in result.body


def test_line_local_webhook_unreachable_is_unhealthy(monkeypatch):
    monkeypatch.setattr(
        mod,
        "http_post",
        lambda *a, **k: mod.HttpResult(status_code=0, body="connection refused"),
    )

    result = mod.check_line_local_webhook(gateway_port=18789, timeout=1)

    assert result.ok is False
    assert result.status == "line_local_route_unreachable"
    assert "Restart OpenClaw gateway" in result.suggested_next_step


def test_line_official_webhook_fails_without_token(monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)

    result = mod.check_line_official_webhook(config={}, timeout=1)

    assert result.ok is False
    assert result.status == "missing_line_token"


def test_line_token_handles_null_channels(monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)

    assert mod.get_line_token({"channels": None}) is None


def test_line_official_webhook_sends_json_content_type(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return mod.HttpResult(status_code=200, body='{"success": true}')

    monkeypatch.setattr(mod, "http_post", fake_post)
    result = mod.check_line_official_webhook(
        {"channels": {"line": {"channelAccessToken": "token"}}},
        timeout=1,
    )

    assert result.ok is True
    assert calls[0][1]["headers"]["Content-Type"] == "application/json"
    assert calls[0][1]["data"] == b"{}"


def test_line_official_webhook_http_200_success_false_is_unhealthy(monkeypatch):
    monkeypatch.setattr(
        mod,
        "http_post",
        lambda *a, **k: mod.HttpResult(status_code=200, body='{"success": false}'),
    )

    result = mod.check_line_official_webhook(
        {"channels": {"line": {"channelAccessToken": "token"}}},
        timeout=1,
    )

    assert result.ok is False
    assert result.status == "line_official_webhook_failed"


def test_whatsapp_probe_healthy(monkeypatch):
    payload = {
        "channels": {"whatsapp": {"configured": True}},
        "channelAccounts": {
            "whatsapp": [{
                "configured": True,
                "linked": True,
                "running": True,
                "connected": True,
                "healthState": "healthy",
            }]
        },
    }
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout: _Proc(0, stdout=json.dumps(payload)),
    )

    result = mod.check_whatsapp(timeout_ms=1000, openclaw_bin="/custom/openclaw")

    assert result.ok is True
    assert result.status == "healthy"


def test_run_command_prepends_homebrew_paths_for_launchd(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.run_command(["/opt/homebrew/bin/openclaw", "--version"], timeout=1)

    path_parts = captured["env"]["PATH"].split(":")
    assert path_parts[:3] == [
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/node/bin",
        "/usr/local/bin",
    ]


def test_run_command_includes_absolute_command_directory(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.run_command(["/custom/bin/openclaw", "--version"], timeout=1)

    assert captured["env"]["PATH"].split(":")[0] == "/custom/bin"


def test_whatsapp_probe_unlinked_suggests_relink(monkeypatch):
    payload = {
        "channels": {"whatsapp": {"configured": True}},
        "channelAccounts": {
            "whatsapp": [{
                "configured": True,
                "linked": False,
                "running": True,
                "connected": False,
                "healthState": "unhealthy",
            }]
        },
    }
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout: _Proc(0, stdout=json.dumps(payload)),
    )

    result = mod.check_whatsapp(timeout_ms=1000, openclaw_bin="/custom/openclaw")

    assert result.ok is False
    assert result.status == "whatsapp_unhealthy"
    assert "login --channel whatsapp" in result.suggested_next_step


def test_whatsapp_probe_nonzero_exit_is_unhealthy(monkeypatch):
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout: _Proc(1, stderr="gateway unreachable"),
    )

    result = mod.check_whatsapp(timeout_ms=1000, openclaw_bin="/custom/openclaw")

    assert result.ok is False
    assert result.status == "whatsapp_probe_failed"
    assert "gateway unreachable" in result.detail


def test_whatsapp_probe_handles_null_json_sections(monkeypatch):
    payload = {"channels": None, "channelAccounts": None}
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout: _Proc(0, stdout=json.dumps(payload)),
    )

    result = mod.check_whatsapp(timeout_ms=1000, openclaw_bin="/custom/openclaw")

    assert result.ok is False
    assert result.status == "whatsapp_unhealthy"


def test_cooldown_elapsed_false_for_recent_timestamp(monkeypatch):
    fixed = mod.dt.datetime(2026, 6, 21, 12, 0, tzinfo=mod.dt.timezone.utc)
    monkeypatch.setattr(mod, "now_utc", lambda: fixed)
    state = {"last_alert_at": "2026-06-21T11:45:00+00:00"}

    assert mod.cooldown_elapsed(state, "last_alert_at", cooldown_minutes=30) is False


def test_main_writes_state_and_returns_unhealthy_without_spam(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    config_file = tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    config_file.write_text("{}")
    notify_bin.write_text("#!/bin/sh\nexit 0\n")
    notify_bin.chmod(0o755)
    calls = []
    monkeypatch.setattr(
        mod,
        "evaluate_channels",
        lambda *a, **k: [mod.CheckResult("whatsapp", False, "bad", "broken", "restart")],
    )
    monkeypatch.setattr(mod, "send_notification", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "watchdog",
            "--channels", "whatsapp",
            "--notify",
            "--state-file", str(state_file),
            "--config-file", str(config_file),
            "--notify-bin", str(notify_bin),
        ],
    )

    assert mod.main() == 1
    saved = json.loads(state_file.read_text())
    assert saved["ok"] is False
    assert saved["results"][0]["channel"] == "whatsapp"
    assert len(calls) == 1


def test_restart_mode_rechecks_and_clears_incident_when_recovered(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    config_file = tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    config_file.write_text("{}")
    notify_bin.write_text("#!/bin/sh\nexit 0\n")
    notify_bin.chmod(0o755)
    calls = {"evaluate": 0, "restart": 0, "notify": 0}

    def fake_evaluate(*args, **kwargs):
        calls["evaluate"] += 1
        if calls["evaluate"] == 1:
            return [mod.CheckResult("whatsapp", False, "bad", "broken", "restart")]
        return [mod.CheckResult("whatsapp", True, "healthy", "ok")]

    def fake_restart(openclaw_bin):
        calls["restart"] += 1
        assert openclaw_bin == "openclaw"
        return mod.CheckResult("gateway", True, "gateway_restart_ok", "restarted")

    monkeypatch.setattr(mod, "evaluate_channels", fake_evaluate)
    monkeypatch.setattr(mod, "restart_gateway", fake_restart)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        mod,
        "send_notification",
        lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "watchdog",
            "--channels", "whatsapp",
            "--notify",
            "--recovery-mode", "restart",
            "--state-file", str(state_file),
            "--config-file", str(config_file),
            "--notify-bin", str(notify_bin),
            "--openclaw-bin", "openclaw",
        ],
    )

    assert mod.main() == 0
    saved = json.loads(state_file.read_text())
    assert saved["ok"] is True
    assert "active_incident" not in saved
    assert calls == {"evaluate": 2, "restart": 1, "notify": 0}


def test_restart_mode_does_not_restart_same_incident_twice(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    config_file = tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    state_file.write_text(json.dumps({
        "active_incident": {
            "key": "whatsapp",
            "first_seen_at": "2026-06-21T11:00:00+00:00",
            "restart_attempted_at": "2026-06-21T11:01:00+00:00",
            "last_alert_at": None,
        }
    }))
    config_file.write_text("{}")
    notify_bin.write_text("#!/bin/sh\nexit 0\n")
    notify_bin.chmod(0o755)
    calls = {"restart": 0, "notify": 0}
    monkeypatch.setattr(
        mod,
        "evaluate_channels",
        lambda *a, **k: [mod.CheckResult("whatsapp", False, "bad", "broken", "restart")],
    )
    monkeypatch.setattr(mod, "restart_gateway", lambda openclaw_bin: calls.__setitem__("restart", calls["restart"] + 1))
    monkeypatch.setattr(mod, "send_notification", lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "watchdog",
            "--channels", "whatsapp",
            "--notify",
            "--recovery-mode", "restart",
            "--state-file", str(state_file),
            "--config-file", str(config_file),
            "--notify-bin", str(notify_bin),
        ],
    )

    assert mod.main() == 1
    saved = json.loads(state_file.read_text())
    assert saved["recovery"]["status"] == "gateway_restart_already_attempted"
    assert saved["active_incident"]["restart_attempted_at"] == "2026-06-21T11:01:00+00:00"
    assert calls == {"restart": 0, "notify": 1}


def test_healthy_run_clears_stale_recovery_state(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    config_file = tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    state_file.write_text(json.dumps({
        "recovery": {"status": "gateway_restart_ok"},
        "active_incident": {"key": "whatsapp"},
    }))
    config_file.write_text("{}")
    notify_bin.write_text("#!/bin/sh\nexit 0\n")
    notify_bin.chmod(0o755)
    monkeypatch.setattr(
        mod,
        "evaluate_channels",
        lambda *a, **k: [mod.CheckResult("whatsapp", True, "healthy", "ok")],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "watchdog",
            "--channels", "whatsapp",
            "--state-file", str(state_file),
            "--config-file", str(config_file),
            "--notify-bin", str(notify_bin),
            "--openclaw-bin", "openclaw",
        ],
    )

    assert mod.main() == 0
    saved = json.loads(state_file.read_text())
    assert "recovery" not in saved
    assert "active_incident" not in saved


def test_restart_gateway_returns_failure_detail(monkeypatch):
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout: subprocess.CompletedProcess(cmd, 7, "", "nope"),
    )

    result = mod.restart_gateway("/custom/openclaw")

    assert result.ok is False
    assert result.status == "gateway_restart_failed"
    assert "nope" in result.detail


# ── #32: 被登出（terminal disconnect）與一般 unhealthy 的分流 ──────────────────

# 2026-09-20 15:18 PT 實際被登出時 `openclaw channels status --probe --json` 的內容。
# 注意 loggedOut 是 false、linked 仍是 true——所以不能靠它們判斷。
LOGGED_OUT_PAYLOAD = {
    "channels": {"whatsapp": {"configured": True}},
    "channelAccounts": {
        "whatsapp": [{
            "configured": True,
            "linked": True,
            "running": True,
            "connected": False,
            "statusState": "linked",
            "healthState": "terminal-disconnect",
            "terminalDisconnect": True,
            "lastDisconnect": {"status": 401, "loggedOut": False},
        }]
    },
}

PLAIN_UNHEALTHY_PAYLOAD = {
    "channels": {"whatsapp": {"configured": True}},
    "channelAccounts": {
        "whatsapp": [{
            "configured": True,
            "linked": True,
            "running": False,
            "connected": False,
            "statusState": "linked",
            "healthState": "degraded",
            "terminalDisconnect": False,
        }]
    },
}


def _probe(monkeypatch, payload):
    monkeypatch.setattr(mod, "run_command",
                        lambda cmd, timeout: _Proc(0, stdout=json.dumps(payload)))
    return mod.check_whatsapp(timeout_ms=1000, openclaw_bin="/custom/openclaw")


def test_logged_out_session_gets_its_own_status_and_a_relink_next_step(monkeypatch):
    result = _probe(monkeypatch, LOGGED_OUT_PAYLOAD)

    assert result.ok is False
    assert result.status == "whatsapp_logged_out"
    assert "重啟 gateway 無效" in result.suggested_next_step
    assert "openclaw channels login --channel whatsapp" in result.suggested_next_step
    assert "Restart OpenClaw gateway" not in result.suggested_next_step
    flags = json.loads(result.detail)
    assert flags["terminalDisconnect"] is True
    assert flags["lastDisconnectStatus"] == 401


def test_plain_unhealthy_session_still_suggests_a_restart(monkeypatch):
    result = _probe(monkeypatch, PLAIN_UNHEALTHY_PAYLOAD)

    assert result.status == "whatsapp_unhealthy"
    assert result.suggested_next_step == "Restart OpenClaw gateway to recover the WhatsApp session."
    flags = json.loads(result.detail)
    assert flags["terminalDisconnect"] is False


def test_health_state_alone_is_enough_when_the_flag_is_missing(monkeypatch):
    payload = json.loads(json.dumps(LOGGED_OUT_PAYLOAD))
    del payload["channelAccounts"]["whatsapp"][0]["terminalDisconnect"]
    assert _probe(monkeypatch, payload).status == "whatsapp_logged_out"


def test_stale_401_does_not_override_an_explicit_false_flag(monkeypatch):
    """lastDisconnect 是歷史欄位：重新連結後那顆 401 還留著，不得蓋過現況。"""
    payload = json.loads(json.dumps(PLAIN_UNHEALTHY_PAYLOAD))
    payload["channelAccounts"]["whatsapp"][0]["lastDisconnect"] = {"status": 401, "loggedOut": False}
    assert _probe(monkeypatch, payload).status == "whatsapp_unhealthy"


def test_401_is_used_when_the_probe_has_no_terminal_flag_at_all(monkeypatch):
    """舊版 gateway 沒給 terminalDisconnect → 才退回用 401 判斷。"""
    payload = json.loads(json.dumps(PLAIN_UNHEALTHY_PAYLOAD))
    del payload["channelAccounts"]["whatsapp"][0]["terminalDisconnect"]
    payload["channelAccounts"]["whatsapp"][0]["lastDisconnect"] = {"status": 401}
    assert _probe(monkeypatch, payload).status == "whatsapp_logged_out"


def test_malformed_last_disconnect_is_ignored(monkeypatch):
    payload = json.loads(json.dumps(PLAIN_UNHEALTHY_PAYLOAD))
    del payload["channelAccounts"]["whatsapp"][0]["terminalDisconnect"]
    for bad in ("nope", {"status": "401"}, {"status": True}, []):
        payload["channelAccounts"]["whatsapp"][0]["lastDisconnect"] = bad
        result = _probe(monkeypatch, payload)
        assert result.status == "whatsapp_unhealthy", bad
        assert json.loads(result.detail)["lastDisconnectStatus"] is None, bad


def _watchdog_argv(tmp_path, state_file, config_file, notify_bin):
    config_file.write_text("{}")
    notify_bin.write_text("#!/bin/sh\nexit 0\n")
    notify_bin.chmod(0o755)
    return [
        "watchdog",
        "--channels", "whatsapp", "line",
        "--notify",
        "--recovery-mode", "restart",
        "--state-file", str(state_file),
        "--config-file", str(config_file),
        "--notify-bin", str(notify_bin),
        "--openclaw-bin", "openclaw",
    ]


def test_restart_mode_does_not_restart_a_logged_out_session(monkeypatch, tmp_path):
    """重啟對 terminal disconnect 必定無效，而且會連帶把 LINE/Telegram 彈掉。"""
    state_file, config_file = tmp_path / "state.json", tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    calls = {"evaluate": 0, "restart": 0, "notify": 0}

    def fake_evaluate(*args, **kwargs):
        calls["evaluate"] += 1
        return [
            mod.CheckResult("whatsapp", False, mod.WHATSAPP_LOGGED_OUT_STATUS,
                            "{}", mod.WHATSAPP_LOGGED_OUT_NEXT_STEP),
            mod.CheckResult("line", True, "healthy", "ok"),
        ]

    monkeypatch.setattr(mod, "evaluate_channels", fake_evaluate)
    monkeypatch.setattr(mod, "restart_gateway",
                        lambda *a, **k: calls.__setitem__("restart", calls["restart"] + 1))
    monkeypatch.setattr(mod, "send_notification",
                        lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1))
    monkeypatch.setattr(sys, "argv", _watchdog_argv(tmp_path, state_file, config_file, notify_bin))

    assert mod.main() == 1
    assert calls["restart"] == 0, "被登出時不得重啟 gateway"
    assert calls["evaluate"] == 1, "沒有重啟就不該有重啟後的重測"
    assert calls["notify"] == 1
    saved = json.loads(state_file.read_text())
    assert saved["recovery"]["status"] == "gateway_restart_skipped_terminal_disconnect"
    assert saved["recovery"]["ok"] is True


def test_restart_still_happens_when_another_channel_is_also_broken(monkeypatch, tmp_path):
    """只有在本輪全部不健康項目都是「被登出」時才跳過重啟——否則會取消別的頻道的自動恢復。"""
    state_file, config_file = tmp_path / "state.json", tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    calls = {"evaluate": 0, "restart": 0}

    def fake_evaluate(*args, **kwargs):
        calls["evaluate"] += 1
        return [
            mod.CheckResult("whatsapp", False, mod.WHATSAPP_LOGGED_OUT_STATUS,
                            "{}", mod.WHATSAPP_LOGGED_OUT_NEXT_STEP),
            mod.CheckResult("line", False, "line_official_webhook_failed", "broken", "restart"),
        ]

    def fake_restart(openclaw_bin):
        calls["restart"] += 1
        return mod.CheckResult("gateway", True, "gateway_restart_ok", "restarted")

    monkeypatch.setattr(mod, "evaluate_channels", fake_evaluate)
    monkeypatch.setattr(mod, "restart_gateway", fake_restart)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mod, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", _watchdog_argv(tmp_path, state_file, config_file, notify_bin))

    assert mod.main() == 1
    assert calls["restart"] == 1, "LINE 也壞掉時，重啟對它仍可能有用，不該一起取消"
    saved = json.loads(state_file.read_text())
    assert saved["recovery"]["status"] == "gateway_restart_ok"


def test_logged_out_alert_says_notify_only_not_restart(monkeypatch, tmp_path):
    """DM 的 Recovery mode 那行不能說「restart once」——這輪根本沒有也不會重啟。"""
    state_file, config_file = tmp_path / "state.json", tmp_path / "openclaw.json"
    notify_bin = tmp_path / "notify-dm"
    messages = []

    monkeypatch.setattr(mod, "evaluate_channels", lambda *a, **k: [
        mod.CheckResult("whatsapp", False, mod.WHATSAPP_LOGGED_OUT_STATUS,
                        "{}", mod.WHATSAPP_LOGGED_OUT_NEXT_STEP),
    ])
    monkeypatch.setattr(mod, "restart_gateway", lambda *a, **k: pytest.fail("不該重啟"))
    monkeypatch.setattr(mod, "send_notification", lambda message, notify_bin: messages.append(message))
    monkeypatch.setattr(sys, "argv", _watchdog_argv(tmp_path, state_file, config_file, notify_bin))

    assert mod.main() == 1
    (message,) = messages
    assert "重啟 gateway 無效" in message
    assert "Recovery mode: notify-only." in message
    assert "restart once" not in message


# --------------------------------------------------------------------------- #
# notify-shim#35 — the alert must outlive a slow gateway, and say so if it does not
# --------------------------------------------------------------------------- #

def test_notify_timeout_covers_every_enabled_channel(monkeypatch, tmp_path):
    """A two-channel route can legitimately take 2 x the per-channel deadline;
    the watchdog's budget must exceed that, not the old flat 30s."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import notify_core

    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"dm": {"channels": [
        {"channel": "telegram", "target": "1"},
        {"channel": "whatsapp", "target": "2"},
        {"channel": "line", "target": "3", "enabled": False},
    ]}}), encoding="utf-8")
    monkeypatch.setenv("NOTIFY_ROUTES", str(routes))

    budget = notify_core.route_send_budget("dm")
    assert budget > 2 * notify_core.SEND_TIMEOUT_SECONDS  # both channels + overhead
    assert mod.notify_timeout_seconds() == budget


def test_notify_budget_falls_back_when_routes_unreadable(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import notify_core

    monkeypatch.setenv("NOTIFY_ROUTES", str(tmp_path / "nope.json"))
    monkeypatch.setattr(notify_core.Path, "home", lambda: tmp_path)
    # One channel's worth of budget — never zero, so a send still gets a chance.
    assert notify_core.route_send_budget("dm") >= notify_core.SEND_TIMEOUT_SECONDS


def test_timed_out_notify_is_reported_as_an_undelivered_alert(
        monkeypatch, tmp_path, capsys):
    shim = tmp_path / "notify-dm"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def _boom(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(mod, "run_command", _boom)
    monkeypatch.setattr(mod, "notify_timeout_seconds", lambda route="dm": 42)

    mod.send_notification("LINE unhealthy\nmore detail", shim)

    err = capsys.readouterr().err
    assert "ALERT NOT DELIVERED" in err
    assert "42s" in err
    assert "LINE unhealthy" in err  # what was lost, not just that it failed
