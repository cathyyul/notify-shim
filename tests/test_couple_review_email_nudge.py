"""Unit tests for couple_review_email_nudge — watermark filter + send decision.

gog / notify-dm are never actually invoked: subprocess.run is monkeypatched.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifiers"))
import couple_review_email_nudge as nudge  # noqa: E402


def write_ledger(tmp_path, lines):
    p = tmp_path / "ledger.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return str(p)


def write_config(tmp_path, to="chi@example.com", frm="sender@gmail.com",
                 route="group-couple", require_channel=None):
    cfg = {"to": to, "from_account": frm, "route": route}
    if require_channel is not None:
        cfg["require_channel"] = require_channel
    p = tmp_path / "review-email.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def entry(ts, route="group-couple", ok=True, channels=None):
    e = {"ts": ts, "route": route, "ok": ok}
    if channels is not None:
        e["channels"] = channels
    return e


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- unnotified (watermark filter) ---

def test_unnotified_none_watermark_counts_all_matching(tmp_path):
    ledger = write_ledger(tmp_path, [
        entry("2026-07-27T09:00:00-07:00"),
        entry("2026-07-27T21:00:00-07:00"),
        entry("2026-07-27T10:00:00-07:00", route="dm"),         # wrong route
        entry("2026-07-27T11:00:00-07:00", ok=False),           # not ok
    ])
    got = nudge.unnotified(ledger, "group-couple", None)
    assert len(got) == 2
    assert got == sorted(got)  # returned ascending


def test_unnotified_only_after_watermark(tmp_path):
    ledger = write_ledger(tmp_path, [
        entry("2026-07-27T09:00:00-07:00"),
        entry("2026-07-27T21:00:00-07:00"),
        entry("2026-07-27T23:00:00-07:00"),
    ])
    since = nudge._parse_ts("2026-07-27T21:00:00-07:00")
    got = nudge.unnotified(ledger, "group-couple", since)
    assert len(got) == 1  # strictly after 21:00 → only the 23:00 one


def test_unnotified_missing_ledger_is_empty(tmp_path):
    assert nudge.unnotified(str(tmp_path / "nope.jsonl"), "group-couple", None) == []


def test_unnotified_gates_on_required_channel(tmp_path):
    """A nudge that points to Telegram must not count a LINE-only success."""
    ledger = write_ledger(tmp_path, [
        entry("2026-07-27T20:00:00-07:00", channels={"telegram": False, "line": True}),
        entry("2026-07-27T21:00:00-07:00", channels={"telegram": True, "line": False}),
    ])
    got = nudge.unnotified(ledger, "group-couple", None, require_channel="telegram")
    assert len(got) == 1  # only the entry where Telegram itself succeeded


def test_unnotified_legacy_entry_falls_back_to_ok(tmp_path):
    """Old ledger lines without per-channel data fall back to the ok flag."""
    ledger = write_ledger(tmp_path, [entry("2026-07-27T21:00:00-07:00", ok=True)])
    got = nudge.unnotified(ledger, "group-couple", None, require_channel="telegram")
    assert len(got) == 1


def test_unnotified_discriminates_within_same_second(tmp_path):
    """Two sends in the same whole second must be separable by the watermark."""
    ledger = write_ledger(tmp_path, [
        entry("2026-07-27T21:00:00.050000-07:00"),   # before watermark
        entry("2026-07-27T21:00:00.500000-07:00"),   # same second, after watermark
    ])
    since = nudge._parse_ts("2026-07-27T21:00:00.100000-07:00")
    got = nudge.unnotified(ledger, "group-couple", since)
    assert len(got) == 1  # only the .500000 entry survives, not the .050000 one


def test_find_gog_probes_intel_path_when_which_fails(monkeypatch):
    monkeypatch.delenv("GOG_BIN", raising=False)
    monkeypatch.setattr(nudge.shutil, "which", lambda _n: None)
    monkeypatch.setattr(nudge.os.path, "isfile", lambda p: p == "/usr/local/bin/gog")
    assert nudge.find_gog() == "/usr/local/bin/gog"


def test_alert_failure_honors_workspace_env(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFY_DM_BIN", raising=False)
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path / "ws"))
    calls = []
    monkeypatch.setattr(nudge.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    nudge.alert_failure("boom")
    assert calls and calls[0][0] == str(tmp_path / "ws" / "scripts" / "notify-dm")


def test_unnotified_ignores_malformed_lines(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text('not json\n'
                 '{"ts":"2026-07-27T01:00:00-07:00","route":"group-couple","ok":true}\n',
                 encoding="utf-8")
    assert len(nudge.unnotified(str(p), "group-couple", None)) == 1


# --- watermark state round-trip ---

def test_watermark_round_trip(tmp_path):
    state = str(tmp_path / "state.json")
    assert nudge.load_watermark(state) is None  # missing → None
    ts = nudge._parse_ts("2026-07-27T21:00:00-07:00")
    nudge.save_watermark(state, ts)
    assert nudge.load_watermark(state) == ts


# --- load_config ---

def test_load_config_ok(tmp_path):
    p = write_config(tmp_path)
    assert nudge.load_config(p) == (
        "chi@example.com", "sender@gmail.com", "group-couple", "telegram")


def test_load_config_custom_require_channel(tmp_path):
    p = write_config(tmp_path, require_channel="line")
    assert nudge.load_config(p)[3] == "line"


def test_load_config_missing_file_is_not_configured(tmp_path):
    with pytest.raises(nudge.NotConfigured):
        nudge.load_config(str(tmp_path / "nope.json"))


def test_load_config_both_placeholder_is_not_configured(tmp_path):
    p = write_config(tmp_path, to="REPLACE_WITH_RECIPIENT@example.com",
                     frm="REPLACE_WITH_SENDER@gmail.com")
    with pytest.raises(nudge.NotConfigured):
        nudge.load_config(p)


def test_load_config_partial_invalid_is_error(tmp_path):
    """A present-but-broken config (not a placeholder) is a real error to alert on."""
    p = write_config(tmp_path, to="not-an-email")
    with pytest.raises(ValueError):
        nudge.load_config(p)


# --- main ---

def test_main_sends_and_advances_watermark(tmp_path, monkeypatch):
    ledger = write_ledger(tmp_path, [
        entry("2026-07-27T20:00:00-07:00"),
        entry("2026-07-27T21:00:00-07:00"),
    ])
    cfg = write_config(tmp_path)
    state = str(tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(nudge.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or FakeProc(0, stdout="sent"))
    monkeypatch.setattr(nudge, "find_gog", lambda: "gog")

    rc = nudge.main(["--config", cfg, "--ledger", ledger, "--state", state])
    assert rc == 0
    cmd = calls[0]
    assert cmd[:2] == ["gog", "send"]
    assert cmd[cmd.index("--to") + 1] == "chi@example.com"
    assert cmd[cmd.index("--account") + 1] == "sender@gmail.com"
    # watermark advanced to the newest covered message
    assert nudge.load_watermark(state) == nudge._parse_ts("2026-07-27T21:00:00-07:00")


def test_main_second_run_no_new_items_skips(tmp_path, monkeypatch):
    """After a send, an unchanged ledger must NOT re-notify (watermark holds)."""
    ledger = write_ledger(tmp_path, [entry("2026-07-27T21:00:00-07:00")])
    cfg = write_config(tmp_path)
    state = str(tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(nudge.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or FakeProc(0))
    monkeypatch.setattr(nudge, "find_gog", lambda: "gog")

    assert nudge.main(["--config", cfg, "--ledger", ledger, "--state", state]) == 0
    assert len(calls) == 1
    # second run, same ledger → nothing new
    assert nudge.main(["--config", cfg, "--ledger", ledger, "--state", state]) == 0
    assert len(calls) == 1  # no second send


def test_main_late_message_caught_next_run(tmp_path, monkeypatch):
    """A message added after the first send is picked up on the next run."""
    cfg = write_config(tmp_path)
    state = str(tmp_path / "state.json")
    lp = tmp_path / "ledger.jsonl"
    lp.write_text(json.dumps(entry("2026-07-27T21:00:00-07:00")) + "\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(nudge.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or FakeProc(0))
    monkeypatch.setattr(nudge, "find_gog", lambda: "gog")

    nudge.main(["--config", cfg, "--ledger", str(lp), "--state", state])
    with lp.open("a", encoding="utf-8") as fh:  # a message arrives after the send
        fh.write(json.dumps(entry("2026-07-27T22:30:00-07:00")) + "\n")
    nudge.main(["--config", cfg, "--ledger", str(lp), "--state", state])

    assert len(calls) == 2  # second, later message triggered a second nudge


def test_main_skips_when_ledger_empty(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    ledger = write_ledger(tmp_path, [])
    state = str(tmp_path / "state.json")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not send when nothing to review")
    monkeypatch.setattr(nudge.subprocess, "run", boom)

    assert nudge.main(["--config", cfg, "--ledger", ledger, "--state", state]) == 0


def test_main_dry_run_does_not_advance_watermark(tmp_path, monkeypatch):
    ledger = write_ledger(tmp_path, [entry("2026-07-27T21:00:00-07:00")])
    cfg = write_config(tmp_path)
    state = str(tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(nudge.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or FakeProc(0))
    monkeypatch.setattr(nudge, "find_gog", lambda: "gog")

    rc = nudge.main(["--config", cfg, "--ledger", ledger, "--state", state, "--dry-run"])
    assert rc == 0
    assert "-n" in calls[0]
    assert nudge.load_watermark(state) is None  # dry-run left state untouched


def test_main_send_failure_returns_2_and_alerts(tmp_path, monkeypatch):
    ledger = write_ledger(tmp_path, [entry("2026-07-27T21:00:00-07:00")])
    cfg = write_config(tmp_path)
    state = str(tmp_path / "state.json")
    alerts = []

    def fake_run(cmd, **k):
        if cmd[:2] == ["gog", "send"]:
            return FakeProc(1, stderr="401 unauthorized")
        alerts.append(cmd)          # the notify-dm failure alert
        return FakeProc(0)

    monkeypatch.setattr(nudge.subprocess, "run", fake_run)
    monkeypatch.setattr(nudge, "find_gog", lambda: "gog")

    rc = nudge.main(["--config", cfg, "--ledger", ledger, "--state", state])
    assert rc == 2
    assert alerts and "401 unauthorized" in " ".join(alerts[0])  # alert carries the cause
    assert nudge.load_watermark(state) is None  # failed send did not advance watermark


def test_main_missing_config_exits_0_quiet(tmp_path, monkeypatch):
    """No config file = feature not set up → quiet exit 0, no send/alert."""
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not send/alert when not configured")
    monkeypatch.setattr(nudge.subprocess, "run", boom)
    rc = nudge.main(["--config", str(tmp_path / "nope.json"),
                     "--ledger", str(tmp_path / "l.jsonl"),
                     "--state", str(tmp_path / "s.json")])
    assert rc == 0


def test_main_placeholder_config_exits_0_quiet(tmp_path, monkeypatch):
    """Untouched placeholder seed → not set up → quiet exit 0, no nightly nag."""
    cfg = write_config(tmp_path, to="REPLACE_WITH_RECIPIENT@example.com",
                       frm="REPLACE_WITH_SENDER@gmail.com")
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not send/alert on placeholder config")
    monkeypatch.setattr(nudge.subprocess, "run", boom)
    rc = nudge.main(["--config", cfg, "--ledger", str(tmp_path / "l.jsonl"),
                     "--state", str(tmp_path / "s.json")])
    assert rc == 0


def test_main_broken_config_alerts_and_exits_2(tmp_path, monkeypatch):
    """A present-but-broken config must alert, not die silently."""
    cfg = write_config(tmp_path, to="not-an-email")  # real-ish but invalid
    alerts = []
    monkeypatch.setattr(nudge.subprocess, "run", lambda cmd, **k: alerts.append(cmd))
    rc = nudge.main(["--config", cfg, "--ledger", str(tmp_path / "l.jsonl"),
                     "--state", str(tmp_path / "s.json")])
    assert rc == 2
    assert alerts and "config error" in " ".join(alerts[0])  # self-explaining alert fired
