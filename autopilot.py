import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878").rstrip("/")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
PROXY_URL = os.environ.get("PROXY_URL", "http://danish-intelligence:9699").rstrip("/")
ALTMOUNT_URL = os.environ.get("ALTMOUNT_URL", "http://altmount:8080/sabnzbd")
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


def _clean_env(name: str) -> str:
    value = os.environ.get(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _read_arr_key(name: str) -> str:
    for path in (f"/arr-config/{name}/config.xml", f"/srv/config/{name}/config.xml"):
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            return ET.parse(cfg).getroot().findtext("ApiKey", default="").strip()
        except Exception:
            continue
    return ""


RADARR_KEY = _clean_env("RADARR_APIKEY") or _clean_env("RADARR_API_KEY") or _read_arr_key("radarr")
PROWLARR_KEY = _clean_env("PROWLARR_APIKEY") or _clean_env("PROWLARR_API_KEY")
ALTMOUNT_KEY = _clean_env("ALTMOUNT_APIKEY") or _clean_env("ALTMOUNT_API_KEY")

CORE_SERIES = ["Dragon Ball Z", "Dragon Ball", "Batman", "Asterix", "South Park", "SpongeBob"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Content-Type": "application/json"}


def _field(obj: dict[str, Any], name: str, default: Any = "") -> Any:
    for field in obj.get("fields", []):
        if field.get("name") == name:
            return field.get("value", default)
    return default


def _get_json(session: requests.Session, url: str, api_key: str) -> Any:
    resp = session.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _clean_name(name: str) -> str:
    name = re.sub(r"(\s*\{DK\})+$", "", name)
    name = re.sub(r"\s*\(Prowlarr\)$", "", name)
    return name.strip().lower()


def _proxy_api_url(prowlarr_indexer_id: str, *, arr_visible: bool = False) -> str:
    base_url = ARR_PROXY_URL if arr_visible else PROXY_URL
    return f"{base_url}/{prowlarr_indexer_id}/api"


def _extract_prowlarr_id(base_url: str) -> str:
    match = re.search(r"/(\d+)(?:/api)?/?$", base_url or "")
    return match.group(1) if match else ""


def _oldboys_prowlarr_id(session: requests.Session) -> str:
    if not PROWLARR_KEY:
        return ""
    try:
        indexers = _get_json(session, f"{PROWLARR_URL}/api/v1/indexer", PROWLARR_KEY)
    except requests.RequestException:
        return ""

    for indexer in indexers:
        if "oldboys" in _clean_name(indexer.get("name", "")):
            return str(indexer.get("id", ""))
    return ""


def _oldboys_radarr_indexer(session: requests.Session) -> dict[str, Any] | None:
    indexers = _get_json(session, f"{RADARR_URL}/api/v3/indexer", RADARR_KEY)
    for indexer in indexers:
        if "oldboys" not in _clean_name(indexer.get("name", "")):
            continue

        prowlarr_id = _extract_prowlarr_id(str(_field(indexer, "baseUrl", "")))
        if not prowlarr_id:
            prowlarr_id = _oldboys_prowlarr_id(session)
        if not prowlarr_id:
            continue

        return {
            "arr_id": int(indexer["id"]),
            "name": indexer.get("name") or "OldBoys",
            "prowlarr_id": prowlarr_id,
        }
    return None


def _custom_format(session: requests.Session, name: str) -> dict[str, Any] | None:
    formats = _get_json(session, f"{RADARR_URL}/api/v3/customformat", RADARR_KEY)
    for custom_format in formats:
        if custom_format.get("name") == name:
            return {"id": int(custom_format["id"]), "name": custom_format["name"]}
    return None


def push_to_radarr(
    session: requests.Session,
    movie: dict[str, Any],
    release_title: str,
    guid: str,
    oldboys_indexer: dict[str, Any],
    dk_audio_format: dict[str, Any] | None,
) -> bool:
    """Force-grab a release via Radarr's release/push API."""
    url = f"{RADARR_URL}/api/v3/release/push"
    clean_title_part = movie["title"].replace(" ", ".").replace(":", "")
    pushed_title = f"{clean_title_part}.{movie['year']}.1080p.WEB-DL-DanskArr"
    internal_nzb_url = (
        f"{_proxy_api_url(oldboys_indexer['prowlarr_id'])}"
        f"?t=get&id={guid}&apikey={PROWLARR_KEY}"
    )
    arr_nzb_url = (
        f"{_proxy_api_url(oldboys_indexer['prowlarr_id'], arr_visible=True)}"
        f"?t=get&id={guid}&apikey={PROWLARR_KEY}"
    )

    payload = {
        "Title": pushed_title,
        "DownloadUrl": arr_nzb_url,
        "Guid": f"danskarr-{guid}-{secrets.token_hex(4)}",
        "IndexerId": oldboys_indexer["arr_id"],
        "Indexer": oldboys_indexer["name"],
        "Protocol": 1,
        "PublishDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Size": 2 * 1024 * 1024 * 1024,
        "Rejected": False,
        "Approved": True,
        "DownloadAllowed": True,
        "CustomFormatScore": 10000,
        "CustomFormats": [dk_audio_format] if dk_audio_format else [],
        "Quality": {
            "Quality": {"id": 3, "name": "WEBDL-1080p"},
            "Revision": {"version": 1, "real": 0},
        },
        "Languages": [{"id": 6, "name": "Danish"}],
        "MovieId": movie["id"],
    }

    try:
        alt_params = {"mode": "addfile", "name": release_title, "cat": "movies"}
        if ALTMOUNT_KEY:
            alt_params["apikey"] = ALTMOUNT_KEY
        elif RADARR_KEY:
            alt_params["ma_username"] = RADARR_URL
            alt_params["ma_password"] = RADARR_KEY

        nzb_resp = session.get(internal_nzb_url, timeout=60)
        if nzb_resp.status_code != 200:
            print("      Proxy Error: Failed to fetch NZB.")
            return False

        files = {"nzbfile": (f"{release_title}.nzb", nzb_resp.content)}
        alt_r = session.post(ALTMOUNT_URL, params=alt_params, files=files, timeout=60)
        if alt_r.status_code not in [200, 201]:
            print(f"      AltMount Error: {alt_r.text}")
            return False

        print("      SUCCESS: Sent directly to AltMount!")
        session.post(url, json=payload, headers=_headers(RADARR_KEY), timeout=30)
        return True
    except Exception as e:
        print(f"      ERROR: Could not push to Radarr: {e}")
        return False


def run_autopilot(dry_run: bool = True) -> None:
    print(f"DanskArr Autopilot starting (Mode: {'Dry-Run' if dry_run else 'ACTIVE'})...")
    if not RADARR_KEY or not PROWLARR_KEY:
        print("DanskArr Autopilot skipped: missing Radarr or Prowlarr API key.")
        return

    session = requests.Session()
    try:
        missing = [
            movie
            for movie in _get_json(session, f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if not movie["hasFile"] and movie["monitored"]
        ]
        oldboys_indexer = _oldboys_radarr_indexer(session)
        if not oldboys_indexer:
            print("DanskArr Autopilot skipped: OldBoys indexer not found in Radarr.")
            return
        dk_audio_format = _custom_format(session, "DKAudio")
    except requests.RequestException as e:
        print(f"DanskArr Autopilot skipped: {e}")
        return

    for core in CORE_SERIES:
        targets = [m for m in missing if m["title"].lower().startswith(core.lower())]
        if not targets:
            continue

        print(f"\n[AUTOPILOT] Hunting {core}...")
        params = {"t": "search", "q": core, "apikey": PROWLARR_KEY}
        try:
            resp = session.get(_proxy_api_url(oldboys_indexer["prowlarr_id"]), params=params, timeout=30)
            resp.raise_for_status()
            items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        except requests.RequestException:
            items = []

        for item_xml in items:
            title_m = re.search(r"<title>(.*?)</title>", item_xml)
            guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", item_xml)
            if not title_m or not guid_m:
                continue

            title = title_m.group(1)
            guid = guid_m.group(1)
            if ".DKaudio" not in title:
                continue

            for movie in targets:
                m_year = str(movie["year"])
                movie_title = movie["title"].lower().split(":")[0]
                if m_year in title or movie_title in title.lower():
                    print(f"  MATCH FOUND: '{title}' for '{movie['title']}'")
                    if dry_run:
                        print("      [DRY-RUN] Would push to Radarr.")
                    else:
                        push_to_radarr(session, movie, title, guid, oldboys_indexer, dk_audio_format)
                        time.sleep(2)
                    break


if __name__ == "__main__":
    import sys

    run_autopilot(dry_run="--active" not in sys.argv)
