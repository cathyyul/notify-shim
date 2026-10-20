#!/usr/bin/env python3
"""Fetch Slickdeals RSS results and print fresh, not-yet-seen matches.

Three independent trackers:

1. KEYWORDS — frontpage-only search (editor-vetted deals) for the standing
   wishlist. Low volume, high signal.
2. DELL — full-site search for deals actually *sold by dell.com*, so the Dell
   statement credit can be applied. Brand-only hits (a Dell monitor sold by
   Best Buy / Amazon / B&H) are rejected: the credit does not work there.
3. STORAGE — frontpage-only search for external/portable SSDs >= 4 TB and
   external/desktop hard drives >= 8 TB (notify-shim#53). Capacity comes only
   from structured tokens ("4TB", "4000 GB"); a storage deal whose capacity
   can't be read is still sent, tagged 容量未判定, rather than dropped.

Cross-day dedup: every delivered deal is recorded in STATE_PATH so the daily
run never re-sends the same thread. Without this the 72h window meant one deal
was announced up to three days running.

Usage:
  python3 scripts/slickdeals_monitor.py
  python3 scripts/slickdeals_monitor.py --no-state   # dry run, don't record
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

KEYWORDS = [
    "uber gift card",
    "doordash gift card",
    "grubhub gift card",
    "instacart gift card",
    "bambu",
    "sunlu",
    "jellycat",
    "nintendo switch",
]

# Full-site queries; the dell.com merchant test below does the real filtering.
DELL_QUERIES = [
    "dell",
    "alienware",
    "dell outlet",
    "dell coupon",
]

# Dell statement credit expiry (Amex-style half-year window; forfeited if
# unspent). Override with SLICKDEALS_DELL_CREDIT_EXPIRY=YYYY-MM-DD.
DELL_CREDIT_EXPIRY = os.environ.get("SLICKDEALS_DELL_CREDIT_EXPIRY", "2026-12-31")

# Frontpage title searches for the storage tracker; classify_storage() filters.
STORAGE_QUERIES = [
    "ssd",
    "hard drive",
    "hdd",
]
STORAGE_TAG = "💾 儲存"
SSD_MIN_TB = 4.0
HDD_MIN_TB = 8.0

# Structured capacity token. Several tokens (1TB/2TB/5TB variants) -> the
# largest; a multi-pack "2x 4TB" is per-unit 4TB, which is what matters.
CAPACITY_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(tb|gb)\b", re.I)
SSD_RE = re.compile(r"\bssds?\b|\bsolid[- ]state\b", re.I)
HDD_RE = re.compile(r"\bhdds?\b|\bhard\s*(?:drive|disk)s?\b", re.I)
# SSD markers: internal-only (M.2/NVMe stick) vs. explicitly external.
SSD_INTERNAL_RE = re.compile(r"\bm\.2\b|\bnvme\b|\binternal\b|\bpcie\b", re.I)
SSD_EXTERNAL_RE = re.compile(r"\bportable\b|\bexternal\b|\busb\w*|\bthunderbolt\b|\benclosure\b", re.I)
# A whole computer/device that merely ships with a drive is not a drive deal.
STORAGE_DEVICE_RE = re.compile(
    r"\b(?:laptops?|notebooks?|macbook\w*|chromebooks?|tablets?|ipad\w*|consoles?|"
    r"steam\s*deck|phones?|mini\s*pc|gaming\s*pc|desktop\s*(?:pc|computer))\b",
    re.I,
)

BLOCK_TITLE_SUBSTRINGS = [
    "expired",
    "dead",
]

# Dell-family product line appearing in the title.
DELL_BRAND_RE = re.compile(
    r"\b(dell|alienware|xps|latitude|precision|inspiron|optiplex|ultrasharp)\b", re.I
)
# dell.com mentioned as the source, after masking the partner-run refurb site.
DELL_REFURB_SITE_RE = re.compile(r"dellrefurbished\.com", re.I)
DELL_DOMAIN_RE = re.compile(r"\bdell\.com\b", re.I)
# dellrefurbished.com is a separate partner storefront — statement credits that
# are scoped to "Dell.com" generally do not post there.
DELL_REFURB_TITLE_RE = re.compile(r"\bdell\s*refurbished\b", re.I)
# A rival retailer named in the title means the credit cannot be applied.
RIVAL_RETAILER_RE = re.compile(
    r"\b(?:at|on|via|from)\s+(?:amazon|b&h|bhphoto\w*|best\s*buy|newegg|micro\s*center|"
    r"ebay|walmart|costco|woot|adorama|target|staples|sam'?s\s*club)\b"
    r"|\b(?:best\s*buy|micro\s*center|newegg|woot|adorama)\s*[:.]"
    r"|\bopen[- ]box(?:es)?\b",
    re.I,
)

RSS_TEMPLATE = (
    "https://slickdeals.net/newsearch.php?mode={mode}&searcharea=deals"
    "&searchin=first&rss=1&q={query}"
)

STATE_PATH = os.path.expanduser(
    os.environ.get(
        "SLICKDEALS_STATE_PATH",
        "~/.openclaw/workspace/data/slickdeals/seen.json",
    )
)
STATE_RETENTION_DAYS = 60

CONTENT_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
THREAD_ID_RE = re.compile(r"/f/(\d+)")


def fetch_feed(query: str, mode: str = "frontpage"):
    url = RSS_TEMPLATE.format(mode=mode, query=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return ET.fromstring(resp.read())


def item_pubdate(item):
    raw = item.findtext("pubDate")
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def deal_key(link: str) -> str:
    """Stable per-thread id, so utm noise doesn't defeat dedup."""
    match = THREAD_ID_RE.search(link)
    return match.group(1) if match else link.split("?", 1)[0]


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("seen", {}) if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(seen: dict) -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=STATE_RETENTION_DAYS)
    pruned = {}
    for key, stamp in seen.items():
        try:
            if dt.datetime.fromisoformat(stamp) >= cutoff:
                pruned[key] = stamp
        except (TypeError, ValueError):
            continue
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"seen": pruned}, handle, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def item_blob(item) -> str:
    parts = [item.findtext("description") or ""]
    parts.append(item.findtext("content:encoded", default="", namespaces=CONTENT_NS))
    return html.unescape("\n".join(parts))


def sold_by_dell(title: str, blob: str) -> bool:
    """True only when the deal looks like a dell.com purchase."""
    if not DELL_DOMAIN_RE.search(DELL_REFURB_SITE_RE.sub("", blob)):
        return False
    if not DELL_BRAND_RE.search(title):
        return False
    if DELL_REFURB_TITLE_RE.search(title):
        return False
    return not RIVAL_RETAILER_RE.search(title)


def storage_capacity_tb(title: str):
    """Largest structured capacity token in the title, in TB; None if absent."""
    values = []
    for number, unit in CAPACITY_RE.findall(title):
        value = float(number)
        values.append(value if unit.lower() == "tb" else value / 1000)
    return max(values) if values else None


def classify_storage(title: str):
    """Storage-tracker label for a title, or None when it's out of scope."""
    if STORAGE_DEVICE_RE.search(title):
        return None
    if SSD_RE.search(title):
        if SSD_INTERNAL_RE.search(title) and not SSD_EXTERNAL_RE.search(title):
            return None
        kind, label, minimum = "ssd", "SSD", SSD_MIN_TB
    elif HDD_RE.search(title):
        kind, label, minimum = "hdd", "硬碟", HDD_MIN_TB
    else:
        return None
    capacity = storage_capacity_tb(title)
    if capacity is None:
        return "容量未判定"
    if capacity < minimum:
        return None
    return f"{label} {capacity:g}TB"


def collect(queries, mode, now, max_age_hours, dell_only=False, storage=False):
    """Return [(age_hours, tag, title, link)] for fresh, in-scope items."""
    seen_pairs = set()
    found = []
    for query in queries:
        try:
            root = fetch_feed(query, mode=mode)
        except Exception as exc:  # network/parse issues shouldn't kill the run
            print(f"fetch failed for {query}: {exc}", file=sys.stderr)
            continue
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            lowered = title.lower()
            if any(bad in lowered for bad in BLOCK_TITLE_SUBSTRINGS):
                continue
            if (title, link) in seen_pairs:
                continue
            seen_pairs.add((title, link))

            if dell_only and not sold_by_dell(title, item_blob(item)):
                continue
            storage_label = classify_storage(title) if storage else None
            if storage and storage_label is None:
                continue

            published = item_pubdate(item)
            if published is None:
                age_hours = None
            else:
                age_hours = (
                    now - published.astimezone(dt.timezone.utc)
                ).total_seconds() / 3600
                if age_hours > max_age_hours:
                    continue
            if dell_only:
                tag = "dell.com"
            elif storage:
                tag = f"{STORAGE_TAG}｜{storage_label}"
            else:
                tag = query
            found.append((age_hours if age_hours is not None else 10**9, tag, title, link))
    return found


def credit_note() -> str:
    """Trailing header note counting down to the credit expiry."""
    try:
        expiry = dt.date.fromisoformat(DELL_CREDIT_EXPIRY)
    except ValueError:
        return ""
    days = (expiry - dt.date.today()).days
    if days < 0:
        return ""
    marker = "🔴" if days <= 30 else ("🟠" if days <= 60 else "💳")
    return f"｜{marker} Dell credit 剩 {days} 天（{expiry:%m/%d} 到期）"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=72.0,
        help="Only show deals newer than this many hours (default: 72)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="Don't read or record the seen-state (dry run)",
    )
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    matches = collect(KEYWORDS, "frontpage", now, args.max_age_hours)
    matches += collect(DELL_QUERIES, "", now, args.max_age_hours, dell_only=True)
    matches += collect(STORAGE_QUERIES, "frontpage", now, args.max_age_hours, storage=True)

    seen = {} if args.no_state else load_state()
    fresh = []
    picked = set()
    for row in sorted(matches, key=lambda r: (r[0], r[2].lower())):
        key = deal_key(row[3])
        if key in seen or key in picked:
            continue
        picked.add(key)
        fresh.append(row)

    print(f"Found {len(fresh)} candidate deals{credit_note()}")
    for age_hours, tag, title, link in fresh:
        age_text = "unknown age" if age_hours == 10**9 else f"{age_hours:.1f}h old"
        print(f"- [{tag}] {title} ({age_text})\n  {link}")

    if not args.no_state and picked:
        stamp = now.isoformat()
        seen.update({key: stamp for key in picked})
        save_state(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
