"""Cosmos-safe Arr auto-configuration for Danish Intelligence.

This module intentionally uses only HTTP APIs reachable from the app container.
The old shell installer needs Docker CLI/socket access, which Cosmos market
containers should not require.
"""

from __future__ import annotations

import copy
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

try:
    from .tags import (
        CF_DANISH_AUDIO,
        CF_DANISH_SUBTITLES,
        DK_AUDIO_TITLE,
        DK_SUBS_TITLE,
        LEGACY_CF_NAMES,
        LEGACY_DK_AUDIO_TITLE,
        LEGACY_DK_SUBS_TITLE,
        LEGACY_PROFILE_NAMES,
        PROFILE_DANISH_AUDIO,
        PROFILE_DANISH_AUDIO_2160P,
        PROFILE_DANISH_SUBTITLES,
        PROFILE_DANISH_SUBTITLES_2160P,
    )
except ImportError:  # Allows `python3 auto_config.py` during manual debugging.
    from tags import (
        CF_DANISH_AUDIO,
        CF_DANISH_SUBTITLES,
        DK_AUDIO_TITLE,
        DK_SUBS_TITLE,
        LEGACY_CF_NAMES,
        LEGACY_DK_AUDIO_TITLE,
        LEGACY_DK_SUBS_TITLE,
        LEGACY_PROFILE_NAMES,
        PROFILE_DANISH_AUDIO,
        PROFILE_DANISH_AUDIO_2160P,
        PROFILE_DANISH_SUBTITLES,
        PROFILE_DANISH_SUBTITLES_2160P,
    )

MANAGED_CF_NAMES = {
    CF_DANISH_AUDIO,
    CF_DANISH_SUBTITLES,
    "TrueHD Atmos",
    "DTS-X",
    "TrueHD",
    "DTS-HD MA",
    "EAC3 Atmos",
    "EAC3",
    "DTS",
    "AAC",
    "DV",
    "HDR",
    "HDR10",
    "HDR10+",
    "HEVC",
}
DEFAULT_APP_URLS = {"Radarr": "http://radarr:7878", "Sonarr": "http://sonarr:8989"}
DEFAULT_2160P_APP_URLS = {
    "Radarr": "http://radarr-2160p:7878",
    "Sonarr": "http://sonarr-2160p:8989",
}
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
    "radarr": ("/arr-config/radarr/config.xml", "/srv/config/radarr/config.xml"),
    "sonarr": ("/arr-config/sonarr/config.xml", "/srv/config/sonarr/config.xml"),
    "radarr-2160p": ("/arr-config/radarr-2160p/config.xml", "/srv/config/radarr-2160p/config.xml"),
    "sonarr-2160p": ("/arr-config/sonarr-2160p/config.xml", "/srv/config/sonarr-2160p/config.xml"),
}
SEERR_CONFIG_PATHS = (
    "/seerr-config/settings.json",
    "/app/config/settings.json",
    "/srv/config/seerr/settings.json",
)


@dataclass
class ArrApp:
    name: str
    kind: str
    slug: str
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


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "arr"


def _env_prefix(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _arr_kind(app: dict[str, Any]) -> str:
    for value in (
        app.get("implementation"),
        app.get("implementationName"),
        app.get("configContract"),
        app.get("name"),
    ):
        text = str(value or "").lower()
        if "radarr" in text:
            return "Radarr"
        if "sonarr" in text:
            return "Sonarr"
    return ""


def _is_2160p_instance(name: str, url: str = "") -> bool:
    text = f"{name} {url}".lower()
    return any(token in text for token in ("2160p", "2160", "uhd", "4k"))


def _altmount_download_category(app: ArrApp) -> str:
    if _is_2160p_instance(app.name, app.url):
        return "movies-2160p" if app.kind == "Radarr" else "tv-2160p"
    return "movies" if app.kind == "Radarr" else "tv"


def _danish_profile_names(app: ArrApp) -> tuple[str, str]:
    if _is_2160p_instance(app.name, app.url):
        return PROFILE_DANISH_AUDIO_2160P, PROFILE_DANISH_SUBTITLES_2160P
    return PROFILE_DANISH_AUDIO, PROFILE_DANISH_SUBTITLES


def _default_arr_url(kind: str, name: str) -> str:
    if _is_2160p_instance(name):
        return DEFAULT_2160P_APP_URLS[kind]
    return DEFAULT_APP_URLS[kind]


def _truthy_env(name: str) -> bool:
    return _clean_env(name).lower() in {"1", "true", "yes", "y", "on"}


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


def _bootstrap_arr_apps(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> None:
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    existing_names = {_slug(str(app.get("name", ""))) for app in apps}
    schemas = _get_json(session, f"{prowlarr_url}/api/v1/applications/schema", prowlarr_key)

    wanted_apps = [
        ("Radarr", "Radarr", DEFAULT_APP_URLS["Radarr"]),
        ("Sonarr", "Sonarr", DEFAULT_APP_URLS["Sonarr"]),
    ]
    if _truthy_env("ENABLE_2160P_ARRS"):
        wanted_apps.extend([
            ("Radarr", "Radarr 2160p", DEFAULT_2160P_APP_URLS["Radarr"]),
            ("Sonarr", "Sonarr 2160p", DEFAULT_2160P_APP_URLS["Sonarr"]),
        ])

    for kind, name, url in wanted_apps:
        if _slug(name) in existing_names:
            continue

        api_key = _first_working_arr_key(session, kind, name, url)
        if not api_key:
            print(f"[Core] Auto-Config: {name} is not reachable yet; Prowlarr registration skipped", flush=True)
            continue

        schema = copy.deepcopy(next((item for item in schemas if _arr_kind(item) == kind), None))
        if not schema:
            print(f"[Core] Auto-Config: Prowlarr {kind} application schema unavailable; {name} skipped", flush=True)
            continue

        schema["name"] = name
        schema["enable"] = True
        schema["syncLevel"] = "addOnly"
        _set_field(schema, "prowlarrUrl", ARR_PROXY_URL)
        _set_field(schema, "baseUrl", url)
        _set_field(schema, "apiKey", api_key)
        try:
            _post_json(session, f"{prowlarr_url}/api/v1/applications?forceSave=true", prowlarr_key, schema)
            print(f"[Core] Auto-Config: registered {name} in Prowlarr", flush=True)
            existing_names.add(_slug(name))
        except requests.RequestException as exc:
            print(f"[Core] Auto-Config: failed to register {name} in Prowlarr: {exc}", flush=True)


def _discover_arr_apps(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> list[ArrApp]:
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    discovered: list[ArrApp] = []
    for app in apps:
        name = app.get("name", "")
        kind = _arr_kind(app)
        if kind not in DEFAULT_APP_URLS:
            continue
        default_url = _default_arr_url(kind, name)
        url = str(_field(app, "baseUrl") or default_url).rstrip("/")
        prowlarr_app_key = str(_field(app, "apiKey") or "")
        api_key = _first_working_arr_key(session, kind, name, url, prowlarr_app_key)
        if not api_key and url != default_url:
            url = default_url
            api_key = _first_working_arr_key(session, kind, name, url, prowlarr_app_key)
        if not api_key:
            print(f"[Core] Auto-Config: {name} API key unavailable or unauthorized", flush=True)
            continue
        discovered.append(ArrApp(name=name, kind=kind, slug=_slug(name), url=url.rstrip("/"), api_key=api_key))
    return discovered


def _first_working_arr_key(session: requests.Session, kind: str, app_name: str, url: str, prowlarr_app_key: str = "") -> str:
    env_prefixes = [_env_prefix(app_name), kind.upper()]
    if _is_2160p_instance(app_name, url):
        env_prefixes.insert(0, f"{kind.upper()}_2160P")
    env_names = [
        env_name
        for prefix in dict.fromkeys(env_prefixes)
        for env_name in (f"{prefix}_API_KEY", f"{prefix}_APIKEY")
    ]
    candidates = [_clean_env(name) for name in env_names]
    candidates.extend(_read_arr_config_keys(kind, app_name, url))
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


def _read_arr_config_keys(kind: str, app_name: str = "", url: str = "") -> list[str]:
    keys: list[str] = []
    config_keys = [kind]
    if app_name:
        config_keys.append(_slug(app_name))
    host = urlparse(url).hostname or ""
    if host:
        config_keys.append(host)
    if _is_2160p_instance(app_name, url):
        config_keys.append(f"{kind.lower()}-2160p")

    seen_paths: set[str] = set()
    for config_key in dict.fromkeys(config_keys):
        for path in ARR_CONFIG_PATHS.get(config_key, ()):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            cfg = Path(path)
            if not cfg.exists():
                continue
            try:
                root = ET.parse(cfg).getroot()
                api_key = root.findtext("ApiKey", default="").strip()
                if api_key:
                    keys.append(api_key)
            except Exception as exc:
                print(f"[Core] Auto-Config: could not read {path}: {exc}", flush=True)
    return keys


def _prowlarr_api_key() -> str:
    return (
        _clean_env("PROWLARR_API_KEY")
        or _clean_env("PROWLARR_APIKEY")
        or next(iter(_read_arr_config_keys("Prowlarr")), "")
    )


def _seerr_api_key() -> str:
    env_key = _clean_env("SEERR_API_KEY") or _clean_env("SEERR_APIKEY")
    if env_key:
        return env_key
    for path in SEERR_CONFIG_PATHS:
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            data = json.loads(cfg.read_text())
            api_key = str(data.get("main", {}).get("apiKey") or "").strip()
            if api_key:
                return api_key
        except Exception as exc:
            print(f"[Core] Auto-Config: could not read Seerr settings from {path}: {exc}", flush=True)
    return ""


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
        _cf_payload(CF_DANISH_AUDIO, rf"(?:\[Danish Audio\]|{re.escape(DK_AUDIO_TITLE)}\b|{re.escape(LEGACY_DK_AUDIO_TITLE)}\b)", include_rename=True),
        _cf_payload(CF_DANISH_SUBTITLES, rf"(?:\[Danish Subtitles\]|{re.escape(DK_SUBS_TITLE)}\b|{re.escape(LEGACY_DK_SUBS_TITLE)}\b)", include_rename=True),
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
                {"name": "EAC3", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"EAC3|DD\+|DDP|DD\.?P|E-AC-3"}]},
                {"name": "Atmos", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"\bAtmos\b"}]},
            ],
        },
        {
            "id": 0,
            "name": "EAC3",
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {"name": "EAC3", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": [{"name": "value", "value": r"EAC3|DD\+|DDP|DD\.?P|E-AC-3"}]},
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
        _cf_payload("DV", r"\bDV\b|Dolby[-. ]?Vision"),
        _cf_payload("HDR10+", r"HDR10\+"),
        _cf_payload("HDR10", r"HDR10(?!\+)"),
        _cf_payload("HDR", r"\bHDR\b"),
        _cf_payload("HEVC", r"x265|HEVC|H[ ._-]?265"),
    ]


def _paint_formats_and_profiles(session: requests.Session, app: ArrApp) -> tuple[int, int]:
    api = f"{app.url}/api/v3"
    old_formats = _get_json(session, f"{api}/customformat", app.api_key)
    old_ids = [fmt["id"] for fmt in old_formats if fmt.get("name") in MANAGED_CF_NAMES or fmt.get("name") in LEGACY_CF_NAMES]
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
    legacy_profile_ids = [
        int(profile["id"])
        for profile in profiles
        if profile.get("name") in LEGACY_PROFILE_NAMES and len(profiles) > 1
    ]
    for profile_id in legacy_profile_ids:
        _delete(session, f"{api}/qualityprofile/{profile_id}", app.api_key)
    if legacy_profile_ids:
        profiles = _get_json(session, f"{api}/qualityprofile", app.api_key)

    scores = {
        CF_DANISH_AUDIO: 10000,
        CF_DANISH_SUBTITLES: 10000,
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
            if name in cf_ids:
                existing_scores[cf_ids[name]] = score
        profile["formatItems"] = _complete_format_items(valid_cf_ids, existing_scores)
        _put_json(session, f"{api}/qualityprofile/{profile['id']}", app.api_key, profile)

    profiles = _get_json(session, f"{api}/qualityprofile", app.api_key)
    if profiles:
        codec_scores = {k: v for k, v in scores.items() if k not in {CF_DANISH_AUDIO, CF_DANISH_SUBTITLES}}
        audio_profile, subtitles_profile = _danish_profile_names(app)
        _upsert_profile(session, app, profiles, audio_profile, {CF_DANISH_AUDIO: 10000, CF_DANISH_SUBTITLES: 0, **codec_scores}, cf_ids, valid_cf_ids)
        _upsert_profile(session, app, profiles, subtitles_profile, {CF_DANISH_AUDIO: 0, CF_DANISH_SUBTITLES: 10000, **codec_scores}, cf_ids, valid_cf_ids)

    return len(cf_ids), 2 if profiles else 0


def _complete_format_items(valid_cf_ids: set[int], scores_by_id: dict[int, int]) -> list[dict[str, int]]:
    return [
        {"format": fmt_id, "score": int(scores_by_id.get(fmt_id, 0))}
        for fmt_id in sorted(valid_cf_ids)
    ]


def _quality_name(item: dict[str, Any]) -> str:
    quality = item.get("quality") or item.get("qualityGroup") or {}
    name = str(quality.get("name") or "")
    child_names = [
        _quality_name(child)
        for child in item.get("items", [])
        if isinstance(child, dict)
    ]
    return " ".join([name, *child_names]).strip()


def _quality_id(item: dict[str, Any]) -> int | None:
    quality = item.get("quality") or {}
    if quality.get("id") is not None:
        return int(quality["id"])
    return None


def _force_2160p_quality_items(profile: dict[str, Any]) -> None:
    cutoff = None
    for item in profile.get("items", []):
        if not isinstance(item, dict):
            continue
        allowed = "2160" in _quality_name(item).lower()
        item["allowed"] = allowed
        if allowed:
            quality_id = _quality_id(item)
            if quality_id is not None:
                cutoff = quality_id
        for child in item.get("items", []):
            if not isinstance(child, dict):
                continue
            child_allowed = "2160" in _quality_name(child).lower()
            child["allowed"] = child_allowed
            child_id = _quality_id(child)
            if child_allowed and child_id is not None:
                cutoff = child_id
    if cutoff is not None:
        profile["cutoff"] = cutoff


_NORMAL_PROFILE_BLOCKED_QUALITIES = {
    "unknown",
    "workprint",
    "cam",
    "telesync",
    "telecine",
    "regional",
    "dvdscr",
    "sdtv",
    "raw-hd",
}


def _normal_profile_quality_allowed(name: str) -> bool:
    lower = name.strip().lower()
    if not lower:
        return False
    if lower in _NORMAL_PROFILE_BLOCKED_QUALITIES:
        return False
    if lower in {"dvd", "dvd-r"}:
        return True
    return any(resolution in lower for resolution in ("720p", "1080p", "2160p"))


def _force_normal_quality_items(profile: dict[str, Any]) -> None:
    """Allow sane fallback qualities for normal Arr profiles.

    Older Danish releases often only exist as DVD/DVD-R or 720p. Keep obvious
    bad/ambiguous qualities disabled, but allow Radarr/Sonarr to grab a valid
    Danish release now and upgrade later if a higher quality appears.
    """
    for item in profile.get("items", []):
        if not isinstance(item, dict):
            continue
        children = [child for child in item.get("items", []) if isinstance(child, dict)]
        if children:
            child_allowed = []
            for child in children:
                allowed = _normal_profile_quality_allowed(_quality_name(child))
                child["allowed"] = allowed
                child_allowed.append(allowed)
            item["allowed"] = any(child_allowed)
        else:
            item["allowed"] = _normal_profile_quality_allowed(_quality_name(item))


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
    if _is_2160p_instance(app.name, app.url):
        _force_2160p_quality_items(profile)
    else:
        _force_normal_quality_items(profile)
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
        _set_field(indexer, "baseUrl", f"{ARR_PROXY_URL}/{app.slug}/{prowlarr_id}/api")
        _set_field(indexer, "apiKey", prowlarr_key)
        indexer["name"] = f"{_display_name(indexer.get('name', ''))} {{DK}}"
        _put_json(session, f"{api}/indexer/{indexer['id']}?forceSave=true", app.api_key, indexer)
        linked += 1
    return linked


def _paint_naming(session: requests.Session, app: ArrApp) -> None:
    api = f"{app.url}/api/v3"
    naming = _get_json(session, f"{api}/config/naming", app.api_key)
    
    if app.kind == "Radarr":
        naming["renameMovies"] = True
        naming["movieFolderFormat"] = "{Movie CleanTitle} ({Release Year}) {imdb-{ImdbId}} {tmdb-{TmdbId}}"
        naming["standardMovieFormat"] = "{Movie CleanTitle} ({Release Year}) {imdb-{ImdbId}} {tmdb-{TmdbId}} [{Quality Full}] [{Custom Formats}]"
    elif app.kind == "Sonarr":
        naming["renameEpisodes"] = True
        naming["seriesFolderFormat"] = "{Series TitleYear} {tvdb-{TvdbId}}"
        naming["standardEpisodeFormat"] = "{Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} {imdb-{ImdbId}} {tmdb-{TmdbId}} [{Quality Full}] [{Custom Formats}]"
	        
    _put_json(session, f"{api}/config/naming?forceSave=true", app.api_key, naming)


def _paint_indexer_config(session: requests.Session, app: ArrApp) -> None:
    """Whitelist proxy markers that Radarr can otherwise misread as hardcoded subs."""
    api = f"{app.url}/api/v3"
    try:
        config = _get_json(session, f"{api}/config/indexer", app.api_key)
    except requests.RequestException as exc:
        print(f"[Core] Auto-Config: {app.name} indexer config skipped: {exc}", flush=True)
        return

    if "whitelistedHardcodedSubs" not in config:
        return

    existing = [
        tag.strip()
        for tag in str(config.get("whitelistedHardcodedSubs") or "").split(",")
        if tag.strip()
    ]
    wanted = [
        DK_SUBS_TITLE.lstrip("."),
        LEGACY_DK_SUBS_TITLE.lstrip("."),
        CF_DANISH_SUBTITLES,
    ]
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*existing, *wanted]:
        key = tag.casefold()
        if key not in seen:
            merged.append(tag)
            seen.add(key)

    new_value = ",".join(merged)
    if new_value != config.get("whitelistedHardcodedSubs"):
        config["whitelistedHardcodedSubs"] = new_value
        _put_json(session, f"{api}/config/indexer", app.api_key, config)


def _ensure_root_folders(session: requests.Session, app: ArrApp) -> int:
    api = f"{app.url}/api/v3"
    existing = _get_json(session, f"{api}/rootfolder", app.api_key)
    existing_paths = {f.get("path", "").rstrip("/") for f in existing}
    
    if _is_2160p_instance(app.name, app.url):
        radarr_paths = ["/media/movies-2160p"]
        sonarr_paths = ["/media/tv-2160p"]
    else:
        radarr_paths = ["/media/movies", "/media/kids-movies"]
        sonarr_paths = ["/media/tv", "/media/kids-tv"]
    
    target_paths = radarr_paths if app.kind == "Radarr" else sonarr_paths
    added = 0
    
    for path in target_paths:
        if path not in existing_paths:
            try:
                _post_json(session, f"{api}/rootfolder", app.api_key, {"path": path})
                added += 1
            except requests.RequestException as e:
                print(f"[Core] Auto-Config: Failed to add root folder {path}: {e}", flush=True)
            
    return added

def _ensure_download_client(session: requests.Session, app: ArrApp) -> int:
    api = f"{app.url}/api/v3"
    clients = _get_json(session, f"{api}/downloadclient", app.api_key)
    proxy_api_key = os.getenv("PROXY_API_KEY", "")
    category = _altmount_download_category(app)
    
    for client in clients:
        if _is_altmount_proxy_client(client):
            _set_field(client, "host", PROXY_HOST)
            _set_field(client, "port", PROXY_PORT)
            _set_field(client, "useSsl", PROXY_USE_SSL)
            _set_field(client, "urlBase", "/altmount")
            _set_field(client, "movieCategory" if app.kind == "Radarr" else "tvCategory", category)
            if proxy_api_key:
                _set_field(client, "apiKey", proxy_api_key)
            _put_json(session, f"{api}/downloadclient/{client['id']}?forceSave=true", app.api_key, client)
            return 1

    if not proxy_api_key:
        print("[Core] Auto-Config: Cannot create AltMount client; PROXY_API_KEY is missing.", flush=True)
        return 0

    new_client = {
        "enable": True,
        "name": "AltMount",
        "implementation": "Sabnzbd",
        "configContract": "SabnzbdSettings",
        "fields": [
            {"name": "host", "value": PROXY_HOST},
            {"name": "port", "value": PROXY_PORT},
            {"name": "useSsl", "value": PROXY_USE_SSL},
            {"name": "urlBase", "value": "/altmount"},
            {"name": "apiKey", "value": proxy_api_key},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "movieCategory" if app.kind == "Radarr" else "tvCategory", "value": category},
            {"name": "recentMoviePriority" if app.kind == "Radarr" else "recentTvPriority", "value": 0},
            {"name": "olderMoviePriority" if app.kind == "Radarr" else "olderTvPriority", "value": 0}
        ]
    }
    _post_json(session, f"{api}/downloadclient?forceSave=true", app.api_key, new_client)
    return 1


def _ensure_marker_webhook(session: requests.Session, app: ArrApp) -> int:
    api = f"{app.url}/api/v3"
    webhook_name = "Danish Intelligence Marker Preserver"
    webhook_url = f"{ARR_PROXY_URL}/arr/{app.slug}"

    notifications = _get_json(session, f"{api}/notification", app.api_key)
    existing = None
    for notification in notifications:
        url = str(_field(notification, "url", ""))
        if notification.get("name") == webhook_name or url == webhook_url:
            existing = notification
            break

    if existing:
        payload = existing
    else:
        schemas = _get_json(session, f"{api}/notification/schema", app.api_key)
        payload = next((schema for schema in schemas if schema.get("implementation") == "Webhook"), None)
        if not payload:
            print(f"[Core] Auto-Config: {app.name} Webhook notification schema unavailable", flush=True)
            return 0

    payload["name"] = webhook_name
    payload["implementation"] = "Webhook"
    payload["implementationName"] = "Webhook"
    payload["configContract"] = "WebhookSettings"
    _set_field(payload, "url", webhook_url)
    _set_field(payload, "method", 1)
    _set_field(payload, "username", "")
    _set_field(payload, "password", "")
    _set_field(payload, "headers", [])

    for flag in (
        "onGrab",
        "onDownload",
        "onUpgrade",
        "onRename",
        "onMovieAdded",
        "onSeriesAdd",
        "onImportComplete",
        "onMovieDelete",
        "onSeriesDelete",
        "onMovieFileDelete",
        "onEpisodeFileDelete",
        "onMovieFileDeleteForUpgrade",
        "onEpisodeFileDeleteForUpgrade",
        "onHealthIssue",
        "includeHealthWarnings",
        "onHealthRestored",
        "onApplicationUpdate",
        "onManualInteractionRequired",
    ):
        if flag in payload:
            payload[flag] = False

    for flag in ("onDownload", "onUpgrade", "onRename"):
        if flag in payload:
            payload[flag] = True
    if app.kind == "Sonarr" and "onImportComplete" in payload:
        payload["onImportComplete"] = True

    if existing:
        _put_json(session, f"{api}/notification/{payload['id']}?forceSave=true", app.api_key, payload)
    else:
        _post_json(session, f"{api}/notification?forceSave=true", app.api_key, payload)
    return 1


def _profile_id_by_name(session: requests.Session, app: ArrApp, profile_name: str) -> int | None:
    profiles = _get_json(session, f"{app.url}/api/v3/qualityprofile", app.api_key)
    profile = next((item for item in profiles if item.get("name") == profile_name), None)
    return int(profile["id"]) if profile and profile.get("id") is not None else None


def _seerr_server_url(app: ArrApp) -> str:
    parsed = urlparse(app.url)
    base_url = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "http", parsed.netloc, base_url, "", "", ""))


def _seerr_base_payload(app: ArrApp, profile_name: str, profile_id: int, root_path: str, is_default: bool) -> dict[str, Any]:
    parsed = urlparse(app.url)
    hostname = parsed.hostname or app.slug
    port = parsed.port or (443 if parsed.scheme == "https" else (8989 if app.kind == "Sonarr" else 7878))
    base_url = parsed.path.rstrip("/")
    label = "Movies" if app.kind == "Radarr" else "TV"
    if "audio" in profile_name.lower():
        label = f"{label} 2160p - Danish Audio"
    else:
        label = f"{label} 2160p - Danish Subtitles"

    payload: dict[str, Any] = {
        "name": label,
        "hostname": hostname,
        "port": port,
        "apiKey": app.api_key,
        "useSsl": parsed.scheme == "https",
        "baseUrl": base_url,
        "url": _seerr_server_url(app),
        "activeProfileId": profile_id,
        "activeProfileName": profile_name,
        "activeDirectory": root_path,
        "is4k": True,
        "isDefault": is_default,
        "externalUrl": "",
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "tags": [],
    }
    if app.kind == "Radarr":
        payload["minimumAvailability"] = "released"
    else:
        payload.update({
            "seriesType": "standard",
            "animeTags": [],
            "enableSeasonFolders": True,
            "monitorNewItems": "all",
        })
    return payload


def _ensure_seerr_2160p_servers(session: requests.Session, app: ArrApp) -> int:
    if not _truthy_env("ENABLE_2160P_ARRS") or not _is_2160p_instance(app.name, app.url):
        return 0

    seerr_key = _seerr_api_key()
    if not seerr_key:
        print("[Core] Auto-Config: Seerr API key unavailable; 2160p Seerr entries skipped", flush=True)
        return 0

    seerr_url = os.getenv("SEERR_URL", "http://seerr:5055").rstrip("/")
    endpoint = "radarr" if app.kind == "Radarr" else "sonarr"
    root_path = "/media/movies-2160p" if app.kind == "Radarr" else "/media/tv-2160p"
    audio_profile, subtitles_profile = _danish_profile_names(app)
    wanted = (
        (subtitles_profile, True),
        (audio_profile, False),
    )

    try:
        existing = _get_json(session, f"{seerr_url}/api/v1/settings/{endpoint}", seerr_key)
    except requests.RequestException as exc:
        print(f"[Core] Auto-Config: Seerr {endpoint} settings unavailable; 2160p entries skipped: {exc}", flush=True)
        return 0

    changed = 0
    for profile_name, is_default in wanted:
        profile_id = _profile_id_by_name(session, app, profile_name)
        if profile_id is None:
            print(f"[Core] Auto-Config: {app.name} profile {profile_name} unavailable; Seerr entry skipped", flush=True)
            continue

        payload = _seerr_base_payload(app, profile_name, profile_id, root_path, is_default)
        match = next((
            item for item in existing
            if bool(item.get("is4k")) is True
            and str(item.get("hostname", "")).lower() == payload["hostname"].lower()
            and int(item.get("port", 0) or 0) == int(payload["port"])
            and item.get("activeDirectory") == root_path
            and (item.get("activeProfileName") == profile_name or item.get("name") == payload["name"])
        ), None)

        try:
            if match:
                payload["id"] = match["id"]
                _put_json(session, f"{seerr_url}/api/v1/settings/{endpoint}/{match['id']}", seerr_key, payload)
            else:
                _post_json(session, f"{seerr_url}/api/v1/settings/{endpoint}", seerr_key, payload)
            changed += 1
        except requests.RequestException as exc:
            print(f"[Core] Auto-Config: failed to upsert Seerr {payload['name']}: {exc}", flush=True)

    return changed


def _harden_prowlarr_app_sync(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> None:
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    for app in apps:
        name = app.get("name", "")
        kind = _arr_kind(app)
        if kind not in DEFAULT_APP_URLS:
            continue
        app["syncLevel"] = "addOnly"
        drop = [2020, 2030, 2060, 2070] if kind == "Radarr" else [5030]
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
    _bootstrap_arr_apps(session, prowlarr_url, prowlarr_key)
    prowlarr_indexers = _get_json(session, f"{prowlarr_url}/api/v1/indexer", prowlarr_key)
    apps = _discover_arr_apps(session, prowlarr_url, prowlarr_key)
    if not apps:
        raise RuntimeError("No reachable Radarr/Sonarr applications found in Prowlarr")

    totals = {"apps": 0, "custom_formats": 0, "profiles": 0, "linked_indexers": 0, "download_clients": 0, "root_folders": 0, "webhooks": 0, "seerr_servers": 0}
    for app in apps:
        _paint_naming(session, app)
        _paint_indexer_config(session, app)
        root_folders = _ensure_root_folders(session, app)
        cf_count, profile_count = _paint_formats_and_profiles(session, app)
        linked = _rewire_indexers(session, app, prowlarr_indexers, prowlarr_key)
        download_clients = _ensure_download_client(session, app)
        webhooks = _ensure_marker_webhook(session, app)
        seerr_servers = _ensure_seerr_2160p_servers(session, app)
        totals["apps"] += 1
        totals["root_folders"] += root_folders
        totals["custom_formats"] += cf_count
        totals["profiles"] += profile_count
        totals["linked_indexers"] += linked
        totals["download_clients"] += download_clients
        totals["webhooks"] += webhooks
        totals["seerr_servers"] += seerr_servers
        print(
            f"[Core] Auto-Config: {app.name} painted {cf_count} CFs, "
            f"{profile_count} profiles, linked {linked} indexers, "
            f"{download_clients} download clients, {webhooks} webhooks, "
            f"{seerr_servers} Seerr servers, "
            f"created {root_folders} root folders",
            flush=True,
        )

    _harden_prowlarr_app_sync(session, prowlarr_url, prowlarr_key)
    return totals
