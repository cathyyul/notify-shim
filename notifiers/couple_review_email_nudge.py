#!/usr/bin/env python3
"""couple_review_email_nudge — nightly email nudge to Chi.

If new items have been posted to the couple-group route **since the last nudge**
(per the ``notify_core`` send-ledger), send a **nudge-only** email (no message
content) telling Chi there are items to review on Telegram (小寶murmur). If
nothing new, send nothing.

Watermark model: after a successful send, a local state file advances to the
newest message just covered, so no message is ever missed (a late-evening post,
or a backlog from a day the job did not run, is picked up on the next run) and
nothing is nudged twice.

Why: the couple group's LINE channel shares one LINE Official Account free push
quota that runs out mid-month, so Chi can miss items there. He checks email in
the evening reliably; this is the backstop. The nudge fires whenever *any*
couple-group channel succeeded (Telegram delivers even while LINE is over quota).

Config (local, never in this public repo): ``~/.openclaw/notify/review-email.json``
    {"to": "chi@example.com", "from_account": "sender@gmail.com", "route": "group-couple"}
State (local, auto-created): ``~/.openclaw/notify/review-email.state.json``

Exit codes:
  0 = email sent, or nothing to send
  2 = config error or send failure
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".openclaw" / "notify" / "review-email.json"
DEFAULT_LEDGER = Path.home() / ".openclaw" / "notify" / "send-ledger.jsonl"
DEFAULT_STATE = Path.home() / ".openclaw" / "notify" / "review-email.state.json"
DEFAULT_ROUTES = Path.home() / ".openclaw" / "notify" / "routes.json"

# gog install locations to probe when launchd's minimal PATH hides it from
# ``shutil.which`` (Apple Silicon vs Intel Homebrew).
_GOG_CANDIDATES = ("/opt/homebrew/bin/gog", "/usr/local/bin/gog")


def find_gog() -> str:
    """Resolve the gog binary (env override > PATH > known install dirs)."""
    env = os.environ.get("GOG_BIN")
    if env:
        return env
    found = shutil.which("gog")
    if found:
        return found
    for cand in _GOG_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return _GOG_CANDIDATES[0]  # last-resort default


def default_notify_dm_bin() -> str:
    """notify-dm path, honoring a relocated workspace (OPENCLAW_WORKSPACE)."""
    ws = os.environ.get("OPENCLAW_WORKSPACE") or str(
        Path.home() / ".openclaw" / "workspace"
    )
    return str(Path(ws) / "scripts" / "notify-dm")


def _env_with_binary_on_path(binary: str) -> dict:
    """os.environ with the binary's directory prepended to PATH.

    Under a minimal launchd PATH, Homebrew CLIs (gog) aren't found; prepend
    their directory so subprocess can resolve them. Only for absolute paths, so
    a bare name never injects the caller's cwd.
    """
    env = dict(os.environ)
    bindir = os.path.dirname(binary)
    if bindir and os.path.isabs(bindir):
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        if bindir not in parts:
            env["PATH"] = os.pathsep.join([bindir, *parts])
    return env


class NotConfigured(Exception):
    """Feature not set up (no file, or the untouched placeholder seed) — the
    caller should exit quietly rather than alert."""


def _placeholder(v: str) -> bool:
    return (not v) or v.startswith("REPLACE_")


def load_config(path: str):
    """Return ``(to, from_account, route, require_channel)``.

    Raises ``NotConfigured`` when the feature simply is not set up (missing file
    or the untouched placeholder seed) so the caller can exit 0 quietly. Raises
    ``ValueError`` when a *real* config is present but broken (malformed JSON or
    a partially-filled/invalid address), so the caller can alert instead of
    failing silently — the deploy seeds a placeholder, so "silently dead on a
    bad config" is a real operational trap in the exact backstop path.
    """
    p = Path(path)
    if not p.is_file():
        raise NotConfigured(f"no review-email config at {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}")
    to = (data.get("to") or "").strip()
    frm = (data.get("from_account") or "").strip()
    route = (data.get("route") or "group-couple").strip() or "group-couple"
    require_channel = (data.get("require_channel") or "telegram").strip() or "telegram"
    if _placeholder(to) and _placeholder(frm):
        raise NotConfigured(f"placeholder review-email config at {path} (not set up)")
    if not to or "@" not in to or to.startswith("REPLACE_"):
        raise ValueError(f"review-email config '{path}' has no valid 'to' address")
    if not frm or "@" not in frm or frm.startswith("REPLACE_"):
        raise ValueError(f"review-email config '{path}' has no valid 'from_account'")
    return to, frm, route, require_channel


def validate_route(route: str, require_channel: str, routes_path: str) -> None:
    """Raise ValueError if ``route`` / ``require_channel`` don't exist in the
    routes config, so a typo surfaces as a config error (alert) instead of a
    forever-empty 'nothing to send'.

    Best-effort: if routes.json is absent/unreadable we cannot validate, so we
    skip rather than invent a new failure mode.
    """
    p = Path(routes_path)
    if not p.is_file():
        return
    try:
        routes = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    # Defensive against a parseable-but-malformed routes.json: turn any schema
    # mismatch into a ValueError (→ alert) rather than an uncaught AttributeError.
    if not isinstance(routes, dict):
        raise ValueError(f"routes config {routes_path} is not a JSON object")
    route_cfg = routes.get(route)
    if route_cfg is None:
        raise ValueError(f"route '{route}' is not in routes config {routes_path}")
    if not isinstance(route_cfg, dict):
        raise ValueError(f"route '{route}' entry is malformed in {routes_path}")
    channels = [c.get("channel") for c in route_cfg.get("channels", [])
                if isinstance(c, dict)]
    if require_channel not in channels:
        raise ValueError(
            f"require_channel '{require_channel}' is not a channel of route "
            f"'{route}' (has: {', '.join(c for c in channels if c)})")


def _parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def load_watermark(path: str):
    """Return the last-notified datetime, or None if no/invalid state file."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return _parse_ts(data.get("last_notified_ts"))


def save_watermark(path: str, ts: dt.datetime) -> None:
    """Persist the watermark to the newest message just covered."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Microsecond precision so the strict ``ts > since`` filter can still
    # distinguish two sends that fell in the same whole second.
    p.write_text(json.dumps({"last_notified_ts": ts.isoformat(timespec="microseconds")}),
                 encoding="utf-8")


def _delivered(entry: dict, require_channel) -> bool:
    """Whether the entry counts as delivered on the channel the nudge points to.

    Prefers the per-channel record so a nudge that tells Chi to open Telegram
    fires only when Telegram itself succeeded (not merely LINE). Falls back to
    the legacy ``ok`` flag for older ledger lines without per-channel data.
    """
    channels = entry.get("channels")
    if require_channel and isinstance(channels, dict):
        return bool(channels.get(require_channel))
    return bool(entry.get("ok"))


def unnotified(ledger_file: str, route: str, since, require_channel=None):
    """Sorted datetimes of ``route`` sends newer than ``since`` that reached the
    channel the nudge points to (``require_channel``).

    ``since=None`` (no watermark yet) counts all matching entries. Timestamps
    are parsed to tz-aware datetimes so the comparison is correct across DST.
    """
    p = Path(ledger_file)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("route") != route or not _delivered(entry, require_channel):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        if since is None or ts > since:
            out.append(ts)
    out.sort()
    return out


def send_email(gog_bin: str, account: str, to: str, subject: str, body: str,
               *, dry_run: bool, timeout: int = 120):
    """Send the nudge via gog. Return ``(ok, detail)`` — never raises, so the
    caller's failure-alert path always runs.

    A bounded timeout stops a stalled gog (network/auth) from wedging the
    LaunchAgent indefinitely; TimeoutExpired and any OSError (not just a missing
    binary) become a failed-send result instead of an uncaught crash.
    """
    cmd = [gog_bin, "send", "--account", account, "--to", to,
           "--subject", subject, "--body", body, "--no-input"]
    if dry_run:
        cmd.insert(2, "-n")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_env_with_binary_on_path(gog_bin), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"gog send timed out after {timeout}s"
    except OSError as exc:
        return False, f"gog send could not run ({exc})"
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def alert_failure(detail: str) -> None:
    """Best-effort self-explaining DM to Yuting when the nudge send fails."""
    bin_ = os.environ.get("NOTIFY_DM_BIN") or default_notify_dm_bin()
    msg = (
        "🔴 couple-review email nudge 失敗 — Chi 的傍晚 review 提醒沒寄出。\n"
        f"原因：{detail[:400]}\n"
        "下一步：檢查 gog 授權（gog send -n --account <sender> ...）或 "
        "~/.openclaw/notify/review-email.json 收件人設定。"
    )
    try:
        subprocess.run([bin_, msg], capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # best-effort: a hung/broken notify-dm must not wedge the job either


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="couple_review_email_nudge")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--ledger",
                    default=os.environ.get("NOTIFY_LEDGER") or str(DEFAULT_LEDGER))
    ap.add_argument("--state",
                    default=os.environ.get("NOTIFY_REVIEW_STATE") or str(DEFAULT_STATE))
    ap.add_argument("--routes",
                    default=os.environ.get("NOTIFY_ROUTES") or str(DEFAULT_ROUTES))
    ap.add_argument("--dry-run", action="store_true",
                    help="pass gog -n and do not advance the watermark")
    args = ap.parse_args(argv)

    try:
        to, frm, route, require_channel = load_config(args.config)
        validate_route(route, require_channel, args.routes)
    except NotConfigured as exc:
        # Feature simply not set up — stay quiet, don't nag nightly.
        print(f"couple-review-nudge: not configured ({exc}); skipping", file=sys.stderr)
        return 0
    except ValueError as exc:
        # Real config present but broken — alert instead of dying silently.
        print(f"couple-review-nudge: config error — {exc}", file=sys.stderr)
        alert_failure(f"review-email config error: {exc}")
        return 2

    since = load_watermark(args.state)
    pending = unnotified(args.ledger, route, since, require_channel)
    if not pending:
        print(f"couple-review-nudge: no new '{route}' items since "
              f"{since.isoformat() if since else 'start'}; nothing to send")
        return 0

    n = len(pending)
    subject = "Claw：小寶murmur 有東西要 review"
    body = (
        f"小寶murmur 有 {n} 則新訊息等你 review。\n\n"
        "請開 Telegram（小寶murmur 群）查看並回覆。\n\n"
        "— Claw"
    )
    ok, detail = send_email(find_gog(), frm, to, subject, body, dry_run=args.dry_run)
    if not ok:
        print(f"couple-review-nudge: send FAILED — {detail}", file=sys.stderr)
        alert_failure(detail)
        return 2

    if not args.dry_run:
        # Advance to the newest message just covered — anything that arrived
        # after our read (even during the send) is caught on the next run.
        try:
            save_watermark(args.state, pending[-1])
        except OSError as exc:
            print(f"couple-review-nudge: WARNING watermark not saved ({exc}); "
                  "may re-notify next run", file=sys.stderr)

    print(f"couple-review-nudge: emailed {to} ({n} new item(s))"
          + (" [dry-run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
