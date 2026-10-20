#!/usr/bin/env python3
"""Fetch Slickdeals RSS results and print fresh, not-yet-seen matches.

Two independent trackers:

1. KEYWORDS — frontpage-only search (editor-vetted deals) for the standing
   wishlist. Low volume, high signal.
2. DELL — full-site search for deals actually *sold by dell.com*, so the Dell
   statement credit can be applied. Brand-only hits (a Dell monitor sold by
   Best Buy / Amazon / B&H) are rejected: the credit does not work there.

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


def collect(queries, mode, now, max_age_hours, dell_only=False):
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

            published = item_pubdate(item)
            if published is None:
                age_hours = None
            else:
                age_hours = (
                    now - published.astimezone(dt.timezone.utc)
                ).total_seconds() / 3600
                if age_hours > max_age_hours:
                    continue
            tag = "dell.com" if dell_only else query
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
