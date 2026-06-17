#!/usr/bin/env bash
set -u

# Fast, read-only live diagnostics for Danish Intelligence installs.
# Use the full collect-server-debug.sh script when you need an archive.

DI_CONTAINER="${DI_CONTAINER:-danish-intelligence}"
LOG_LINES="${LOG_LINES:-80}"

redact_stream() {
  perl -pe '
    s/((?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|plex[_-]?claim|claim|auth)[A-Za-z0-9_.-]*\s*[:=]\s*)("[^"]*"|'\''[^'\'']*'\''|[^\s,}]+)/$1"<redacted>"/ig;
    s/("(?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|plex[_-]?claim|claim|auth)[^"]*"\s*:\s*)("[^"]*"|[0-9A-Za-z_.:\/+=-]+)/$1"<redacted>"/ig;
    s/(cosmos_[A-Za-z0-9_-]{20,})/<redacted>/g;
    s/([A-Za-z0-9_-]{32,})/<redacted>/g;
  '
}

section() {
  printf '\n## %s\n' "$1"
}

run() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  "$@" 2>&1 | redact_stream
}

docker_running() {
  command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"
}

section "Host"
run date -Is
run hostname
run sh -c 'df -hT / /srv /var/lib/docker /var/lib/cosmos 2>/dev/null || df -hT'

section "Docker containers"
if ! command -v docker >/dev/null 2>&1; then
  printf 'Docker is not available on this host.\n'
  exit 2
fi
run docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'

section "Danish Intelligence logs"
if ! docker ps -a --format '{{.Names}}' | grep -qx "$DI_CONTAINER"; then
  printf 'Container not found: %s\n' "$DI_CONTAINER"
  exit 2
fi
run docker logs --tail "$LOG_LINES" --timestamps "$DI_CONTAINER"

if ! docker_running "$DI_CONTAINER"; then
  printf '\nContainer exists but is not running: %s\n' "$DI_CONTAINER"
  exit 2
fi

section "Danish Intelligence API"
run docker exec -i "$DI_CONTAINER" python3 - <<'PY'
import json
import urllib.error
import urllib.request

def fetch(path):
    url = f"http://127.0.0.1:9699{path}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(f"{path}: HTTP {resp.status}")
            if path.endswith(".json") or path.startswith("/debug/"):
                try:
                    parsed = json.loads(body)
                    print(json.dumps(parsed, indent=2, sort_keys=True)[:5000])
                except Exception:
                    print(body[:5000])
            else:
                print(body[:1000])
    except Exception as exc:
        print(f"{path}: ERROR {type(exc).__name__}: {exc}")

fetch("/health")
fetch("/status.json")
fetch("/debug/install?limit=80")
PY

section "Internal Arr readiness"
run docker exec -i "$DI_CONTAINER" python3 - <<'PY'
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse, urlunparse

APPS = {
    "prowlarr": ("http://prowlarr:9696", "/arr-config/prowlarr/config.xml", "/api/v1/system/status"),
    "radarr": (os.getenv("RADARR_URL", "http://radarr:7878"), "/arr-config/radarr/config.xml", "/api/v3/system/status"),
    "sonarr": (os.getenv("SONARR_URL", "http://sonarr:8989"), "/arr-config/sonarr/config.xml", "/api/v3/system/status"),
    "radarr-2160p": (os.getenv("RADARR_2160P_URL", "http://radarr-2160p:7878"), "/arr-config/radarr-2160p/config.xml", "/api/v3/system/status"),
    "sonarr-2160p": (os.getenv("SONARR_2160P_URL", "http://sonarr-2160p:8989"), "/arr-config/sonarr-2160p/config.xml", "/api/v3/system/status"),
}

def read_config(path):
    p = Path(path)
    if not p.exists():
        return "", "", "missing"
    try:
        root = ET.parse(p).getroot()
        return (root.findtext("ApiKey", "") or "").strip(), (root.findtext("Port", "") or "").strip(), "ok"
    except Exception as exc:
        return "", "", f"parse-error:{type(exc).__name__}"

def with_port(url, port):
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc
    if not host:
        return url
    return urlunparse((parsed.scheme or "http", f"{host}:{port}", parsed.path.rstrip("/"), "", "", "")).rstrip("/")

def get_json(url, key):
    req = request.Request(url, headers={"X-Api-Key": key})
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        return exc.code, {"error": str(exc), "body": body}
    except Exception as exc:
        return "error", {"error": f"{type(exc).__name__}: {exc}"}

def fields(item):
    return {f.get("name"): f.get("value") for f in item.get("fields", []) if isinstance(f, dict)}

keys = {}
resolved = {}

print("Secrets are not printed.")
for name, (base_url, cfg, status_path) in APPS.items():
    key, port, cfg_status = read_config(cfg)
    if port.isdigit():
        base_url = with_port(base_url, int(port))
    keys[name] = key
    resolved[name] = (base_url, status_path)
    status, data = get_json(base_url.rstrip("/") + status_path, key) if key else ("no-key", {})
    app = data.get("appName") if isinstance(data, dict) else None
    version = data.get("version") if isinstance(data, dict) else None
    print(f"{name}: url={base_url} config={cfg_status} config_port={port or '<none>'} key_present={bool(key)} http_status={status} app={app or '<none>'} version={version or '<none>'}")

prowlarr_key = keys.get("prowlarr")
if prowlarr_key:
    status, indexers = get_json("http://prowlarr:9696/api/v1/indexer", prowlarr_key)
    print(f"\nprowlarr indexers: http_status={status} count={len(indexers) if isinstance(indexers, list) else '<unknown>'}")
    if isinstance(indexers, list):
        for idx in indexers:
            print(f"- {idx.get('name')} enable={idx.get('enable')} appProfileId={idx.get('appProfileId')} protocol={idx.get('protocol')}")

    status, apps = get_json("http://prowlarr:9696/api/v1/applications", prowlarr_key)
    print(f"\nprowlarr applications: http_status={status} count={len(apps) if isinstance(apps, list) else '<unknown>'}")
    if isinstance(apps, list):
        for app in apps:
            app_fields = fields(app)
            print(f"- {app.get('name')} implementation={app.get('implementation')} syncLevel={app.get('syncLevel')} baseUrl={app_fields.get('baseUrl')}")

for name in ("radarr", "sonarr", "radarr-2160p", "sonarr-2160p"):
    key = keys.get(name)
    if not key:
        continue
    base_url = resolved[name][0].rstrip("/")
    print(f"\n{name}:")
    for endpoint, label in (
        ("/api/v3/rootfolder", "root folders"),
        ("/api/v3/customformat", "custom formats"),
        ("/api/v3/qualityprofile", "quality profiles"),
        ("/api/v3/downloadclient", "download clients"),
    ):
        status, data = get_json(base_url + endpoint, key)
        count = len(data) if isinstance(data, list) else "<unknown>"
        print(f"  {label}: http_status={status} count={count}")
        if not isinstance(data, list):
            if isinstance(data, dict) and data.get("error"):
                print(f"    error={data.get('error')}")
            continue
        if label == "root folders":
            for item in data:
                print(f"    - {item.get('path')} freeSpace={item.get('freeSpace')}")
        elif label == "quality profiles":
            for item in data:
                print(f"    - {item.get('name')} cutoff={item.get('cutoff')} cutoffFormatScore={item.get('cutoffFormatScore')}")
        elif label == "download clients":
            for item in data:
                item_fields = fields(item)
                category = item_fields.get("movieCategory") or item_fields.get("tvCategory")
                print(f"    - {item.get('name')} implementation={item.get('implementation')} enable={item.get('enable')} priority={item.get('priority')} host={item_fields.get('host')} port={item_fields.get('port')} category={category}")
PY

section "Recent important errors"
run sh -c "docker logs --tail 1200 '$DI_CONTAINER' 2>&1 | grep -Ei 'critical|failed|error|rejected|unauthorized|unavailable|downloadclient|qualityprofile|rootfolder|auto-config|oldboys indexer|2160p api key' | grep -Evi 'REQ-START|mode=queue|mode=history' | tail -160 || true"

section "Done"
printf 'For a full archive, run:\n'
printf 'curl -fsSL https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/tools/collect-server-debug.sh -o /tmp/collect-server-debug.sh && bash /tmp/collect-server-debug.sh\n'
