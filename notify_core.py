#!/usr/bin/env python3
"""notify_core — fan a single message out to every channel of a named route.

The thin shim scripts (``notify-dm``, ``notify-group-couple`` …) all delegate
here with a ``--route`` argument. Each route maps to a list of channels; the
message is delivered to each channel via ``openclaw message send``.

Design notes
------------
* **Partial failure is not fatal (notify-shim#26).** If some channels fail but
  at least one delivers, the message reached the user, so the process exits 0
  and sends a throttled email naming the failed channel(s) (recipient from
  config) — a per-channel summary still prints to stderr, so a failure is never
  silently swallowed. Only a *total* outage (every channel failed) exits
  non-zero.
* **Config-driven.** Channels per route live in a JSON file (see
  ``routes.example.json``). Adding/removing a channel — or a whole route — is a
  config edit, no code change.
* **Privacy.** Real chat/user/group IDs live in a local, gitignored routes file
  (default ``~/.openclaw/notify/routes.json``), never in the repo.
* **Gateway dependency.** Delivery goes through ``openclaw message send``, so the
  OpenClaw gateway must be running.

Stdlib only; runs on the system ``python3`` (3.9) and the workspace venv (3.14).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def default_routes_paths() -> list[str]:
    """Candidate routes files, highest precedence first."""
    return [
        os.environ.get("NOTIFY_ROUTES", "") or "",
        str(Path.home() / ".openclaw" / "notify" / "routes.json"),
    ]


def ledger_path() -> str:
    """Local JSONL send-ledger path (env override for tests)."""
    return os.environ.get("NOTIFY_LEDGER", "") or str(
        Path.home() / ".openclaw" / "notify" / "send-ledger.jsonl"
    )


# --------------------------------------------------------------------------- #
# Partial-failure email alert (notify-shim#26)
#
# When some — but not all — channels fail, the message still reached the user on
# a working channel, so the process must NOT fail the whole flow (that turned a
# transient WhatsApp-listener blip into a self-reported job FAILURE upstream —
# see drift-sentinel exit=1, wsi#121). Instead: keep exit 0 as long as at least
# one channel delivered, and send a best-effort email naming the failed
# channel(s) so the failure is never silently swallowed. When EVERY channel
# fails, exit stays non-zero (a genuine notification outage) — and the email
# (an independent transport) is the escalation path.
#
# The email goes through gog (same mechanism as couple_review_email_nudge.py);
# recipient/sender come from config, not hardcoded. Throttled to at most one
# email per channel per day so a channel that keeps failing does not spam.
# --------------------------------------------------------------------------- #
def alert_config_path() -> str:
    """Failure-alert email config path (env override for tests)."""
    return os.environ.get("NOTIFY_ALERT_CONFIG", "") or str(
        Path.home() / ".openclaw" / "notify" / "failure-alert.json"
    )


def alert_state_path() -> str:
    """Per-channel-per-day throttle state path (env override for tests)."""
    return os.environ.get("NOTIFY_ALERT_STATE", "") or str(
        Path.home() / ".openclaw" / "notify" / "failure-alert.state.json"
    )


def _placeholder(v: str) -> bool:
    return not v or v.startswith("REPLACE_WITH")


def load_alert_config():
    """Return the alert-email config dict, or ``None`` when alerting is not set
    up (file missing, ``enabled: false``, placeholder/blank fields, or malformed
    JSON). Never raises — a broken alert config must not break delivery."""
    path = alert_config_path()
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"notify: failure-alert config unreadable ({exc}); "
              f"skipping email alert", file=sys.stderr)
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    to = cfg.get("to", "")
    frm = cfg.get("from_account", "")
    if not isinstance(to, str) or not isinstance(frm, str) \
            or _placeholder(to) or _placeholder(frm):
        return None
    return {"to": to, "from_account": frm}


def find_gog() -> str:
    """Resolve the gog binary (env override > PATH > known install dirs).
    Under launchd's minimal PATH, Homebrew CLIs are not on PATH."""
    override = os.environ.get("GOG_BIN", "")
    if override:
        return override
    found = shutil.which("gog")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/gog", "/usr/local/bin/gog"):
        if Path(cand).exists():
            return cand
    return "gog"


def _send_alert_email(cfg, subject: str, body: str, *, timeout: int = 30):
    """Send the failure alert via gog. Return ``(ok, detail)`` — never raises,
    so a stalled/absent gog becomes a failed-send result, not a crash. A bounded
    timeout stops a wedged gog from hanging the notify call."""
    gog = find_gog()
    cmd = [gog, "send", "--account", cfg["from_account"], "--to", cfg["to"],
           "--subject", subject, "--body", body, "--no-input"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_env_with_binary_on_path(gog), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"gog send timed out after {timeout}s"
    except OSError as exc:
        return False, f"gog send could not run ({exc})"
    if proc.returncode == 0:
        return True, "sent"
    return False, (proc.stderr or proc.stdout or "gog send failed").strip()[:200]


# ANSI/CSI escape sequences (colours, cursor moves) — openclaw's stderr is full
# of them, and raw ESC (0x1b) bytes in an email body get the message silently
# dropped by Gmail (notify-shim#28: the API accepts it and returns a messageId,
# but it never reaches the inbox — not even spam).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# C0 control characters except tab/newline, plus DEL — also unsafe in a body.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# openclaw frames its advisory banners ("◇ Update history", "◇ Doctor warnings"
# …) in box-drawing characters, and every line of such a block starts with a
# frame glyph. Those banners replay past status, not the failure being
# reported, and they are long enough to consume the whole excerpt budget on
# their own — notify-shim#34: a 2.8 KB banner left every alert unreadable.
_BANNER_LINE_RE = re.compile(
    r"^[\s\u2500-\u257f\u25c6-\u25c8]*[\u2500-\u257f\u25c6-\u25c8]")
# Per-channel excerpt budget. The alert formatter re-sanitizes with headroom so
# the "exit N: " prefix that send_one puts in front survives that second pass.
_DETAIL_LIMIT = 420
_ALERT_DETAIL_LIMIT = _DETAIL_LIMIT + 100


def _strip_openclaw_banners(text: str) -> str:
    """Drop openclaw's box-drawing banner lines, keeping real output."""
    return "\n".join(line for line in (text or "").splitlines()
                      if not _BANNER_LINE_RE.match(line))


def _sanitize_for_email(text: str, *, limit: int = 500,
                        keep: str = "tail") -> str:
    """Strip ANSI escapes, control characters and openclaw's banner frames so
    the alert body is plain, deliverable text (notify-shim#28, #34).

    Truncation keeps the **tail** by default: a command's fatal error lands at
    the end of its output, behind whatever advisory noise the CLI printed
    first. Keeping the head (the old behaviour) meant the reader only ever saw
    the banner. Pass ``keep="head"`` for text that reads front-to-back, such as
    a caller's command line, where the program name comes first.
    """
    clean = _ANSI_RE.sub("", text or "")
    clean = _CTRL_RE.sub("", clean)
    clean = _strip_openclaw_banners(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > limit:
        clean = (clean[:limit].rstrip() + "…" if keep == "head"
                 else "…" + clean[-limit:].lstrip())
    return clean


def _describe_caller() -> str:
    """Best-effort one-line identity of whatever invoked this shim.

    An alert that only names the failing channel cannot say *which* job hit it,
    leaving the reader to correlate timestamps across logs (notify-shim#34).
    ``NOTIFY_CALLER`` lets a caller name itself. Never raises — a diagnostic
    helper must not be able to break the alert it is annotating.
    """
    try:
        host = socket.gethostname()
    except OSError:
        host = "?"
    caller = os.environ.get("NOTIFY_CALLER", "").strip()
    if not caller:
        try:
            proc = subprocess.run(["ps", "-o", "args=", "-p", str(os.getppid())],
                                  capture_output=True, text=True, timeout=5)
            caller = (proc.stdout.strip().splitlines()[0]
                      if proc.returncode == 0 and proc.stdout.strip() else "")
        except Exception:
            caller = ""
    return (f"Host: {host} | pid {os.getpid()} | caller: "
            f"{_sanitize_for_email(caller, limit=160, keep='head') or 'unknown'}")


def _probe_writable(path: str):
    """Return ``(ok, detail)`` for whether ``path`` can be written.

    The throttle water-mark is written *after* the email goes out, so a failure
    there can never appear in the alert it belongs to. Probing first lets that
    alert say "expect repeats" in the same message (notify-shim#34).
    """
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        probe = target.with_name(target.name + ".probe")
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def maybe_send_failure_alert(route: str, results, *, dry_run: bool,
                             notes=None) -> None:
    """Best-effort email when one or more channels failed, throttled to one
    email per channel per day. Never raises.

    ``notes`` carries diagnostics gathered earlier in the run (e.g. a failed
    send-ledger append). They used to go only to stderr, which for a launchd or
    cron caller is a log nobody tails — so a half-broken run looked
    clean (notify-shim#34).
    """
    failed = [(ch, tgt, detail) for (ch, tgt, ok, detail) in results if not ok]
    if not failed or dry_run:
        return
    cfg = load_alert_config()
    if cfg is None:
        print("notify: channel(s) failed but failure-alert email is not "
              "configured (~/.openclaw/notify/failure-alert.json) — skipping",
              file=sys.stderr)
        return
    today = dt.date.today().isoformat()
    try:
        state = json.loads(Path(alert_state_path()).read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, OSError, ValueError):
        state = {}
    fresh = [f for f in failed if state.get(f[0]) != today]
    if not fresh:  # every failed channel already alerted today
        return
    diagnostics = list(notes or [])
    throttle_ok, throttle_err = _probe_writable(alert_state_path())
    if not throttle_ok:
        diagnostics.append(
            f"throttle water-mark is not persistable ({throttle_err}) — "
            f"the one-email-per-channel-per-day limit cannot hold, so "
            f"expect repeat alerts until that path is writable")
    lines = [f"Route: {route}", f"Time: {dt.datetime.now().astimezone().isoformat()}",
             _describe_caller(),
             "", "Failed channel(s):"]
    lines += [f"  - {ch}:{tgt} — "
              f"{_sanitize_for_email(detail, limit=_ALERT_DETAIL_LIMIT)}"
              for (ch, tgt, detail) in failed]
    if diagnostics:
        lines += ["", "Diagnostics:"]
        lines += [f"  - {_sanitize_for_email(note)}" for note in diagnostics]
    lines += ["", "Other channels on this route delivered normally (the message "
              "was not lost) unless this route has only failed channels.",
              "Fix the failing channel (e.g. re-link WhatsApp / restart the "
              "gateway), then delivery resumes automatically."]
    subject = f"[notify] {len(failed)} channel(s) failed on route '{route}'"
    ok, detail = _send_alert_email(cfg, subject, "\n".join(lines))
    if not ok:
        print(f"notify: failure-alert email send failed ({detail})",
              file=sys.stderr)
        return  # do not advance throttle → retry on the next failure
    for ch, _tgt, _detail in fresh:
        state[ch] = today
    try:
        Path(alert_state_path()).parent.mkdir(parents=True, exist_ok=True)
        Path(alert_state_path()).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"notify: failure-alert throttle-state write failed ({exc})",
              file=sys.stderr)
    print(f"notify: failure-alert email sent to {cfg['to']} "
          f"({len(fresh)} channel(s))", file=sys.stderr)


def _record_ledger(route: str, results, *, dry_run: bool, notes=None) -> None:
    """Best-effort append of one send record; never raises.

    A downstream digest (e.g. the couple-group evening email nudge) reads this
    to learn whether anything was posted to a route today. A ledger write must
    never affect notification delivery, so every error here is swallowed.
    """
    if dry_run:
        return
    try:
        channels = {ch: ok for (ch, _tgt, ok, _detail) in results}
        entry = {
            # Microsecond precision: the couple-group nudge watermark filters
            # with a strict ``ts > since``, so two sends in the same whole
            # second must still get distinct, ordered timestamps or the later
            # one would compare equal to the watermark and be skipped forever.
            "ts": dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
            "route": route,
            "ok": any(channels.values()),
            # Per-channel outcome: a nudge that directs the reader to a specific
            # channel must gate on THAT channel, not on "any channel succeeded"
            # (LINE up but Telegram down must not trigger a "check Telegram").
            "channels": channels,
        }
        path = Path(ledger_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        # Never break delivery — but don't fail silently either: the couple-group
        # nudge relies on this ledger as its only source of truth, so a lost
        # append must at least be visible in logs.
        message = (f"send-ledger append failed ({exc}); "
                   f"'{route}' event not recorded")
        print(f"notify: {message}", file=sys.stderr)
        if notes is not None:
            notes.append(message)


def find_openclaw() -> str:
    """Resolve the openclaw binary (env override > PATH > Homebrew default)."""
    return (
        os.environ.get("OPENCLAW_BIN")
        or shutil.which("openclaw")
        or "/opt/homebrew/bin/openclaw"
    )


def load_routes(path: str | None = None):
    """Return ``(routes_dict, path_used)``; raise FileNotFoundError if none."""
    candidates = [path] if path else default_routes_paths()
    for cand in candidates:
        if cand and Path(cand).is_file():
            return json.loads(Path(cand).read_text(encoding="utf-8")), cand
    raise FileNotFoundError(
        "no routes file found (looked at: "
        + ", ".join(c for c in candidates if c)
        + "). Copy routes.example.json to ~/.openclaw/notify/routes.json and fill in IDs."
    )


def _env_with_binary_on_path(binary: str) -> dict:
    """os.environ with the openclaw binary's directory prepended to PATH.

    openclaw is a Node CLI; under a minimal launchd PATH
    (``/usr/bin:/bin:/usr/sbin:/sbin``) it can't find its ``node`` runtime,
    which lives alongside it (e.g. ``/opt/homebrew/bin``). Prepending that
    directory lets the CLI resolve node when shims run from a LaunchAgent.
    """
    env = dict(os.environ)
    bindir = os.path.dirname(binary)
    # Only act on an explicit absolute directory. A bare name ("openclaw") has
    # no dirname, and a relative one must not cause the caller's cwd to be
    # injected into PATH.
    if bindir and os.path.isabs(bindir):
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        if bindir not in parts:
            env["PATH"] = os.pathsep.join([bindir, *parts])
    return env


def _command_detail(proc) -> str:
    """Summarise a finished ``openclaw`` call for humans.

    stdout goes first and stderr last because the excerpt is truncated from the
    end: openclaw prints its advisory banner on stdout while the fatal error
    lands on stderr, so keeping the tail keeps the part that explains the
    failure. The exit code is prefixed *after* truncation so it always shows
    (notify-shim#34).
    """
    body = _sanitize_for_email(
        "\n".join(part for part in (proc.stdout or "", proc.stderr or "")
                   if part.strip()),
        limit=_DETAIL_LIMIT)
    if proc.returncode == 0:
        return body
    return f"exit {proc.returncode}: {body}" if body else f"exit {proc.returncode} (no output)"


#: Per-channel deadline for one ``openclaw message send``. Callers that wrap
#: the shim in their own timeout must budget for *every* enabled channel hitting
#: this, not just one — see ``route_send_budget`` (notify-shim#35).
SEND_TIMEOUT_SECONDS = 60


def route_send_budget(route: str, *, routes_path: str | None = None,
                      overhead: int = 30) -> int:
    """Worst-case wall-clock a ``notify_core`` run of ``route`` can take.

    A caller that wraps the shim in ``subprocess.run(..., timeout=X)`` with a
    guessed X kills delivery mid-flight: the channel-watchdog used 30s while a
    two-channel route can legitimately take 120s, so its alerts were killed
    every time and never reached anyone (notify-shim#35). Ask here instead of
    writing a second number.

    Falls back to a single channel's budget when the routes file cannot be
    read — a diagnostic helper must not become a new failure mode.
    """
    try:
        routes, _used = load_routes(routes_path)
        channels = [ch for ch in routes.get(route, {}).get("channels", [])
                    if ch.get("enabled", True)]
    except (FileNotFoundError, OSError, ValueError):
        channels = []
    return max(len(channels), 1) * SEND_TIMEOUT_SECONDS + overhead


def send_one(channel: str, target: str, message: str, *, dry_run: bool,
             openclaw_bin: str | None = None,
             timeout: int = SEND_TIMEOUT_SECONDS):
    """Send to one channel. Return ``(ok: bool, detail: str)`` — never raises.

    A bounded timeout plus catching every OSError (not just a missing binary)
    means one wedged/erroring channel becomes a normal per-channel failure
    rather than aborting the whole fan-out. That matters because the caller
    records the send-ledger only after the loop finishes: a later channel that
    hung or raised would otherwise erase an already-successful earlier channel
    (e.g. Telegram delivered, then LINE hangs) from the ledger entirely.
    """
    binary = openclaw_bin or find_openclaw()
    cmd = [binary, "message", "send",
           "--channel", channel, "--target", target, "--message", message]
    if dry_run:
        return True, "dry-run (not sent): " + " ".join(cmd[:-1] + ["<message>"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_env_with_binary_on_path(binary), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run openclaw ({exc})"
    return proc.returncode == 0, _command_detail(proc)


def notify(route: str, message: str, *, routes_path: str | None = None,
           dry_run: bool = False, notes=None):
    """Fan ``message`` out to every channel of ``route``.

    Returns a list of ``(channel, target, ok, detail)`` tuples.
    """
    routes, _used = load_routes(routes_path)
    if route not in routes:
        raise KeyError(
            f"unknown route '{route}'. Known routes: {', '.join(sorted(routes))}"
        )
    channels = routes[route].get("channels", [])
    if not channels:
        raise ValueError(f"route '{route}' has no channels configured")

    results = []
    for ch in channels:
        # A channel can be turned off with "enabled": false (default true),
        # so toggling where notifications go is a one-field edit in routes.json.
        if not ch.get("enabled", True):
            continue
        ok, detail = send_one(
            ch["channel"], ch["target"], message, dry_run=dry_run
        )
        results.append((ch["channel"], ch["target"], ok, detail))
    _record_ledger(route, results, dry_run=dry_run, notes=notes)
    return results


def _resolve_message(args) -> str:
    if args.message is not None:
        return args.message
    if args.words:
        return " ".join(args.words)
    return sys.stdin.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="notify_core",
        description="Fan a message out to every channel of a named route.",
    )
    ap.add_argument("--route", required=True, help="route name, e.g. dm")
    ap.add_argument("-m", "--message", help="message text (else positional, else stdin)")
    ap.add_argument("words", nargs="*", help="message text as positional words")
    ap.add_argument("--routes", help="explicit routes.json path")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; do NOT call openclaw")
    args = ap.parse_args(argv)

    message = _resolve_message(args).strip()
    if not message:
        print("notify: empty message", file=sys.stderr)
        return 2

    notes: list[str] = []
    try:
        results = notify(args.route, message,
                         routes_path=args.routes, dry_run=args.dry_run,
                         notes=notes)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 2

    if not results:
        print(f"notify: route '{args.route}' has no enabled channels — "
              f"nothing sent", file=sys.stderr)
        return 0

    failed = [r for r in results if not r[2]]
    succeeded = [r for r in results if r[2]]
    for channel, target, ok, detail in results:
        status = "ok" if ok else "FAIL"
        line = f"[{status}] {channel}:{target}"
        if not ok and detail:
            line += f" — {detail}"
        print(line, file=sys.stderr)

    if failed:
        maybe_send_failure_alert(args.route, results, dry_run=args.dry_run,
                                 notes=notes)
        print(
            f"notify: {len(failed)}/{len(results)} channel(s) FAILED "
            f"for route '{args.route}'",
            file=sys.stderr,
        )
        # Partial failure must NOT fail the whole flow (notify-shim#26): if at
        # least one channel delivered, the message reached the user, so exit 0
        # and let the throttled email alert carry the failure. Only a total
        # outage (no channel delivered) is a non-zero exit.
        if not succeeded:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
