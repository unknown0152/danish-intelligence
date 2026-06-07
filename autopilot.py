import requests
import os
import re
import time
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# --- Configuration ---
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
PROXY_URL = os.environ.get("PROXY_URL", "http://danish-intelligence:9699")
ALTMOUNT_URL = os.environ.get("ALTMOUNT_URL", "http://altmount:8080/sabnzbd")

def _clean_env(name):
    value = os.environ.get(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value

def _read_arr_key(name):
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
SONARR_KEY = os.environ.get("SONARR_APIKEY", "")
PROWLARR_KEY = os.environ.get("PROWLARR_APIKEY", os.environ.get("PROWLARR_API_KEY", ""))
ALTMOUNT_KEY = _clean_env("ALTMOUNT_APIKEY") or _clean_env("ALTMOUNT_API_KEY")
SEARCH_INDEXER_ID = "16" # OldBoys (Specialized for DK content)

CORE_SERIES = ["Dragon Ball Z", "Dragon Ball", "Batman", "Asterix", "South Park", "SpongeBob"]

def push_to_radarr(movie, release_title, guid, indexer_id):
    """Force-grab a release via Radarr's release/push API."""
    url = f"{RADARR_URL}/api/v3/release/push"
    
    # Helper: Use a Scene-Perfect title to ensure Radarr accepts it
    # We strip all the 'extra' info from the original title and make it look like
    # a standard, high-quality individual movie release.
    clean_title_part = movie['title'].replace(" ", ".").replace(":", "")
    pushed_title = f"{clean_title_part}.{movie['year']}.1080p.WEB-DL-DanskArr"

    payload = {
        "Title": pushed_title,
        "DownloadUrl": f"{PROXY_URL}/{indexer_id}/api?t=get&id={guid}&apikey={PROWLARR_KEY}",
        "Guid": f"danskarr-{guid}-{secrets.token_hex(4)}",
        "IndexerId": 16, # OldBoys
        "Indexer": "OldBoys {DK}",
        "Protocol": 1, # Usenet
        "PublishDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Size": 2 * 1024 * 1024 * 1024, # 2GB
        "Rejected": False,
        "Approved": True,
        "DownloadAllowed": True,
        "CustomFormatScore": 10000,
        "CustomFormats": [{"id": 74, "name": "DKAudio"}],
        "Quality": {
            "Quality": {"id": 3, "name": "WEBDL-1080p"},
            "Revision": {"version": 1, "real": 0}
        },
        "Languages": [{"id": 6, "name": "Danish"}],
        "MovieId": movie['id'],
    }
    
    try:
        # 1. Start the download in AltMount directly
        # AltMount expects mode=addfile via POST
        alt_params = {"mode": "addfile", "name": release_title, "cat": "movies"}
        if ALTMOUNT_KEY:
            alt_params["apikey"] = ALTMOUNT_KEY
        elif RADARR_KEY:
            alt_params["ma_username"] = RADARR_URL
            alt_params["ma_password"] = RADARR_KEY

        # We fetch the NZB from the proxy and send it to AltMount
        nzb_url = f"{PROXY_URL}/16/api?t=get&id={guid}&apikey={PROWLARR_KEY}"

        nzb_resp = requests.get(nzb_url, timeout=60)
        
        if nzb_resp.status_code == 200:
            files = {'nzbfile': (f'{release_title}.nzb', nzb_resp.content)}
            alt_r = requests.post(ALTMOUNT_URL, params=alt_params, files=files, timeout=60)
            
            if alt_r.status_code in [200, 201]:
                print(f"      SUCCESS: Sent directly to AltMount!")
                # 2. Tell Radarr to look for it (optional but good for tracking)
                requests.post(url, json=payload, headers={"X-Api-Key": RADARR_KEY})
                return True
            else:
                print(f"      AltMount Error: {alt_r.text}")
        else:
            print(f"      Proxy Error: Failed to fetch NZB.")
        return False
    except Exception as e:
        print(f"      ERROR: Could not push to Radarr: {e}")
        return False

def run_autopilot(dry_run=True):
    print(f"DanskArr Autopilot starting (Mode: {'Dry-Run' if dry_run else 'ACTIVE'})...")
    
    # 1. Fetch missing movies
    try:
        r = requests.get(f"{RADARR_URL}/api/v3/movie", headers={"X-Api-Key": RADARR_KEY})
        missing = [m for m in r.json() if not m['hasFile'] and m['monitored']]
    except: return

    for core in CORE_SERIES:
        targets = [m for m in missing if m['title'].lower().startswith(core.lower())]
        if not targets: continue
        
        print(f"\n[AUTOPILOT] Hunting {core}...")
        
        # Broad Search via Indexer 16 (OldBoys)
        params = {"t": "search", "q": core, "apikey": PROWLARR_KEY}
        try:
            resp = requests.get(f"{PROXY_URL}/16/api", params=params, timeout=30)
            items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        except: items = []
        
        for item_xml in items:
            title_m = re.search(r"<title>(.*?)</title>", item_xml)
            guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", item_xml)
            if not title_m or not guid_m: continue
            
            title = title_m.group(1)
            guid = guid_m.group(1)
            
            # Check for Danish Audio proof
            if ".DKaudio" in title:
                for movie in targets:
                    m_year = str(movie['year'])
                    if m_year in title or movie['title'].lower().split(":")[0] in title.lower():
                        print(f"  MATCH FOUND: '{title}' for '{movie['title']}'")
                        if dry_run:
                            print(f"      [DRY-RUN] Would push to Radarr.")
                        else:
                            push_to_radarr(movie, title, guid, "16")
                            time.sleep(2) 
                        break

if __name__ == "__main__":
    import sys
    is_dry = "--active" not in sys.argv
    run_autopilot(dry_run=is_dry)
