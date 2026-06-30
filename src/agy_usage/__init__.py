#!/usr/bin/env python3
"""agy-usage - Antigravity CLI usage and quota monitor."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"
TOKEN_FILE = AGY_DIR / "antigravity-oauth-token"
SETTINGS_FILE = AGY_DIR / "settings.json"
HISTORY_FILE = AGY_DIR / "history.jsonl"
DEFAULT_USAGE_FILE = AGY_DIR / "usage-limits.json"

DAEMON_INTERVAL = 300
CACHE_MAX_AGE = 300
CODE_ASSIST_BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"

_TTY = sys.stdout.isatty()
_RED = "\033[0;31m" if _TTY else ""
_YELLOW = "\033[0;33m" if _TTY else ""
_GREEN = "\033[0;32m" if _TTY else ""
_DIM = "\033[0;90m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _format_duration_until(iso_timestamp: str | None) -> str:
    reset = _parse_iso(iso_timestamp)
    if not reset:
        return ""
    seconds = int((reset - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return ""
    minutes = seconds // 60
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60}m"
    return f"{minutes}m"


def _format_pct(pct: float | int | None) -> str:
    if pct is None:
        return "?"
    value = float(pct)
    if value >= 1:
        return f"{value:.1f}%"
    return f"{value:.2f}%"


def _color_pct(pct: float | int | None) -> str:
    if pct is None:
        return "?"
    value = float(pct)
    color = _RED if value >= 70 else _YELLOW if value >= 40 else _GREEN
    return f"{color}{_format_pct(value)}{_RESET}"


def get_usage_file() -> Path:
    override = os.environ.get("AGY_USAGE_FILE") or os.environ.get("ANTIGRAVITY_USAGE_FILE")
    return Path(override).expanduser() if override else DEFAULT_USAGE_FILE


def get_settings() -> dict:
    data = _read_json(SETTINGS_FILE)
    return data if isinstance(data, dict) else {}


def get_auth() -> dict | None:
    data = _read_json(TOKEN_FILE)
    return data if isinstance(data, dict) else None


def get_access_token() -> str:
    env_token = os.environ.get("AGY_ACCESS_TOKEN") or os.environ.get("ANTIGRAVITY_ACCESS_TOKEN")
    if env_token:
        return env_token

    auth = get_auth()
    if not auth:
        raise RuntimeError("No Antigravity OAuth token at ~/.gemini/antigravity-cli/antigravity-oauth-token")

    token_payload = auth.get("token")
    token = auth.get("access_token") or auth.get("AccessToken")
    if isinstance(token_payload, dict):
        token = token or token_payload.get("access_token") or token_payload.get("AccessToken")
    elif isinstance(token_payload, str):
        token = token or token_payload
    if not isinstance(token, str) or not token:
        raise RuntimeError("No access token in ~/.gemini/antigravity-cli/antigravity-oauth-token")
    return token


def _code_assist_post(method: str, payload: dict, access_token: str) -> dict:
    base_url = os.environ.get("AGY_CODE_ASSIST_BASE_URL", CODE_ASSIST_BASE_URL).rstrip("/")
    req = urllib.request.Request(
        f"{base_url}:{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agy-usage/0.1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _load_code_assist(access_token: str) -> dict:
    project_id = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        or _read_default_project_id()
    )
    metadata: dict[str, Any] = {
        "ideType": "IDE_UNSPECIFIED",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }
    if project_id:
        metadata["duetProject"] = project_id
    return _code_assist_post(
        "loadCodeAssist",
        {
            "cloudaicompanionProject": project_id,
            "metadata": metadata,
        },
        access_token,
    )


def _read_default_project_id() -> str | None:
    try:
        project_id = (AGY_DIR / "cache" / "default_project_id.txt").read_text().strip()
    except OSError:
        return None
    return project_id or None


def _parse_quota_buckets(buckets: list[dict]) -> list[dict]:
    parsed = []
    for bucket in buckets:
        remaining = None
        limit = None
        used_pct = None
        remaining_fraction = bucket.get("remainingFraction")

        try:
            if bucket.get("remainingAmount") is not None:
                remaining = int(bucket["remainingAmount"])
        except (TypeError, ValueError):
            remaining = None

        if isinstance(remaining_fraction, int | float):
            used_pct = (1 - float(remaining_fraction)) * 100
        if remaining is not None and isinstance(remaining_fraction, int | float):
            if remaining_fraction > 0:
                limit = round(remaining / float(remaining_fraction))

        parsed.append(
            {
                "model": bucket.get("modelId") or bucket.get("model"),
                "remaining": remaining,
                "limit": limit,
                "used_pct": used_pct,
                "remaining_fraction": remaining_fraction,
                "reset_time": bucket.get("resetTime"),
                "token_type": bucket.get("tokenType"),
                "disabled": bool(bucket.get("disabled", False)),
            }
        )
    return parsed


def _select_summary_bucket(quota: dict) -> dict | None:
    buckets = quota.get("buckets") or []
    if not isinstance(buckets, list):
        return None
    active = [bucket for bucket in buckets if not bucket.get("disabled")]
    scored = [bucket for bucket in active if bucket.get("used_pct") is not None]
    if scored:
        return max(scored, key=lambda bucket: bucket["used_pct"])
    return active[0] if active else (buckets[0] if buckets else None)


def fetch_quota() -> dict:
    access_token = get_access_token()
    load_res = _load_code_assist(access_token)

    project_id = (
        load_res.get("cloudaicompanionProject")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        or _read_default_project_id()
    )
    if not project_id:
        raise RuntimeError("No Code Assist project ID available. Set GOOGLE_CLOUD_PROJECT if needed.")

    quota_res = _code_assist_post("retrieveUserQuota", {"project": project_id}, access_token)
    current_tier = load_res.get("currentTier") or {}
    paid_tier = load_res.get("paidTier") or {}
    result = {
        "project_id": project_id,
        "user_tier": paid_tier.get("id") or current_tier.get("id"),
        "user_tier_name": paid_tier.get("name") or current_tier.get("name"),
        "buckets": _parse_quota_buckets(quota_res.get("buckets") or []),
    }
    result["summary_bucket"] = _select_summary_bucket(result)
    credits = quota_res.get("credits") or quota_res.get("g1Credits")
    if credits is not None:
        result["credits"] = credits
    return result


def read_history_summary(path: Path = HISTORY_FILE) -> dict:
    commands: Counter[str] = Counter()
    workspaces: Counter[str] = Counter()
    total = 0
    latest_ms: int | None = None

    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []

    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        total += 1
        display = item.get("display")
        workspace = item.get("workspace")
        timestamp = item.get("timestamp")
        if isinstance(display, str):
            commands[display] += 1
        if isinstance(workspace, str):
            workspaces[workspace] += 1
        if isinstance(timestamp, int):
            latest_ms = timestamp if latest_ms is None else max(latest_ms, timestamp)

    latest_at = (
        datetime.fromtimestamp(latest_ms / 1000, tz=UTC).isoformat()
        if latest_ms is not None
        else None
    )
    return {
        "entries": total,
        "latest_at": latest_at,
        "top_commands": dict(commands.most_common(10)),
        "top_workspaces": dict(workspaces.most_common(10)),
    }


def build_usage_json(project_root: Path | None = None) -> dict:
    root = (project_root or Path.cwd()).resolve()
    settings = get_settings()
    result = {
        "project_root": str(root),
        "model": settings.get("model"),
        "source": [],
        "updated_at": _iso_now(),
        "history": read_history_summary(),
    }
    if result["history"]["entries"]:
        result["source"].append("history")

    try:
        result["account_quota"] = fetch_quota()
        result["source"].append("quota_api")
    except Exception as exc:
        result["quota_error"] = str(exc)

    return result


def write_usage_file(data: dict):
    usage_file = get_usage_file()
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(data, indent=2) + "\n")


def _print_status(data: dict):
    print(f"Project: {Path(data['project_root']).name}")
    model = data.get("model")
    if model:
        print(f"Model: {model}")

    quota = data.get("account_quota")
    if quota:
        bucket_names = [
            (bucket.get("model") or "unknown")
            for bucket in quota.get("buckets", [])
            if isinstance(bucket, dict)
        ]
        name_width = max(map(len, bucket_names), default=len("Quota"))
        for bucket in quota.get("buckets", []):
            model_name = bucket.get("model") or "unknown"
            if bucket.get("disabled"):
                print(f"  {model_name:{name_width}s} {_DIM}disabled{_RESET}")
                continue
            reset_time = _format_duration_until(bucket.get("reset_time"))
            reset_part = f"  resets {reset_time}" if reset_time else ""
            remaining = bucket.get("remaining")
            limit = bucket.get("limit")
            remain_part = (
                f"  {remaining} / {limit} remaining"
                if remaining is not None and limit is not None
                else ""
            )
            print(
                f"  {model_name:{name_width}s} {_color_pct(bucket.get('used_pct'))} used"
                f"{remain_part}{_DIM}{reset_part}{_RESET}"
            )
    elif data.get("quota_error"):
        print(f"  {'Quota':20s} {_DIM}{data['quota_error']}{_RESET}")

    history = data.get("history") or {}
    if history.get("entries"):
        latest = history.get("latest_at") or "unknown"
        print(f"History: {history['entries']} entries, latest {latest}")


def _statusline_text(data: dict) -> str:
    parts = []
    quota = data.get("account_quota")
    if quota and quota.get("summary_bucket"):
        summary = quota["summary_bucket"]
        if summary.get("disabled"):
            parts.append("q:disabled")
        elif summary.get("used_pct") is not None:
            parts.append(f"q:{_format_pct(summary['used_pct'])}")
        reset_time = _format_duration_until(summary.get("reset_time"))
        if reset_time:
            parts.append(f"reset:{reset_time}")
    elif data.get("quota_error"):
        parts.append("q:err")

    model = data.get("model")
    if model:
        parts.append(f"model:{model.replace(' ', '_')}")
    return " ".join(parts)


def _get_cached_usage(
    project_root: Path | None = None,
    max_age: int = CACHE_MAX_AGE,
    force_refresh: bool = False,
) -> dict:
    usage_file = get_usage_file()
    root = str((project_root or Path.cwd()).resolve())
    if not force_refresh:
        try:
            cached = json.loads(usage_file.read_text())
            updated = _parse_iso(cached.get("updated_at"))
            if updated and cached.get("project_root") == root:
                age = (datetime.now(UTC) - updated).total_seconds()
                if age < max_age and "quota_api" in cached.get("source", []):
                    return cached
        except Exception:
            pass

    try:
        fresh = build_usage_json(project_root)
        write_usage_file(fresh)
        return fresh
    except Exception:
        try:
            return json.loads(usage_file.read_text())
        except Exception:
            return build_usage_json(project_root)


def cmd_status(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    _print_status(data)


def cmd_json(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    print(json.dumps(data, indent=2))


def cmd_daemon(args):
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    root = Path(args.root).resolve() if args.root else None
    usage_file = get_usage_file()

    print(f"agy-usage daemon started (refreshing every {args.interval}s)")
    print(f"Writing to {usage_file}")

    while True:
        try:
            data = build_usage_json(project_root=root)
            write_usage_file(data)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {_statusline_text(data)}")
        except Exception as exc:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {exc}", file=sys.stderr)
        time.sleep(args.interval)


def cmd_statusline(args):
    data = _get_cached_usage(
        project_root=Path(args.root).resolve() if args.root else None,
        max_age=args.max_age,
        force_refresh=args.refresh,
    )
    print(_statusline_text(data))


def cmd_refresh(args):
    data = build_usage_json(project_root=Path(args.root).resolve() if args.root else None)
    write_usage_file(data)
    _print_status(data)


def cmd_install(_args):
    print(
        "Install with:\n"
        "  uv tool install agy-usage\n\n"
        "For local development:\n"
        "  uv tool install .\n\n"
        "Then run:\n"
        "  agy-usage\n"
        "  agy-usage statusline\n"
        "  agy-usage refresh\n"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity CLI usage and quota monitor")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "json", "daemon", "statusline", "refresh", "install"],
    )
    parser.add_argument("--root", help="Project root to inspect (default: current working directory)")
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=DAEMON_INTERVAL,
        help="Daemon refresh interval in seconds",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=CACHE_MAX_AGE,
        help="Maximum cache age in seconds for statusline",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and force a fresh fetch where applicable",
    )
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()
    commands = {
        "status": cmd_status,
        "json": cmd_json,
        "daemon": cmd_daemon,
        "statusline": cmd_statusline,
        "refresh": cmd_refresh,
        "install": cmd_install,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
