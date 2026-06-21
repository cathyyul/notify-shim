"""Unit tests for notify_core — route resolution + fail-loud fan-out.

No network / no openclaw binary: subprocess.run is monkeypatched.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notify_core  # noqa: E402


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

    def _run(cmd, capture_output=False, text=False):
        calls.append(cmd)
        target = cmd[cmd.index("--target") + 1]
        if target in fail_targets:
            return FakeProc(1, stderr=f"boom for {target}")
        return FakeProc(0, stdout="✅ sent")

    _run.calls = calls
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


def test_fail_loud_when_one_channel_fails(monkeypatch, routes_file):
    run = make_run(fail_targets={"Uabc"})  # LINE fails, Telegram ok
    monkeypatch.setattr(notify_core.subprocess, "run", run)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")

    rc = notify_core.main(["--route", "dm", "-m", "hi", "--routes", routes_file])
    assert rc == 1  # any failure -> non-zero


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
