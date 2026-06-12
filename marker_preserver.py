"""Preserve Danish Intelligence markers after Arr imports.

Radarr and Sonarr score the grabbed release title first, but AltMount imports may
expose an inner MKV filename that does not contain the proxy marker. This module
copies the release-level marker into the imported symlink filename so
`[{Custom Formats}]` remains stable after rescans and renames.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

DANISH_MARKERS = (
    (
        "Danish Audio",
        re.compile(r"(?i)(?:\[Danish Audio\]|(?:^|[._\-\s])(?:DanishAudio|DKaudio)\b)"),
    ),
    (
        "Danish Subtitles",
        re.compile(r"(?i)(?:\[Danish Subtitles\]|(?:^|[._\-\s])(?:DanishSubs|DKOK)\b)"),
    ),
)

ARR_CONFIG_PATHS = {
    "radarr": ("/arr-config/radarr/config.xml", "/srv/config/radarr/config.xml"),
    "sonarr": ("/arr-config/sonarr/config.xml", "/srv/config/sonarr/config.xml"),
}


def _clean_env(name: str) -> str:
    value = os.getenv(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _read_arr_key(source: str) -> str:
    app_name = source.upper()
    for env_name in (f"{app_name}_API_KEY", f"{app_name}_APIKEY"):
        value = _clean_env(env_name)
        if value:
            return value

    for path in ARR_CONFIG_PATHS[source]:
        try:
            root = ET.parse(path).getroot()
            return (root.findtext("ApiKey") or "").strip()
        except Exception:
            continue
    return ""


def _arr_url(source: str) -> str:
    if source == "sonarr":
        return os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/")
    return os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Content-Type": "application/json"}


async def _get_json(session: aiohttp.ClientSession, source: str, path: str, api_key: str) -> Any:
    async with session.get(f"{_arr_url(source)}/api/v3/{path.lstrip('/')}", headers=_headers(api_key), timeout=20) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _post_json(session: aiohttp.ClientSession, source: str, path: str, api_key: str, payload: dict[str, Any]) -> None:
    async with session.post(f"{_arr_url(source)}/api/v3/{path.lstrip('/')}", headers=_headers(api_key), json=payload, timeout=20) as resp:
        resp.raise_for_status()


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _markers_from_text(*values: str) -> list[str]:
    text = "\n".join(value for value in values if value)
    return [name for name, pattern in DANISH_MARKERS if pattern.search(text)]


def _payload_markers(payload: dict[str, Any]) -> list[str]:
    return _markers_from_text(*_iter_strings(payload))


def _insert_marker(filename: str, markers: list[str]) -> str:
    path = Path(filename)
    stem = path.stem
    suffix = path.suffix
    if all(marker in stem for marker in markers):
        return filename

    replacement = ", ".join(markers)
    if re.search(r"\[(?:Danish Audio|Danish Subtitles)?\]$", stem):
        stem = re.sub(r"\[[^\]]*\]$", f"[{replacement}]", stem)
    else:
        stem = f"{stem} [{replacement}]"
    return f"{stem}{suffix}"


def _rename_imported_file(path: str, markers: list[str]) -> str:
    if not path or not path.startswith("/media/"):
        return ""

    current = Path(path)
    if not os.path.lexists(current):
        return ""

    target = current.with_name(_insert_marker(current.name, markers))
    if target == current:
        return str(current)
    if os.path.lexists(target):
        return str(target)

    current.rename(target)
    return str(target)


async def _history_marker_candidates(session: aiohttp.ClientSession, source: str, item_id: int | None, api_key: str) -> list[str]:
    if not item_id:
        return []

    path = f"history/movie?movieId={item_id}" if source == "radarr" else f"history/series?seriesId={item_id}"
    history = await _get_json(session, source, path, api_key)
    records = history.get("records", []) if isinstance(history, dict) else history

    candidates: list[str] = []
    for record in records[:20]:
        candidates.extend(_iter_strings(record.get("sourceTitle", "")))
        candidates.extend(_iter_strings(record.get("data", {})))
    return candidates


async def _rescan(session: aiohttp.ClientSession, source: str, item_id: int | None, api_key: str) -> None:
    if not item_id:
        return
    if source == "sonarr":
        await _post_json(session, "sonarr", "command", api_key, {"name": "RescanSeries", "seriesId": item_id})
    else:
        await _post_json(session, "radarr", "command", api_key, {"name": "RescanMovie", "movieId": item_id})


async def _preserve_radarr(session: aiohttp.ClientSession, payload: dict[str, Any], api_key: str) -> str:
    movie = payload.get("movie") or payload.get("remoteMovie", {}).get("movie") or {}
    movie_file = payload.get("movieFile") or {}
    movie_id = movie.get("id") or payload.get("movieId")
    movie_file_id = movie_file.get("id") or payload.get("movieFileId")

    markers = _payload_markers(payload)
    if not markers:
        markers = _markers_from_text(*await _history_marker_candidates(session, "radarr", movie_id, api_key))
    if not markers:
        return "no marker"

    if movie_file_id:
        movie_file = await _get_json(session, "radarr", f"moviefile/{movie_file_id}", api_key)

    path = movie_file.get("path") or movie_file.get("relativePath") or ""
    if path and not path.startswith("/"):
        movie_path = movie.get("path") or ""
        path = f"{movie_path.rstrip('/')}/{path.lstrip('/')}" if movie_path else path

    new_path = _rename_imported_file(path, markers)
    if not new_path:
        return "no file"

    await _rescan(session, "radarr", movie_id, api_key)
    return f"preserved {', '.join(markers)}"


async def _preserve_sonarr(session: aiohttp.ClientSession, payload: dict[str, Any], api_key: str) -> str:
    series = payload.get("series") or {}
    series_id = series.get("id") or payload.get("seriesId")
    files = list(payload.get("episodeFiles") or [])
    if payload.get("episodeFile"):
        files.append(payload["episodeFile"])

    markers = _payload_markers(payload)
    if not markers:
        markers = _markers_from_text(*await _history_marker_candidates(session, "sonarr", series_id, api_key))
    if not markers:
        return "no marker"

    preserved = 0
    for episode_file in files:
        episode_file_id = episode_file.get("id")
        if episode_file_id:
            episode_file = await _get_json(session, "sonarr", f"episodefile/{episode_file_id}", api_key)

        path = episode_file.get("path") or episode_file.get("relativePath") or ""
        if path and not path.startswith("/"):
            series_path = series.get("path") or ""
            path = f"{series_path.rstrip('/')}/{path.lstrip('/')}" if series_path else path

        if _rename_imported_file(path, markers):
            preserved += 1

    if not preserved:
        return "no file"

    await _rescan(session, "sonarr", series_id, api_key)
    return f"preserved {', '.join(markers)} on {preserved} file(s)"


async def _preserve_after_delay(source: str, payload: dict[str, Any]) -> None:
    await asyncio.sleep(8)
    api_key = _read_arr_key(source)
    if not api_key:
        print(f"[MarkerPreserver] {source}: missing API key", flush=True)
        return

    try:
        async with aiohttp.ClientSession() as session:
            if source == "sonarr":
                result = await _preserve_sonarr(session, payload, api_key)
            else:
                result = await _preserve_radarr(session, payload, api_key)
        print(f"[MarkerPreserver] {source}: {result}", flush=True)
    except Exception as exc:
        print(f"[MarkerPreserver] {source}: failed: {exc}", flush=True)


async def handle_arr_webhook(request: web.Request, source: str) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}

    event_type = str(payload.get("eventType") or payload.get("event") or "")
    if any(token in event_type.lower() for token in ("download", "upgrade", "rename", "import")):
        asyncio.create_task(_preserve_after_delay(source, payload))

    return web.Response(status=202, text="accepted\n")
