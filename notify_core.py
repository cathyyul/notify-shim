#!/usr/bin/env python3
"""notify_core — fan a single message out to every channel of a named route.

The thin shim scripts (``notify-dm``, ``notify-group-couple`` …) all delegate
here with a ``--route`` argument. Each route maps to a list of channels; the
message is delivered to each channel via ``openclaw message send``.

Design notes
------------
* **Fail-loud.** If *any* channel fails, the process exits non-zero with a
  per-channel summary, so a Telegram success can never hide a LINE failure.
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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def default_routes_paths() -> list[str]:
    """Candidate routes files, highest precedence first."""
    return [
        os.environ.get("NOTIFY_ROUTES", "") or "",
        str(Path.home() / ".openclaw" / "notify" / "routes.json"),
    ]


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
    bindir = os.path.dirname(os.path.abspath(binary))
    if bindir:
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        if bindir not in parts:
            env["PATH"] = os.pathsep.join([bindir, *parts])
    return env


def send_one(channel: str, target: str, message: str, *, dry_run: bool,
             openclaw_bin: str | None = None):
    """Send to one channel. Return ``(ok: bool, detail: str)``."""
    binary = openclaw_bin or find_openclaw()
    cmd = [binary, "message", "send",
           "--channel", channel, "--target", target, "--message", message]
    if dry_run:
        return True, "dry-run (not sent): " + " ".join(cmd[:-1] + ["<message>"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_env_with_binary_on_path(binary))
    except FileNotFoundError:
        return False, f"openclaw binary not found: {binary}"
    detail = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, detail


def notify(route: str, message: str, *, routes_path: str | None = None,
           dry_run: bool = False):
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
        ok, detail = send_one(
            ch["channel"], ch["target"], message, dry_run=dry_run
        )
        results.append((ch["channel"], ch["target"], ok, detail))
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

    try:
        results = notify(args.route, message,
                         routes_path=args.routes, dry_run=args.dry_run)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 2

    failed = [r for r in results if not r[2]]
    for channel, target, ok, detail in results:
        status = "ok" if ok else "FAIL"
        line = f"[{status}] {channel}:{target}"
        if not ok and detail:
            line += f" — {detail}"
        print(line, file=sys.stderr)

    if failed:
        print(
            f"notify: {len(failed)}/{len(results)} channel(s) FAILED "
            f"for route '{args.route}'",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
