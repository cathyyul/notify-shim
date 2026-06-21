"""meal-reminder.sh routes its prompt through the notify-dm shim.

Runs the real script with NOTIFY_DM_BIN pointed at a stub that records the
message, so we assert the per-meal text without sending anything.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "meal-reminder.sh"


def _stub(tmp_path):
    out = tmp_path / "received.txt"
    stub = tmp_path / "notify-dm"
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out}"\n')
    stub.chmod(0o755)
    return stub, out


def _run(script_args, tmp_path):
    stub, out = _stub(tmp_path)
    env = dict(os.environ, NOTIFY_DM_BIN=str(stub))
    proc = subprocess.run(["/bin/zsh", str(SCRIPT), *script_args],
                          env=env, capture_output=True, text=True)
    return proc, out


@pytest.mark.parametrize("meal,needle", [
    ("breakfast", "早餐"),
    ("lunch", "午餐"),
    ("dinner", "晚餐"),
])
def test_meal_reminder_sends_per_meal_text(tmp_path, meal, needle):
    proc, out = _run([meal], tmp_path)
    assert proc.returncode == 0, proc.stderr
    msg = out.read_text()
    assert needle in msg
    assert "🍽️" in msg


def test_unknown_meal_exits_nonzero_and_sends_nothing(tmp_path):
    proc, out = _run(["brunch"], tmp_path)
    assert proc.returncode != 0
    assert not out.exists()


def test_missing_arg_exits_nonzero(tmp_path):
    proc, out = _run([], tmp_path)
    assert proc.returncode != 0
    assert not out.exists()
