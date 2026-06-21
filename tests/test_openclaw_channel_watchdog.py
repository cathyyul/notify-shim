from __future__ import annotations

import json
import subprocess
import sys
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
