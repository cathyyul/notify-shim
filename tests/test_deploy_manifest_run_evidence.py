"""notify-shim#24 — launchd liveness run_evidence for the two watchdogs.

Both watchdogs are silent on a healthy run (StandardOutPath stays 0 byte /
stale), so the drift sentinel's launchd track (workspace-infra#104) cannot use
the log mtime as run evidence. Each plist target must declare the state file
the script rewrites on EVERY run, and that path must match the script's own
DEFAULT_STATE_FILE so the two can never silently diverge.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "notifiers"))

import claude_scheduler_watchdog  # noqa: E402
import openclaw_channel_watchdog  # noqa: E402

MANIFEST = REPO / "deploy.manifest.json"

# plist src → the module whose DEFAULT_STATE_FILE is the per-run artifact
WATCHDOGS = {
    "launchagents/com.openclaw.channel-watchdog.plist": openclaw_channel_watchdog,
    "launchagents/com.openclaw.claude-scheduler-watchdog.plist": claude_scheduler_watchdog,
}


def _targets_by_src() -> dict[str, dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {t["src"]: t for t in manifest["targets"]}


def test_watchdog_plists_declare_run_evidence_matching_state_file():
    targets = _targets_by_src()
    for src, mod in WATCHDOGS.items():
        target = targets[src]
        assert target["dest"].startswith("~/Library/LaunchAgents/"), src
        decl = target.get("run_evidence")
        assert decl is not None, f"{src} lacks run_evidence (#24)"
        # schema: {path, grace_seconds?} and nothing else
        assert set(decl) <= {"path", "grace_seconds"}, decl
        expected = mod.DEFAULT_STATE_FILE.relative_to(mod.WORKSPACE).as_posix()
        assert decl["path"] == expected, (src, decl["path"], expected)


def test_run_evidence_paths_are_distinct_per_job():
    targets = _targets_by_src()
    paths = [targets[src]["run_evidence"]["path"] for src in WATCHDOGS]
    assert len(set(paths)) == len(paths), paths
