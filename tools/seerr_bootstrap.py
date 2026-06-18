#!/usr/bin/env python3
"""Seed Seerr settings before the Seerr web process starts.

Seerr locks most settings APIs while the app is in first-run mode. The market
stack uses this helper as a short-lived bootstrap service so Seerr starts with
an initialized settings file and Danish Intelligence can then add Arr servers
through Seerr's supported API.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse


SETTINGS_PATH = Path(os.getenv("SEERR_SETTINGS_PATH", "/seerr-config/settings.json"))
SEERR_MEDIA_SERVER_TYPE_PLEX = 1
SEERR_MEDIA_SERVER_TYPE_JELLYFIN = 2
SEERR_DEFAULT_PERMISSIONS = 48


def clean_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    return "" if value.startswith("{") and value.endswith("}") else value


def media_server_type() -> str:
    value = clean_env("MEDIA_SERVER_TYPE").lower()
    return value if value in {"plex", "jellyfin"} else "jellyfin"


def media_server_url() -> str:
    media_type = media_server_type()
    default = "http://jellyfin:8096" if media_type == "jellyfin" else "http://plex:32400"
    return (clean_env("MEDIA_SERVER_URL") or default).rstrip("/")


def host_block(url: str, default_port: int) -> tuple[str, int, bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or ""
    port = int(parsed.port or (443 if parsed.scheme == "https" else default_port))
    return host, port, parsed.scheme == "https", parsed.path.rstrip("/")


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        backup = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".invalid")
        SETTINGS_PATH.replace(backup)
        return {}


def default_settings() -> dict:
    media_type = media_server_type()
    return {
        "public": {"initialized": True},
        "main": {
            "apiKey": clean_env("SEERR_API_KEY") or secrets.token_hex(16),
            "applicationTitle": "Danish Requests",
            "applicationUrl": clean_env("SEERR_APPLICATION_URL") or clean_env("SEERR_EXTERNAL_URL"),
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
                if media_type == "jellyfin"
                else SEERR_MEDIA_SERVER_TYPE_PLEX
            ),
            "partialRequestsEnabled": True,
            "enableSpecialEpisodes": False,
            "locale": "en",
            "youtubeUrl": "",
        },
        "plex": {"name": "", "ip": "", "port": 32400, "useSsl": False, "libraries": [], "accessToken": ""},
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


def apply_defaults(settings: dict) -> bool:
    changed = False
    defaults = default_settings()
    for section, value in defaults.items():
        if section not in settings:
            settings[section] = value
            changed = True

    public = settings.setdefault("public", {})
    if public.get("initialized") is not True:
        public["initialized"] = True
        changed = True

    main = settings.setdefault("main", {})
    for key, value in defaults["main"].items():
        if key == "apiKey" and main.get("apiKey"):
            continue
        if main.get(key) != value:
            main[key] = value
            changed = True

    media_type = media_server_type()
    host, port, use_ssl, url_base = host_block(media_server_url(), 8096 if media_type == "jellyfin" else 32400)
    external = clean_env("MEDIA_SERVER_EXTERNAL_URL")
    if media_type == "jellyfin":
        jellyfin = settings.setdefault("jellyfin", defaults["jellyfin"])
        values = {
            "name": jellyfin.get("name") or "Danish Jellyfin",
            "ip": host,
            "port": port,
            "useSsl": use_ssl,
            "urlBase": url_base,
            "externalHostname": external or jellyfin.get("externalHostname", ""),
            "apiKey": clean_env("JELLYFIN_API_KEY") or clean_env("JELLYFIN_APIKEY") or jellyfin.get("apiKey", ""),
        }
        for key, value in values.items():
            if jellyfin.get(key) != value:
                jellyfin[key] = value
                changed = True
    else:
        plex = settings.setdefault("plex", defaults["plex"])
        values = {
            "name": clean_env("PLEX_SERVER_NAME") or plex.get("name") or "Danish Plex",
            "machineId": clean_env("PLEX_MACHINE_ID") or plex.get("machineId", ""),
            "ip": host,
            "port": port,
            "useSsl": use_ssl,
            "accessToken": clean_env("PLEX_TOKEN") or clean_env("PLEX_ACCESS_TOKEN") or plex.get("accessToken", ""),
        }
        for key, value in values.items():
            if plex.get(key) != value:
                plex[key] = value
                changed = True

    return changed


def main() -> None:
    settings = load_settings()
    changed = apply_defaults(settings)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if changed or not SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Seerr bootstrap complete: {SETTINGS_PATH}")


if __name__ == "__main__":
    main()
