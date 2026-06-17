#!/usr/bin/env bash
set -u

# Read-only Danish media stack diagnostics collector.
# Writes a redacted report under /tmp and packages it as a tar.gz.

umask 077

TS="$(date -u +%Y%m%dT%H%M%SZ)"
HOST="$(hostname 2>/dev/null || echo unknown-host)"
OUT="${1:-/tmp/danish-server-debug-${HOST}-${TS}}"
ARCHIVE="${OUT}.tar.gz"
TIMEOUT="${DIAG_TIMEOUT:-45}"
LOG_LINES="${DIAG_LOG_LINES:-500}"

mkdir -p \
  "$OUT/system" \
  "$OUT/network" \
  "$OUT/docker" \
  "$OUT/docker/containers" \
  "$OUT/docker/logs" \
  "$OUT/cosmos" \
  "$OUT/paths" \
  "$OUT/danish-intelligence" \
  "$OUT/summary"

redact_stream() {
  perl -pe '
    s/((?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|plex[_-]?claim|claim|auth)[A-Za-z0-9_.-]*\s*[:=]\s*)("[^"]*"|'\''[^'\'']*'\''|[^\s,}]+)/$1"<redacted>"/ig;
    s/("(?:api[_-]?key|apikey|token|secret|password|passwd|pwd|rid|jwt[_-]?secret|plex[_-]?claim|claim|auth)[^"]*"\s*:\s*)("[^"]*"|[0-9A-Za-z_.:\/+=-]+)/$1"<redacted>"/ig;
    s/(cosmos_[A-Za-z0-9_-]{20,})/<redacted>/g;
    s/([A-Za-z0-9_-]{32,})/<redacted>/g;
  '
}

write_note() {
  printf '%s\n' "$*" | tee -a "$OUT/README.txt" >/dev/null
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

interesting_container() {
  case "$1" in
    danish-intelligence|prowlarr|radarr|sonarr|radarr-2160p|sonarr-2160p|seerr|altmount|plex|jellyfin|trailarr)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

write_note "Danish server debug report"
write_note "Generated: ${TS}"
write_note "Host: ${HOST}"
write_note ""
write_note "This report is intended to be shareable. Values that look like API keys,"
write_note "tokens, passwords, JWT secrets, RID values, and long opaque secrets are redacted."
write_note "Still review files before posting publicly."

run_cmd system/date date -Is
run_cmd system/uname uname -a
run_cmd system/os-release sh -c 'cat /etc/os-release 2>/dev/null || true'
run_cmd system/hostnamectl hostnamectl
run_cmd system/uptime uptime
run_cmd system/users sh -c 'id; getent passwd root nuc media cosmos 2>/dev/null || true; getent group root docker media cosmos 2>/dev/null || true'
run_cmd system/processes sh -c 'ps -eo pid,ppid,user,group,stat,comm,args --sort=comm | head -300'
run_cmd system/cpu-memory sh -c 'lscpu 2>/dev/null; echo; free -h; echo; vmstat 1 3 2>/dev/null || true'
run_cmd system/disk-free df -hT
run_cmd system/inodes df -ih
run_cmd system/block-devices lsblk -f
run_cmd system/mounts findmnt
run_cmd system/systemd-failed sh -c 'systemctl --failed --no-pager 2>/dev/null || true'
run_cmd system/recent-kernel-log sh -c 'dmesg -T 2>/dev/null | tail -250 || true'

run_cmd network/ip-address ip addr
run_cmd network/ip-route ip route
run_cmd network/resolv-conf sh -c 'cat /etc/resolv.conf 2>/dev/null || true'
run_cmd network/listening-ports sh -c 'ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true'
run_cmd network/firewall sh -c 'nft list ruleset 2>/dev/null || iptables-save 2>/dev/null || true'
run_cmd network/dns-hosts sh -c 'getent hosts prowlarr radarr sonarr radarr-2160p sonarr-2160p seerr altmount plex jellyfin danish-intelligence 2>/dev/null || true'

run_cmd paths/srv-stat sh -c 'for p in /srv /srv/config /srv/media /srv/cosmos /srv/cosmos/config /srv/cosmos-storage /srv/docker /srv/backups /mnt /mnt/altmount /dev/fuse; do echo "## $p"; stat "$p" 2>&1; done'
run_cmd paths/srv-listing sh -c 'for p in /srv /srv/config /srv/media /mnt; do echo "## $p"; find "$p" -maxdepth 3 -xdev -printf "%M %u %g %s %p\n" 2>/dev/null | sort | head -1000; done'
run_cmd paths/acl sh -c 'if command -v getfacl >/dev/null 2>&1; then getfacl -p /srv /srv/config /srv/media /srv/docker /mnt /mnt/altmount 2>/dev/null; fi'
run_cmd paths/du-summary sh -c 'du -xhd1 /srv /srv/config /srv/media /var/lib/docker /var/lib/cosmos /mnt 2>/dev/null | sort -h'

copy_file_redacted /etc/docker/daemon.json docker/daemon.json.redacted
run_cmd docker/version docker version
run_cmd docker/info docker info
run_cmd docker/system-df docker system df -v
run_cmd docker/contexts docker context ls
run_cmd docker/ps sh -c 'docker ps -a --no-trunc'
run_cmd docker/images sh -c 'docker images --digests --no-trunc'
run_cmd docker/volumes docker volume ls
run_cmd docker/networks docker network ls
run_cmd docker/network-inspect sh -c 'for n in $(docker network ls --format "{{.Name}}"); do echo "## NETWORK $n"; docker network inspect "$n"; done'
run_cmd docker/events-recent sh -c 'docker events --since 24h --until 0s 2>/dev/null || true'

for name in $(docker_names); do
  if interesting_container "$name"; then
    run_cmd "docker/containers/${name}-inspect" docker inspect "$name"
    run_cmd "docker/containers/${name}-top" docker top "$name" aux
    run_cmd "docker/containers/${name}-stats" sh -c "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}' '$name'"
    run_cmd "docker/logs/${name}" sh -c "docker logs --tail '$LOG_LINES' --timestamps '$name' 2>&1"
  fi
done

run_cmd docker/compose-labels sh -c 'docker inspect $(docker ps -a -q) --format "{{.Name}} {{json .Config.Labels}}" 2>/dev/null || true'

copy_file_redacted /var/lib/cosmos/backup.cosmos-compose.json cosmos/backup.cosmos-compose.json.redacted
run_cmd cosmos/files sh -c 'for p in /var/lib/cosmos /srv/cosmos /srv/cosmos/config /srv/cosmos-storage; do echo "## $p"; find "$p" -maxdepth 3 -printf "%M %u %g %s %p\n" 2>/dev/null | sort | head -1000; done'
run_cmd cosmos/systemd sh -c 'systemctl cat cosmos 2>/dev/null || true; systemctl status cosmos --no-pager 2>/dev/null || true'

copy_file_redacted /srv/config/danish-intelligence/install-debug.jsonl danish-intelligence/install-debug.jsonl
copy_file_redacted /srv/config/danish-intelligence/install-debug-latest.json danish-intelligence/install-debug-latest.json
copy_file_redacted /srv/config/danish-intelligence/native-dk-titles.txt danish-intelligence/native-dk-titles.txt
run_cmd danish-intelligence/config-files sh -c 'for p in /srv/config/danish-intelligence /srv/config/prowlarr /srv/config/radarr /srv/config/sonarr /srv/config/radarr-2160p /srv/config/sonarr-2160p /srv/config/seerr /srv/config/altmount /srv/config/jellyfin /srv/config/plex; do echo "## $p"; find "$p" -maxdepth 2 -printf "%M %u %g %s %p\n" 2>/dev/null | sort | head -500; done'

if docker ps --format '{{.Names}}' | grep -qx danish-intelligence; then
  run_cmd danish-intelligence/health docker exec danish-intelligence python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:9699/health", timeout=8).read().decode())'
  run_cmd danish-intelligence/status docker exec danish-intelligence python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:9699/status.json", timeout=8).read().decode())'
  run_cmd danish-intelligence/debug-install docker exec danish-intelligence python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:9699/debug/install?limit=400", timeout=8).read().decode())'
  run_cmd danish-intelligence/env docker exec danish-intelligence sh -c 'env | sort'
  run_cmd danish-intelligence/internal-dns docker exec danish-intelligence sh -c 'for h in prowlarr radarr sonarr radarr-2160p sonarr-2160p seerr altmount plex jellyfin danish-intelligence; do echo "## $h"; getent hosts "$h" || true; done'
  run_cmd danish-intelligence/internal-paths docker exec danish-intelligence sh -c 'for p in /config /arr-config/prowlarr /arr-config/radarr /arr-config/sonarr /arr-config/radarr-2160p /arr-config/sonarr-2160p /seerr-config /media /mnt; do echo "## $p"; ls -ld "$p" 2>&1; done'
  run_cmd danish-intelligence/arr-api-readiness docker exec danish-intelligence sh -c 'python3 <<'"'"'PY'"'"'
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse, urlunparse

APPS = {
    "prowlarr": ("http://prowlarr:9696", "/arr-config/prowlarr/config.xml", "/api/v1/system/status"),
    "radarr": (os.getenv("RADARR_URL", "http://radarr:7878"), "/arr-config/radarr/config.xml", "/api/v3/system/status"),
    "sonarr": (os.getenv("SONARR_URL", "http://sonarr:8989"), "/arr-config/sonarr/config.xml", "/api/v3/system/status"),
    "radarr-2160p": (os.getenv("RADARR_2160P_URL", "http://radarr-2160p:7878"), "/arr-config/radarr-2160p/config.xml", "/api/v3/system/status"),
    "sonarr-2160p": (os.getenv("SONARR_2160P_URL", "http://sonarr-2160p:8989"), "/arr-config/sonarr-2160p/config.xml", "/api/v3/system/status"),
}

def config(path):
    p = Path(path)
    if not p.exists():
        return "", "", "missing"
    try:
        root = ET.parse(p).getroot()
        return (root.findtext("ApiKey", "") or "").strip(), (root.findtext("Port", "") or "").strip(), "ok"
    except Exception as exc:
        return "", "", f"parse-error:{type(exc).__name__}"

def get_json(url, key):
    req = request.Request(url, headers={"X-Api-Key": key})
    try:
        with request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, {"error": str(exc)}
    except Exception as exc:
        return "error", {"error": f"{type(exc).__name__}: {exc}"}

def with_port(url, port):
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc
    if not host:
        return url
    return urlunparse((parsed.scheme or "http", f"{host}:{port}", parsed.path.rstrip("/"), "", "", "")).rstrip("/")

print("ARR API readiness from inside danish-intelligence")
print("Secrets are not printed.")
keys = {}
for name, (base_url, cfg, status_path) in APPS.items():
    key, config_port, cfg_status = config(cfg)
    if config_port.isdigit():
        base_url = with_port(base_url, int(config_port))
        APPS[name] = (base_url, cfg, status_path)
    keys[name] = key
    status, data = get_json(base_url.rstrip("/") + status_path, key) if key else ("no-key", {})
    version = data.get("version") if isinstance(data, dict) else None
    app_name = data.get("appName") if isinstance(data, dict) else None
    config_port = config_port or "<none>"
    app_name = app_name or "<none>"
    version = version or "<none>"
    print(f"{name}: url={base_url} config={cfg_status} config_port={config_port} key_present={bool(key)} http_status={status} app={app_name} version={version}")

prowlarr_key = keys.get("prowlarr", "")
if prowlarr_key:
    status, apps = get_json("http://prowlarr:9696/api/v1/applications", prowlarr_key)
    print(f"\nProwlarr applications: http_status={status}")
    if isinstance(apps, list):
        for app in apps:
            fields = {field.get("name"): field.get("value") for field in app.get("fields", []) if isinstance(field, dict)}
            app_name = app.get("name")
            implementation = app.get("implementation")
            sync_level = app.get("syncLevel")
            base_url = fields.get("baseUrl")
            print(f"- name={app_name} implementation={implementation} syncLevel={sync_level} baseUrl={base_url}")

for name in ("radarr", "sonarr", "radarr-2160p", "sonarr-2160p"):
    key = keys.get(name, "")
    if not key:
        continue
    base_url = APPS[name][0].rstrip("/")
    status, clients = get_json(base_url + "/api/v3/downloadclient", key)
    print(f"\n{name} download clients: http_status={status}")
    if isinstance(clients, list):
        for client in clients:
            fields = {field.get("name"): field.get("value") for field in client.get("fields", []) if isinstance(field, dict)}
            client_name = client.get("name")
            implementation = client.get("implementation")
            enabled = client.get("enable")
            host = fields.get("host")
            port = fields.get("port")
            url_base = fields.get("urlBase")
            category = fields.get("movieCategory") or fields.get("tvCategory")
            print(f"- name={client_name} implementation={implementation} enable={enabled} host={host} port={port} urlBase={url_base} category={category}")
PY'
fi

run_cmd summary/tree sh -c "find '$OUT' -type f -printf '%s %p\n' | sort -n"

tar -czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")"

printf '\nCreated diagnostic archive:\n%s\n\n' "$ARCHIVE"
printf 'Send that tar.gz back for review. The unpacked report is also at:\n%s\n' "$OUT"
