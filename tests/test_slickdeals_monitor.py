"""slickdeals_monitor.py: storage tracker (notify-shim#53) + existing trackers unchanged."""
import datetime as dt
import email.utils
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "notifiers" / "slickdeals_monitor.py"


@pytest.fixture
def mon(tmp_path, monkeypatch):
    monkeypatch.setenv("SLICKDEALS_STATE_PATH", str(tmp_path / "seen.json"))
    spec = importlib.util.spec_from_file_location("slickdeals_monitor_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feed(items):
    now = dt.datetime.now(dt.timezone.utc)
    parts = []
    for thread_id, title in items:
        parts.append(
            "<item><title>{}</title><link>https://slickdeals.net/f/{}-x?utm=1</link>"
            "<pubDate>{}</pubDate><description></description></item>".format(
                title.replace("&", "&amp;").replace('"', "&quot;"),
                thread_id,
                email.utils.format_datetime(now - dt.timedelta(hours=1)),
            )
        )
    return ET.fromstring("<rss><channel>{}</channel></rss>".format("".join(parts)))


def _install_feeds(mon, monkeypatch, by_query):
    calls = []

    def fake_fetch(query, mode="frontpage"):
        calls.append((query, mode))
        return _feed(by_query.get(query, []))

    monkeypatch.setattr(mon, "fetch_feed", fake_fetch)
    return calls


def _run(mon, monkeypatch, capsys, argv=("--no-state",)):
    monkeypatch.setattr("sys.argv", ["slickdeals_monitor.py", *argv])
    assert mon.main() == 0
    return capsys.readouterr().out


# --- capacity / classification (structured tokens only) ---------------------

@pytest.mark.parametrize(
    "title, expected",
    [
        ("Samsung T7 Shield 4TB Portable SSD $229", 4.0),
        ("Crucial X10 Pro 3.84TB external SSD", 3.84),
        ("WD 8 TB My Book Desktop External Hard Drive", 8.0),
        ("SanDisk 4000GB Extreme Portable SSD", 4.0),
        ("2x 4TB Samsung T9 Portable SSD bundle", 4.0),
        ("Seagate Expansion 1TB/2TB/5TB External Hard Drive", 5.0),
        ("Samsung Portable SSD sale", None),
    ],
)
def test_capacity_tokens(mon, title, expected):
    assert mon.storage_capacity_tb(title) == expected


@pytest.mark.parametrize(
    "title, label",
    [
        ("Samsung T7 Shield 4TB Portable SSD", "SSD 4TB"),
        ("SanDisk 8TB Desert Portable SSD", "SSD 8TB"),
        ("2x 4TB Samsung T9 Portable SSD bundle", "SSD 4TB"),
        ("WD 8TB Elements Desktop External Hard Drive", "硬碟 8TB"),
        ("Seagate IronWolf 12TB NAS HDD", "硬碟 12TB"),
        ("Samsung Portable SSD Sitewide Sale", "容量未判定"),
        ("Seagate External Hard Drive deals", "容量未判定"),
    ],
)
def test_storage_included(mon, title, label):
    assert mon.classify_storage(title) == label


@pytest.mark.parametrize(
    "title",
    [
        "Crucial X10 Pro 3.84TB external SSD",       # below SSD threshold
        "Samsung T7 2TB Portable SSD",
        "WD 6TB Elements Desktop Hard Drive",        # below HDD threshold
        "2x 4TB WD Blue Hard Drive bundle",          # per-unit 4TB < 8TB
        "Samsung 990 Pro 4TB M.2 NVMe Internal SSD",  # internal SSD, not external
        "Dell XPS 16 Laptop w/ 4TB SSD",             # whole computer, not a drive
        "Jellycat bunny plush",                      # not storage at all
    ],
)
def test_storage_excluded(mon, title):
    assert mon.classify_storage(title) is None


def test_external_enclosure_nvme_counts_as_external(mon):
    assert mon.classify_storage("Acasis USB4 NVMe enclosure + 4TB SSD") == "SSD 4TB"


# --- end-to-end through main() ------------------------------------------------

def test_storage_section_tagged_and_filtered(mon, monkeypatch, capsys):
    calls = _install_feeds(mon, monkeypatch, {
        "ssd": [(101, "Samsung T7 Shield 4TB Portable SSD $229"),
                (102, "Samsung T7 2TB Portable SSD $119"),
                (103, "Samsung Portable SSD Sitewide Sale")],
        "hard drive": [(201, "WD 8TB Elements Desktop External Hard Drive $129")],
    })
    out = _run(mon, monkeypatch, capsys)
    assert "[💾 儲存｜SSD 4TB] Samsung T7 Shield 4TB" in out
    assert "[💾 儲存｜硬碟 8TB] WD 8TB Elements" in out
    assert "[💾 儲存｜容量未判定] Samsung Portable SSD Sitewide Sale" in out
    assert "2TB Portable SSD" not in out
    assert out.startswith("Found 3 candidate deals")
    storage_calls = [c for c in calls if c[0] in mon.STORAGE_QUERIES]
    assert storage_calls and all(mode == "frontpage" for _, mode in storage_calls)


def test_keywords_unaffected_by_storage_filter(mon, monkeypatch, capsys):
    _install_feeds(mon, monkeypatch, {
        "bambu": [(301, "Bambu Lab A1 mini 3D printer $199")],
        "nintendo switch": [(302, "Nintendo Switch 2 w/ 512GB microSD")],
    })
    out = _run(mon, monkeypatch, capsys)
    assert "- [bambu] Bambu Lab A1 mini" in out
    assert "- [nintendo switch] Nintendo Switch 2 w/ 512GB microSD" in out
    assert "💾" not in out


def test_existing_keywords_and_dell_queries_unchanged(mon):
    assert mon.KEYWORDS == [
        "uber gift card", "doordash gift card", "grubhub gift card",
        "instacart gift card", "bambu", "sunlu", "jellycat", "nintendo switch",
    ]
    assert mon.DELL_QUERIES == ["dell", "alienware", "dell outlet", "dell coupon"]


def test_dedup_across_trackers_and_days(mon, monkeypatch, capsys, tmp_path):
    feeds = {
        "ssd": [(401, "Samsung T9 4TB Portable SSD")],
        "hdd": [(401, "Samsung T9 4TB Portable SSD")],  # same thread twice
    }
    _install_feeds(mon, monkeypatch, feeds)
    first = _run(mon, monkeypatch, capsys, argv=())
    assert first.startswith("Found 1 candidate deals")
    seen = json.loads((tmp_path / "seen.json").read_text())["seen"]
    assert "401" in seen
    second = _run(mon, monkeypatch, capsys, argv=())
    assert second.startswith("Found 0 candidate deals")


def test_existing_seen_state_suppresses_storage(mon, monkeypatch, capsys, tmp_path):
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    (tmp_path / "seen.json").write_text(json.dumps({"seen": {"501": stamp}}))
    _install_feeds(mon, monkeypatch, {"ssd": [(501, "SanDisk 8TB Desert Portable SSD")]})
    out = _run(mon, monkeypatch, capsys, argv=())
    assert out.startswith("Found 0 candidate deals")
