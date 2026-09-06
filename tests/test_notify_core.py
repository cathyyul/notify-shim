"""Unit tests for notify_core — route resolution + fail-loud fan-out.

No network / no openclaw binary: subprocess.run is monkeypatched.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notify_core  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    """Point the send-ledger and failure-alert config/state at temp paths so
    tests never touch the real files (and, by default, never send an email:
    the alert config points at a non-existent file → alerting disabled)."""
    monkeypatch.setenv("NOTIFY_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("NOTIFY_ALERT_CONFIG", str(tmp_path / "no-alert.json"))
    monkeypatch.setenv("NOTIFY_ALERT_STATE", str(tmp_path / "alert.state.json"))


@pytest.fixture
def alert_config(tmp_path, monkeypatch):
    """Enable failure-alert email with a temp config; return a spy that records
    each _send_alert_email call and reports success."""
    cfg = tmp_path / "failure-alert.json"
    cfg.write_text(json.dumps({"to": "recip@example.com",
                               "from_account": "sender@example.com"}),
                   encoding="utf-8")
    monkeypatch.setenv("NOTIFY_ALERT_CONFIG", str(cfg))
    sent = []

    def _spy(config, subject, body, *, timeout=30):
        sent.append({"config": config, "subject": subject, "body": body})
        return True, "sent"

    monkeypatch.setattr(notify_core, "_send_alert_email", _spy)
    return sent


ROUTES = {
    "dm": {
        "description": "test dm",
        "channels": [
            {"channel": "telegram", "target": "111"},
            {"channel": "line", "target": "Uabc"},
        ],
    },
    "group-couple": {
        "description": "test group",
        "channels": [
            {"channel": "telegram", "target": "-100999"},
            {"channel": "line", "target": "Cdef"},
        ],
    },
    "empty": {"description": "no channels", "channels": []},
}


@pytest.fixture
def routes_file(tmp_path):
    p = tmp_path / "routes.json"
    p.write_text(json.dumps(ROUTES), encoding="utf-8")
    return str(p)


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_run(fail_targets=()):
    """Return a fake subprocess.run; commands whose --target is in
    fail_targets return a non-zero exit code."""
    calls = []

    def _run(cmd, capture_output=False, text=False, env=None, timeout=None):
        calls.append(cmd)
        _run.envs.append(env)
        target = cmd[cmd.index("--target") + 1]
        if target in fail_targets:
            return FakeProc(1, stderr=f"boom for {target}")
        return FakeProc(0, stdout="✅ sent")

    _run.calls = calls
    _run.envs = []
    return _run


def test_notify_fans_out_to_all_channels(monkeypatch, routes_file):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    results = notify_core.notify("dm", "hello", routes_path=routes_file)

    assert len(results) == 2
    assert all(ok for *_, ok, _detail in results)
    # one openclaw invocation per channel, with the right channel+target
    sent = [(c[c.index("--channel") + 1], c[c.index("--target") + 1]) for c in run.calls]
    assert sent == [("telegram", "111"), ("line", "Uabc")]
    # message is forwarded verbatim
    assert run.calls[0][run.calls[0].index("--message") + 1] == "hello"


def test_partial_failure_exits_zero(monkeypatch, routes_file):
    # notify-shim#26: LINE fails but Telegram delivers → message reached the
    # user, so the whole flow must NOT fail.
    run = make_run(fail_targets={"Uabc"})  # LINE fails, Telegram ok
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 0  # partial failure is not fatal


def test_total_failure_exits_one(monkeypatch, routes_file):
    # Every channel failed → genuine outage → non-zero.
    run = make_run(fail_targets={"111", "Uabc"})
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 1


def test_partial_failure_sends_throttled_alert(monkeypatch, routes_file, alert_config):
    run = make_run(fail_targets={"Uabc"})  # LINE fails
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 0
    assert len(alert_config) == 1
    assert alert_config[0]["config"]["to"] == "recip@example.com"
    assert "line:Uabc" in alert_config[0]["body"]

    # Second failure of the SAME channel on the same day → throttled, no 2nd email.
    rc = notify_core.main(["--route", "dm", "-m", "hi again", "--routes", routes_file])
    assert rc == 0
    assert len(alert_config) == 1  # still one


def test_alert_body_strips_ansi_and_control_chars(monkeypatch, routes_file, alert_config):
    # notify-shim#28: a failing channel's detail carries openclaw's ANSI/control
    # noise; raw ESC bytes in the body get the email silently dropped by Gmail.
    def run(cmd, capture_output=False, text=False, env=None, timeout=None):
        target = cmd[cmd.index("--target") + 1]
        if target == "Uabc":  # LINE fails with ANSI + box-drawing + control noise
            return FakeProc(1, stderr="\x1b[32m[state-migrations]\x1b[39m boom\x07\r\n"
                                      "╭ Doctor notices ──╮")
        return FakeProc(0, stdout="✅ sent")

    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 0
    assert len(alert_config) == 1
    body = alert_config[0]["body"]
    # no ESC (0x1b) or other C0 control chars (except the tab/newline layout)
    assert "\x1b" not in body
    assert not any(ord(c) < 0x20 and c not in "\n\t" for c in body)
    assert "\x7f" not in body
    # the real error text still survives, sanitized
    assert "boom" in body


def test_no_alert_config_still_exits_and_does_not_crash(monkeypatch, routes_file):
    # Default fixture points alert config at a non-existent file → no email,
    # but delivery + exit-code behavior is unaffected.
    run = make_run(fail_targets={"Uabc"})
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 0  # partial failure, no config, no crash


def test_all_ok_exits_zero(monkeypatch, routes_file):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "group-couple", "-m", "yo", "--routes", routes_file])
    assert rc == 0
    assert len(run.calls) == 2


def test_dry_run_does_not_invoke_openclaw(monkeypatch, routes_file):
    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("subprocess.run called during dry-run")

    monkeypatch.setattr(notify_core.subprocess, "run", boom)
    results = notify_core.notify("dm", "hello", routes_path=routes_file, dry_run=True)
    assert all(ok for *_, ok, _ in results)
    assert all("dry-run" in detail for *_, detail in results)


def test_unknown_route_exits_2(monkeypatch, routes_file):
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")
    rc = notify_core.main(["--route", "nope", "-m", "x", "--routes", routes_file])
    assert rc == 2


def test_empty_route_exits_2(routes_file):
    rc = notify_core.main(["--route", "empty", "-m", "x", "--routes", routes_file])
    assert rc == 2


def test_empty_message_exits_2(routes_file):
    rc = notify_core.main(["--route", "dm", "-m", "   ", "--routes", routes_file])
    assert rc == 2


def test_missing_routes_file_exits_2(monkeypatch, tmp_path):
    missing = str(tmp_path / "nope.json")
    rc = notify_core.main(["--route", "dm", "-m", "x", "--routes", missing])
    assert rc == 2


def test_positional_message_joined(monkeypatch, routes_file):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")
    rc = notify_core.main(["--route", "dm", "hello", "world", "--routes", routes_file])
    assert rc == 0
    assert run.calls[0][run.calls[0].index("--message") + 1] == "hello world"


def test_send_one_prepends_binary_dir_to_path(monkeypatch):
    """openclaw (a Node CLI) needs its own dir on PATH to find node when run
    under a minimal launchd environment."""
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    ok, _detail = notify_core.send_one(
        "line", "Uabc", "hi", dry_run=False,
        openclaw_bin="/opt/homebrew/bin/openclaw",
    )

    assert ok
    env = run.envs[0]
    assert env is not None
    assert env["PATH"].split(os.pathsep)[0] == "/opt/homebrew/bin"
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_send_one_does_not_duplicate_existing_dir(monkeypatch):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")

    notify_core.send_one("line", "Uabc", "hi", dry_run=False,
                         openclaw_bin="/opt/homebrew/bin/openclaw")

    parts = run.envs[0]["PATH"].split(os.pathsep)
    assert parts.count("/opt/homebrew/bin") == 1


def test_send_one_bare_binary_name_does_not_inject_cwd(monkeypatch):
    """A bare command name must NOT cause the caller's cwd to be prepended to
    PATH (os.path.abspath('openclaw') would resolve to $PWD/openclaw)."""
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    notify_core.send_one("line", "Uabc", "hi", dry_run=False, openclaw_bin="openclaw")

    assert run.envs[0]["PATH"] == "/usr/bin:/bin"  # unchanged; no cwd injection


def test_send_one_timeout_returns_false(monkeypatch):
    def raise_timeout(*a, **k):
        raise notify_core.subprocess.TimeoutExpired(cmd="openclaw", timeout=1)
    monkeypatch.setattr(notify_core.subprocess, "run", raise_timeout)
    ok, detail = notify_core.send_one("line", "Uabc", "hi", dry_run=False,
                                      openclaw_bin="openclaw", timeout=1)
    assert not ok and "timed out" in detail


def test_send_one_oserror_returns_false(monkeypatch):
    def raise_oserror(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(notify_core.subprocess, "run", raise_oserror)
    ok, detail = notify_core.send_one("line", "Uabc", "hi", dry_run=False,
                                      openclaw_bin="openclaw")
    assert not ok and "could not run" in detail


# --- per-channel enabled toggle ---

DISABLED_ROUTES = {
    "mixed": {"channels": [
        {"channel": "telegram", "target": "111"},
        {"channel": "line", "target": "Uabc", "enabled": False},
    ]},
    "all-off": {"channels": [
        {"channel": "telegram", "target": "111", "enabled": False},
        {"channel": "line", "target": "Uabc", "enabled": False},
    ]},
    "explicit-on": {"channels": [
        {"channel": "telegram", "target": "111", "enabled": True},
    ]},
}


@pytest.fixture
def toggle_routes(tmp_path):
    p = tmp_path / "routes.json"
    p.write_text(json.dumps(DISABLED_ROUTES), encoding="utf-8")
    return str(p)


def test_disabled_channel_is_skipped(monkeypatch, toggle_routes):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    results = notify_core.notify("mixed", "hi", routes_path=toggle_routes)

    # only the enabled telegram channel was sent
    assert [(c, t) for c, t, _ok, _d in results] == [("telegram", "111")]
    assert len(run.calls) == 1


def test_all_disabled_route_sends_nothing_exits_zero(monkeypatch, toggle_routes):
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not send when all channels disabled")
    monkeypatch.setattr(notify_core.subprocess, "run", boom)

    rc = notify_core.main(["--route", "all-off", "-m", "hi", "--routes", toggle_routes])
    assert rc == 0


def test_explicit_enabled_true_sends(monkeypatch, toggle_routes):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")
    rc = notify_core.main(["--route", "explicit-on", "-m", "hi", "--routes", toggle_routes])
    assert rc == 0
    assert len(run.calls) == 1


# --- send ledger ---

def test_ledger_records_successful_send(monkeypatch, routes_file, tmp_path):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    notify_core.notify("group-couple", "hi", routes_path=routes_file)

    entries = [json.loads(l) for l in
               Path(notify_core.ledger_path()).read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["route"] == "group-couple"
    assert entries[0]["ok"] is True
    assert entries[0]["ts"][:4].isdigit()  # ISO timestamp present


def test_ledger_records_ok_false_when_all_channels_fail(monkeypatch, routes_file):
    run = make_run(fail_targets={"-100999", "Cdef"})  # both group channels fail
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    notify_core.notify("group-couple", "hi", routes_path=routes_file)

    entries = [json.loads(l) for l in
               Path(notify_core.ledger_path()).read_text().splitlines() if l.strip()]
    assert entries[-1]["route"] == "group-couple"
    assert entries[-1]["ok"] is False


def test_ledger_skipped_on_dry_run(monkeypatch, routes_file):
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("subprocess.run called during dry-run")
    monkeypatch.setattr(notify_core.subprocess, "run", boom)

    notify_core.notify("dm", "hi", routes_path=routes_file, dry_run=True)

    assert not Path(notify_core.ledger_path()).exists()


def test_ledger_failure_never_breaks_delivery_but_logs(monkeypatch, routes_file,
                                                        tmp_path, capsys):
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")
    # Ledger parent is a *file*, so mkdir/open must fail — delivery must survive.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NOTIFY_LEDGER", str(blocker / "ledger.jsonl"))

    results = notify_core.notify("dm", "hi", routes_path=routes_file)

    assert all(ok for *_, ok, _ in results)  # send succeeded despite ledger error
    assert "send-ledger append failed" in capsys.readouterr().err  # not silent


def test_ledger_records_per_channel_outcome(monkeypatch, routes_file):
    """A nudge that points to one channel must be able to gate on that channel,
    so the ledger records per-channel success, not just any_ok."""
    run = make_run(fail_targets={"Cdef"})  # group-couple: LINE fails, Telegram ok
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    notify_core.notify("group-couple", "hi", routes_path=routes_file)

    entry = json.loads(Path(notify_core.ledger_path()).read_text().splitlines()[-1])
    assert entry["channels"] == {"telegram": True, "line": False}
    assert entry["ok"] is True  # any_ok still recorded for back-compat


def test_ledger_written_even_when_a_later_channel_raises(monkeypatch, routes_file):
    """A later channel that hangs/raises must not erase an earlier channel's
    success: send_one never raises, so the loop finishes and the ledger is
    written with the accumulated per-channel outcomes."""
    def run(cmd, capture_output=False, text=False, env=None, timeout=None):
        target = cmd[cmd.index("--target") + 1]
        if target == "Cdef":  # LINE (second channel) raises
            raise PermissionError("boom")
        return FakeProc(0, stdout="ok")
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    notify_core.notify("group-couple", "hi", routes_path=routes_file)

    entry = json.loads(Path(notify_core.ledger_path()).read_text().splitlines()[-1])
    assert entry["channels"] == {"telegram": True, "line": False}  # telegram preserved


def test_ledger_ts_has_microsecond_precision(monkeypatch, routes_file):
    """Sub-second precision — two sends in the same whole second must still get
    distinct, ordered timestamps for the strict watermark filter downstream."""
    import re
    run = make_run()
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    notify_core.notify("dm", "hi", routes_path=routes_file)

    ts = json.loads(Path(notify_core.ledger_path()).read_text().splitlines()[0])["ts"]
    assert re.search(r"T\d{2}:\d{2}:\d{2}\.\d{6}", ts), ts  # fractional seconds present
