"""notify-shim#47 — group-couple is LINE-only; the Chi review-email nudge is retired.

Chi now reads the LINE group, so the Telegram 小寶 group is no longer a
group-couple channel and the nightly email nudge (which told Chi to check the
group's unread Telegram messages) is retired. Every other route — notably the
``dm`` route with its Telegram channel — must stay exactly as it was.

No real sends: notify_core is driven with ``dry_run=True`` and subprocess.run
is replaced with a guard that fails the test if anything is executed.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import notify_core  # noqa: E402

EXAMPLE = REPO / "routes.example.json"
MANIFEST = REPO / "deploy.manifest.json"
NUDGE_PLIST = "~/Library/LaunchAgents/com.openclaw.couple-review-nudge.plist"


@pytest.fixture(autouse=True)
def _no_real_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("NOTIFY_ALERT_CONFIG", str(tmp_path / "no-alert.json"))
    monkeypatch.setenv("NOTIFY_ALERT_STATE", str(tmp_path / "alert.state.json"))

    def _forbidden(*a, **k):
        raise AssertionError(f"unexpected subprocess.run in test: {a!r}")

    monkeypatch.setattr(notify_core.subprocess, "run", _forbidden)
    monkeypatch.setattr(notify_core, "find_openclaw", lambda: "openclaw")


def _example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _channels(route):
    return [c["channel"] for c in _example()[route]["channels"]]


def test_group_couple_example_is_line_only():
    route = _example()["group-couple"]
    assert [c["channel"] for c in route["channels"]] == ["line"]
    assert route["channels"][0]["target"] == "LINE_GROUP_ID"
    assert "telegram" not in route["description"].lower()
    assert "小寶" not in route["description"]


def test_group_couple_dry_run_resolves_only_line():
    results = notify_core.notify("group-couple", "hi",
                                 routes_path=str(EXAMPLE), dry_run=True)
    assert [(ch, tgt) for ch, tgt, _ok, _d in results] == [("line", "LINE_GROUP_ID")]
    assert all(ok for _c, _t, ok, _d in results)


def test_dm_route_unchanged():
    assert _example()["dm"] == {
        "description": "Yuting personal DM",
        "channels": [
            {"channel": "telegram", "target": "TELEGRAM_DM_CHAT_ID"},
            {"channel": "line", "target": "LINE_USER_ID"},
            {"channel": "whatsapp", "target": "WHATSAPP_E164_NUMBER", "enabled": False},
        ],
    }
    results = notify_core.notify("dm", "hi", routes_path=str(EXAMPLE), dry_run=True)
    assert [ch for ch, *_ in results] == ["telegram", "line"]  # whatsapp disabled


def test_group_family_route_unchanged():
    assert _example()["group-family"] == {
        "description": "Yuting family group — LINE 尤家人",
        "channels": [{"channel": "line", "target": "LINE_FAMILY_GROUP_ID"}],
    }


def test_no_other_routes_added_or_removed():
    assert set(_example()) == {"dm", "group-couple", "group-family"}


def test_nudge_artifacts_removed_from_repo():
    for rel in ("notifiers/couple_review_email_nudge.py",
                "launchagents/com.openclaw.couple-review-nudge.plist",
                "review-email.example.json"):
        assert not (REPO / rel).exists(), rel


def test_manifest_no_longer_deploys_nudge_or_review_email_seed():
    for t in _manifest()["targets"]:
        assert "couple-review-nudge" not in t.get("src", ""), t
        assert "review-email" not in t["dest"], t


def test_manifest_retires_nudge_agent_and_script():
    removals = _manifest()["removals"]
    plist = [r for r in removals if isinstance(r, dict) and r["path"] == NUDGE_PLIST]
    assert plist and plist[0].get("launchctl") is True  # bootout before delete
    assert "scripts/couple_review_email_nudge.py" in [
        r if isinstance(r, str) else r["path"] for r in removals]


def test_other_shims_and_watchdogs_still_deployed():
    dests = {t["dest"] for t in _manifest()["targets"]}
    for d in ("scripts/notify-dm", "scripts/notify-group-couple",
              "scripts/notify-group-family", "scripts/notify_core.py",
              "~/.openclaw/notify/routes.json",
              "~/.openclaw/notify/failure-alert.json",
              "~/Library/LaunchAgents/com.openclaw.channel-watchdog.plist",
              "~/Library/LaunchAgents/com.openclaw.claude-scheduler-watchdog.plist"):
        assert d in dests, d
