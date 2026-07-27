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
DEFAULT_NOTIFY_DM_BIN = Path.home() / ".openclaw" / "workspace" / "scripts" / "notify-dm"


def find_gog() -> str:
    """Resolve the gog binary (env override > PATH > Homebrew default)."""
    return os.environ.get("GOG_BIN") or shutil.which("gog") or "/opt/homebrew/bin/gog"


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


def load_config(path: str):
    """Return ``(to, from_account, route)``; raise ValueError if unfilled."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    to = (data.get("to") or "").strip()
    frm = (data.get("from_account") or "").strip()
    route = (data.get("route") or "group-couple").strip() or "group-couple"
    if not to or "@" not in to or to.startswith("REPLACE_"):
        raise ValueError(f"review-email config '{path}' has no real 'to' address")
    if not frm or "@" not in frm or frm.startswith("REPLACE_"):
        raise ValueError(f"review-email config '{path}' has no real 'from_account'")
    return to, frm, route


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
    p.write_text(json.dumps({"last_notified_ts": ts.isoformat(timespec="seconds")}),
                 encoding="utf-8")


def unnotified(ledger_file: str, route: str, since):
    """Sorted datetimes of successful ``route`` sends newer than ``since``.

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
        if entry.get("route") != route or not entry.get("ok"):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        if since is None or ts > since:
            out.append(ts)
    out.sort()
    return out


def send_email(gog_bin: str, account: str, to: str, subject: str, body: str,
               *, dry_run: bool):
    """Send the nudge via gog. Return ``(ok, detail)``."""
    cmd = [gog_bin, "send", "--account", account, "--to", to,
           "--subject", subject, "--body", body, "--no-input"]
    if dry_run:
        cmd.insert(2, "-n")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_env_with_binary_on_path(gog_bin))
    except FileNotFoundError:
        return False, f"gog binary not found: {gog_bin}"
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def alert_failure(detail: str) -> None:
    """Best-effort self-explaining DM to Yuting when the nudge send fails."""
    bin_ = os.environ.get("NOTIFY_DM_BIN") or str(DEFAULT_NOTIFY_DM_BIN)
    msg = (
        "🔴 couple-review email nudge 失敗 — Chi 的傍晚 review 提醒沒寄出。\n"
        f"原因：{detail[:400]}\n"
        "下一步：檢查 gog 授權（gog send -n --account <sender> ...）或 "
        "~/.openclaw/notify/review-email.json 收件人設定。"
    )
    try:
        subprocess.run([bin_, msg], capture_output=True, text=True)
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="couple_review_email_nudge")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--ledger",
                    default=os.environ.get("NOTIFY_LEDGER") or str(DEFAULT_LEDGER))
    ap.add_argument("--state",
                    default=os.environ.get("NOTIFY_REVIEW_STATE") or str(DEFAULT_STATE))
    ap.add_argument("--dry-run", action="store_true",
                    help="pass gog -n and do not advance the watermark")
    args = ap.parse_args(argv)

    try:
        to, frm, route = load_config(args.config)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"couple-review-nudge: {exc}", file=sys.stderr)
        return 2

    since = load_watermark(args.state)
    pending = unnotified(args.ledger, route, since)
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
