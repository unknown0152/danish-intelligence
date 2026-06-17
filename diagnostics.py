"""Persistent install diagnostics for Cosmos market deployments."""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEBUG_FILE = Path(os.getenv("INSTALL_DEBUG_FILE", "/config/install-debug.jsonl"))
SNAPSHOT_FILE = Path(os.getenv("INSTALL_DEBUG_SNAPSHOT", "/config/install-debug-latest.json"))
MAX_EVENTS = int(os.getenv("INSTALL_DEBUG_MAX_EVENTS", "400"))
SECRET_NAME_RE = re.compile(r"(key|token|secret|password|apikey|api_key|rid)", re.I)
SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{18,}$")


def _enabled() -> bool:
    return os.getenv("INSTALL_DEBUG", "1").strip().lower() not in {"0", "false", "no", "off"}


def redact(value: Any, key_name: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key_name) for item in value]
    if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value

    text = str(value)
    if SECRET_NAME_RE.search(key_name):
        return "<set>" if text else ""
    if SECRET_VALUE_RE.match(text):
        return "<redacted>"
    return text


def record(event: str, **data: Any) -> None:
    if not _enabled():
        return
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "data": redact(data),
    }
    try:
        DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        _write_snapshot()
    except Exception as exc:
        print(f"[Diagnostics] Could not write install debug event {event}: {exc}", flush=True)


def _read_events(limit: int = MAX_EVENTS) -> list[dict[str, Any]]:
    if not DEBUG_FILE.exists():
        return []
    lines = DEBUG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _write_snapshot() -> None:
    events = _read_events()
    SNAPSHOT_FILE.write_text(
        json.dumps({"path": str(DEBUG_FILE), "events": events}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def summary(limit: int = 120) -> dict[str, Any]:
    events = _read_events(limit)
    return {
        "debug_enabled": _enabled(),
        "debug_file": str(DEBUG_FILE),
        "snapshot_file": str(SNAPSHOT_FILE),
        "event_count": len(events),
        "events": events,
    }


def safe_env(names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        raw = os.getenv(name, "")
        result[name] = {
            "set": bool(raw),
            "placeholder": raw.startswith("{") and raw.endswith("}"),
            "value": redact(raw, name) if not SECRET_NAME_RE.search(name) else ("<set>" if raw else ""),
        }
    return result


def path_state(paths: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            stat = path.stat()
            result[raw_path] = {
                "exists": True,
                "is_dir": path.is_dir(),
                "mode": oct(stat.st_mode & 0o7777),
                "uid": stat.st_uid,
                "gid": stat.st_gid,
            }
        except FileNotFoundError:
            result[raw_path] = {"exists": False}
        except Exception as exc:
            result[raw_path] = {"exists": None, "error": str(exc)}
    return result


def dns_state(urls: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, url in urls.items():
        host = urlparse(url).hostname or url
        try:
            result[name] = {"host": host, "addresses": sorted({item[4][0] for item in socket.getaddrinfo(host, None)})}
        except Exception as exc:
            result[name] = {"host": host, "error": str(exc)}
    return result
