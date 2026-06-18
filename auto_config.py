"""Cosmos-safe Arr auto-configuration for Danish Intelligence.

This module intentionally uses only HTTP APIs reachable from the app container.
The old shell installer needs Docker CLI/socket access, which Cosmos market
containers should not require.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
import json
import os
import re
import secrets
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests

try:
    from .diagnostics import path_state, record, safe_env
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
    from diagnostics import path_state, record, safe_env
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
ARR_READY_TIMEOUT_SECONDS = int(os.getenv("ARR_READY_TIMEOUT_SECONDS", "240"))
ARR_READY_RETRY_SECONDS = int(os.getenv("ARR_READY_RETRY_SECONDS", "10"))
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
SEERR_DB_PATHS = (
    "/seerr-config/db/db.sqlite3",
    "/app/config/db/db.sqlite3",
    "/srv/config/seerr/db/db.sqlite3",
)
SEERR_MEDIA_SERVER_TYPE_PLEX = 1
SEERR_MEDIA_SERVER_TYPE_JELLYFIN = 2
SEERR_DEFAULT_PERMISSIONS = 48
SEERR_LOCAL_USER_TYPE = 2
SEERR_LOCAL_ADMIN_PERMISSIONS = 2
JELLYFIN_DB_PATHS = (
    "/jellyfin-config/data/data/jellyfin.db",
    "/app/jellyfin-config/data/data/jellyfin.db",
    "/srv/config/jellyfin/data/data/jellyfin.db",
)
JELLYFIN_API_KEY_NAME = "Danish Intelligence"
JELLYFIN_DEFAULT_LIBRARIES = (
    ("Movies", "movies", "/media/movies"),
    ("Danish Movies", "movies", "/media/danish-movies"),
    ("Documentaries", "movies", "/media/documentaries"),
    ("Christmas Movies", "movies", "/media/christmas-movies"),
    ("Classics", "movies", "/media/classics"),
    ("TV Shows", "tvshows", "/media/tv"),
    ("Danish TV", "tvshows", "/media/danish-tv"),
    ("Documentary Series", "tvshows", "/media/documentary-series"),
    ("Christmas TV", "tvshows", "/media/christmas-tv"),
    ("Kids Movies", "movies", "/media/kids-movies"),
    ("Kids TV", "tvshows", "/media/kids-tv"),
)
JELLYFIN_2160P_LIBRARIES = (
    ("Movies 2160p", "movies", "/media/movies-2160p"),
    ("TV Shows 2160p", "tvshows", "/media/tv-2160p"),
)
PLEX_CONFIG_PATHS = (
    "/plex-config/Library/Application Support/Plex Media Server",
    "/app/plex-config/Library/Application Support/Plex Media Server",
    "/srv/config/plex/Library/Application Support/Plex Media Server",
)
PLEX_DEFAULT_LIBRARIES = (
    ("Movies", "movie", "/media/movies"),
    ("Danish Movies", "movie", "/media/danish-movies"),
    ("Documentaries", "movie", "/media/documentaries"),
    ("Christmas Movies", "movie", "/media/christmas-movies"),
    ("Classics", "movie", "/media/classics"),
    ("TV Shows", "show", "/media/tv"),
    ("Danish TV", "show", "/media/danish-tv"),
    ("Documentary Series", "show", "/media/documentary-series"),
    ("Christmas TV", "show", "/media/christmas-tv"),
    ("Kids Movies", "movie", "/media/kids-movies"),
    ("Kids TV", "show", "/media/kids-tv"),
)
PLEX_2160P_LIBRARIES = (
    ("Movies 2160p", "movie", "/media/movies-2160p"),
    ("TV Shows 2160p", "show", "/media/tv-2160p"),
)
SEERR_ADMIN_PASSWORD_FILE = "/config/seerr-admin-password.txt"
SEERR_TITLE = "Danish Requests"
SEERR_DEFAULT_MOVIE_ROOTS = (
    ("Movies - Danish Subtitles", "/media/movies", "subtitles", True),
    ("Movies - Danish Audio", "/media/movies", "audio", False),
    ("Kids Movies - Danish Audio", "/media/kids-movies", "audio", False),
    ("Danish Movies - Danish Audio", "/media/danish-movies", "audio", False),
    ("Documentaries - Danish Subtitles", "/media/documentaries", "subtitles", False),
    ("Christmas Movies - Danish Audio", "/media/christmas-movies", "audio", False),
    ("Classics - Danish Audio", "/media/classics", "audio", False),
)
SEERR_DEFAULT_TV_ROOTS = (
    ("TV - Danish Subtitles", "/media/tv", "subtitles", True),
    ("TV - Danish Audio", "/media/tv", "audio", False),
    ("Kids TV - Danish Audio", "/media/kids-tv", "audio", False),
    ("Danish TV - Danish Audio", "/media/danish-tv", "audio", False),
    ("Documentary Series - Danish Subtitles", "/media/documentary-series", "subtitles", False),
    ("Christmas TV - Danish Audio", "/media/christmas-tv", "audio", False),
)
SEERR_2160P_MOVIE_ROOTS = (
    ("Movies 2160p - Danish Subtitles", "/media/movies-2160p", "subtitles", True),
    ("Movies 2160p - Danish Audio", "/media/movies-2160p", "audio", False),
)
SEERR_2160P_TV_ROOTS = (
    ("TV 2160p - Danish Subtitles", "/media/tv-2160p", "subtitles", True),
    ("TV 2160p - Danish Audio", "/media/tv-2160p", "audio", False),
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


def _arr_config_exists(config_key: str) -> bool:
    return any(Path(path).exists() for path in ARR_CONFIG_PATHS.get(config_key, ()))


def _enable_2160p_arrs() -> bool:
    value = _clean_env("ENABLE_2160P_ARRS").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    if _clean_env("RADARR_2160P_URL") or _clean_env("SONARR_2160P_URL"):
        return True
    return _arr_config_exists("radarr-2160p") or _arr_config_exists("sonarr-2160p")


def _altmount_download_category(app: ArrApp) -> str:
    if _is_2160p_instance(app.name, app.url):
        return "movies-2160p" if app.kind == "Radarr" else "tv-2160p"
    return "movies" if app.kind == "Radarr" else "tv"


def _danish_profile_names(app: ArrApp) -> tuple[str, str]:
    if _is_2160p_instance(app.name, app.url):
        return PROFILE_DANISH_AUDIO_2160P, PROFILE_DANISH_SUBTITLES_2160P
    return PROFILE_DANISH_AUDIO, PROFILE_DANISH_SUBTITLES


def _env_arr_url(kind: str, name: str) -> str:
    if _is_2160p_instance(name):
        return _clean_env(f"{kind.upper()}_2160P_URL").rstrip("/")
    return _clean_env(f"{kind.upper()}_URL").rstrip("/")


def _url_with_port(url: str, port: int) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or ""
    if not host:
        return url.rstrip("/")
    netloc = f"{host}:{port}"
    return urlunparse((parsed.scheme or "http", netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")


def _read_arr_config_port(kind: str, app_name: str = "", url: str = "") -> int | None:
    config_keys: list[str] = []
    if app_name:
        config_keys.append(_slug(app_name))
    host = urlparse(url).hostname or ""
    if host:
        config_keys.append(host)
    if _is_2160p_instance(app_name, url):
        config_keys.append(f"{kind.lower()}-2160p")
    config_keys.append(kind)

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
                port = (ET.parse(cfg).getroot().findtext("Port", default="") or "").strip()
                if port.isdigit() and int(port) > 0:
                    record("auto_config.config_port.read", kind=kind, app=app_name, path=path, port=int(port))
                    return int(port)
            except Exception as exc:
                record("auto_config.config_port.read_failed", kind=kind, app=app_name, path=path, error=str(exc), error_type=type(exc).__name__)
    return None


def _default_arr_url(kind: str, name: str) -> str:
    env_url = _env_arr_url(kind, name)
    if env_url:
        return env_url
    default_url = DEFAULT_2160P_APP_URLS[kind] if _is_2160p_instance(name) else DEFAULT_APP_URLS[kind]
    config_port = _read_arr_config_port(kind, name, default_url)
    if config_port is not None:
        return _url_with_port(default_url, config_port)
    return default_url


def _truthy_env(name: str) -> bool:
    return _clean_env(name).lower() in {"1", "true", "yes", "y", "on"}


def _get_json(session: requests.Session, url: str, api_key: str) -> Any:
    return _request_json(session, "GET", url, api_key, timeout=20)


def _put_json(session: requests.Session, url: str, api_key: str, payload: Any) -> Any:
    return _request_json(session, "PUT", url, api_key, payload=payload, timeout=30)


def _post_json(session: requests.Session, url: str, api_key: str, payload: Any) -> Any:
    return _request_json(session, "POST", url, api_key, payload=payload, timeout=30)


def _altmount_api_key() -> str:
    return _clean_env("ALTMOUNT_API_KEY") or _clean_env("PROXY_API_KEY")


def _altmount_url() -> str:
    url = (
        _clean_env("ALTMOUNT_API_BASE_URL")
        or _clean_env("ALTMOUNT_BASE_URL")
        or _clean_env("ALTMOUNT_URL")
        or "http://altmount:8080"
    ).rstrip("/")
    return url.rsplit("/sabnzbd", 1)[0].rstrip("/")


def _altmount_api_url(path: str, api_key: str) -> str:
    path = path if path.startswith("/") else f"/{path}"
    return f"{_altmount_url()}/api{path}?{urlencode({'apikey': api_key})}"


def _desired_altmount_import_strategy() -> str:
    return (_clean_env("DANISH_ALTMOUNT_IMPORT_STRATEGY") or "SYMLINK").strip().upper()


def _desired_altmount_mount_path() -> str:
    return (_clean_env("DANISH_ALTMOUNT_MOUNT_PATH") or "/mnt/altmount").rstrip("/")


def _desired_altmount_import_dir() -> str:
    return (_clean_env("DANISH_ALTMOUNT_IMPORT_DIR") or "/mnt/altmount-import").rstrip("/")


def _desired_altmount_complete_dir() -> str:
    return _clean_env("DANISH_ALTMOUNT_COMPLETE_DIR") or "/"


def _desired_altmount_health_library_dir() -> str:
    return (_clean_env("DANISH_ALTMOUNT_HEALTH_LIBRARY_DIR") or "/media").rstrip("/")


def _ensure_altmount_import_dir_path(path: str) -> None:
    if not path or not path.startswith("/"):
        return
    try:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        uid = _clean_env("PUID")
        gid = _clean_env("PGID")
        if uid.isdigit() and gid.isdigit():
            os.chown(target, int(uid), int(gid))
            target.chmod(0o2775)
        else:
            target.chmod(0o2777)
        record("auto_config.altmount_import_dir.ready", path=path, uid=uid or None, gid=gid or None)
    except OSError as exc:
        record(
            "auto_config.altmount_import_dir.failed",
            path=path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise RuntimeError(f"AltMount import directory is not writable: {path}") from exc


def _delete(session: requests.Session, url: str, api_key: str) -> None:
    resp = _request(session, "DELETE", url, api_key, timeout=20)
    if resp.status_code not in (200, 202, 204, 404):
        _raise_for_status(resp, "DELETE", url)


def _request(
    session: requests.Session,
    method: str,
    url: str,
    api_key: str,
    payload: Any | None = None,
    timeout: int = 20,
) -> requests.Response:
    started = time_ms()
    try:
        resp = session.request(method, url, headers=_headers(api_key), json=payload, timeout=timeout)
        record(
            "auto_config.http",
            method=method,
            url=_safe_url(url),
            status=resp.status_code,
            elapsed_ms=time_ms() - started,
            api_key_set=bool(api_key),
        )
        return resp
    except requests.RequestException as exc:
        record(
            "auto_config.http_error",
            method=method,
            url=_safe_url(url),
            elapsed_ms=time_ms() - started,
            error=str(exc),
            error_type=type(exc).__name__,
            api_key_set=bool(api_key),
        )
        raise


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    api_key: str,
    payload: Any | None = None,
    timeout: int = 20,
) -> Any:
    resp = _request(session, method, url, api_key, payload=payload, timeout=timeout)
    _raise_for_status(resp, method, url)
    return resp.json() if resp.content else None


def _raise_for_status(resp: requests.Response, method: str, url: str) -> None:
    if resp.status_code < 400:
        return
    body = (resp.text or "").strip()
    snippet = body[:1200]
    record(
        "auto_config.http_rejected",
        method=method,
        url=_safe_url(url),
        status=resp.status_code,
        body=snippet,
    )
    message = f"{resp.status_code} Client Error for url: {_safe_url(url)}"
    if snippet:
        message = f"{message}; response={snippet}"
    raise requests.HTTPError(message, response=resp)


def time_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _clean_name(name: str) -> str:
    name = re.sub(r"\s*\(Prowlarr\)$", "", name)
    name = re.sub(r"(\s*\{DK\})+$", "", name)
    name = re.sub(r"\s*\[[^\]]+\]$", "", name)
    return name.strip().lower()


def _display_name(name: str) -> str:
    name = re.sub(r"\s*\(Prowlarr\)$", "", name)
    name = re.sub(r"(\s*\{DK\})+$", "", name)
    return name.strip()


RADARR_INDEXER_CATEGORIES = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090]
SONARR_INDEXER_CATEGORIES = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090, 5070]


def _capability_category_ids(indexer: dict[str, Any]) -> set[int]:
    ids: set[int] = set()

    def visit(categories: list[dict[str, Any]]) -> None:
        for category in categories:
            try:
                ids.add(int(category.get("id")))
            except (TypeError, ValueError):
                pass
            visit(category.get("subCategories") or [])

    capabilities = indexer.get("capabilities") or {}
    visit(capabilities.get("categories") or [])
    return ids


def _indexer_target_categories(indexer: dict[str, Any], app: ArrApp) -> tuple[list[int], list[int]]:
    available = _capability_category_ids(indexer)
    if app.kind == "Radarr":
        categories = [cat for cat in RADARR_INDEXER_CATEGORIES if cat in available]
        return categories, []
    categories = [cat for cat in SONARR_INDEXER_CATEGORIES if cat in available]
    anime = [5070] if 5070 in available else []
    return categories, anime


def _managed_prowlarr_targets(prowlarr_indexers: list[dict[str, Any]], app: ArrApp) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for indexer in prowlarr_indexers:
        if not indexer.get("enable", True):
            continue
        if str(indexer.get("implementation", "")).lower() != "newznab":
            continue
        name = str(indexer.get("name", ""))
        if "{dk}" not in name.lower():
            continue
        clean_name = _clean_name(name)
        if not clean_name:
            continue
        categories, anime_categories = _indexer_target_categories(indexer, app)
        if clean_name == "oldboys":
            base_url = f"{ARR_PROXY_URL}/ob"
            api_key = _clean_env("PROXY_API_KEY")
        else:
            base_url = f"{ARR_PROXY_URL}/{app.slug}/{indexer.get('id')}"
            api_key = ""
        targets[clean_name] = {
            "id": str(indexer.get("id")),
            "name": f"{_display_name(name)} {{DK}}",
            "baseUrl": base_url,
            "apiKey": api_key,
            "categories": categories,
            "animeCategories": anime_categories,
        }
    return targets


def _newznab_schema(session: requests.Session, app: ArrApp) -> dict[str, Any] | None:
    schemas = _get_json(session, f"{app.url}/api/v3/indexer/schema", app.api_key)
    schema = next((item for item in schemas if item.get("implementation") == "Newznab"), None)
    return copy.deepcopy(schema) if schema else None


def _set_indexer_target_fields(indexer: dict[str, Any], app: ArrApp, target: dict[str, Any], prowlarr_key: str) -> None:
    _set_field(indexer, "baseUrl", target["baseUrl"])
    _set_field(indexer, "apiKey", target.get("apiKey") or prowlarr_key)
    if target["categories"]:
        _set_field(indexer, "categories", target["categories"])
    if app.kind == "Sonarr":
        _set_field(indexer, "animeCategories", target["animeCategories"])
    indexer["name"] = target["name"]
    indexer["enableRss"] = True
    indexer["enableAutomaticSearch"] = True
    indexer["enableInteractiveSearch"] = True
    indexer["priority"] = max(1, int(indexer.get("priority") or 25))


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
    record("auto_config.bootstrap.begin", prowlarr_url=prowlarr_url)
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    existing_names = {_slug(str(app.get("name", ""))) for app in apps}
    schemas = _get_json(session, f"{prowlarr_url}/api/v1/applications/schema", prowlarr_key)
    record(
        "auto_config.bootstrap.loaded",
        existing_app_count=len(apps),
        schema_count=len(schemas),
        enable_2160p=_enable_2160p_arrs(),
    )

    wanted_apps = [
        ("Radarr", "Radarr", _default_arr_url("Radarr", "Radarr")),
        ("Sonarr", "Sonarr", _default_arr_url("Sonarr", "Sonarr")),
    ]
    if _enable_2160p_arrs():
        wanted_apps.extend([
            ("Radarr", "Radarr 2160p", _default_arr_url("Radarr", "Radarr 2160p")),
            ("Sonarr", "Sonarr 2160p", _default_arr_url("Sonarr", "Sonarr 2160p")),
        ])

    for kind, name, url in wanted_apps:
        api_key = _first_working_arr_key(session, kind, name, url)
        if not api_key:
            print(f"[Core] Auto-Config: {name} is not reachable yet; Prowlarr registration skipped", flush=True)
            record("auto_config.bootstrap.skip_unreachable", app=name, kind=kind, url=url)
            continue

        existing_app = next((app for app in apps if _slug(str(app.get("name", ""))) == _slug(name)), None)
        if existing_app:
            existing_app["enable"] = True
            existing_app["syncLevel"] = "addOnly"
            _set_field(existing_app, "prowlarrUrl", ARR_PROXY_URL)
            _set_field(existing_app, "baseUrl", url)
            _set_field(existing_app, "apiKey", api_key)
            try:
                _put_json(session, f"{prowlarr_url}/api/v1/applications/{existing_app['id']}?forceSave=true", prowlarr_key, existing_app)
                print(f"[Core] Auto-Config: refreshed {name} in Prowlarr", flush=True)
                record("auto_config.bootstrap.refreshed", app=name, kind=kind, url=url)
            except requests.RequestException as exc:
                print(f"[Core] Auto-Config: failed to refresh {name} in Prowlarr: {exc}", flush=True)
                record("auto_config.bootstrap.refresh_failed", app=name, kind=kind, error=str(exc), error_type=type(exc).__name__)
            continue

        schema = copy.deepcopy(next((item for item in schemas if _arr_kind(item) == kind), None))
        if not schema:
            print(f"[Core] Auto-Config: Prowlarr {kind} application schema unavailable; {name} skipped", flush=True)
            record("auto_config.bootstrap.skip_schema_missing", app=name, kind=kind)
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
            record("auto_config.bootstrap.registered", app=name, kind=kind, url=url)
            existing_names.add(_slug(name))
        except requests.RequestException as exc:
            print(f"[Core] Auto-Config: failed to register {name} in Prowlarr: {exc}", flush=True)
            record("auto_config.bootstrap.register_failed", app=name, kind=kind, url=url, error=str(exc), error_type=type(exc).__name__)


def _sync_prowlarr_indexers(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> None:
    """Force an initial Prowlarr application sync so fresh Arrs receive indexers."""
    record("auto_config.prowlarr_sync.begin")
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    wanted = {_slug(name) for name in _required_arr_app_names()}
    managed: list[dict[str, Any]] = []
    changed = 0

    for app in apps:
        if _slug(str(app.get("name", ""))) not in wanted:
            continue
        managed.append(app)
        if app.get("syncLevel") != "fullSync":
            app["syncLevel"] = "fullSync"
            _put_json(session, f"{prowlarr_url}/api/v1/applications/{app['id']}?forceSave=true", prowlarr_key, app)
            changed += 1

    try:
        command = _post_json(session, f"{prowlarr_url}/api/v1/command", prowlarr_key, {"name": "ApplicationIndexerSync"})
    except requests.RequestException as exc:
        record("auto_config.prowlarr_sync.command_failed", error=str(exc), error_type=type(exc).__name__)
        print(f"[Core] Auto-Config: Prowlarr indexer sync failed to start: {exc}", flush=True)
    else:
        command_id = command.get("id")
        status = str(command.get("status", "")).lower()
        deadline = time.monotonic() + 45
        while command_id and status not in {"completed", "failed", "aborted"} and time.monotonic() < deadline:
            time.sleep(3)
            try:
                command = _get_json(session, f"{prowlarr_url}/api/v1/command/{command_id}", prowlarr_key)
                status = str(command.get("status", "")).lower()
            except requests.RequestException as exc:
                record("auto_config.prowlarr_sync.poll_failed", command_id=command_id, error=str(exc), error_type=type(exc).__name__)
                break
        record("auto_config.prowlarr_sync.command_complete", command_id=command_id, status=status or "unknown")

    for app in managed:
        app["syncLevel"] = "addOnly"
        try:
            _put_json(session, f"{prowlarr_url}/api/v1/applications/{app['id']}?forceSave=true", prowlarr_key, app)
        except requests.RequestException as exc:
            record("auto_config.prowlarr_sync.restore_failed", app=app.get("name"), error=str(exc), error_type=type(exc).__name__)

    record("auto_config.prowlarr_sync.complete", changed=changed, restored=len(managed))


def _ensure_prowlarr_oldboys_proxy_key(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> int:
    """Create or repair Prowlarr's OldBoys indexer for this install's proxy key."""
    if not (_clean_env("OB_API_TOKEN") and _clean_env("OB_RID")):
        record("auto_config.prowlarr_oldboys.skip_missing_oldboys_credentials")
        return 0

    proxy_key = _clean_env("PROXY_API_KEY")
    if not proxy_key:
        record("auto_config.prowlarr_oldboys.skip_missing_proxy_key")
        return 0

    changed = 0
    indexers = _get_json(session, f"{prowlarr_url}/api/v1/indexer", prowlarr_key)
    oldboys = next((indexer for indexer in indexers if _clean_name(str(indexer.get("name", ""))) == "oldboys"), None)
    app_profile_id = _prowlarr_default_app_profile_id(session, prowlarr_url, prowlarr_key)

    if oldboys is None:
        schemas = _get_json(session, f"{prowlarr_url}/api/v1/indexer/schema", prowlarr_key)
        oldboys = copy.deepcopy(next((item for item in schemas if item.get("implementation") == "Newznab"), None))
        if not oldboys:
            record("auto_config.prowlarr_oldboys.create_skip_schema_missing")
            return 0
        oldboys.pop("id", None)
        oldboys["name"] = "OldBoys {DK}"
        oldboys["enable"] = True
        oldboys["priority"] = 25
        oldboys["appProfileId"] = app_profile_id
        _set_field(oldboys, "baseUrl", f"{ARR_PROXY_URL}/ob")
        _set_field(oldboys, "apiPath", "/api")
        _set_field(oldboys, "apiKey", proxy_key)
        try:
            _post_json(session, f"{prowlarr_url}/api/v1/indexer?forceSave=true", prowlarr_key, oldboys)
            changed += 1
            record("auto_config.prowlarr_oldboys.created", name="OldBoys {DK}", base_url=f"{ARR_PROXY_URL}/ob")
        except requests.RequestException as exc:
            record("auto_config.prowlarr_oldboys.create_failed", error=str(exc), error_type=type(exc).__name__)
            return 0
    else:
        oldboys["name"] = "OldBoys {DK}"
        oldboys["enable"] = True
        try:
            oldboys["priority"] = max(1, int(oldboys.get("priority") or 25))
        except (TypeError, ValueError):
            oldboys["priority"] = 25
        oldboys["appProfileId"] = app_profile_id
        _set_field(oldboys, "baseUrl", f"{ARR_PROXY_URL}/ob")
        _set_field(oldboys, "apiPath", "/api")
        _set_field(oldboys, "apiKey", proxy_key)
        try:
            _put_json(session, f"{prowlarr_url}/api/v1/indexer/{oldboys['id']}?forceSave=true", prowlarr_key, oldboys)
            changed += 1
            record("auto_config.prowlarr_oldboys.updated", indexer=oldboys.get("name"), base_url=f"{ARR_PROXY_URL}/ob")
        except requests.RequestException as exc:
            record(
                "auto_config.prowlarr_oldboys.update_failed",
                indexer=oldboys.get("name"),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # Clean up duplicate OldBoys entries left by previous manual or failed runs.
    for indexer in indexers:
        if oldboys.get("id") and indexer.get("id") == oldboys.get("id"):
            continue
        if _clean_name(str(indexer.get("name", ""))) != "oldboys":
            continue
        try:
            _delete(session, f"{prowlarr_url}/api/v1/indexer/{indexer['id']}", prowlarr_key)
            changed += 1
            record("auto_config.prowlarr_oldboys.duplicate_deleted", indexer=indexer.get("name"))
        except requests.RequestException as exc:
            record("auto_config.prowlarr_oldboys.duplicate_delete_failed", indexer=indexer.get("name"), error=str(exc), error_type=type(exc).__name__)

    if changed:
        try:
            _post_json(session, f"{prowlarr_url}/api/v1/command", prowlarr_key, {"name": "CheckHealth"})
        except requests.RequestException as exc:
            record("auto_config.prowlarr_oldboys.health_check_failed", error=str(exc), error_type=type(exc).__name__)
    record("auto_config.prowlarr_oldboys.complete", changed=changed)
    return changed


def _prowlarr_default_app_profile_id(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> int:
    try:
        profiles = _get_json(session, f"{prowlarr_url}/api/v1/appProfile", prowlarr_key)
    except requests.RequestException as exc:
        record("auto_config.prowlarr_app_profile.default_failed", error=str(exc), error_type=type(exc).__name__)
        return 1
    if isinstance(profiles, list):
        for profile in profiles:
            try:
                profile_id = int(profile.get("id") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if profile_id > 0:
                return profile_id
    record("auto_config.prowlarr_app_profile.default_missing")
    return 1


def _discover_arr_apps(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> list[ArrApp]:
    record("auto_config.discover.begin", prowlarr_url=prowlarr_url)
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
            record("auto_config.discover.default_url_retry", app=name, kind=kind, configured_url=url, default_url=default_url)
            url = default_url
            api_key = _first_working_arr_key(session, kind, name, url, prowlarr_app_key)
        if not api_key:
            print(f"[Core] Auto-Config: {name} API key unavailable or unauthorized", flush=True)
            record("auto_config.discover.skip_key_or_auth", app=name, kind=kind, url=url)
            continue
        discovered.append(ArrApp(name=name, kind=kind, slug=_slug(name), url=url.rstrip("/"), api_key=api_key))
        record("auto_config.discover.found", app=name, kind=kind, slug=_slug(name), url=url.rstrip("/"))
    record("auto_config.discover.complete", discovered_count=len(discovered), prowlarr_app_count=len(apps))
    return discovered


def _required_arr_app_names() -> set[str]:
    names = {"Radarr", "Sonarr"}
    if _enable_2160p_arrs():
        names.update({"Radarr 2160p", "Sonarr 2160p"})
    return names


def _missing_required_arr_apps(apps: list[ArrApp]) -> set[str]:
    discovered = {_slug(app.name) for app in apps}
    return {name for name in _required_arr_app_names() if _slug(name) not in discovered}


def _wait_for_arr_apps(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> list[ArrApp]:
    deadline = time.monotonic() + max(0, ARR_READY_TIMEOUT_SECONDS)
    attempt = 0
    last_apps: list[ArrApp] = []
    while True:
        attempt += 1
        _bootstrap_arr_apps(session, prowlarr_url, prowlarr_key)
        last_apps = _discover_arr_apps(session, prowlarr_url, prowlarr_key)
        missing = _missing_required_arr_apps(last_apps)
        if not missing:
            record("auto_config.wait_arrs.ready", attempt=attempt, discovered=[app.name for app in last_apps])
            return last_apps

        if time.monotonic() >= deadline:
            print(f"[Core] Auto-Config: Arr readiness timed out; missing: {', '.join(sorted(missing))}", flush=True)
            record("auto_config.wait_arrs.timeout", attempt=attempt, missing=sorted(missing), discovered=[app.name for app in last_apps])
            return last_apps

        print(f"[Core] Auto-Config: waiting for Arr API keys/routes: {', '.join(sorted(missing))}", flush=True)
        record("auto_config.wait_arrs.retry", attempt=attempt, missing=sorted(missing), retry_seconds=ARR_READY_RETRY_SECONDS)
        time.sleep(max(1, ARR_READY_RETRY_SECONDS))


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
            record("auto_config.arr_key.working", app=app_name, kind=kind, url=url, candidate_count=len(seen))
            return key
    record("auto_config.arr_key.none_working", app=app_name, kind=kind, url=url, candidate_count=len(seen))
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
                    record("auto_config.config_key.read", kind=kind, app=app_name, path=path)
            except Exception as exc:
                print(f"[Core] Auto-Config: could not read {path}: {exc}", flush=True)
                record("auto_config.config_key.read_failed", kind=kind, app=app_name, path=path, error=str(exc), error_type=type(exc).__name__)
    return keys


def _prowlarr_api_key() -> str:
    return (
        next(iter(_read_arr_config_keys("Prowlarr")), "")
        or _clean_env("PROWLARR_API_KEY")
        or _clean_env("PROWLARR_APIKEY")
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


def _seerr_url() -> str:
    return _clean_env("SEERR_URL").rstrip("/") or "http://seerr:5055"


def _seerr_config_path() -> Path | None:
    first_candidate = Path(SEERR_CONFIG_PATHS[0])
    for path in SEERR_CONFIG_PATHS:
        cfg = Path(path)
        if cfg.exists():
            return cfg
    for path in SEERR_CONFIG_PATHS:
        cfg = Path(path)
        if cfg.parent.exists():
            return cfg
    return first_candidate if first_candidate.parent.exists() else None


def _read_seerr_settings() -> dict[str, Any]:
    cfg = _seerr_config_path()
    if not cfg or not cfg.exists():
        return {}
    try:
        return json.loads(cfg.read_text())
    except Exception as exc:
        record("auto_config.seerr_settings.read_failed", path=str(cfg), error=str(exc), error_type=type(exc).__name__)
        return {}


def _write_seerr_settings(settings: dict[str, Any]) -> bool:
    cfg = _seerr_config_path()
    if not cfg:
        record("auto_config.seerr_settings.write_skipped", reason="missing_parent")
        return False
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.with_suffix(cfg.suffix + ".tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(cfg)
        record("auto_config.seerr_settings.written", path=str(cfg))
        return True
    except OSError as exc:
        record("auto_config.seerr_settings.write_failed", path=str(cfg), error=str(exc), error_type=type(exc).__name__)
        return False


def _seerr_db_path() -> Path | None:
    timeout = int(_clean_env("SEERR_DB_READY_TIMEOUT_SECONDS") or "90")
    deadline = time.time() + max(timeout, 0)
    while True:
        for path in SEERR_DB_PATHS:
            db = Path(path)
            if not db.exists():
                continue
            try:
                with sqlite3.connect(str(db), timeout=10) as conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "select name from sqlite_master where type='table' and name in ('user', 'user_settings')"
                        ).fetchall()
                    }
                if {"user", "user_settings"}.issubset(tables):
                    return db
            except sqlite3.Error as exc:
                record("auto_config.seerr_admin.db_probe_failed", path=str(db), error=str(exc), error_type=type(exc).__name__)
        if time.time() >= deadline:
            return None
        time.sleep(2)


def _seerr_admin_email() -> str:
    email = (_clean_env("SEERR_ADMIN_EMAIL") or "admin@danish.requests").strip().lower()
    return email or "admin@danish.requests"


def _seerr_admin_password() -> tuple[str, bool]:
    password = _clean_env("SEERR_ADMIN_PASSWORD")
    if password:
        return password, False

    password_file = Path(_clean_env("SEERR_ADMIN_PASSWORD_FILE") or SEERR_ADMIN_PASSWORD_FILE)
    try:
        if password_file.exists():
            existing = password_file.read_text().strip()
            if existing:
                return existing, False

        password = f"Danish-{secrets.token_urlsafe(18)}"
        password_file.parent.mkdir(parents=True, exist_ok=True)
        password_file.write_text(password + "\n")
        try:
            password_file.chmod(0o600)
        except OSError:
            pass
        record("auto_config.seerr_admin.password_generated", path=str(password_file))
        return password, True
    except OSError as exc:
        record("auto_config.seerr_admin.password_file_failed", path=str(password_file), error=str(exc), error_type=type(exc).__name__)
        return f"Danish-{secrets.token_urlsafe(18)}", True


def _ensure_seerr_admin_user() -> int:
    db = _seerr_db_path()
    if not db:
        record("auto_config.seerr_admin.skip_missing_db", paths=list(SEERR_DB_PATHS))
        return 0

    try:
        import bcrypt
    except ImportError as exc:
        record("auto_config.seerr_admin.skip_missing_bcrypt", error=str(exc), error_type=type(exc).__name__)
        return 0

    try:
        with sqlite3.connect(str(db), timeout=30) as conn:
            user_count = int(conn.execute("select count(*) from user").fetchone()[0])
            if user_count > 0:
                record("auto_config.seerr_admin.exists", path=str(db), users=user_count)
                return 0

            email = _seerr_admin_email()
            password, generated = _seerr_admin_password()
            password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
            conn.execute(
                """
                insert into user (id, email, username, permissions, avatar, password, userType)
                values (1, ?, ?, ?, ?, ?, ?)
                """,
                (email, "admin", SEERR_LOCAL_ADMIN_PERMISSIONS, "/avatar.png", password_hash, SEERR_LOCAL_USER_TYPE),
            )
            conn.execute(
                """
                insert into user_settings (locale, discoverRegion, streamingRegion, originalLanguage, userId)
                values (?, ?, ?, ?, 1)
                """,
                ("en", "", "DK", "da|en"),
            )
            conn.commit()
            record("auto_config.seerr_admin.created", path=str(db), email=email, generated_password=generated)
            if generated:
                print("[Core] Auto-Config: created Seerr admin; password saved to /config/seerr-admin-password.txt", flush=True)
            else:
                print(f"[Core] Auto-Config: created Seerr admin {email}", flush=True)
            return 1
    except sqlite3.Error as exc:
        record("auto_config.seerr_admin.failed", path=str(db), error=str(exc), error_type=type(exc).__name__)
        print(f"[Core] Auto-Config: failed to create Seerr admin: {exc}", flush=True)
        return 0


def _media_server_type() -> str:
    value = _clean_env("MEDIA_SERVER_TYPE").lower().strip()
    return value if value in {"plex", "jellyfin"} else ""


def _media_server_url() -> str:
    media_type = _media_server_type()
    default = "http://jellyfin:8096" if media_type == "jellyfin" else "http://plex:32400"
    return (_clean_env("MEDIA_SERVER_URL") or default).rstrip("/")


def _jellyfin_db_path() -> Path | None:
    for path in JELLYFIN_DB_PATHS:
        db = Path(path)
        if db.exists():
            return db
    return None


def _jellyfin_api_key() -> str:
    env_key = _clean_env("JELLYFIN_API_KEY") or _clean_env("JELLYFIN_APIKEY")
    if env_key:
        return env_key

    settings_key = str(_read_seerr_settings().get("jellyfin", {}).get("apiKey") or "").strip()
    if settings_key:
        return settings_key

    db = _jellyfin_db_path()
    if not db:
        record("auto_config.jellyfin_api_key.skip_missing_db", paths=list(JELLYFIN_DB_PATHS))
        return ""

    try:
        with sqlite3.connect(str(db), timeout=30) as conn:
            row = conn.execute(
                "select AccessToken from ApiKeys where Name = ? order by Id limit 1",
                (JELLYFIN_API_KEY_NAME,),
            ).fetchone()
            if row and row[0]:
                record("auto_config.jellyfin_api_key.existing", path=str(db), name=JELLYFIN_API_KEY_NAME)
                return str(row[0])

            token = secrets.token_hex(16)
            now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            conn.execute(
                "insert into ApiKeys (DateCreated, DateLastActivity, Name, AccessToken) values (?, ?, ?, ?)",
                (now, now, JELLYFIN_API_KEY_NAME, token),
            )
            conn.commit()
            record("auto_config.jellyfin_api_key.created", path=str(db), name=JELLYFIN_API_KEY_NAME)
            return token
    except sqlite3.Error as exc:
        record("auto_config.jellyfin_api_key.failed", path=str(db), error=str(exc), error_type=type(exc).__name__)
        return ""


def _jellyfin_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": (
            'MediaBrowser Client="Danish Intelligence", Device="Server", '
            f'DeviceId="danish-intelligence", Version="1.0", Token="{api_key}"'
        ),
    }


def _ensure_jellyfin_libraries(session: requests.Session) -> int:
    if _media_server_type() != "jellyfin":
        return 0

    api_key = _jellyfin_api_key()
    if not api_key:
        record("auto_config.jellyfin_libraries.skip_missing_key")
        return 0

    jellyfin_url = _media_server_url()
    headers = _jellyfin_headers(api_key)
    try:
        response = session.get(f"{jellyfin_url}/Library/VirtualFolders", headers=headers, timeout=20)
        response.raise_for_status()
        existing = response.json() if response.content else []
        existing_names = {str(item.get("Name") or "").lower() for item in existing if isinstance(item, dict)}
        created = 0

        libraries = list(JELLYFIN_DEFAULT_LIBRARIES)
        if _enable_2160p_arrs():
            libraries.extend(JELLYFIN_2160P_LIBRARIES)

        for name, collection_type, path in libraries:
            Path(path).mkdir(parents=True, exist_ok=True)
            if name.lower() in existing_names:
                continue
            params = urlencode({"name": name, "collectionType": collection_type, "refreshLibrary": "false"})
            payload = {"LibraryOptions": {"PathInfos": [{"Path": path}]}}
            create_response = session.post(
                f"{jellyfin_url}/Library/VirtualFolders?{params}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            create_response.raise_for_status()
            existing_names.add(name.lower())
            created += 1

        record("auto_config.jellyfin_libraries.complete", created=created, total=len(existing_names))
        return created
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"[Core] Auto-Config: Jellyfin library setup skipped: {exc}", flush=True)
        record("auto_config.jellyfin_libraries.failed", error=str(exc), error_type=type(exc).__name__)
        return 0


def _plex_config_root() -> Path | None:
    for path in PLEX_CONFIG_PATHS:
        root = Path(path)
        if root.exists():
            return root
    return None


def _plex_preferences() -> dict[str, str]:
    root = _plex_config_root()
    if not root:
        return {}
    prefs = root / "Preferences.xml"
    if not prefs.exists():
        return {}
    try:
        return dict(ET.parse(prefs).getroot().attrib)
    except ET.ParseError as exc:
        record("auto_config.plex_preferences.failed", path=str(prefs), error=str(exc), error_type=type(exc).__name__)
        return {}


def _plex_token() -> str:
    env_token = _clean_env("PLEX_TOKEN") or _clean_env("PLEX_ACCESS_TOKEN")
    if env_token:
        return env_token

    prefs = _plex_preferences()
    online_token = str(prefs.get("PlexOnlineToken") or "").strip()
    if online_token:
        record("auto_config.plex_token.discovered", source="preferences")
        return online_token

    root = _plex_config_root()
    if root:
        local_token_file = root / ".LocalAdminToken"
        try:
            local_token = local_token_file.read_text().strip() if local_token_file.exists() else ""
        except OSError:
            local_token = ""
        if local_token:
            record("auto_config.plex_token.discovered", source="local_admin")
            return local_token

    record("auto_config.plex_token.missing")
    return ""


def _plex_machine_id() -> str:
    return _clean_env("PLEX_MACHINE_ID") or str(_plex_preferences().get("MachineIdentifier") or "")


def _plex_headers(token: str) -> dict[str, str]:
    return {
        "X-Plex-Token": token,
        "X-Plex-Product": "Danish Intelligence",
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": "danish-intelligence",
        "X-Plex-Platform": "Linux",
    }


def _ensure_plex_libraries(session: requests.Session) -> int:
    if _media_server_type() != "plex":
        return 0

    token = _plex_token()
    if not token:
        record("auto_config.plex_libraries.skip_missing_token")
        return 0

    plex_url = _media_server_url()
    headers = _plex_headers(token)
    try:
        prefs_url = f"{plex_url}/:/prefs"
        prefs = _plex_preferences()
        if prefs.get("AcceptedEULA") in {"1", "true", "True"}:
            record("auto_config.plex_libraries.eula_already_accepted")
        else:
            prefs_response = session.put(
                prefs_url,
                params={"AcceptedEULA": "1"},
                headers=headers,
                timeout=20,
            )
            if prefs_response.status_code == 403:
                # Some claimed Plex installs reject this preference write even
                # while the same token can manage library sections.
                record("auto_config.plex_libraries.eula_accept_forbidden")
            else:
                _raise_for_status(prefs_response, "PUT", prefs_url)
                record("auto_config.plex_libraries.eula_accepted")

        sections_url = f"{plex_url}/library/sections"
        response = session.get(sections_url, headers=headers, timeout=20)
        _raise_for_status(response, "GET", sections_url)
        root = ET.fromstring(response.text)
        existing_names = {
            str(directory.attrib.get("title") or "").lower()
            for directory in root.findall("Directory")
        }
        created = 0

        libraries = list(PLEX_DEFAULT_LIBRARIES)
        if _enable_2160p_arrs():
            libraries.extend(PLEX_2160P_LIBRARIES)

        for name, library_type, path in libraries:
            Path(path).mkdir(parents=True, exist_ok=True)
            if name.lower() in existing_names:
                continue
            scanner = "Plex Movie" if library_type == "movie" else "Plex TV Series"
            agent = "tv.plex.agents.movie" if library_type == "movie" else "tv.plex.agents.series"
            create_url = f"{plex_url}/library/sections"
            create_response = session.post(
                create_url,
                params={
                    "type": library_type,
                    "name": name,
                    "scanner": scanner,
                    "agent": agent,
                    "language": "en-US",
                    "location": path,
                },
                headers=headers,
                timeout=30,
            )
            _raise_for_status(create_response, "POST", create_url)
            existing_names.add(name.lower())
            created += 1

        record("auto_config.plex_libraries.complete", created=created, total=len(existing_names))
        return created
    except (requests.RequestException, ET.ParseError, OSError) as exc:
        print(f"[Core] Auto-Config: Plex library setup skipped: {exc}", flush=True)
        record("auto_config.plex_libraries.failed", error=str(exc), error_type=type(exc).__name__)
        return 0


def _settings_host_block(url: str, default_port: int) -> tuple[str, int, bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or ""
    port = int(parsed.port or (443 if parsed.scheme == "https" else default_port))
    use_ssl = parsed.scheme == "https"
    url_base = parsed.path.rstrip("/")
    return host, port, use_ssl, url_base


def _seed_seerr_settings_file() -> bool:
    settings = _read_seerr_settings()
    if not settings:
        api_key = _clean_env("SEERR_API_KEY") or os.urandom(16).hex()
        settings = {
            "public": {"initialized": True},
            "main": {
                "apiKey": api_key,
                "applicationTitle": SEERR_TITLE,
                "applicationUrl": _clean_env("SEERR_APPLICATION_URL") or _clean_env("SEERR_EXTERNAL_URL"),
                "cacheImages": True,
                "defaultPermissions": SEERR_DEFAULT_PERMISSIONS,
                "defaultQuotas": {"movie": {}, "tv": {}},
                "hideAvailable": False,
                "hideBlocklisted": False,
                "localLogin": True,
                "mediaServerLogin": True,
                "newPlexLogin": False,
                "discoverRegion": "",
                "streamingRegion": "DK",
                "originalLanguage": "da|en",
                "blocklistRegion": "",
                "blocklistLanguage": "",
                "blocklistedTags": "",
                "blocklistedTagsLimit": 50,
                "mediaServerType": (
                    SEERR_MEDIA_SERVER_TYPE_JELLYFIN
                    if _media_server_type() == "jellyfin"
                    else SEERR_MEDIA_SERVER_TYPE_PLEX
                ),
                "partialRequestsEnabled": True,
                "enableSpecialEpisodes": False,
                "locale": "en",
                "youtubeUrl": "",
            },
            "plex": {"name": "", "ip": "", "port": 32400, "useSsl": False, "libraries": []},
            "jellyfin": {
                "name": "",
                "ip": "",
                "port": 8096,
                "useSsl": False,
                "urlBase": "",
                "externalHostname": "",
                "jellyfinForgotPasswordUrl": "",
                "libraries": [],
                "serverId": "",
                "apiKey": "",
            },
            "radarr": [],
            "sonarr": [],
        }

    public = settings.setdefault("public", {})
    main = settings.setdefault("main", {})
    changed = False

    if not public.get("initialized"):
        public["initialized"] = True
        changed = True
    for key, value in {
        "applicationTitle": SEERR_TITLE,
        "cacheImages": True,
        "defaultPermissions": SEERR_DEFAULT_PERMISSIONS,
        "streamingRegion": "DK",
        "originalLanguage": "da|en",
        "localLogin": True,
        "mediaServerLogin": True,
        "newPlexLogin": False,
    }.items():
        if main.get(key) != value:
            main[key] = value
            changed = True

    media_type = _media_server_type()
    if media_type:
        wanted_type = (
            SEERR_MEDIA_SERVER_TYPE_JELLYFIN
            if media_type == "jellyfin"
            else SEERR_MEDIA_SERVER_TYPE_PLEX
        )
        if main.get("mediaServerType") != wanted_type:
            main["mediaServerType"] = wanted_type
            changed = True

    if _seed_seerr_media_server_block(settings):
        changed = True

    return _write_seerr_settings(settings) if changed or not _seerr_config_path() or not _seerr_config_path().exists() else True


def _seed_seerr_media_server_block(settings: dict[str, Any]) -> bool:
    media_type = _media_server_type()
    if not media_type:
        return False

    host, port, use_ssl, url_base = _settings_host_block(_media_server_url(), 8096 if media_type == "jellyfin" else 32400)
    external = _clean_env("MEDIA_SERVER_EXTERNAL_URL")
    changed = False

    if media_type == "jellyfin":
        jellyfin = settings.setdefault("jellyfin", {})
        api_key = _jellyfin_api_key() or str(jellyfin.get("apiKey") or "")
        values = {
            "name": jellyfin.get("name") or "Danish Jellyfin",
            "ip": host,
            "port": port,
            "useSsl": use_ssl,
            "urlBase": url_base,
            "externalHostname": external or str(jellyfin.get("externalHostname") or ""),
            "jellyfinForgotPasswordUrl": str(jellyfin.get("jellyfinForgotPasswordUrl") or ""),
            "libraries": jellyfin.get("libraries") or [],
            "serverId": str(jellyfin.get("serverId") or ""),
            "apiKey": api_key,
        }
        for key, value in values.items():
            if jellyfin.get(key) != value:
                jellyfin[key] = value
                changed = True
    elif media_type == "plex":
        plex = settings.setdefault("plex", {})
        token = _plex_token() or str(plex.get("accessToken") or "")
        values = {
            "name": _clean_env("PLEX_SERVER_NAME") or str(plex.get("name") or "Danish Plex"),
            "machineId": _plex_machine_id() or str(plex.get("machineId") or ""),
            "ip": host,
            "port": port,
            "useSsl": use_ssl,
            "libraries": plex.get("libraries") or [],
            "accessToken": token,
        }
        web_app_url = _clean_env("PLEX_WEB_APP_URL")
        if web_app_url:
            values["webAppUrl"] = web_app_url
        for key, value in values.items():
            if plex.get(key) != value:
                plex[key] = value
                changed = True
    return changed


def _arr_reachable(session: requests.Session, url: str, api_key: str) -> bool:
    try:
        resp = _request(session, "GET", f"{url.rstrip('/')}/api/v3/system/status", api_key, timeout=8)
        ok = resp.ok
        record("auto_config.arr_reachable", url=url, status=resp.status_code, ok=ok)
        return ok
    except requests.RequestException as exc:
        record("auto_config.arr_reachable_error", url=url, error=str(exc), error_type=type(exc).__name__)
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
    nordic_subtitle_title = (
        rf"(?:\[Danish Subtitles\]|{re.escape(DK_SUBS_TITLE)}\b|{re.escape(LEGACY_DK_SUBS_TITLE)}\b"
        r"|(?:^|[._\-\s])NORD(?:iC|IC)(?:[._\-\s]|$)"
        r"|(?:^|[._\-\s])NorTekst(?:[._\-\s]|$))"
    )
    return [
        _cf_payload(CF_DANISH_AUDIO, rf"(?:\[Danish Audio\]|{re.escape(DK_AUDIO_TITLE)}\b|{re.escape(LEGACY_DK_AUDIO_TITLE)}\b)", include_rename=True),
        _cf_payload(CF_DANISH_SUBTITLES, nordic_subtitle_title, include_rename=True),
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
    record("auto_config.paint_formats.begin", app=app.name, kind=app.kind, url=app.url)
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
        wanted_profiles = {audio_profile, subtitles_profile}
        for profile in _get_json(session, f"{api}/qualityprofile", app.api_key):
            profile_id = profile.get("id")
            profile_name = str(profile.get("name", ""))
            if profile_id is None or profile_name in wanted_profiles:
                continue
            try:
                _delete(session, f"{api}/qualityprofile/{profile_id}", app.api_key)
                record("auto_config.quality_profile.pruned", app=app.name, profile=profile_name)
            except requests.RequestException as exc:
                record(
                    "auto_config.quality_profile.prune_failed",
                    app=app.name,
                    profile=profile_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    result = (len(cf_ids), 2 if profiles else 0)
    record("auto_config.paint_formats.complete", app=app.name, custom_formats=result[0], profiles=result[1])
    return result


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


def _quality_or_group_id(item: dict[str, Any]) -> int | None:
    quality_group = item.get("qualityGroup") or {}
    if quality_group.get("id") is not None:
        return int(quality_group["id"])
    return _quality_id(item)


def _allowed_cutoff_ids(profile: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    child_ids: list[int] = []
    for item in profile.get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("allowed"):
            cutoff_id = _quality_or_group_id(item)
            if cutoff_id is not None:
                ids.append(cutoff_id)
        for child in item.get("items", []):
            if not isinstance(child, dict) or not child.get("allowed"):
                continue
            cutoff_id = _quality_or_group_id(child)
            if cutoff_id is not None:
                child_ids.append(cutoff_id)
    return ids or child_ids


def _set_profile_cutoff_to_allowed(profile: dict[str, Any]) -> None:
    allowed_ids = _allowed_cutoff_ids(profile)
    if allowed_ids:
        profile["cutoff"] = allowed_ids[-1]


def _force_2160p_quality_items(profile: dict[str, Any]) -> None:
    for item in profile.get("items", []):
        if not isinstance(item, dict):
            continue
        children = [child for child in item.get("items", []) if isinstance(child, dict)]
        if children:
            child_allowed = []
            for child in children:
                child["allowed"] = "2160" in _quality_name(child).lower()
                child_allowed.append(child["allowed"])
            item["allowed"] = any(child_allowed)
            continue
        item["allowed"] = "2160" in _quality_name(item).lower()
    _set_profile_cutoff_to_allowed(profile)


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
    _set_profile_cutoff_to_allowed(profile)


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


def _upsert_profile(session: requests.Session, app: ArrApp, profiles: list[dict[str, Any]], name: str, scores: dict[str, int], cf_ids: dict[str, int], valid_cf_ids: set[int]) -> None:
    existing = next((profile for profile in profiles if profile.get("name") == name), None)
    profile = copy.deepcopy(existing or profiles[0])
    if existing is None:
        profile.pop("id", None)
    profile["name"] = name
    profile["upgradeAllowed"] = True
    profile["minFormatScore"] = 10000
    profile["cutoffFormatScore"] = 0
    if "language" in profile:
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
    record("auto_config.rewire_indexers.begin", app=app.name, kind=app.kind, prowlarr_indexer_count=len(prowlarr_indexers))
    api = f"{app.url}/api/v3"
    targets = _managed_prowlarr_targets(prowlarr_indexers, app)
    arr_indexers = _get_json(session, f"{api}/indexer", app.api_key)

    by_clean_name: dict[str, list[dict[str, Any]]] = {}
    for indexer in arr_indexers:
        base_name = _clean_name(indexer.get("name", ""))
        if base_name in targets:
            by_clean_name.setdefault(base_name, []).append(indexer)

    schema: dict[str, Any] | None = None
    linked = 0
    created = 0
    deleted = 0

    for clean_name, target in targets.items():
        candidates = by_clean_name.get(clean_name, [])
        if candidates:
            target_url = target["baseUrl"].rstrip("/")
            candidates.sort(key=lambda indexer: (
                str(_field(indexer, "baseUrl", "")).rstrip("/") != target_url,
                "(prowlarr)" in str(indexer.get("name", "")).lower(),
                int(indexer.get("id") or 0),
            ))
            canonical = candidates[0]
            _set_indexer_target_fields(canonical, app, target, prowlarr_key)
            _put_json(session, f"{api}/indexer/{canonical['id']}?forceSave=true", app.api_key, canonical)
            for duplicate in candidates[1:]:
                _delete(session, f"{api}/indexer/{duplicate['id']}", app.api_key)
                deleted += 1
        else:
            schema = schema or _newznab_schema(session, app)
            if not schema:
                record("auto_config.rewire_indexers.schema_missing", app=app.name, target=target["name"])
                continue
            canonical = copy.deepcopy(schema)
            canonical.pop("id", None)
            _set_indexer_target_fields(canonical, app, target, prowlarr_key)
            try:
                _post_json(session, f"{api}/indexer?forceSave=true", app.api_key, canonical)
                created += 1
            except requests.RequestException as exc:
                record(
                    "auto_config.rewire_indexers.create_failed",
                    app=app.name,
                    target=target["name"],
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
        linked += 1
    record(
        "auto_config.rewire_indexers.complete",
        app=app.name,
        linked=linked,
        created=created,
        deleted=deleted,
        arr_indexer_count=len(arr_indexers),
        target_count=len(targets),
    )
    return linked


def _paint_naming(session: requests.Session, app: ArrApp) -> None:
    record("auto_config.naming.begin", app=app.name, kind=app.kind)
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
    record("auto_config.naming.complete", app=app.name, kind=app.kind)


def _paint_indexer_config(session: requests.Session, app: ArrApp) -> None:
    """Whitelist proxy markers that Radarr can otherwise misread as hardcoded subs."""
    record("auto_config.indexer_config.begin", app=app.name, kind=app.kind)
    api = f"{app.url}/api/v3"
    try:
        config = _get_json(session, f"{api}/config/indexer", app.api_key)
    except requests.RequestException as exc:
        print(f"[Core] Auto-Config: {app.name} indexer config skipped: {exc}", flush=True)
        record("auto_config.indexer_config.skipped", app=app.name, error=str(exc), error_type=type(exc).__name__)
        return

    if "whitelistedHardcodedSubs" not in config:
        record("auto_config.indexer_config.unsupported", app=app.name)
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
    changed = new_value != config.get("whitelistedHardcodedSubs")
    if changed:
        config["whitelistedHardcodedSubs"] = new_value
        _put_json(session, f"{api}/config/indexer", app.api_key, config)
    record("auto_config.indexer_config.complete", app=app.name, changed=changed)


def _ensure_root_folders(session: requests.Session, app: ArrApp) -> int:
    record("auto_config.root_folders.begin", app=app.name, kind=app.kind)
    api = f"{app.url}/api/v3"
    existing = _get_json(session, f"{api}/rootfolder", app.api_key)
    existing_paths = {f.get("path", "").rstrip("/") for f in existing}
    
    if _is_2160p_instance(app.name, app.url):
        radarr_paths = ["/media/movies-2160p"]
        sonarr_paths = ["/media/tv-2160p"]
    else:
        radarr_paths = [
            "/media/movies",
            "/media/kids-movies",
            "/media/danish-movies",
            "/media/documentaries",
            "/media/christmas-movies",
            "/media/classics",
        ]
        sonarr_paths = [
            "/media/tv",
            "/media/kids-tv",
            "/media/danish-tv",
            "/media/documentary-series",
            "/media/christmas-tv",
        ]
    
    target_paths = radarr_paths if app.kind == "Radarr" else sonarr_paths
    added = 0
    
    for path in target_paths:
        if path not in existing_paths:
            try:
                _ensure_physical_media_path(path)
                _post_json(session, f"{api}/rootfolder", app.api_key, {"path": path})
                added += 1
            except requests.RequestException as e:
                print(f"[Core] Auto-Config: Failed to add root folder {path}: {e}", flush=True)
                record("auto_config.root_folders.add_failed", app=app.name, path=path, error=str(e), error_type=type(e).__name__)
            
    record("auto_config.root_folders.complete", app=app.name, added=added, target_paths=target_paths)
    return added


def _ensure_physical_media_path(path: str) -> None:
    """Create clean-server media subfolders before asking Arrs to add them.

    Radarr/Sonarr reject root folders that do not exist inside their container.
    The Danish Intelligence container sees the same /media bind, so creating the
    path here makes it visible to the Arr containers without remote mappings.
    """
    try:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        uid = _clean_env("PUID")
        gid = _clean_env("PGID")
        if uid.isdigit() and gid.isdigit():
            os.chown(target, int(uid), int(gid))
        target.chmod(0o2775)
        record("auto_config.root_folders.path_ready", path=path, uid=uid if uid.isdigit() else "", gid=gid if gid.isdigit() else "")
    except OSError as exc:
        record("auto_config.root_folders.path_create_failed", path=path, error=str(exc), error_type=type(exc).__name__)
        raise requests.RequestException(f"could not create physical media path {path}: {exc}") from exc

def _ensure_download_client(session: requests.Session, app: ArrApp) -> int:
    record("auto_config.download_client.begin", app=app.name, kind=app.kind)
    api = f"{app.url}/api/v3"
    clients = _get_json(session, f"{api}/downloadclient", app.api_key)
    proxy_api_key = os.getenv("PROXY_API_KEY", "")
    category = _altmount_download_category(app)
    
    for client in clients:
        if _is_altmount_proxy_client(client):
            client["priority"] = 1
            _set_field(client, "host", PROXY_HOST)
            _set_field(client, "port", PROXY_PORT)
            _set_field(client, "useSsl", PROXY_USE_SSL)
            _set_field(client, "urlBase", "/altmount")
            _set_field(client, "priority", 1)
            _set_field(client, "movieCategory" if app.kind == "Radarr" else "tvCategory", category)
            _set_field(client, "recentMoviePriority" if app.kind == "Radarr" else "recentTvPriority", 1)
            _set_field(client, "olderMoviePriority" if app.kind == "Radarr" else "olderTvPriority", 1)
            if proxy_api_key:
                _set_field(client, "apiKey", proxy_api_key)
            _put_json(session, f"{api}/downloadclient/{client['id']}?forceSave=true", app.api_key, client)
            record("auto_config.download_client.updated", app=app.name, category=category, host=PROXY_HOST, port=PROXY_PORT)
            return 1

    if not proxy_api_key:
        print("[Core] Auto-Config: Cannot create AltMount client; PROXY_API_KEY is missing.", flush=True)
        record("auto_config.download_client.skip_missing_proxy_key", app=app.name)
        return 0

    new_client = {
        "enable": True,
        "name": "AltMount",
        "implementation": "Sabnzbd",
        "configContract": "SabnzbdSettings",
        "priority": 1,
        "fields": [
            {"name": "host", "value": PROXY_HOST},
            {"name": "port", "value": PROXY_PORT},
            {"name": "useSsl", "value": PROXY_USE_SSL},
            {"name": "urlBase", "value": "/altmount"},
            {"name": "apiKey", "value": proxy_api_key},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "priority", "value": 1},
            {"name": "movieCategory" if app.kind == "Radarr" else "tvCategory", "value": category},
            {"name": "recentMoviePriority" if app.kind == "Radarr" else "recentTvPriority", "value": 1},
            {"name": "olderMoviePriority" if app.kind == "Radarr" else "olderTvPriority", "value": 1}
        ]
    }
    _post_json(session, f"{api}/downloadclient?forceSave=true", app.api_key, new_client)
    record("auto_config.download_client.created", app=app.name, category=category, host=PROXY_HOST, port=PROXY_PORT)
    return 1


def _ensure_marker_webhook(session: requests.Session, app: ArrApp) -> int:
    record("auto_config.webhook.begin", app=app.name, kind=app.kind)
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
            record("auto_config.webhook.schema_missing", app=app.name)
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
        record("auto_config.webhook.updated", app=app.name, url=webhook_url)
    else:
        _post_json(session, f"{api}/notification?forceSave=true", app.api_key, payload)
        record("auto_config.webhook.created", app=app.name, url=webhook_url)
    return 1


def _altmount_arr_instance(app: ArrApp) -> dict[str, Any]:
    return {
        "name": app.name,
        "type": app.kind.lower(),
        "url": app.url.rstrip("/"),
        "api_key": app.api_key,
        "category": _altmount_download_category(app),
        "enabled": True,
    }


def _safe_altmount_instance(instance: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in instance.items() if key != "api_key"}


def _ensure_altmount_arr_management(session: requests.Session, apps: list[ArrApp]) -> int:
    """Populate AltMount's own ARR Management instance list.

    The Arrs already get an AltMount download client above. This configures the
    opposite direction: AltMount knowing which Arr APIs it may use for media
    synchronization, queue cleanup, and repair checks.
    """
    record("auto_config.altmount_arrs.begin", apps=[app.name for app in apps])
    api_key = _altmount_api_key()
    if not api_key:
        record("auto_config.altmount_arrs.skip_missing_key")
        return 0

    radarr_instances = [_altmount_arr_instance(app) for app in apps if app.kind == "Radarr"]
    sonarr_instances = [_altmount_arr_instance(app) for app in apps if app.kind == "Sonarr"]
    if not radarr_instances and not sonarr_instances:
        record("auto_config.altmount_arrs.skip_no_instances")
        return 0

    try:
        response = _get_json(session, _altmount_api_url("/config", api_key), api_key)
        config = response.get("data", response) if isinstance(response, dict) else response
        if not isinstance(config, dict):
            raise RuntimeError("AltMount config response did not contain an object")

        before_config = copy.deepcopy(config)
        desired_mount_path = _desired_altmount_mount_path()
        desired_import_dir = _desired_altmount_import_dir()
        desired_complete_dir = _desired_altmount_complete_dir()
        desired_health_library_dir = _desired_altmount_health_library_dir()
        desired_import_strategy = _desired_altmount_import_strategy()

        config["mount_path"] = desired_mount_path
        fuse = config.setdefault("fuse", {})
        if isinstance(fuse, dict):
            fuse["enabled"] = True
            fuse["mount_path"] = desired_mount_path

        sabnzbd = config.setdefault("sabnzbd", {})
        if isinstance(sabnzbd, dict):
            sabnzbd["enabled"] = True
            sabnzbd["complete_dir"] = desired_complete_dir

        import_config = config.setdefault("import", {})
        if isinstance(import_config, dict):
            import_config["import_strategy"] = desired_import_strategy
            if desired_import_strategy != "NONE":
                _ensure_altmount_import_dir_path(desired_import_dir)
                import_config["import_dir"] = desired_import_dir

        health = config.setdefault("health", {})
        if isinstance(health, dict):
            health["enabled"] = True
            health["library_dir"] = desired_health_library_dir

        arrs = config.setdefault("arrs", {})
        arrs["enabled"] = True
        arrs["webhook_base_url"] = arrs.get("webhook_base_url") or _altmount_url()
        arrs["radarr_instances"] = radarr_instances
        arrs["sonarr_instances"] = sonarr_instances
        for key in ("lidarr_instances", "readarr_instances", "whisparr_instances"):
            arrs.setdefault(key, [])

        changed = config != before_config
        if changed:
            _put_json(session, _altmount_api_url("/config", api_key), api_key, config)
            try:
                _request_json(session, "POST", _altmount_api_url("/config/reload", api_key), api_key, timeout=20)
            except requests.RequestException as exc:
                record("auto_config.altmount_arrs.reload_failed", error=str(exc), error_type=type(exc).__name__)

        try:
            _request_json(session, "POST", _altmount_api_url("/arrs/webhook/register", api_key), api_key, timeout=30)
            record("auto_config.altmount_arrs.webhooks_registered")
        except requests.RequestException as exc:
            record("auto_config.altmount_arrs.webhooks_failed", error=str(exc), error_type=type(exc).__name__)

        record(
            "auto_config.altmount_arrs.complete",
            changed=changed,
            import_strategy=desired_import_strategy,
            import_dir=desired_import_dir,
            mount_path=desired_mount_path,
            complete_dir=desired_complete_dir,
            health_library_dir=desired_health_library_dir,
            radarr_instances=[_safe_altmount_instance(item) for item in radarr_instances],
            sonarr_instances=[_safe_altmount_instance(item) for item in sonarr_instances],
        )
        return len(radarr_instances) + len(sonarr_instances)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"[Core] Auto-Config: AltMount ARR Management skipped: {exc}", flush=True)
        record("auto_config.altmount_arrs.failed", error=str(exc), error_type=type(exc).__name__)
        return 0


def _profile_id_by_name(session: requests.Session, app: ArrApp, profile_name: str) -> int | None:
    profiles = _get_json(session, f"{app.url}/api/v3/qualityprofile", app.api_key)
    profile = next((item for item in profiles if item.get("name") == profile_name), None)
    return int(profile["id"]) if profile and profile.get("id") is not None else None


def _seerr_server_url(app: ArrApp) -> str:
    parsed = urlparse(app.url)
    base_url = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "http", parsed.netloc, base_url, "", "", ""))


def _seerr_external_url(app: ArrApp) -> str:
    prefix = _env_prefix(app.slug)
    return (
        _clean_env(f"{prefix}_EXTERNAL_URL")
        or _clean_env(f"{app.kind.upper()}_EXTERNAL_URL")
        or ""
    ).rstrip("/")


def _seerr_base_payload(app: ArrApp, name: str, profile_name: str, profile_id: int, root_path: str, is_default: bool) -> dict[str, Any]:
    parsed = urlparse(app.url)
    hostname = parsed.hostname or app.slug
    port = parsed.port or (443 if parsed.scheme == "https" else (8989 if app.kind == "Sonarr" else 7878))
    base_url = parsed.path.rstrip("/")

    payload: dict[str, Any] = {
        "name": name,
        "hostname": hostname,
        "port": port,
        "apiKey": app.api_key,
        "useSsl": parsed.scheme == "https",
        "baseUrl": base_url,
        "url": _seerr_server_url(app),
        "activeProfileId": profile_id,
        "activeProfileName": profile_name,
        "activeDirectory": root_path,
        "is4k": _is_2160p_instance(app.name, app.url),
        "isDefault": is_default,
        "externalUrl": _seerr_external_url(app),
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


def _seerr_targets(app: ArrApp) -> tuple[tuple[str, str, str, bool], ...]:
    if app.kind == "Radarr":
        return SEERR_2160P_MOVIE_ROOTS if _is_2160p_instance(app.name, app.url) else SEERR_DEFAULT_MOVIE_ROOTS
    return SEERR_2160P_TV_ROOTS if _is_2160p_instance(app.name, app.url) else SEERR_DEFAULT_TV_ROOTS


def _seerr_profile_name(app: ArrApp, profile_kind: str) -> str:
    audio_profile, subtitles_profile = _danish_profile_names(app)
    return audio_profile if profile_kind == "audio" else subtitles_profile


def _seerr_is_api_ready(session: requests.Session, seerr_url: str, seerr_key: str) -> bool:
    try:
        public = _get_json(session, f"{seerr_url}/api/v1/settings/public", seerr_key)
        if isinstance(public, dict) and public.get("initialized"):
            return True
    except requests.RequestException as exc:
        record("auto_config.seerr.public_failed", error=str(exc), error_type=type(exc).__name__)
        return False

    seeded = _seed_seerr_settings_file()
    record("auto_config.seerr.seeded_settings", seeded=seeded)
    return False


def _ensure_seerr_servers(session: requests.Session, apps: list[ArrApp]) -> int:
    record("auto_config.seerr.begin", apps=[app.name for app in apps])
    seerr_key = _seerr_api_key()
    if not seerr_key:
        if _seed_seerr_settings_file():
            seerr_key = _seerr_api_key()
    if not seerr_key:
        print("[Core] Auto-Config: Seerr API key unavailable; Seerr entries skipped", flush=True)
        record("auto_config.seerr.skip_missing_key")
        return 0

    seerr_url = _seerr_url()
    payloads = _build_seerr_payloads(session, apps)
    if not _seerr_is_api_ready(session, seerr_url, seerr_key):
        return _write_seerr_servers_to_file(payloads, needs_restart=True)

    _seed_seerr_media_server_via_api(session, seerr_url, seerr_key)
    changed = 0
    try:
        existing_by_endpoint = {
            "radarr": _get_json(session, f"{seerr_url}/api/v1/settings/radarr", seerr_key),
            "sonarr": _get_json(session, f"{seerr_url}/api/v1/settings/sonarr", seerr_key),
        }
    except requests.RequestException as exc:
        print(f"[Core] Auto-Config: Seerr API locked; writing Seerr settings file instead: {exc}", flush=True)
        record("auto_config.seerr.settings_failed", error=str(exc), error_type=type(exc).__name__)
        return _write_seerr_servers_to_file(payloads, needs_restart=True)

    for endpoint, endpoint_payloads in payloads.items():
        existing = existing_by_endpoint.get(endpoint) or []
        for payload in endpoint_payloads:
            match = next((
                item for item in existing
                if bool(item.get("is4k")) is bool(payload["is4k"])
                and str(item.get("hostname", "")).lower() == payload["hostname"].lower()
                and int(item.get("port", 0) or 0) == int(payload["port"])
                and item.get("activeDirectory") == payload["activeDirectory"]
                and (item.get("activeProfileName") == payload["activeProfileName"] or item.get("name") == payload["name"])
            ), None)

            try:
                if match:
                    update_payload = {key: value for key, value in payload.items() if key != "id"}
                    _put_json(session, f"{seerr_url}/api/v1/settings/{endpoint}/{match['id']}", seerr_key, update_payload)
                else:
                    created = _post_json(session, f"{seerr_url}/api/v1/settings/{endpoint}", seerr_key, payload)
                    if isinstance(created, dict):
                        existing.append(created)
                changed += 1
            except requests.RequestException as exc:
                print(f"[Core] Auto-Config: failed to upsert Seerr {payload['name']}: {exc}", flush=True)
                record("auto_config.seerr.upsert_failed", endpoint=endpoint, name=payload["name"], error=str(exc), error_type=type(exc).__name__)

    record("auto_config.seerr.complete", changed=changed)
    return changed


def _build_seerr_payloads(session: requests.Session, apps: list[ArrApp]) -> dict[str, list[dict[str, Any]]]:
    payloads: dict[str, list[dict[str, Any]]] = {"radarr": [], "sonarr": []}
    for app in apps:
        endpoint = "radarr" if app.kind == "Radarr" else "sonarr"
        for name, root_path, profile_kind, is_default in _seerr_targets(app):
            profile_name = _seerr_profile_name(app, profile_kind)
            profile_id = _profile_id_by_name(session, app, profile_name)
            if profile_id is None:
                print(f"[Core] Auto-Config: {app.name} profile {profile_name} unavailable; Seerr entry skipped", flush=True)
                record("auto_config.seerr.profile_missing", app=app.name, profile=profile_name)
                continue
            payloads[endpoint].append(_seerr_base_payload(app, name, profile_name, profile_id, root_path, is_default))
    return payloads


def _write_seerr_servers_to_file(payloads: dict[str, list[dict[str, Any]]], needs_restart: bool) -> int:
    settings = _read_seerr_settings()
    if not settings:
        _seed_seerr_settings_file()
        settings = _read_seerr_settings()
    settings.setdefault("public", {})["initialized"] = True
    settings.setdefault("main", {}).update({
        "applicationTitle": SEERR_TITLE,
        "cacheImages": True,
        "defaultPermissions": 48,
        "streamingRegion": "DK",
        "originalLanguage": "da|en",
        "localLogin": True,
        "mediaServerLogin": True,
        "newPlexLogin": False,
    })
    _seed_seerr_media_server_block(settings)

    total = 0
    for endpoint in ("radarr", "sonarr"):
        items = []
        for item_id, payload in enumerate(payloads.get(endpoint, [])):
            item = copy.deepcopy(payload)
            item["id"] = item_id
            items.append(item)
        settings[endpoint] = items
        total += len(items)

    if not _write_seerr_settings(settings):
        return 0
    if needs_restart:
        print("[Core] Auto-Config: Seerr settings file updated; restart Seerr once to load protected settings", flush=True)
    record(
        "auto_config.seerr.file_complete",
        radarr=len(settings.get("radarr") or []),
        sonarr=len(settings.get("sonarr") or []),
        needs_restart=needs_restart,
    )
    return total


def _seed_seerr_media_server_via_api(session: requests.Session, seerr_url: str, seerr_key: str) -> None:
    media_type = _media_server_type()
    if not media_type:
        return
    if media_type == "jellyfin":
        api_key = _jellyfin_api_key()
        if not api_key:
            record("auto_config.seerr.media_server.skip_missing_jellyfin_key")
            return
        try:
            host, port, use_ssl, url_base = _settings_host_block(_media_server_url(), 8096)
            payload = {
                "ip": host,
                "port": port,
                "useSsl": use_ssl,
                "urlBase": url_base,
                "apiKey": api_key,
                "externalHostname": _clean_env("MEDIA_SERVER_EXTERNAL_URL"),
                "jellyfinForgotPasswordUrl": _clean_env("JELLYFIN_FORGOT_PASSWORD_URL"),
            }
            _post_json(session, f"{seerr_url}/api/v1/settings/jellyfin", seerr_key, payload)
            record("auto_config.seerr.media_server.jellyfin_complete")
        except requests.RequestException as exc:
            record("auto_config.seerr.media_server.jellyfin_failed", error=str(exc), error_type=type(exc).__name__)
    elif media_type == "plex":
        token = _plex_token()
        try:
            host, port, use_ssl, _url_base = _settings_host_block(_media_server_url(), 32400)
            payload = {
                "ip": host,
                "port": port,
                "useSsl": use_ssl,
            }
            if token:
                payload["accessToken"] = token
            _post_json(session, f"{seerr_url}/api/v1/settings/plex", seerr_key, payload)
            record("auto_config.seerr.media_server.plex_complete")
        except requests.RequestException as exc:
            record("auto_config.seerr.media_server.plex_failed", error=str(exc), error_type=type(exc).__name__)


def _harden_prowlarr_app_sync(session: requests.Session, prowlarr_url: str, prowlarr_key: str) -> None:
    record("auto_config.prowlarr_app_harden.begin", prowlarr_url=prowlarr_url)
    apps = _get_json(session, f"{prowlarr_url}/api/v1/applications", prowlarr_key)
    changed = 0
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
        changed += 1
    record("auto_config.prowlarr_app_harden.complete", changed=changed)


def paint() -> dict[str, int]:
    prowlarr_url = os.getenv("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
    record(
        "auto_config.paint.begin",
        prowlarr_url=prowlarr_url,
        enable_2160p=_enable_2160p_arrs(),
        env=safe_env([
            "PROWLARR_URL",
            "PROWLARR_API_KEY",
            "PROXY_URL",
            "ARR_PROXY_URL",
            "RADARR_URL",
            "SONARR_URL",
            "RADARR_2160P_URL",
            "SONARR_2160P_URL",
            "SEERR_URL",
            "SEERR_ADMIN_EMAIL",
            "SEERR_ADMIN_PASSWORD",
            "SEERR_ADMIN_PASSWORD_FILE",
            "JELLYFIN_API_KEY",
            "PLEX_TOKEN",
            "ENABLE_2160P_ARRS",
            "PROXY_API_KEY",
        ]),
        paths=path_state([
            "/arr-config/prowlarr/config.xml",
            "/arr-config/radarr/config.xml",
            "/arr-config/sonarr/config.xml",
            "/arr-config/radarr-2160p/config.xml",
            "/arr-config/sonarr-2160p/config.xml",
            "/seerr-config/settings.json",
            "/seerr-config/db/db.sqlite3",
            "/jellyfin-config/data/data/jellyfin.db",
            "/plex-config/Library/Application Support/Plex Media Server/Preferences.xml",
            "/plex-config/Library/Application Support/Plex Media Server/.LocalAdminToken",
            "/media",
            "/mnt",
        ]),
    )
    prowlarr_key = _prowlarr_api_key()
    if not prowlarr_key:
        record("auto_config.paint.missing_prowlarr_key")
        raise RuntimeError("Prowlarr API key is not set and no mounted Prowlarr config.xml was found")

    session = requests.Session()
    prowlarr_indexers = _get_json(session, f"{prowlarr_url}/api/v1/indexer", prowlarr_key)
    record("auto_config.paint.indexers_loaded", count=len(prowlarr_indexers))
    if _ensure_prowlarr_oldboys_proxy_key(session, prowlarr_url, prowlarr_key):
        prowlarr_indexers = _get_json(session, f"{prowlarr_url}/api/v1/indexer", prowlarr_key)
        record("auto_config.paint.indexers_reloaded", count=len(prowlarr_indexers))
    apps = _wait_for_arr_apps(session, prowlarr_url, prowlarr_key)
    if not apps:
        record("auto_config.paint.no_apps")
        raise RuntimeError("No reachable Radarr/Sonarr applications found in Prowlarr")
    _sync_prowlarr_indexers(session, prowlarr_url, prowlarr_key)

    totals = {"apps": 0, "custom_formats": 0, "profiles": 0, "linked_indexers": 0, "download_clients": 0, "root_folders": 0, "webhooks": 0, "seerr_admin": 0, "seerr_servers": 0, "altmount_arr_instances": 0, "jellyfin_libraries": 0, "plex_libraries": 0}
    for app in apps:
        record("auto_config.app.begin", app=app.name, kind=app.kind, slug=app.slug, url=app.url)
        _paint_naming(session, app)
        _paint_indexer_config(session, app)
        root_folders = _ensure_root_folders(session, app)
        cf_count, profile_count = _paint_formats_and_profiles(session, app)
        linked = _rewire_indexers(session, app, prowlarr_indexers, prowlarr_key)
        download_clients = _ensure_download_client(session, app)
        webhooks = _ensure_marker_webhook(session, app)
        seerr_servers = 0
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
        record(
            "auto_config.app.complete",
            app=app.name,
            kind=app.kind,
            root_folders=root_folders,
            custom_formats=cf_count,
            profiles=profile_count,
            linked_indexers=linked,
            download_clients=download_clients,
            webhooks=webhooks,
            seerr_servers=seerr_servers,
        )

    _harden_prowlarr_app_sync(session, prowlarr_url, prowlarr_key)
    totals["altmount_arr_instances"] = _ensure_altmount_arr_management(session, apps)
    totals["seerr_admin"] = _ensure_seerr_admin_user()
    totals["jellyfin_libraries"] = _ensure_jellyfin_libraries(session)
    totals["plex_libraries"] = _ensure_plex_libraries(session)
    totals["seerr_servers"] = _ensure_seerr_servers(session, apps)
    time.sleep(10)
    for app in apps:
        _rewire_indexers(session, app, prowlarr_indexers, prowlarr_key)
    record("auto_config.paint.complete", totals=totals)
    return totals
