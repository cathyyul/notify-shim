"""slickdeals_deliver.sh formats the monitor output and delivers via notify-dm."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "slickdeals_deliver.sh"


def _stub(tmp_path, name="notify-dm"):
    out = tmp_path / f"{name}.txt"
    stub = tmp_path / name
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out}"\n')
    stub.chmod(0o755)
    return stub, out


def _run(tmp_path, monitor_body, extra_args=()):
    notify, sent = _stub(tmp_path)
    mon = tmp_path / "monitor.py"
    mon.write_text(monitor_body)
    env = dict(os.environ,
               PYTHON_BIN="/usr/bin/python3",
               SLICKDEALS_MONITOR=str(mon),
               NOTIFY_DM_BIN=str(notify))
    proc = subprocess.run(["/bin/zsh", str(SCRIPT), *extra_args],
                          env=env, capture_output=True, text=True)
    return proc, sent


def test_no_match_logs_without_delivery(tmp_path):
    proc, sent = _run(tmp_path, 'print("Found 0 candidate deals")\n')
    assert proc.returncode == 0, proc.stderr
    assert not sent.exists()
    assert "Found 0 candidate deals" in proc.stdout


def test_match_delivers_bullets(tmp_path):
    body = ('print("Found 1 candidate deals")\n'
            'print("- [Best Buy] $50 GC")\n'
            'print("  https://x")\n')
    proc, sent = _run(tmp_path, body)
    assert proc.returncode == 0, proc.stderr
    msg = sent.read_text()
    assert "🛒 Slickdeals 監控" in msg
    assert "• [Best Buy] $50 GC" in msg


def test_no_deliver_flag_skips_send(tmp_path):
    proc, sent = _run(tmp_path, 'print("Found 0 candidate deals")\n',
                      extra_args=("--no-deliver",))
    assert proc.returncode == 0, proc.stderr
    assert not sent.exists()
    assert "Found 0 candidate deals" in proc.stdout


def test_monitor_failure_surfaces_error_and_does_not_send(tmp_path):
    body = 'import sys\nsys.stderr.write("BOOM monitor crashed\\n")\nsys.exit(3)\n'
    proc, sent = _run(tmp_path, body)
    assert proc.returncode != 0
    assert "BOOM monitor crashed" in proc.stderr  # error must not be swallowed
    assert not sent.exists()


def test_nonempty_no_deliver_preview_keeps_bullets_without_send(tmp_path):
    body = 'print("Found 1 candidate deals\\n- [bambu] Printer\\n  https://example.com/deal")\n'
    proc, sent = _run(tmp_path, body, extra_args=("--no-deliver",))
    assert proc.returncode == 0, proc.stderr
    assert not sent.exists()
    assert '• [bambu] Printer' in proc.stdout and 'https://example.com/deal' in proc.stdout
