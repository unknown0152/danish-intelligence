"""Cosmos-safe Arr auto-configuration for Danish Intelligence.

This module intentionally uses only HTTP APIs reachable from the app container.
The old shell installer needs Docker CLI/socket access, which Cosmos market
containers should not require.
"""

from __future__ import annotations

import copy
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


MANAGED_CF_NAMES = {
    "DK",
    "DKSubs",
    "DKAudio",
    "NORDIC.ENG",
    "TrueHD Atmos",
    "DTS-X",
    "TrueHD",
    "DTS-HD MA",
    "EAC3 Atmos",
    "EAC3",
    "DTS",
    "AAC",
}
MANAGED_PROFILE_NAMES = {"NORDIC", "DanishAudio", "EnglishSubs"}
DEFAULT_APP_URLS = {"Radarr": "http://radarr:7878", "Sonarr": "http://sonarr:8989"}
PROXY_URL = os.getenv("PROXY_URL", "http://danish-intelligence:9699").rstrip("/")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


def _arr_visible_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "danish-intelligence"
    if host in LOOPBACK_HOSTS:
        host = "danish-intelligence"
    port = parsed.port
    netloc = f"{host}:{port}" if port else host
    return urlunparse((scheme, netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")


ARR_PROXY_URL = os.getenv("ARR_PROXY_URL", "").rstrip("/") or _arr_visible_proxy_url(PROXY_URL)
_PROXY_PARSED = urlparse(ARR_PROXY_URL)
PROXY_HOST = _PROXY_PARSED.hostname or "danish-intelligence"
PROXY_PORT = _PROXY_PARSED.port or (443 if _PROXY_PARSED.scheme == "https" else 80)
PROXY_USE_SSL = _PROXY_PARSED.scheme == "https"
LEGACY_PROXY_HOSTS = {"dksubs-proxy", "danish-intelligence", PROXY_HOST}
ALTMOUNT_SOURCE_HOSTS = {"altmount", "altmount-danish-edition", "nzbdav"}
ARR_CONFIG_PATHS = {
    "Prowlarr": ("/arr-config/prowlarr/config.xml", "/srv/config/prowlarr/config.xml"),
    "Radarr": ("/arr-config/radarr/config.xml", "/srv/config/radarr/config.xml"),
    "Sonarr": ("/arr-config/sonarr/config.xml", "/srv/config/sonarr/config.xml"),
}


@dataclass
class ArrApp:
    name: str
    url: str
    api_key: str


def _field(obj: dict[str, Any], name: str, default: Any = "") -> Any:
    for field in obj.get("fields", []):
        if field.get("name") == name:
            return field.get("value", default)
    return default


def _set_field(obj: dict[str, Any], name: str, value: Any) -> None:
    for field in obj.get("fields", []):
        if field.get("name") == name:
            field["value"] = value
            return


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Content-Type": "application/json"}


def _clean_env(name: str) -> str:
    value = os.getenv(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _get_json(session: requests.Session, url: str, api_key: str) -> Any:
    resp = session.get(url, headers=_headers(api_key), timeout=20)
    resp.raise_for_status()
    return resp.json()


def _put_json(session: requests.Session, url: str, api_key: str, payload: Any) -> Any:
    resp = session.put(url, headers=_headers(api_key), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def _post_json(session: requests.Session, url: str, api_key: str, payload: Any) -> Any:
    resp = session.post(url, headers=_headers(api_key), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def _delete(session: requests.Session, url: str, api_key: str) -> None:
    resp = session.delete(url, headers=_headers(api_key), timeout=20)
    if resp.status_code not in (200, 202, 204, 404):
        resp.raise_for_status()


def _clean_name(name: str) -> str:
    name = re.sub(r"(\s*\{DK\})+$", "", name)
    name = re.sub(r"\s*\(Prowlarr\)$", "", name)
    name = re.sub(r"\s*\[[^\]]+\]$", "", name)
    return name.strip().lower()


def _display_name(name: str) -> str:
    name = re.sub(r"(\s*\{DK\})+$", "", name)
    name = re.sub(r"\s*\(Prowlarr\)$", "", name)
    return name.strip()


def _is_altmount_proxy_client(client: dict[str, Any]) -> bool:
    implementation = str(client.get("implementation", "")).lower()
    if implementation != "sabnzbd":
        return False

    name = str(client.get("name", "")).lower()
    host = str(_field(client, "host", "")).lower()
    url_base = str(_field(client, "urlBase", "")).strip("/").lower()
    port = _field(client, "port", 0)

    return (
        "altmount" in name
        or "nzbdav" in name
        or host in LEGACY_PROXY_HOSTS
        or host in ALTMOUNT_SOURCE_HOSTS
        or url_base == "altmount"
        or (url_base == "sabnzbd" and (host in ALTMOUNT_SOURCE_HOSTS or "nzbdav" in name))
        or str(port) == str(PROXY_PORT)
    )


def _discover_arr_apps(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> list[ArrApp]:
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    discovered: list[ArrApp] = []
    for app in apps:
        name = app.get("name", "")
        if name not in DEFAULT_APP_URLS:
            continue
        url = str(_field(app, "baseUrl") or DEFAULT_APP_URLS[name]).rstrip("/")
        prowlarr_app_key = str(_field(app, "apiKey") or "")
        api_key = _first_working_arr_key(session, name, url, prowlarr_app_key)
        if not api_key and url != DEFAULT_APP_URLS[name]:
            url = DEFAULT_APP_URLS[name]
            api_key = _first_working_arr_key(session, name, url, prowlarr_app_key)
        if not api_key:
            print(f"[Core] Auto-Config: {name} API key unavailable or unauthorized", flush=True)
            continue
        discovered.append(ArrApp(name=name, url=url.rstrip("/"), api_key=api_key))
    return discovered


def _first_working_arr_key(session: requests.Session, app_name: str, url: str, prowlarr_app_key: str = "") -> str:
    env_names = [f"{app_name.upper()}_API_KEY", f"{app_name.upper()}_APIKEY"]
    candidates = [_clean_env(name) for name in env_names]
    candidates.append(_read_arr_config_key(app_name))
    # Prowlarr often stores stale 8-char app keys; keep it last as a compatibility fallback.
    if prowlarr_app_key:
        candidates.append(prowlarr_app_key)

    seen: set[str] = set()
    for key in candidates:
        if not key or key in seen:
            continue
        seen.add(key)
        if _arr_reachable(session, url, key):
            return key
    return ""


def _read_arr_config_key(app_name: str) -> str:
    key = app_name[:1].upper() + app_name[1:].lower()
    for path in ARR_CONFIG_PATHS.get(key, ()):
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            root = ET.parse(cfg).getroot()
            key = root.findtext("ApiKey", default="").strip()
            if key:
                return key
        except Exception as exc:
            print(f"[Core] Auto-Config: could not read {path}: {exc}", flush=True)
    return ""


def _prowlarr_api_key() -> str:
    return (
        _clean_env("PROWLARR_API_KEY")
        or _clean_env("PROWLARR_APIKEY")
        or _read_arr_config_key("Prowlarr")
    )


def _arr_reachable(session: requests.Session, url: str, api_key: str) -> bool:
    try:
        resp = session.get(f"{url.rstrip('/')}/api/v3/system/status", headers=_headers(api_key), timeout=8)
        return resp.ok
    except requests.RequestException:
        return False


def _cf_payload(name: str, pattern: str, include_rename: bool = False, required: bool = True) -> dict[str, Any]:
    return {
        "id": 0,
        "name": name,
        "includeCustomFormatWhenRenaming": include_rename,
        "specifications": [
            {
                "name": f"{name} Tag",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": required,
                "fields": [{"name": "value", "value": pattern}],
            }
        ],
    }


def _managed_cf_payloads() -> list[dict[str, Any]]:
    return [
        _cf_payload("DKAudio", r"(?:\[DKAudio[-:.][^\]]+\]|\.DKaudio\b)", include_rename=True),
        _cf_payload("DKSubs", r"(?:\[DK[-:.][^\]]+\]|\.DKOK\b)", include_rename=True),
        {
            "id": 0,
            "name": "TrueHD Atmos",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "TrueHD", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bTrueHD\b"}]},
                {"name": "Atmos", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bAtmos\b"}]},
            ],
        },
        _cf_payload("DTS-X", r"DTS[-. ]?X\b|DTS:X"),
        {
            "id": 0,
            "name": "TrueHD",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "TrueHD", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bTrueHD\b"}]},
                {"name": "NOT Atmos", "implementation": "ReleaseTitleSpecification", "negate": True, "required": True, "fields": [{"name": "value", "value": r"\bAtmos\b"}]},
            ],
        },
        _cf_payload("DTS-HD MA", r"DTS[-. ]HD[-. ]?MA|DTS\.HD\.MA|DTSMA\b"),
        {
            "id": 0,
            "name": "EAC3 Atmos",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "EAC3", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"EAC3|DD\+|E-AC-3"}]},
                {"name": "Atmos", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bAtmos\b"}]},
            ],
        },
        {
            "id": 0,
            "name": "EAC3",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "EAC3", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"EAC3|DD\+|E-AC-3"}]},
                {"name": "NOT Atmos", "implementation": "ReleaseTitleSpecification", "negate": True, "required": True, "fields": [{"name": "value", "value": r"\bAtmos\b"}]},
            ],
        },
        {
            "id": 0,
            "name": "DTS",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "DTS", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bDTS\b"}]},
                {"name": "NOT DTS-HD/X", "implementation": "ReleaseTitleSpecification", "negate": True, "required": True, "fields": [{"name": "value", "value": r"DTS[-. ]HD|DTS[-. ]?X\b|DTS:X"}]},
            ],
        },
        _cf_payload("AAC", r"\bAAC\b"),
    ]


def _paint_formats_and_profiles(session: requests.Session, app: ArrApp) -> tuple[int, int]:
    api = f"{app.url}/api/v3"
    old_formats = _get_json(session, f"{api}/customformat", app.api_key)
    old_ids = [fmt["id"] for fmt in old_formats if fmt.get("name") in MANAGED_CF_NAMES]
    for fmt_id in old_ids:
        _delete(session, f"{api}/customformat/{fmt_id}", app.api_key)

    cf_ids: dict[str, int] = {}
    for payload in _managed_cf_payloads():
        created = _post_json(session, f"{api}/customformat", app.api_key, payload)
        cf_ids[payload["name"]] = int(created["id"])
    valid_cf_ids = {
        int(fmt["id"])
        for fmt in _get_json(session, f"{api}/customformat", app.api_key)
        if fmt.get("id") is not None
    }

    profiles = _get_json(session, f"{api}/qualityprofile", app.api_key)
    scores = {
        "DKAudio": 10000,
        "DKSubs": 10000,
        "TrueHD Atmos": 2000,
        "DTS-X": 1800,
        "TrueHD": 1600,
        "DTS-HD MA": 1400,
        "EAC3 Atmos": 1200,
        "EAC3": 1000,
        "DTS": 500,
        "AAC": 100,
    }
    for profile in profiles:
        existing_scores = {
            int(item["format"]): int(item.get("score", 0))
            for item in profile.get("formatItems", [])
            if item.get("format") in valid_cf_ids and item.get("format") not in old_ids
        }
        for name, score in scores.items():
            existing_scores[cf_ids[name]] = score
        profile["formatItems"] = _complete_format_items(valid_cf_ids, existing_scores)
        _put_json(session, f"{api}/qualityprofile/{profile['id']}", app.api_key, profile)

    profiles = _get_json(session, f"{api}/qualityprofile", app.api_key)
    if profiles:
        _upsert_profile(session, app, profiles, "DanishAudio", {"DKAudio": 10000, "DKSubs": 0, **{k: v for k, v in scores.items() if k not in {"DKAudio", "DKSubs"}}}, cf_ids, valid_cf_ids)
        _upsert_profile(session, app, profiles, "EnglishSubs", scores, cf_ids, valid_cf_ids)

    return len(cf_ids), 2 if profiles else 0


def _complete_format_items(valid_cf_ids: set[int], scores_by_id: dict[int, int]) -> list[dict[str, int]]:
    return [
        {"format": fmt_id, "score": int(scores_by_id.get(fmt_id, 0))}
        for fmt_id in sorted(valid_cf_ids)
    ]


def _upsert_profile(session: requests.Session, app: ArrApp, profiles: list[dict[str, Any]], name: str, scores: dict[str, int], cf_ids: dict[str, int], valid_cf_ids: set[int]) -> None:
    existing = next((profile for profile in profiles if profile.get("name") == name), None)
    profile = copy.deepcopy(existing or profiles[0])
    if existing is None:
        profile.pop("id", None)
    profile["name"] = name
    profile["minFormatScore"] = 10000
    profile["cutoffFormatScore"] = 0
    profile["language"] = {"id": -1, "name": "Any"}
    profile["formatItems"] = _complete_format_items(valid_cf_ids, {
        cf_ids[fmt_name]: score
        for fmt_name, score in scores.items()
        if fmt_name in cf_ids
    })
    if existing is None:
        _post_json(session, f"{app.url}/api/v3/qualityprofile", app.api_key, profile)
    else:
        _put_json(session, f"{app.url}/api/v3/qualityprofile/{profile['id']}", app.api_key, profile)


def _rewire_indexers(session: requests.Session, app: ArrApp, prowlarr_indexers: list[dict[str, Any]], prowlarr_key: str) -> int:
    api = f"{app.url}/api/v3"
    by_name = {_clean_name(ix.get("name", "")): str(ix.get("id")) for ix in prowlarr_indexers}
    arr_indexers = _get_json(session, f"{api}/indexer", app.api_key)
    dk_names = {
        _clean_name(indexer.get("name", ""))
        for indexer in arr_indexers
        if "{dk}" in str(indexer.get("name", "")).lower()
    }
    linked = 0
    for indexer in arr_indexers:
        base_name = _clean_name(indexer.get("name", ""))
        if base_name in dk_names and "{dk}" not in str(indexer.get("name", "")).lower():
            continue

        prowlarr_id = by_name.get(base_name)
        if not prowlarr_id:
            continue
        _set_field(indexer, "baseUrl", f"{ARR_PROXY_URL}/{prowlarr_id}/api")
        _set_field(indexer, "apiKey", prowlarr_key)
        indexer["name"] = f"{_display_name(indexer.get('name', ''))} {{DK}}"
        _put_json(session, f"{api}/indexer/{indexer['id']}?forceSave=true", app.api_key, indexer)
        linked += 1
    return linked


def _rewire_download_clients(session: requests.Session, app: ArrApp) -> int:
    api = f"{app.url}/api/v3"
    clients = _get_json(session, f"{api}/downloadclient", app.api_key)
    linked = 0
    for client in clients:
        if not _is_altmount_proxy_client(client):
            continue

        _set_field(client, "host", PROXY_HOST)
        _set_field(client, "port", PROXY_PORT)
        _set_field(client, "useSsl", PROXY_USE_SSL)
        _set_field(client, "urlBase", "/altmount")
        _put_json(session, f"{api}/downloadclient/{client['id']}?forceSave=true", app.api_key, client)
        linked += 1
    return linked


def _harden_prowlarr_app_sync(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> None:
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    for app in apps:
        name = app.get("name", "")
        if name not in DEFAULT_APP_URLS:
            continue
        app["syncLevel"] = "addOnly"
        drop = [2020, 2030, 2060, 2070] if name == "Radarr" else [5030]
        sync_categories = _field(app, "syncCategories")
        if isinstance(sync_categories, list):
            _set_field(app, "syncCategories", [cat for cat in sync_categories if cat not in drop])
        _put_json(session, f"{prowlarr_url}/api/v1/applications/{app['id']}?forceSave=true", prowlarr_key, app)
    try:
        _post_json(session, f"{prowlarr_url}/api/v1/command", prowlarr_key, {"name": "ApplicationIndexerSync", "forceSync": True})
    except requests.RequestException as exc:
        print(f"[Core] Auto-Config: Prowlarr sync command skipped: {exc}", flush=True)


def paint() -> dict[str, int]:
    prowlarr_url = os.getenv("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
    prowlarr_key = _prowlarr_api_key()
    if not prowlarr_key:
        raise RuntimeError("Prowlarr API key is not set and no mounted Prowlarr config.xml was found")

    session = requests.Session()
    prowlarr_indexers = _get_json(session, f"{prowlarr_url}/api/v1/indexer", prowlarr_key)
    apps = _discover_arr_apps(session, prowlarr_url, prowlarr_key)
    if not apps:
        raise RuntimeError("No reachable Radarr/Sonarr applications found in Prowlarr")

    totals = {"apps": 0, "custom_formats": 0, "profiles": 0, "linked_indexers": 0, "download_clients": 0}
    for app in apps:
        cf_count, profile_count = _paint_formats_and_profiles(session, app)
        linked = _rewire_indexers(session, app, prowlarr_indexers, prowlarr_key)
        download_clients = _rewire_download_clients(session, app)
        totals["apps"] += 1
        totals["custom_formats"] += cf_count
        totals["profiles"] += profile_count
        totals["linked_indexers"] += linked
        totals["download_clients"] += download_clients
        print(
            f"[Core] Auto-Config: {app.name} painted {cf_count} CFs, "
            f"{profile_count} profiles, linked {linked} indexers, "
            f"{download_clients} download clients",
            flush=True,
        )

    _harden_prowlarr_app_sync(session, prowlarr_url, prowlarr_key)
    return totals
