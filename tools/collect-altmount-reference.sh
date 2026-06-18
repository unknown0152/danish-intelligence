#!/usr/bin/env bash
set -u

# Read-only AltMount/ARR reference collector.
# Use this on a VPS where AltMount imports are working correctly, then send the
# generated tar.gz back for comparison.

umask 077

TS="$(date -u +%Y%m%dT%H%M%SZ)"
HOST="$(hostname 2>/dev/null || echo unknown-host)"
OUT="${1:-/tmp/altmount-reference-${HOST}-${TS}}"
ARCHIVE="${OUT}.tar.gz"
TIMEOUT="${DIAG_TIMEOUT:-45}"
LOG_LINES="${DIAG_LOG_LINES:-700}"

mkdir -p \
  "$OUT/system" \
  "$OUT/docker" \
  "$OUT/docker/containers" \
  "$OUT/docker/logs" \
  "$OUT/config" \
  "$OUT/api" \
  "$OUT/paths" \
  "$OUT/summary"

redact_stream() {
  perl -pe '
    s/((?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|auth|username|user)[A-Za-z0-9_.-]*\s*[:=]\s*)("[^"]*"|'\''[^'\'']*'\''|[^\s,}]+)/$1"<redacted>"/ig;
    s/("(?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|auth|username|user)[^"]*"\s*:\s*)("[^"]*"|[0-9A-Za-z_.:\/+=-]+)/$1"<redacted>"/ig;
    s/(cosmos_[A-Za-z0-9_-]{20,})/<redacted>/g;
    s/([A-Za-z0-9_-]{32,})/<redacted>/g;
  '
}

run_cmd() {
  local target="$1"
  shift
  {
    printf '$'
    printf ' %q' "$@"
    printf '\n\n'
    timeout "$TIMEOUT" "$@" 2>&1
    local code=$?
    printf '\n[exit=%s]\n' "$code"
  } | redact_stream > "$OUT/$target.txt"
}

run_stdin_cmd() {
  local target="$1"
  shift
  {
    printf '$'
    printf ' %q' "$@"
    printf ' <stdin>\n\n'
    timeout "$TIMEOUT" "$@" 2>&1
    local code=$?
    printf '\n[exit=%s]\n' "$code"
  } | redact_stream > "$OUT/$target.txt"
}

copy_file_redacted() {
  local source="$1"
  local target="$2"
  if [ -r "$source" ]; then
    redact_stream < "$source" > "$OUT/$target"
  else
    printf 'not readable or missing: %s\n' "$source" > "$OUT/$target"
  fi
}

docker_names() {
  if command -v docker >/dev/null 2>&1; then
    docker ps -a --format '{{.Names}}' 2>/dev/null | sort
  fi
}

has_container() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$1"
}

running_container() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"
}

interesting_container() {
  case "$1" in
    altmount|nzbdav|radarr|sonarr|radarr-2160p|sonarr-2160p|prowlarr|jellyfin|plex|seerr|danish-intelligence)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

cat > "$OUT/README.txt" <<EOF
AltMount reference report
Generated: ${TS}
Host: ${HOST}

This report is intended to compare a working AltMount setup against another
server. It is read-only. Secrets that look like API keys, passwords, tokens,
JWT secrets, usernames, and long opaque values are redacted.

Review the archive before sharing publicly.
EOF

run_cmd system/date date -Is
run_cmd system/uname uname -a
run_cmd system/os-release sh -c 'cat /etc/os-release 2>/dev/null || true'
run_cmd system/disk-free df -hT
run_cmd system/mounts findmnt
run_cmd system/listening-ports sh -c 'ss -tulpn 2>/dev/null || true'

if ! command -v docker >/dev/null 2>&1; then
  printf 'Docker is not available on this host.\n' | tee "$OUT/summary/error.txt"
  tar -czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  printf '\nCreated diagnostic archive:\n%s\n\n' "$ARCHIVE"
  exit 0
fi

run_cmd docker/version docker version
run_cmd docker/info docker info
run_cmd docker/ps sh -c 'docker ps -a --no-trunc'
run_cmd docker/networks docker network ls
run_cmd docker/volumes docker volume ls

for name in $(docker_names); do
  if interesting_container "$name"; then
    run_cmd "docker/containers/${name}-inspect" docker inspect "$name"
    run_cmd "docker/containers/${name}-mounts" sh -c "docker inspect '$name' --format '{{range .Mounts}}{{println .Destination \"<-\" .Source \"type=\" .Type \"mode=\" .Mode \"prop=\" .Propagation}}{{end}}'"
    run_cmd "docker/containers/${name}-env" sh -c "docker inspect '$name' --format '{{range .Config.Env}}{{println .}}{{end}}' | sort"
    run_cmd "docker/logs/${name}" sh -c "docker logs --tail '$LOG_LINES' --timestamps '$name' 2>&1"
  fi
done

run_cmd paths/key-paths sh -c '
for p in \
  /srv /srv/config /srv/media /srv/cosmos-storage \
  /data /data/media /data/appdata \
  /mnt /mnt/altmount /mnt/remotes /mnt/remotes/altmount \
  /mnt/symlinks /mnt/symlinks/altmount /mnt/strm /mnt/strm/altmount \
  /media /complete /config /dev/fuse
do
  echo "## $p"
  stat "$p" 2>&1 || true
  find "$p" -maxdepth 2 -xdev -printf "%M %u %g %s %p -> %l\n" 2>/dev/null | sort | head -200 || true
done'

copy_file_redacted /srv/config/altmount/config.yaml config/altmount-config.srv.yaml.redacted
copy_file_redacted /config/config.yaml config/altmount-config.container-path.yaml.redacted
copy_file_redacted /srv/config/radarr/config.xml config/radarr-config.xml.redacted
copy_file_redacted /srv/config/sonarr/config.xml config/sonarr-config.xml.redacted
copy_file_redacted /srv/config/prowlarr/config.xml config/prowlarr-config.xml.redacted

run_stdin_cmd config/altmount-config-discovery python3 - <<'PY'
import sys
from pathlib import Path
try:
    import yaml
except Exception as exc:
    print(f"python-yaml unavailable: {type(exc).__name__}: {exc}")
    raise SystemExit(0)

paths = [
    "/srv/config/altmount/config.yaml",
    "/data/appdata/altmount/config.yaml",
    "/opt/altmount/config.yaml",
    "/config/config.yaml",
]

def names(items):
    return [item.get("name") for item in items or [] if isinstance(item, dict)]

for raw_path in paths:
    p = Path(raw_path)
    print(f"## {raw_path}")
    if not p.is_file():
        print("missing")
        continue
    data = yaml.safe_load(p.read_text(errors="replace")) or {}
    print("mount_path=", data.get("mount_path"))
    imp = data.get("import") or {}
    print("import_strategy=", imp.get("import_strategy"))
    print("import_dir=", imp.get("import_dir"))
    print("watch_dir=", imp.get("watch_dir"))
    sab = data.get("sabnzbd") or {}
    print("sab_complete_dir=", sab.get("complete_dir"))
    print("sab_categories=", names(sab.get("categories")))
    health = data.get("health") or {}
    print("health_enabled=", health.get("enabled"))
    print("health_library_dir=", health.get("library_dir"))
    arrs = data.get("arrs") or {}
    print("arrs_enabled=", arrs.get("enabled"))
    print("queue_cleanup_enabled=", arrs.get("queue_cleanup_enabled"))
    print("radarr_instances=", [(x.get("name"), x.get("url"), x.get("category"), x.get("enabled")) for x in arrs.get("radarr_instances") or [] if isinstance(x, dict)])
    print("sonarr_instances=", [(x.get("name"), x.get("url"), x.get("category"), x.get("enabled")) for x in arrs.get("sonarr_instances") or [] if isinstance(x, dict)])
    print("provider_count=", len(data.get("providers") or []))
PY

if running_container altmount; then
  run_stdin_cmd api/altmount-host-api python3 - <<'PY'
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

def clean(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in k.lower() for s in ("key", "token", "secret", "password", "username", "user")):
                out[k] = "<redacted>"
            else:
                out[k] = clean(v)
        return out
    if isinstance(obj, list):
        return [clean(x) for x in obj]
    return obj

def docker_ip(name):
    try:
        raw = subprocess.check_output(
            ["docker", "inspect", name, "--format", "{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}"],
            text=True,
            timeout=10,
        )
        return next((line.strip() for line in raw.splitlines() if line.strip()), "")
    except Exception as exc:
        print(f"docker ip lookup error: {type(exc).__name__}: {exc}")
        return ""

def load_config():
    if yaml is None:
        return {}
    for raw_path in (
        "/srv/config/altmount/config.yaml",
        "/data/appdata/altmount/config.yaml",
        "/opt/altmount/config.yaml",
    ):
        p = Path(raw_path)
        if p.is_file():
            try:
                return yaml.safe_load(p.read_text(errors="replace")) or {}
            except Exception:
                return {}
    return {}

def candidate_api_keys(obj, path=()):
    blocked = {"provider", "providers", "nntp", "indexer", "indexers"}
    if any(part.lower() in blocked for part in path):
        return []
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            next_path = path + (str(key),)
            if isinstance(value, str) and value and (
                key_l in {"apikey", "api_key", "api-key"}
                or ("api" in key_l and "key" in key_l)
            ):
                found.append(value)
            found.extend(candidate_api_keys(value, next_path))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(candidate_api_keys(item, path))
    deduped = []
    for value in found:
        if value not in deduped:
            deduped.append(value)
    return deduped

def fetch(base_url, path, keys):
    attempts = [""]
    attempts.extend(keys)
    last_error = None
    for key in attempts:
        url = base_url + path
        if key:
            url += "?" + urllib.parse.urlencode({"apikey": key})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body), bool(key)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return {"error": last_error or "request failed"}, False

ip = docker_ip("altmount")
if not ip:
    print("altmount container IP not found")
    raise SystemExit(0)

cfg = load_config()
keys = candidate_api_keys(cfg)
base_url = f"http://{ip}:8080"
print(f"altmount_base_url={base_url}")
print(f"candidate_key_count={len(keys)}")

for path in ("/api/config", "/api/arrs/instances", "/api/arrs/health", "/api/arrs/stats", "/api/health/stats", "/api/system"):
    print(f"\n## {path}")
    data, used_key = fetch(base_url, path, keys)
    print(f"credential_used={'yes' if used_key else 'no'}")
    if path == "/api/config" and isinstance(data, dict):
        data = data.get("data", data)
    print(json.dumps(clean(data), indent=2, sort_keys=True)[:30000])
PY
fi

if running_container danish-intelligence; then
  API_CONTAINER=danish-intelligence
elif running_container radarr; then
  API_CONTAINER=radarr
else
  API_CONTAINER=""
fi

if [ -n "$API_CONTAINER" ]; then
  run_stdin_cmd api/arr-paths-and-clients docker exec -i "$API_CONTAINER" python3 - <<'PY'
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse, urlunparse

APPS = {
    "radarr": ("http://radarr:7878", ["/arr-config/radarr/config.xml", "/config/config.xml", "/srv/config/radarr/config.xml"], "/api/v3"),
    "sonarr": ("http://sonarr:8989", ["/arr-config/sonarr/config.xml", "/config/config.xml", "/srv/config/sonarr/config.xml"], "/api/v3"),
    "radarr-2160p": ("http://radarr-2160p:7878", ["/arr-config/radarr-2160p/config.xml", "/srv/config/radarr-2160p/config.xml"], "/api/v3"),
    "sonarr-2160p": ("http://sonarr-2160p:8989", ["/arr-config/sonarr-2160p/config.xml", "/srv/config/sonarr-2160p/config.xml"], "/api/v3"),
}

def config(paths):
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        try:
            root = ET.parse(p).getroot()
            return (root.findtext("ApiKey", "") or "").strip(), (root.findtext("Port", "") or "").strip(), str(p)
        except Exception:
            continue
    return "", "", ""

def with_port(url, port):
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc
    if not host:
        return url
    return urlunparse((parsed.scheme or "http", f"{host}:{port}", parsed.path.rstrip("/"), "", "", "")).rstrip("/")

def get_json(url, key):
    req = request.Request(url, headers={"X-Api-Key": key})
    try:
        with request.urlopen(req, timeout=12) as resp:
            return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as exc:
        return exc.code, {"error": str(exc), "body": exc.read().decode("utf-8", "replace")[:1000]}
    except Exception as exc:
        return "error", {"error": f"{type(exc).__name__}: {exc}"}

def fields(item):
    return {f.get("name"): f.get("value") for f in item.get("fields", []) if isinstance(f, dict)}

for name, (base, cfg_paths, api) in APPS.items():
    key, port, cfg_path = config(cfg_paths)
    if port.isdigit():
        base = with_port(base, int(port))
    print(f"\n## {name} base={base} config={cfg_path or 'missing'} key_present={bool(key)}")
    if not key:
        continue
    status, system = get_json(base + api + "/system/status", key)
    print(f"system_status={status} app={system.get('appName') if isinstance(system, dict) else None} version={system.get('version') if isinstance(system, dict) else None}")
    for endpoint in ("rootfolder", "downloadclient", "qualityprofile", "queue"):
        status, data = get_json(base + api + "/" + endpoint, key)
        print(f"\n{endpoint}: status={status}")
        if not isinstance(data, list) and endpoint != "queue":
            print(json.dumps(data, indent=2)[:2000])
            continue
        if endpoint == "rootfolder" and isinstance(data, list):
            for item in data:
                print(f"- path={item.get('path')} accessible={item.get('accessible')} freeSpace={item.get('freeSpace')}")
        elif endpoint == "downloadclient" and isinstance(data, list):
            for item in data:
                f = fields(item)
                category = f.get("movieCategory") or f.get("tvCategory")
                print(f"- name={item.get('name')} impl={item.get('implementation')} enable={item.get('enable')} removeCompleted={item.get('removeCompletedDownloads')} host={f.get('host')} port={f.get('port')} urlBase={f.get('urlBase')} category={category}")
        elif endpoint == "qualityprofile" and isinstance(data, list):
            for item in data:
                print(f"- id={item.get('id')} name={item.get('name')} language={item.get('language')} upgradeAllowed={item.get('upgradeAllowed')} cutoffFormatScore={item.get('cutoffFormatScore')} minFormatScore={item.get('minFormatScore')}")
        elif endpoint == "queue":
            records = data.get("records", []) if isinstance(data, dict) else data
            for item in records[:25]:
                print(f"- title={item.get('title')} status={item.get('status')} state={item.get('trackedDownloadState')} outputPath={item.get('outputPath')} client={item.get('downloadClient')}")
PY
fi

run_cmd summary/relevant-log-lines sh -c '
for c in altmount radarr sonarr danish-intelligence; do
  if docker ps -a --format "{{.Names}}" | grep -qx "$c"; then
    echo "## $c"
    docker logs --tail 2000 "$c" 2>&1 | grep -Ei "import|symlink|strm|history|storage|complete|queue|scan|webhook|ffprobe|copy|hardlink|failed|error|warn|altmount|sabnzbd" | tail -250 || true
  fi
done'

SUMMARY_FILE="$OUT/SUMMARY.txt"
{
  printf 'AltMount reference quick summary\n'
  printf 'Generated: %s\n' "$TS"
  printf 'Host: %s\n' "$HOST"
  printf 'Archive: %s\n' "$ARCHIVE"
  printf 'Report directory: %s\n\n' "$OUT"

  printf '## AltMount Config Discovery\n'
  if [ -r "$OUT/config/altmount-config-discovery.txt" ]; then
    grep -E 'mount_path=|import_strategy=|import_dir=|watch_dir=|sab_complete_dir=|sab_categories=|health_enabled=|health_library_dir=|arrs_enabled=|queue_cleanup_enabled=|radarr_instances=|sonarr_instances=|provider_count=' \
      "$OUT/config/altmount-config-discovery.txt" || true
  else
    printf 'missing\n'
  fi

  printf '\n## AltMount Live API Highlights\n'
  if [ -r "$OUT/api/altmount-host-api.txt" ]; then
    grep -E 'altmount_base_url=|candidate_key_count=|credential_used=|"import_strategy"|"import_dir"|"mount_path"|"complete_dir"|"categories"|"queue_cleanup_enabled"|"radarr_instances"|"sonarr_instances"|"webhook_base_url"|"library_dir"|"check_interval_seconds"' \
      "$OUT/api/altmount-host-api.txt" | awk '!seen[$0]++' | head -220 || true
  else
    printf 'missing\n'
  fi

  printf '\n## Arr API State\n'
  if [ -r "$OUT/api/arr-paths-and-clients.txt" ]; then
    sed -n '1,260p' "$OUT/api/arr-paths-and-clients.txt"
  else
    printf 'missing\n'
  fi

  printf '\n## Important Warnings and Errors\n'
  if [ -r "$OUT/summary/relevant-log-lines.txt" ]; then
    grep -Ei 'importBlocked|failed|error|warn|missing article|unhealthy|repair|copy|hardlink|symlink|strm' \
      "$OUT/summary/relevant-log-lines.txt" | tail -120 || true
  else
    printf 'missing\n'
  fi

  printf '\n## Report Files\n'
  find "$OUT" -maxdepth 2 -type f -printf '%P\n' | sort
} | redact_stream > "$SUMMARY_FILE"

run_cmd summary/tree sh -c "find '$OUT' -type f -printf '%s %p\n' | sort -n"

tar -czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")"

printf '\nCreated AltMount reference archive:\n%s\n\n' "$ARCHIVE"
printf 'Quick summary file:\n%s\n\n' "$SUMMARY_FILE"
printf '%s\n' '------------------------------------------------------------'
cat "$SUMMARY_FILE"
printf '\n%s\n' '------------------------------------------------------------'
printf 'Send the tar.gz back for deep review if needed. The unpacked report is at:\n%s\n' "$OUT"
