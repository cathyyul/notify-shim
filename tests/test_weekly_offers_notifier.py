"""weekly_offers_notifier.sh routes its review output through notify-dm."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "weekly_offers_notifier.sh"


def _stub_notify(tmp_path):
    out = tmp_path / "sent.txt"
    stub = tmp_path / "notify-dm"
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out}"\n')
    stub.chmod(0o755)
    return stub, out


def _run(tmp_path, review_body):
    notify, sent = _stub_notify(tmp_path)
    review = tmp_path / "review.py"
    review.write_text(review_body)
    env = dict(os.environ,
               WORKSPACE_DIR=str(tmp_path),
               NOTIFY_DM_BIN=str(notify),
               WEEKLY_OFFERS_REVIEW=str(review),
               PYTHON_BIN="/usr/bin/python3")
    proc = subprocess.run(["/bin/zsh", str(SCRIPT)], env=env,
                          capture_output=True, text=True)
    return proc, sent


def test_empty_output_sends_nothing(tmp_path):
    proc, sent = _run(tmp_path, "")  # no output
    assert proc.returncode == 0, proc.stderr
    assert not sent.exists()


def test_output_triggers_notify(tmp_path):
    proc, sent = _run(tmp_path, 'print("Amex Plat: 100k MR offer live")\n')
    assert proc.returncode == 0, proc.stderr
    assert sent.exists()
    msg = sent.read_text()
    assert "Weekly Standard Offers Update" in msg
    assert "Amex Plat" in msg
