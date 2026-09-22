#!/usr/bin/env python3
"""Watch OpenClaw chat channel health and optionally recover the gateway.

This is a local operational workaround for channel routes/sessions going stale
while the OpenClaw gateway process is still alive.

Usage:
  python3 notifiers/openclaw_channel_watchdog.py --channels line
  python3 notifiers/openclaw_channel_watchdog.py --channels line whatsapp --notify
  python3 notifiers/openclaw_channel_watchdog.py --channels line whatsapp --notify --recovery-mode restart

Exit codes:
  0 = all requested checks healthy
  1 = one or more requested checks unhealthy
  2 = watchdog configuration or execution error
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = Path.home() / ".openclaw" / "workspace"
DEFAULT_STATE_FILE = WORKSPACE / "data" / "health" / "openclaw-channel-watchdog.json"
DEFAULT_CONFIG_FILE = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_NOTIFY_DM_BIN = WORKSPACE / "scripts" / "notify-dm"
#: Used only when notify_core cannot be imported to state its own budget.
#: Generous on purpose: over-waiting costs a slow watchdog tick, while
#: under-waiting costs the alert entirely (notify-shim#35).
FALLBACK_NOTIFY_TIMEOUT_SECONDS = 300


@dataclass
class CheckResult:
    channel: str
    ok: bool
    status: str
    detail: str
    suggested_next_step: str = ""


@dataclass
class HttpResult:
    status_code: int
    body: str


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_line_token(config: dict[str, Any]) -> str | None:
    env_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if env_token:
        return env_token
    channels = config.get("channels")
    if not isinstance(channels, dict):
        return None
    line_config = channels.get("line")
    if not isinstance(line_config, dict):
        return None
    token = line_config.get("channelAccessToken")
    return token if isinstance(token, str) and token.strip() else None


def default_openclaw_bin() -> str:
    env_bin = os.environ.get("OPENCLAW_BIN")
    if env_bin:
        return env_bin
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw"):
        if Path(candidate).exists():
            return candidate
    return "openclaw"


def command_env(cmd: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    path_parts: list[str] = []
    if cmd:
        command_path = Path(cmd[0])
        if command_path.is_absolute():
            path_parts.append(str(command_path.parent))
    path_parts.extend([
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/node/bin",
        "/usr/local/bin",
    ])
    path_parts.extend(env.get("PATH", "").split(os.pathsep))

    deduped: list[str] = []
    seen: set[str] = set()
    for part in path_parts:
        if part and part not in seen:
            seen.add(part)
            deduped.append(part)
    env["PATH"] = os.pathsep.join(deduped)
    return env


def http_post(url: str, headers: dict[str, str] | None = None,
              data: bytes = b"", timeout: float = 10.0) -> HttpResult:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return HttpResult(status_code=resp.status, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return HttpResult(status_code=exc.code, body=body)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return HttpResult(status_code=0, body=str(exc))


def run_command(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=True,
        text=True,
        env=command_env(cmd),
    )


def check_line_official_webhook(config: dict[str, Any], timeout: float) -> CheckResult:
    token = get_line_token(config)
    if not token:
        return CheckResult(
            channel="line",
            ok=False,
            status="missing_line_token",
            detail="LINE channel access token was not found in env or openclaw.json",
            suggested_next_step="Set LINE_CHANNEL_ACCESS_TOKEN or restore channels.line.channelAccessToken.",
        )
    result = http_post(
        "https://api.line.me/v2/bot/channel/webhook/test",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=b"{}",
        timeout=timeout,
    )
    if 200 <= result.status_code < 300:
        try:
            payload = json.loads(result.body or "{}")
        except json.JSONDecodeError as exc:
            return CheckResult(
                channel="line",
                ok=False,
                status="line_official_webhook_bad_json",
                detail=f"LINE official webhook test returned invalid JSON: {exc}",
                suggested_next_step="Inspect LINE webhook settings and retry the webhook test.",
            )
        if payload.get("success") is True:
            return CheckResult("line", True, "line_official_webhook_ok",
                               f"LINE official webhook test returned success=true")
        return CheckResult(
            channel="line",
            ok=False,
            status="line_official_webhook_failed",
            detail=f"LINE official webhook test returned {result.status_code}: {result.body[:240]}",
            suggested_next_step="Restart OpenClaw gateway, then retry LINE webhook verification.",
        )
    return CheckResult(
        channel="line",
        ok=False,
        status="line_official_webhook_failed",
        detail=f"LINE official webhook test returned {result.status_code}: {result.body[:240]}",
        suggested_next_step="Restart OpenClaw gateway, then retry LINE webhook verification.",
    )


def check_line_local_webhook(gateway_port: int, timeout: float) -> CheckResult:
    result = http_post(
        f"http://127.0.0.1:{gateway_port}/line/webhook",
        headers={"Content-Type": "application/json"},
        data=b"{}",
        timeout=timeout,
    )
    if result.status_code == 0:
        return CheckResult(
            channel="line",
            ok=False,
            status="line_local_route_unreachable",
            detail=f"Local /line/webhook could not be reached: {result.body[:240]}",
            suggested_next_step="Restart OpenClaw gateway, then retry the local LINE route check.",
        )
    if result.status_code != 404:
        return CheckResult("line", True, "line_local_route_present",
                           f"Local /line/webhook returned {result.status_code}, not 404")
    return CheckResult(
        channel="line",
        ok=False,
        status="line_local_route_missing",
        detail="Local /line/webhook returned 404 while gateway is reachable",
        suggested_next_step="Restart OpenClaw gateway to re-register the LINE route.",
    )


def check_line(config: dict[str, Any], gateway_port: int, timeout: float) -> CheckResult:
    checks = [
        check_line_official_webhook(config, timeout),
        check_line_local_webhook(gateway_port, timeout),
    ]
    failures = [check for check in checks if not check.ok]
    if not failures:
        details = "; ".join(check.detail for check in checks)
        return CheckResult("line", True, "healthy", details)
    detail = "; ".join(check.detail for check in failures)
    next_steps = " ".join(check.suggested_next_step for check in failures if check.suggested_next_step)
    return CheckResult("line", False, failures[0].status, detail, next_steps)


# WhatsApp 被伺服器登出（terminal disconnect）是一種**重啟無效**的失敗：creds 檔還在，
# 所以 linked / statusState 仍是 true / "linked"，但 session 已經死了。判別靠 probe 的
# terminalDisconnect / healthState；`lastDisconnect.loggedOut` **不可用**——實測被登出時
# 它是 false（notify-shim#32 的實際 payload）。
TERMINAL_DISCONNECT_HEALTH = "terminal-disconnect"
WHATSAPP_LOGGED_OUT_STATUS = "whatsapp_logged_out"
WHATSAPP_LOGGED_OUT_NEXT_STEP = (
    "WhatsApp session 已被登出，重啟 gateway 無效；需本人執行 "
    "`openclaw channels login --channel whatsapp` 掃 QR 重新連結。"
)
GATEWAY_RESTART_SKIPPED_STATUS = "gateway_restart_skipped_terminal_disconnect"


def whatsapp_terminal_disconnect(account: dict[str, Any], channel: dict[str, Any],
                                 health: str | None) -> tuple[bool, int | None]:
    """(是不是 terminal disconnect, lastDisconnect 的 HTTP 狀態碼)。

    `lastDisconnect` 是**歷史**欄位：重新連結後那顆 401 還會留著。所以只有在 probe 根本
    沒給 `terminalDisconnect` 布林值時（舊版 gateway）才退回用它判斷——探針明講
    `terminalDisconnect: false` 時，不得讓一顆過期的 401 蓋過現況。
    """
    last_disconnect = account.get("lastDisconnect")
    if not isinstance(last_disconnect, dict):
        last_disconnect = channel.get("lastDisconnect")
    if not isinstance(last_disconnect, dict):
        last_disconnect = {}
    status = last_disconnect.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        status = None

    flag = account.get("terminalDisconnect")
    if not isinstance(flag, bool):
        flag = channel.get("terminalDisconnect")
    if isinstance(flag, bool):
        return (flag or health == TERMINAL_DISCONNECT_HEALTH), status
    return (health == TERMINAL_DISCONNECT_HEALTH or status == 401), status


def check_whatsapp(timeout_ms: int, openclaw_bin: str) -> CheckResult:
    cmd = [
        openclaw_bin, "channels", "status",
        "--channel", "whatsapp",
        "--probe",
        "--timeout", str(timeout_ms),
        "--json",
    ]
    try:
        proc = run_command(cmd, timeout=(timeout_ms / 1000) + 5)
    except Exception as exc:
        return CheckResult(
            channel="whatsapp",
            ok=False,
            status="whatsapp_probe_error",
            detail=f"Failed to run OpenClaw WhatsApp probe: {exc}",
            suggested_next_step="Check that the openclaw CLI is installed and the gateway is reachable.",
        )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        return CheckResult(
            channel="whatsapp",
            ok=False,
            status="whatsapp_probe_failed",
            detail=f"OpenClaw WhatsApp probe exited {proc.returncode}: {detail[:240]}",
            suggested_next_step="Restart OpenClaw gateway, then run the WhatsApp probe again.",
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            channel="whatsapp",
            ok=False,
            status="whatsapp_probe_bad_json",
            detail=f"OpenClaw WhatsApp probe returned invalid JSON: {exc}",
            suggested_next_step="Run openclaw channels status --channel whatsapp --probe manually.",
        )

    channel_accounts = payload.get("channelAccounts")
    if not isinstance(channel_accounts, dict):
        channel_accounts = {}
    accounts = channel_accounts.get("whatsapp")
    if not isinstance(accounts, list) or not accounts:
        accounts = [{}]
    account = accounts[0] if isinstance(accounts[0], dict) else {}
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        channels = {}
    channel = channels.get("whatsapp")
    if not isinstance(channel, dict):
        channel = {}
    health = account.get("healthState") or channel.get("healthState")
    linked = bool(account.get("linked", channel.get("linked")))
    running = bool(account.get("running", channel.get("running")))
    connected = bool(account.get("connected", channel.get("connected")))
    configured = bool(account.get("configured", channel.get("configured")))
    if configured and linked and running and connected and health == "healthy":
        return CheckResult(
            "whatsapp", True, "healthy",
            "WhatsApp is configured, linked, running, connected, and healthy",
        )

    terminal, last_disconnect_status = whatsapp_terminal_disconnect(account, channel, health)
    flags = {
        "configured": configured,
        "linked": linked,
        "running": running,
        "connected": connected,
        "healthState": health,
        "terminalDisconnect": terminal,
        "lastDisconnectStatus": last_disconnect_status,
        "lastError": account.get("lastError") or channel.get("lastError"),
    }
    detail = json.dumps(flags, ensure_ascii=False, sort_keys=True)
    if terminal:
        # 與 whatsapp_unhealthy 分開，state 檔才判讀得出「這輪重啟不會有用」。
        return CheckResult(
            channel="whatsapp",
            ok=False,
            status=WHATSAPP_LOGGED_OUT_STATUS,
            detail=detail,
            suggested_next_step=WHATSAPP_LOGGED_OUT_NEXT_STEP,
        )
    next_step = "Restart OpenClaw gateway to recover the WhatsApp session."
    if not linked:
        next_step = "WhatsApp appears unlinked; re-link with openclaw channels login --channel whatsapp."
    return CheckResult(
        channel="whatsapp",
        ok=False,
        status="whatsapp_unhealthy",
        detail=detail,
        suggested_next_step=next_step,
    )


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def cooldown_elapsed(state: dict[str, Any], key: str, cooldown_minutes: int) -> bool:
    ts = parse_ts(state.get(key))
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now_utc() - ts).total_seconds() >= cooldown_minutes * 60


def format_alert(results: list[CheckResult], recovery_mode: str) -> str:
    lines = ["OpenClaw channel watchdog detected unhealthy channel(s):"]
    for result in results:
        if result.ok:
            continue
        lines.append(f"- {result.channel}: {result.status}")
        lines.append(f"  {result.detail}")
        if result.suggested_next_step:
            lines.append(f"  Next: {result.suggested_next_step}")
    if recovery_mode == "restart":
        lines.append("Recovery mode: restart once for this active incident, then notify-only.")
    else:
        lines.append("Recovery mode: notify-only.")
    return "\n".join(lines)


def incident_key(results: list[CheckResult]) -> str:
    unhealthy = [result for result in results if not result.ok]
    return "|".join(sorted({result.channel for result in unhealthy}))


def active_incident(state: dict[str, Any], key: str) -> dict[str, Any]:
    current = state.get("active_incident")
    if isinstance(current, dict) and current.get("key") == key:
        return current
    return {
        "key": key,
        "first_seen_at": iso_now(),
        "restart_attempted_at": None,
        "last_alert_at": None,
    }


def notify_timeout_seconds(route: str = "dm") -> int:
    """Wall-clock budget to give notify-dm, asked of notify_core itself.

    This watchdog notifies right after it restarts the gateway, which is
    exactly when ``openclaw message send`` is slowest, so a too-small timeout
    kills the alert every single time it matters. Never raises: a helper that
    cannot answer must not stop the alert from being attempted
    (notify-shim#35).
    """
    try:
        sys.path.insert(0, str(DEFAULT_NOTIFY_DM_BIN.parent))
        import notify_core  # noqa: PLC0415 - optional, resolved at call time

        return notify_core.route_send_budget(route)
    except Exception:
        return FALLBACK_NOTIFY_TIMEOUT_SECONDS


def send_notification(message: str, notify_bin: Path) -> None:
    try:
        if not notify_bin.exists():
            print(f"notify: shim not found at {notify_bin}", file=sys.stderr)
            return
        timeout = notify_timeout_seconds()
        try:
            proc = run_command([str(notify_bin), message], timeout=timeout)
        except subprocess.TimeoutExpired:
            # Say the alert was LOST, not just that a command misbehaved: the
            # operator has to know nothing reached them (notify-shim#35).
            print(f"notify: ALERT NOT DELIVERED — notify-dm exceeded "
                  f"{timeout}s and was killed; the channel-health alert did not "
                  f"reach anyone. First line was: "
                  f"{message.splitlines()[0] if message else ''}",
                  file=sys.stderr)
            return
        if proc.returncode != 0:
            detail = (proc.stdout + proc.stderr).strip()
            print(f"notify: notify-dm exited {proc.returncode}: {detail}",
                  file=sys.stderr)
    except Exception as exc:
        print(f"notify: failed to invoke notify-dm: {exc}", file=sys.stderr)


def restart_gateway(openclaw_bin: str, timeout: float = 60) -> CheckResult:
    try:
        proc = run_command([openclaw_bin, "gateway", "restart"], timeout=timeout)
    except Exception as exc:
        return CheckResult("gateway", False, "gateway_restart_error",
                           f"Failed to run gateway restart: {exc}")
    if proc.returncode == 0:
        return CheckResult("gateway", True, "gateway_restart_ok",
                           "openclaw gateway restart completed")
    detail = (proc.stdout + proc.stderr).strip()
    return CheckResult("gateway", False, "gateway_restart_failed",
                       f"openclaw gateway restart exited {proc.returncode}: {detail[:240]}")


def maybe_notify(unhealthy: list[CheckResult], recovery_mode: str, notify_bin: Path,
                 incident: dict[str, Any], cooldown_minutes: int) -> bool:
    if not cooldown_elapsed(incident, "last_alert_at", cooldown_minutes):
        return False
    send_notification(format_alert(unhealthy, recovery_mode), notify_bin)
    incident["last_alert_at"] = iso_now()
    return True


def evaluate_channels(channels: list[str], config: dict[str, Any],
                      gateway_port: int, timeout_ms: int,
                      openclaw_bin: str) -> list[CheckResult]:
    requested = ["line", "whatsapp"] if "all" in channels else channels
    results: list[CheckResult] = []
    for channel in requested:
        if channel == "line":
            results.append(check_line(config, gateway_port, timeout_ms / 1000))
        elif channel == "whatsapp":
            results.append(check_whatsapp(timeout_ms, openclaw_bin))
        else:
            results.append(CheckResult(channel, False, "unsupported_channel",
                                       f"Unsupported channel: {channel}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch OpenClaw LINE/WhatsApp health and optionally recover the gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0=healthy, 1=unhealthy, 2=watchdog error",
    )
    parser.add_argument("--channels", nargs="+", choices=["line", "whatsapp", "all"],
                        default=["line"], help="Channels to check (default: line)")
    parser.add_argument("--notify", action="store_true",
                        help="Send notify-dm alert on unhealthy result, subject to cooldown")
    parser.add_argument("--recovery-mode", choices=["notify", "restart"], default="notify",
                        help="notify=alert only; restart=restart gateway once per active incident")
    parser.add_argument("--cooldown-minutes", type=int, default=30,
                        help="Minimum minutes between alerts")
    parser.add_argument("--post-restart-delay-sec", type=float, default=5.0,
                        help="Seconds to wait before re-checking after gateway restart")
    parser.add_argument("--gateway-port", type=int,
                        default=int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789")))
    parser.add_argument("--timeout-ms", type=int, default=10000)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--config-file", type=Path,
                        default=Path(os.environ.get("OPENCLAW_CONFIG_PATH", DEFAULT_CONFIG_FILE)))
    parser.add_argument("--notify-bin", type=Path,
                        default=Path(os.environ.get("NOTIFY_DM_BIN", DEFAULT_NOTIFY_DM_BIN)))
    parser.add_argument("--openclaw-bin", default=default_openclaw_bin(),
                        help="Path to openclaw CLI; use an absolute path for LaunchAgent runs")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args()

    try:
        state = load_json(args.state_file)
        config = load_json(args.config_file)
        results = evaluate_channels(
            args.channels, config, args.gateway_port, args.timeout_ms,
            args.openclaw_bin,
        )
        unhealthy = [result for result in results if not result.ok]

        recovery_result: CheckResult | None = None
        incident: dict[str, Any] | None = None
        notified = False

        if unhealthy:
            key = incident_key(unhealthy)
            incident = active_incident(state, key)
            # 重啟對 terminal disconnect 必定無效，而且會連帶把 LINE/Telegram 彈掉。
            # 但只有在**本輪全部的不健康項目都是**這種狀況時才跳過——若同時還有別的
            # 頻道壞掉，重啟對那一個仍可能有用，不該連它的自動恢復一起取消。
            restart_is_futile = all(
                result.status == WHATSAPP_LOGGED_OUT_STATUS for result in unhealthy
            )
            if args.recovery_mode == "restart" and restart_is_futile:
                recovery_result = CheckResult(
                    "gateway", True, GATEWAY_RESTART_SKIPPED_STATUS,
                    "WhatsApp session was logged out by the server; a gateway restart cannot "
                    "recover it and would drop LINE/Telegram too. Notifying only.",
                )
                if args.notify:
                    notified = maybe_notify(
                        unhealthy, "notify", args.notify_bin,
                        incident, args.cooldown_minutes,
                    )
            elif args.recovery_mode == "restart":
                if not incident.get("restart_attempted_at"):
                    recovery_result = restart_gateway(args.openclaw_bin)
                    incident["restart_attempted_at"] = iso_now()
                    if recovery_result.ok and args.post_restart_delay_sec > 0:
                        time.sleep(args.post_restart_delay_sec)
                    results = evaluate_channels(
                        args.channels, config, args.gateway_port, args.timeout_ms,
                        args.openclaw_bin,
                    )
                    unhealthy = [result for result in results if not result.ok]
                    incident["post_restart_results"] = [asdict(result) for result in results]
                    if unhealthy and args.notify:
                        notified = maybe_notify(
                            unhealthy, args.recovery_mode, args.notify_bin,
                            incident, args.cooldown_minutes,
                        )
                else:
                    recovery_result = CheckResult(
                        "gateway", True, "gateway_restart_already_attempted",
                        "Restart already attempted for this active incident; notifying only",
                    )
                    if args.notify:
                        notified = maybe_notify(
                            unhealthy, args.recovery_mode, args.notify_bin,
                            incident, args.cooldown_minutes,
                        )
            elif args.notify:
                notified = maybe_notify(
                    unhealthy, args.recovery_mode, args.notify_bin,
                    incident, args.cooldown_minutes,
                )

        payload: dict[str, Any] = {
            "checked_at": iso_now(),
            "ok": not unhealthy,
            "results": [asdict(result) for result in results],
            "notified": notified,
        }
        if recovery_result:
            payload["recovery"] = asdict(recovery_result)
        else:
            state.pop("recovery", None)
        if unhealthy and incident:
            state["active_incident"] = incident
        else:
            state.pop("active_incident", None)
        state.update(payload)
        save_json(args.state_file, state)

        if args.json or unhealthy:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not unhealthy else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
