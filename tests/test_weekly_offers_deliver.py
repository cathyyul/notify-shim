"""weekly_offers_deliver.sh delivers to BOTH the DM and the couple group."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "weekly_offers_deliver.sh"


def _stub(tmp_path, name):
    out = tmp_path / f"{name}.txt"
    stub = tmp_path / name
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out}"\n')
    stub.chmod(0o755)
    return stub, out


def _run(tmp_path, review_body, extra_args=()):
    dm, dm_out = _stub(tmp_path, "notify-dm")
    grp, grp_out = _stub(tmp_path, "notify-group-couple")
    review = tmp_path / "review.py"
    review.write_text(review_body)
    env = dict(os.environ,
               PYTHON_BIN="/usr/bin/python3",
               WEEKLY_OFFERS_REVIEW=str(review),
               NOTIFY_DM_BIN=str(dm),
               NOTIFY_GROUP_COUPLE_BIN=str(grp))
    proc = subprocess.run(["/bin/zsh", str(SCRIPT), *extra_args],
                          env=env, capture_output=True, text=True)
    return proc, dm_out, grp_out


def test_delivers_to_both_dm_and_group(tmp_path):
    proc, dm_out, grp_out = _run(tmp_path, 'print("- Amex Plat 100k MR")\n')
    assert proc.returncode == 0, proc.stderr
    assert dm_out.exists() and grp_out.exists()
    for out in (dm_out, grp_out):
        msg = out.read_text()
        assert "📋 Weekly Standard Offers Review" in msg
        assert "Amex Plat 100k MR" in msg


def test_no_offers_still_delivers_both(tmp_path):
    proc, dm_out, grp_out = _run(tmp_path, 'print("checked 5 cards")\n')
    assert proc.returncode == 0, proc.stderr
    assert dm_out.exists() and grp_out.exists()
    assert "沒有本季到期" in dm_out.read_text()


def test_no_deliver_skips_both(tmp_path):
    proc, dm_out, grp_out = _run(tmp_path, 'print("- Amex")\n',
                                 extra_args=("--no-deliver",))
    assert proc.returncode == 0
    assert not dm_out.exists() and not grp_out.exists()


def test_review_failure_surfaces_error_and_does_not_send(tmp_path):
    body = 'import sys\nsys.stderr.write("BOOM review crashed\\n")\nsys.exit(3)\n'
    proc, dm_out, grp_out = _run(tmp_path, body)
    assert proc.returncode != 0
    assert "BOOM review crashed" in proc.stderr
    assert not dm_out.exists() and not grp_out.exists()
