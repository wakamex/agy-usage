#!/usr/bin/env python3
"""agy-usage - Antigravity CLI usage and quota monitor."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"
TOKEN_FILE = AGY_DIR / "antigravity-oauth-token"
SETTINGS_FILE = AGY_DIR / "settings.json"
HISTORY_FILE = AGY_DIR / "history.jsonl"
DEFAULT_USAGE_FILE = AGY_DIR / "usage-limits.json"

DAEMON_INTERVAL = 300
CACHE_MAX_AGE = 300
CODE_ASSIST_BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
AGY_USER_AGENT = "antigravity/cli/1.0.14 (aidev_client; os_type=linux; arch=amd64)"

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
    if minutes >= 24 * 60:
        days = minutes // (24 * 60)
        hours = (minutes % (24 * 60)) // 60
        return f"{days}d{hours}h"
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


def _color_remaining_pct(pct: float | int | None) -> str:
    if pct is None:
        return "?"
    value = float(pct)
    color = _GREEN if value >= 70 else _YELLOW if value >= 40 else _RED
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
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": AGY_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body)


def _read_default_project_id() -> str | None:
    try:
        project_id = (AGY_DIR / "cache" / "default_project_id.txt").read_text().strip()
    except OSError:
        return None
    return project_id or None


def _parse_quota_summary(summary: dict) -> dict:
    groups = []
    for group in summary.get("groups") or []:
        if not isinstance(group, dict):
            continue
        buckets = []
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            remaining_fraction = bucket.get("remainingFraction")
            remaining_pct = None
            if isinstance(remaining_fraction, int | float):
                remaining_pct = float(remaining_fraction) * 100
            buckets.append(
                {
                    "bucket_id": bucket.get("bucketId"),
                    "display_name": bucket.get("displayName") or bucket.get("bucketId") or "Quota",
                    "description": bucket.get("description"),
                    "window": bucket.get("window"),
                    "remaining_fraction": remaining_fraction,
                    "remaining_pct": remaining_pct,
                    "reset_time": bucket.get("resetTime"),
                    "disabled": bool(bucket.get("disabled", False)),
                }
            )
        groups.append(
            {
                "display_name": group.get("displayName") or "Quota",
                "description": group.get("description"),
                "buckets": buckets,
            }
        )
    return {"description": summary.get("description"), "groups": groups}


def _load_antigravity_code_assist(access_token: str) -> dict:
    return _code_assist_post(
        "loadCodeAssist",
        {"metadata": {"ideType": "ANTIGRAVITY"}},
        access_token,
    )


def fetch_quota_summary() -> dict:
    access_token = get_access_token()
    load_res = _load_antigravity_code_assist(access_token)
    project_id = load_res.get("cloudaicompanionProject")
    if not project_id:
        raise RuntimeError("No Antigravity Code Assist project returned by loadCodeAssist")
    summary = _code_assist_post("retrieveUserQuotaSummary", {"project": project_id}, access_token)
    parsed = _parse_quota_summary(summary)
    parsed["project_id"] = project_id
    parsed["source"] = "quota_summary_api"
    return parsed


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
        result["quota_summary"] = fetch_quota_summary()
        result["source"].append(result["quota_summary"].get("source") or "quota_summary")
    except Exception as summary_exc:
        result["quota_summary_error"] = str(summary_exc)

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

    quota_summary = data.get("quota_summary")
    if quota_summary:
        for group in quota_summary.get("groups", []):
            print()
            print(str(group.get("display_name") or "Quota").upper())
            if group.get("description"):
                print(f"  {_DIM}{group['description']}{_RESET}")

            bucket_names = [
                str(bucket.get("display_name") or "Quota")
                for bucket in group.get("buckets", [])
                if isinstance(bucket, dict)
            ]
            name_width = max(map(len, bucket_names), default=len("Quota"))
            for bucket in group.get("buckets", []):
                bucket_name = str(bucket.get("display_name") or "Quota")
                if bucket.get("disabled"):
                    print(f"  {bucket_name:{name_width}s} {_DIM}disabled{_RESET}")
                    continue
                remaining_pct = bucket.get("remaining_pct")
                if remaining_pct is None:
                    print(f"  {bucket_name:{name_width}s} ? remaining")
                    continue
                if float(remaining_pct) >= 99.995:
                    reset_part = f"{_DIM}quota available{_RESET}"
                else:
                    reset_time = _format_duration_until(bucket.get("reset_time"))
                    reset_part = f"{_DIM}resets {reset_time}{_RESET}" if reset_time else ""
                print(
                    f"  {bucket_name:{name_width}s} "
                    f"{_color_remaining_pct(remaining_pct)} remaining  {reset_part}".rstrip()
                )
    else:
        if data.get("quota_summary_error"):
            print(f"  {'Quota summary':20s} {_DIM}{data['quota_summary_error']}{_RESET}")

    history = data.get("history") or {}
    if history.get("entries"):
        latest = history.get("latest_at") or "unknown"
        print(f"History: {history['entries']} entries, latest {latest}")


def _pick_statusline_summary_bucket(quota_summary: dict) -> dict | None:
    groups = quota_summary.get("groups") or []
    gemini_groups = [
        group for group in groups if "gemini" in str(group.get("display_name") or "").lower()
    ]
    candidates = gemini_groups or groups
    buckets = [
        bucket
        for group in candidates
        for bucket in group.get("buckets", [])
        if isinstance(bucket, dict) and not bucket.get("disabled")
    ]
    for bucket in buckets:
        if str(bucket.get("window") or "").lower() == "5h":
            return bucket
    scored = [bucket for bucket in buckets if bucket.get("remaining_pct") is not None]
    if scored:
        return min(scored, key=lambda bucket: bucket["remaining_pct"])
    return buckets[0] if buckets else None


def _statusline_text(data: dict) -> str:
    parts = []
    quota_summary = data.get("quota_summary")
    if quota_summary:
        summary_bucket = _pick_statusline_summary_bucket(quota_summary)
        if summary_bucket:
            if summary_bucket.get("disabled"):
                parts.append("q:disabled")
            elif summary_bucket.get("remaining_pct") is not None:
                parts.append(f"q:{_format_pct(summary_bucket['remaining_pct'])}left")
            reset_time = ""
            if float(summary_bucket.get("remaining_pct") or 0) < 99.995:
                reset_time = _format_duration_until(summary_bucket.get("reset_time"))
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
                quota_sources = {"quota_summary_api"}
                if age < max_age and quota_sources.intersection(cached.get("source", [])):
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
