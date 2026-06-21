"""slickdeals_notifier.sh must not abort when the monitor reports no matches.

`grep -c` exits 1 on zero matches; under `set -euo pipefail` that aborts the
script. These tests stub the monitor (and the shim) to exercise both paths.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "slickdeals_notifier.sh"


def _stub_notify(tmp_path):
    out = tmp_path / "sent.txt"
    stub = tmp_path / "notify-dm"
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out}"\n')
    stub.chmod(0o755)
    return stub, out


def _run(tmp_path, monitor_body):
    notify, sent = _stub_notify(tmp_path)
    mon = tmp_path / "monitor.py"
    mon.write_text(monitor_body)
    env = dict(os.environ,
               WORKSPACE_DIR=str(tmp_path),
               NOTIFY_DM_BIN=str(notify),
               SLICKDEALS_MONITOR=str(mon),
               PYTHON_BIN="/usr/bin/python3")
    proc = subprocess.run(["/bin/zsh", str(SCRIPT)], env=env,
                          capture_output=True, text=True)
    return proc, sent


def test_no_matches_exits_clean_without_notifying(tmp_path):
    proc, sent = _run(tmp_path, 'print("Found 0 candidate deals")\n')
    assert proc.returncode == 0, proc.stderr
    assert not sent.exists()


def test_matches_trigger_notify(tmp_path):
    body = ('print("Found 1 candidate deals")\n'
            'print("- [Best Buy] $50 GC 10% off")\n')
    proc, sent = _run(tmp_path, body)
    assert proc.returncode == 0, proc.stderr
    assert sent.exists()
    msg = sent.read_text()
    assert "Slickdeals Gift Card Alert" in msg
    assert "Best Buy" in msg
