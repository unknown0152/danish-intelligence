#!/usr/bin/env bash
# setup-proxy.sh — DKSubs Proxy Auto-Setup v9.0
# Discovers your Prowlarr/Radarr/Sonarr stack, probes each usenet indexer
# for real NFO support, wires everything up, and syncs Arr custom formats.
set -uo pipefail

VERSION="9.0"
PUBLIC_REPO="https://raw.githubusercontent.com/unknown0152/dksubs-proxy-installer/master"
ENV_FILE=".env"
COMPOSE_FILE="docker-compose.yml"
TMP_JSON="/tmp/prowlarr_indexers.json"
TMP_DB="/tmp/prowlarr_copy.db"
DRY_RUN=${DRY_RUN:-0}

echo "=== DKSubs Proxy Auto-Setup v$VERSION (+ audio CFs) ==="

# ── 0. Preflight ──────────────────────────────────────────────────────────────
for cmd in jq curl docker python3; do
    command -v "$cmd" &>/dev/null || { echo "Error: '$cmd' is required."; exit 1; }
done
docker ps >/dev/null 2>&1 || { echo "Error: Cannot connect to Docker. Run with sudo."; exit 1; }

if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "Error: docker compose not found."; exit 1
fi
echo "  + Docker Compose: $DC"

# ── 1. Discover containers & networks ─────────────────────────────────────────
echo "[1/6] Discovering containers and networks..."
docker ps --format '{{.Names}}' > /tmp/dksubs_containers.txt
find_cntr() {
    grep -E "^$1$" /tmp/dksubs_containers.txt | head -1 \
    || grep -i "$1" /tmp/dksubs_containers.txt | head -1 \
    || echo ""
}

PROWLARR_CNTR=$(find_cntr "prowlarr")
RADARR_CNTR=$(find_cntr "radarr")
SONARR_CNTR=$(find_cntr "sonarr")
[[ -z "$PROWLARR_CNTR" ]] && { echo "Error: Prowlarr container not found."; exit 1; }

# Patch O.a: pin dksubs-proxy to media-stack (friend's-box convention).
# Replaces the original network-discovery from arr containers — we don't want
# dksubs attaching to per-app cosmos-secured networks; only to the shared
# media-stack so other containers reach it via Docker DNS at
# http://dksubs-proxy:9699.
# Auto-detect Docker networks from the Prowlarr container (exclude bridge/host/none).
NETWORKS=$(docker inspect "$PROWLARR_CNTR" \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    | tr ' ' '\n' | grep -v '^$' | grep -vE '^(bridge|host|none)$' | tr '\n' ' ' | xargs)
[[ -z "$NETWORKS" ]] && NETWORKS="bridge"
echo "  + Networks: $NETWORKS (auto-detected from $PROWLARR_CNTR)"

# ── 2. Prowlarr config ────────────────────────────────────────────────────────
echo "[2/6] Reading Prowlarr configuration..."
get_tag() { docker exec "$1" grep -o "<$2>[^<]*</$2>" /config/config.xml \
    | sed -n "s|<$2>\(.*\)</$2>|\1|p" | head -1 || echo ""; }

PROWLARR_API_KEY=$(get_tag "$PROWLARR_CNTR" "ApiKey")
PROWLARR_PORT=$(get_tag "$PROWLARR_CNTR" "Port")
PROWLARR_IP=$(docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{println}}{{end}}" \
    "$PROWLARR_CNTR" | grep -v '^$' | head -1)
P_URL="http://${PROWLARR_IP}:${PROWLARR_PORT}"

RADARR_PORT=7878; SONARR_PORT=8989
[[ -n "$RADARR_CNTR" ]] && RADARR_PORT=$(get_tag "$RADARR_CNTR" "Port")
[[ -n "$SONARR_CNTR" ]] && SONARR_PORT=$(get_tag "$SONARR_CNTR" "Port")
RADARR_PORT=${RADARR_PORT:-7878}; SONARR_PORT=${SONARR_PORT:-8989}
echo "  + Radarr :$RADARR_PORT  Sonarr :$SONARR_PORT"

# Download proxy if missing
[[ ! -f "dksubs-proxy.py" ]] && {
    echo "  + Downloading dksubs-proxy.py..."
    curl -s "https://api.github.com/repos/unknown0152/dksubs-proxy-installer/contents/dksubs-proxy.py" \
        | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['content']).decode())" \
        > dksubs-proxy.py \
    || curl -sSL "$PUBLIC_REPO/dksubs-proxy.py" -o dksubs-proxy.py
}

# Rename Prowlarr indexers to include {DK} tag
if [[ $DRY_RUN -eq 0 ]]; then
    p_idx=$(mktemp)
    curl -sf -H "X-Api-Key: $PROWLARR_API_KEY" "$P_URL/api/v1/indexer" -o "$p_idx" 2>/dev/null
    if [[ -s "$p_idx" ]] && jq -e . "$p_idx" >/dev/null 2>&1; then
        while read -r i; do
            name=$(echo "$i" | jq -r '.name')
            [[ "$name" == *"{DK}"* ]] && continue
            new="${name% [*} {DK}"
            curl -sf -X PUT -H "X-Api-Key: $PROWLARR_API_KEY" -H "Content-Type: application/json" \
                -d "$(echo "$i" | jq --arg n "$new" '.name=$n')" \
                "$P_URL/api/v1/indexer/$(echo "$i" | jq -r '.id')" >/dev/null
        done < <(jq -c '.[]' "$p_idx")
    fi
    rm -f "$p_idx"
fi

curl -sf -H "X-Api-Key: $PROWLARR_API_KEY" "$P_URL/api/v1/indexer" -o "$TMP_JSON"

# ── 3. Extract keys + classify indexers ──────────────────────────────────────
echo "[3/6] Extracting API keys and classifying indexers..."
NFO_IDS="" UNIT3D_IDS="" TITLE_IDS="" CARDI_IDS="" ENRICH_IDS=""
INDEXER_ENV_LINES="" EXTRA_ENV_LINES=""

# Preserve manually tuned per-indexer overrides and routing from an existing .env
SAVED_RATE_LINES=""
SAVED_TITLE_IDS=""
SAVED_ENRICH_IDS=""
[[ -f "$ENV_FILE" ]] && \
    SAVED_RATE_LINES=$(grep -E "^INDEXER_[0-9]+_(RATE_(CALLS|WINDOW)|MAX_NFO_CANDIDATES)=" "$ENV_FILE" 2>/dev/null || true)
[[ -f "$ENV_FILE" ]] && \
    SAVED_TITLE_IDS=$(grep "^TITLE_ONLY_INDEXERS=" "$ENV_FILE" | cut -d= -f2 || true)
[[ -f "$ENV_FILE" ]] && \
    SAVED_ENRICH_IDS=$(grep "^ENRICH_INDEXERS=" "$ENV_FILE" | cut -d= -f2 || true)

if docker cp "$PROWLARR_CNTR:/config/prowlarr.db" "$TMP_DB" 2>/dev/null; then
    if PROBE_OUT=$(python3 - "$TMP_DB" "$TMP_JSON" <<'PYEOF'
import sys, json, sqlite3, urllib.request, re, time

db_path, idx_path = sys.argv[1], sys.argv[2]
with open(idx_path) as f:
    indexers = json.load(f)

# Load API keys + base URLs from Prowlarr's SQLite DB
con = sqlite3.connect(db_path)
key_map, env_out = {}, []
for iid, raw in con.execute("SELECT Id, Settings FROM Indexers WHERE Enable=1"):
    try: s = json.loads(raw or '{}')
    except: continue
    ef = s.get('extraFieldData') or {}
    apikey  = (s.get('apiKey') or s.get('apikey') or ef.get('apiKey') or ef.get('apikey') or '').strip()
    baseurl = (s.get('baseUrl') or s.get('baseurl') or s.get('sitelink') or '').strip().rstrip('/')
    key_map[iid] = {'apikey': apikey, 'baseurl': baseurl}
    if apikey:  env_out.append(f"INDEXER_{iid}_APIKEY={apikey}")
    if baseurl: env_out.append(f"INDEXER_{iid}_BASEURL={baseurl}")
con.close()
for l in env_out: print(f"__ENV__:{l}")

def fetch(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'DKSubs-Proxy/5.3'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except: return None

def nfo_valid(text):
    if not text or len(text) < 30: return False
    t = text.strip()
    if re.match(r'<\?xml|<rss|<html|<!', t, re.I): return False
    if re.search(r'<error\s+code=', t[:300]): return False
    return True

def extract_ids(xml, prefer_nfo=False, limit=5):
    """Extract NZB IDs. If prefer_nfo=True, prefer IDs from releases with nfo=1 attr."""
    nfo_ids, other_ids = [], []
    items = re.split(r'<item[\s>]', xml)
    for item in items[1:]:
        nfo_attr = re.search(r'name=["\']nfo["\'] value=["\'](\d)["\']', item)
        has_nfo = nfo_attr and nfo_attr.group(1) == '1'
        m = re.search(r'[?&](?:id|guid)=([A-Za-z0-9_\-]+)', item)
        if not m:
            m = re.search(r'<guid[^>]*>https?://[^/]+/[^/]+/([^/<&?\s]+)', item)
        if m:
            cid = m.group(1)
            if prefer_nfo and has_nfo:
                if cid not in nfo_ids: nfo_ids.append(cid)
            else:
                if cid not in other_ids: other_ids.append(cid)
    combined = nfo_ids + other_ids
    return combined[:limit]

nfo_ids, nfo_assumed_ids, unit3d_ids, title_ids, extra_out = [], [], [], [], []
QUERIES = ['danish', '1080p', 'bluray', 'remux']

for idx in indexers:
    iid   = idx['id']
    name  = idx.get('name', f'#{iid}')
    proto = idx.get('protocol', '')
    impl  = (idx.get('implementation') or '').lower()

    # ── Torrent indexers ──────────────────────────────────────────────────────
    if proto == 'torrent':
        if 'unit3d' in impl:
            unit3d_ids.append(str(iid))
            print(f"  [UNIT3D]      {iid:3d}  {name}", flush=True)
        else:
            title_ids.append(str(iid))
            print(f"  [Title-only]  {iid:3d}  {name}  (torrent)", flush=True)
        continue

    # ── Usenet indexers ───────────────────────────────────────────────────────
    cfg     = key_map.get(iid, {})
    apikey  = cfg.get('apikey', '')
    baseurl = cfg.get('baseurl', '')
    is_omg  = 'omgwtfnzbs' in baseurl.lower()

    if is_omg:
        extra_out += [f"INDEXER_{iid}_RATE_CALLS=160", f"INDEXER_{iid}_RATE_WINDOW=360",
                      f"INDEXER_{iid}_MAX_NFO_CANDIDATES=5"]

    if not baseurl or not apikey:
        title_ids.append(str(iid))
        print(f"  [Title-only]  {iid:3d}  {name:<38}  ⚠ no API key in Prowlarr DB", flush=True)
        continue

    # Probe: search with multiple queries, test getnfo on candidates.
    # Prefer items with nfo=1 attr. Stop as soon as we get a valid NFO.
    # If inconclusive → still NFO-Hunter (proxy handles failures gracefully).
    found, got_results, seen = False, False, set()
    for q in QUERIES:
        xml = fetch(f"{baseurl}/api?t=search&apikey={apikey}&q={q}&limit=10", timeout=15)
        if not xml: continue
        candidates = [c for c in extract_ids(xml, prefer_nfo=True, limit=5) if c not in seen]
        seen.update(candidates)
        if candidates: got_results = True
        for cid in candidates:
            nfo_text = fetch(f"{baseurl}/api?t=getnfo&apikey={apikey}&id={cid}&raw=1", timeout=10)
            if nfo_valid(nfo_text or ''):
                found = True
                break
            time.sleep(0.15)
        if found: break

    rate = " (rate-limited)" if is_omg else ""
    if not got_results:
        title_ids.append(str(iid))
        print(f"  [Title-only]  {iid:3d}  {name:<38}  ✗ unreachable{rate}", flush=True)
    elif found:
        nfo_ids.append(str(iid))
        print(f"  [NFO-Hunter]  {iid:3d}  {name:<38}  ✓ NFO confirmed{rate}", flush=True)
    else:
        # Inconclusive — probe got results but couldn't confirm getnfo. Emit as
        # "assumed" so bash can respect any existing manual TITLE_ONLY assignment.
        nfo_assumed_ids.append(str(iid))
        print(f"  [assumed]     {iid:3d}  {name:<38}  ~ inconclusive{rate}", flush=True)

# Extended-attr probe: check which title-only indexers return subs/language via extended=1.
# (NFO-Hunter indexers already get enrichment; this only adds to ENRICH_INDEXERS.)
enrich_ids = []
for iid_str in title_ids:
    try: iid = int(iid_str)
    except ValueError: continue
    cfg = key_map.get(iid, {})
    apikey  = cfg.get('apikey', '')
    baseurl = cfg.get('baseurl', '')
    if not apikey or not baseurl: continue
    name = next((idx.get('name', f'#{iid}') for idx in indexers if idx['id'] == iid), f'#{iid}')
    xml = fetch(f"{baseurl}/api?t=movie&imdbid=0360556&apikey={apikey}&extended=1&limit=20", timeout=10)
    if not xml or '<item>' not in xml:
        xml = fetch(f"{baseurl}/api?t=search&q=john+wick&apikey={apikey}&extended=1&limit=10", timeout=10)
    has_attrs = bool(xml and re.search(
        r'name=["\'](?:subs|language)["\'] value=["\'][^"\']{3,}', xml, re.IGNORECASE))
    if has_attrs:
        enrich_ids.append(iid_str)
        print(f"  [Enrich-only] {iid:3d}  {name:<38}  ✓ extended attrs (subs/language)", flush=True)
    time.sleep(0.15)

print(f"__NFO_IDS__:{','.join(nfo_ids)}")
print(f"__NFO_ASSUMED_IDS__:{','.join(nfo_assumed_ids)}")
print(f"__UNIT3D_IDS__:{','.join(unit3d_ids)}")
print(f"__TITLE_IDS__:{','.join(title_ids)}")
print(f"__ENRICH_IDS__:{','.join(enrich_ids)}")
for l in extra_out: print(f"__EXTRA__:{l}")
PYEOF
    ); then
        rm -f "$TMP_DB"
        echo "$PROBE_OUT" | grep -v "^__"
        NFO_IDS=$(echo        "$PROBE_OUT" | grep "^__NFO_IDS__:"         | sed 's/^__NFO_IDS__://')
        NFO_ASSUMED_IDS=$(echo "$PROBE_OUT" | grep "^__NFO_ASSUMED_IDS__:" | sed 's/^__NFO_ASSUMED_IDS__://')
        UNIT3D_IDS=$(echo     "$PROBE_OUT" | grep "^__UNIT3D_IDS__:"      | sed 's/^__UNIT3D_IDS__://')
        TITLE_IDS=$(echo      "$PROBE_OUT" | grep "^__TITLE_IDS__:"       | sed 's/^__TITLE_IDS__://')
        ENRICH_IDS=$(echo     "$PROBE_OUT" | grep "^__ENRICH_IDS__:"      | sed 's/^__ENRICH_IDS__://')
        INDEXER_ENV_LINES=$(echo "$PROBE_OUT" | grep "^__ENV__:"   | sed 's/^__ENV__://')
        EXTRA_ENV_LINES=$(echo   "$PROBE_OUT" | grep "^__EXTRA__:" | sed 's/^__EXTRA__://')
        # For inconclusive indexers: respect existing manual TITLE_ONLY assignment;
        # new indexers (not previously seen) default to NFO-Hunter.
        for id in $(echo "$NFO_ASSUMED_IDS" | tr ',' ' '); do
            [[ -z "$id" ]] && continue
            if echo ",$SAVED_TITLE_IDS," | grep -q ",$id,"; then
                TITLE_IDS="${TITLE_IDS:+$TITLE_IDS,}$id"
                echo "  [Title-only]  $id  (kept from previous manual assignment)"
            else
                NFO_IDS="${NFO_IDS:+$NFO_IDS,}$id"
            fi
        done
        n=$(echo "$INDEXER_ENV_LINES" | grep -c 'APIKEY' 2>/dev/null || echo 0)
        echo "  + Keys loaded: $n indexers"
    else
        rm -f "$TMP_DB"
        echo "  ! Probe failed — using naive classification (all usenet → NFO-Hunter, manual assignments preserved)"
        ALL_USENET=$(jq -r '.[]|select(.protocol=="usenet")|.id|tostring' "$TMP_JSON" | tr '\n' ',' | sed 's/,$//' || echo "")
        UNIT3D_IDS=$(jq -r '.[]|select(.protocol=="torrent" and (.implementation|ascii_downcase|contains("unit3d")))|.id' "$TMP_JSON" | paste -sd, || echo "")
        TITLE_IDS=$(jq -r '.[]|select(.protocol=="torrent" and (.implementation|ascii_downcase|contains("unit3d")|not))|.id' "$TMP_JSON" | paste -sd, || echo "")
        NFO_IDS=""
        for id in $(echo "$ALL_USENET" | tr ',' ' '); do
            [[ -z "$id" ]] && continue
            if echo ",$SAVED_TITLE_IDS," | grep -q ",$id,"; then
                TITLE_IDS="${TITLE_IDS:+$TITLE_IDS,}$id"
            else
                NFO_IDS="${NFO_IDS:+$NFO_IDS,}$id"
            fi
        done
        ENRICH_IDS="$SAVED_ENRICH_IDS"
    fi
else
    rm -f "$TMP_DB" 2>/dev/null || true
    echo "  ! Cannot read Prowlarr DB — using naive classification (manual assignments preserved)"
    ALL_USENET=$(jq -r '.[]|select(.protocol=="usenet")|.id|tostring' "$TMP_JSON" | tr '\n' ',' | sed 's/,$//' || echo "")
    UNIT3D_IDS=$(jq -r '.[]|select(.protocol=="torrent" and (.implementation|ascii_downcase|contains("unit3d")))|.id' "$TMP_JSON" | paste -sd, || echo "")
    TITLE_IDS=$(jq -r '.[]|select(.protocol=="torrent" and (.implementation|ascii_downcase|contains("unit3d")|not))|.id' "$TMP_JSON" | paste -sd, || echo "")
    NFO_IDS=""
    for id in $(echo "$ALL_USENET" | tr ',' ' '); do
        [[ -z "$id" ]] && continue
        if echo ",$SAVED_TITLE_IDS," | grep -q ",$id,"; then
            TITLE_IDS="${TITLE_IDS:+$TITLE_IDS,}$id"
        else
            NFO_IDS="${NFO_IDS:+$NFO_IDS,}$id"
        fi
    done
    ENRICH_IDS="$SAVED_ENRICH_IDS"
fi

# Detect Cosmos-Market-managed install: a running container of the same name
# whose image is the GHCR-published one. In that case we DON'T rebuild from a
# local Dockerfile — we leave the Market container alone and only refresh its
# credentials at /srv/config/dksubs-proxy/.env (then docker restart).
COSMOS_MANAGED=0
if docker inspect dksubs-proxy --format '{{.Config.Image}}' 2>/dev/null \
    | grep -q '^ghcr\.io/.*dksubs-proxy'; then
    COSMOS_MANAGED=1
fi

# ── 4. Build docker-compose + .env ───────────────────────────────────────────
if [[ $DRY_RUN -eq 0 && $COSMOS_MANAGED -eq 0 ]]; then
    echo "[4/6] Building Docker assets..."
    cat > Dockerfile <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dksubs-proxy.py /proxy.py
CMD ["python3", "/proxy.py"]
EOF
    printf 'aiohttp\naiosqlite\npython-dotenv\n' > requirements.txt

    docker rm -f dksubs-proxy 2>/dev/null || true

    {
        echo "services:"
        echo "  dksubs-proxy:"
        echo "    build: ."
        echo "    image: dksubs-proxy:latest"
        echo "    container_name: dksubs-proxy"
        echo "    restart: unless-stopped"
        echo "    env_file: [.env]"
        # Patch M (AI-SAFETY rule 5): NO host port publish.
        # dksubs-proxy is Docker-internal only; reachable as http://dksubs-proxy:9699 via media-stack.
        # echo "    ports: [\"9699:9699\"]"   # ← removed; do NOT re-add
        echo "    volumes: [\"./dksubs-proxy.py:/proxy.py:ro\", \"proxy-cache:/cache\"]"
        echo "    environment: [\"CACHE_DB=/cache/proxy_cache.db\"]"
        echo "    networks:"
        for net in $NETWORKS; do echo "      - $net"; done
        echo "volumes:"
        echo "  proxy-cache:"
        echo "    name: dksubs-proxy-cache"
        if docker volume inspect dksubs-proxy-cache &>/dev/null; then
            echo "    external: true"
        else
            echo "    external: false"
        fi
        echo "networks:"
        for net in $NETWORKS; do
            echo "  $net:"
            echo "    external: true"
        done
    } > "$COMPOSE_FILE"
elif [[ $COSMOS_MANAGED -eq 1 ]]; then
    echo "[4/6] Cosmos-Market-managed install detected — skipping local Dockerfile/compose generation"
fi

# Write .env — merge auto-generated config with preserved manual rate limits
{
    echo "PROWLARR_URL=http://${PROWLARR_CNTR}:${PROWLARR_PORT}"
    echo "PROWLARR_API_KEY=${PROWLARR_API_KEY}"
    echo "NFO_INDEXERS=${NFO_IDS}"
    echo "UNIT3D_INDEXERS=${UNIT3D_IDS}"
    echo "CARDIGANN_INDEXERS=${CARDI_IDS}"
    echo "TITLE_ONLY_INDEXERS=${TITLE_IDS}"
    echo "ENRICH_INDEXERS=${ENRICH_IDS}"
    # Filter mode: drop non-Danish releases from search responses rather than
    # passing them through untagged. Cuts Radarr-visible release count ~84%.
    echo "DROP_NON_DK=1"
    # Minimum release size by category (skipped before NFO/title scan)
    echo "MIN_RELEASE_SIZE_MOVIE=3221225472"
    echo "MIN_RELEASE_SIZE_TV=524288000"
    echo "MIN_RELEASE_SIZE=0"
    echo "${INDEXER_ENV_LINES}"
    # Auto-detected rate limits (e.g. omgwtfnzbs)
    echo "${EXTRA_ENV_LINES}"
    # Re-apply manual rate limit overrides for IDs not already covered above
    if [[ -n "$SAVED_RATE_LINES" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            key="${line%%=*}"
            # Skip if already emitted by auto-detection
            echo "${EXTRA_ENV_LINES}" | grep -q "^${key}=" || echo "$line"
        done <<< "$SAVED_RATE_LINES"
    fi
} | grep -v '^[[:space:]]*$' > "$ENV_FILE.tmp"

# ── 5. Start proxy ────────────────────────────────────────────────────────────
if [[ $DRY_RUN -eq 0 ]]; then
    echo "[5/6] Starting proxy..."
    mv "$ENV_FILE.tmp" "$ENV_FILE"
    chmod 0600 "$ENV_FILE"   # Patch N: .env contains Prowlarr API key + per-indexer API keys (AI-SAFETY rule 4)
    # Also mirror to /srv/config/dksubs-proxy/.env so a Cosmos Market install
    # (which bind-mounts /srv/config/dksubs-proxy → /config) picks up the
    # same credentials. Safe to run on a local-dev install too — the proxy
    # reads /config/.env with override=True only when the file exists.
    mkdir -p /srv/config/dksubs-proxy
    cp -f "$ENV_FILE" /srv/config/dksubs-proxy/.env
    chmod 0600        /srv/config/dksubs-proxy/.env
    if [[ $COSMOS_MANAGED -eq 1 ]]; then
        echo "  + Cosmos-managed: restarting container to pick up new /config/.env"
        docker restart dksubs-proxy >/dev/null
    else
        $DC up -d --build >/dev/null
    fi
    echo -n "  + Waiting for health check"
    until docker exec dksubs-proxy python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:9699/health')" \
        &>/dev/null; do
        echo -n "."; sleep 1
    done
    echo " OK"
fi

# ── 6. Sync Radarr / Sonarr ───────────────────────────────────────────────────
TOTAL_LINKED=0
update_arr() {
    local label=$1 port=$2
    local cntr; cntr=$(find_cntr "$label"); [[ -z "$cntr" ]] && return
    local key; key=$(get_tag "$cntr" "ApiKey")
    local ip; ip=$(docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{println}}{{end}}" \
        "$cntr" | grep -v '^$' | head -1)
    local api="http://${ip}:${port}/api/v3"
    local linked=0

    echo "  + Syncing $label..."

    # ── Delete all managed CFs ─────────────────────────────────────────────────
    # Covers old names (DK, DKSubs, NORDIC.ENG) and current names (DKAudio, DKSubs,
    # plus the 8 audio codec CFs) so re-runs always start clean.
    local all_cfs; all_cfs=$(curl -s -H "X-Api-Key: $key" "$api/customformat")
    local old_cf_ids='[]'
    if jq -e . <<<"$all_cfs" >/dev/null 2>&1; then
        old_cf_ids=$(echo "$all_cfs" | jq '[.[]|select(
            .name=="DK" or .name=="DKSubs" or .name=="DKAudio" or .name=="NORDIC.ENG" or
            .name=="TrueHD Atmos" or .name=="DTS-X" or .name=="TrueHD" or
            .name=="DTS-HD MA" or .name=="EAC3 Atmos" or .name=="EAC3" or
            .name=="DTS" or .name=="AAC"
        )|.id]')
        local deleted=0
        while read -r cf_del_id; do
            curl -s -X DELETE -H "X-Api-Key: $key" "$api/customformat/$cf_del_id" >/dev/null
            ((deleted++)) || true
        done < <(echo "$all_cfs" | jq -r '.[]|select(
            .name=="DK" or .name=="DKSubs" or .name=="DKAudio" or .name=="NORDIC.ENG" or
            .name=="TrueHD Atmos" or .name=="DTS-X" or .name=="TrueHD" or
            .name=="DTS-HD MA" or .name=="EAC3 Atmos" or .name=="EAC3" or
            .name=="DTS" or .name=="AAC"
        )|.id')
        [[ $deleted -gt 0 ]] && { echo "    + Removed $deleted old CF(s)"; sleep 1; }
    fi

    # ── Create DKAudio CF ──────────────────────────────────────────────────────
    local p
    # DKAudio regex matches both legacy bracketed tags (any separator) AND the
    # current bracketless `.DKaudio` suffix. Bracketless required because nzbdav's
    # symlink layer corrupts bracket+separator filenames; bracketless because
    # `.DKSubs` would trip Radarr's hardcoded-subs detector.
    p='{"id":0,"name":"DKAudio","includeCustomFormatWhenRenaming":true,"specifications":[{"name":"DKAudio Tag","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"(?:\\[DKAudio[-:.][^\\]]+\\]|\\.DKaudio\\b)"}]}]}'
    local dkaudio_id; dkaudio_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: DKAudio (id=$dkaudio_id)"

    # ── Create DKSubs CF ───────────────────────────────────────────────────────
    # DKSubs regex: backward-compatible with legacy `[DK*]` bracket forms +
    # current `.DKOK` suffix. `.DKOK` was chosen because `.DKSubs` would match
    # Radarr's built-in hardcoded-subs detector and get every release rejected.
    p='{"id":0,"name":"DKSubs","includeCustomFormatWhenRenaming":true,"specifications":[{"name":"DKSubs Tag","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"(?:\\[DK[-:.][^\\]]+\\]|\\.DKOK\\b)"}]}]}'
    local dksubs_id; dksubs_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: DKSubs (id=$dksubs_id)"

    # ── Create audio codec CFs (used as tiebreakers within profiles) ───────────
    p='{"id":0,"name":"TrueHD Atmos","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"TrueHD","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bTrueHD\\b"}]},{"name":"Atmos","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bAtmos\\b"}]}]}'
    local truehd_atmos_id; truehd_atmos_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: TrueHD Atmos (id=$truehd_atmos_id)"

    p='{"id":0,"name":"DTS-X","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"DTS-X","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"DTS[-. ]?X\\b|DTS:X"}]}]}'
    local dtsx_id; dtsx_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: DTS-X (id=$dtsx_id)"

    p='{"id":0,"name":"TrueHD","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"TrueHD","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bTrueHD\\b"}]},{"name":"NOT Atmos","implementation":"ReleaseTitleSpecification","negate":true,"required":true,"fields":[{"name":"value","value":"\\bAtmos\\b"}]}]}'
    local truehd_id; truehd_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: TrueHD (id=$truehd_id)"

    p='{"id":0,"name":"DTS-HD MA","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"DTS-HD MA","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"DTS[-. ]HD[-. ]?MA|DTS\\.HD\\.MA|DTSMA\\b"}]}]}'
    local dtshd_id; dtshd_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: DTS-HD MA (id=$dtshd_id)"

    p='{"id":0,"name":"EAC3 Atmos","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"EAC3","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"EAC3|DD\\+|E-AC-3"}]},{"name":"Atmos","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bAtmos\\b"}]}]}'
    local eac3_atmos_id; eac3_atmos_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: EAC3 Atmos (id=$eac3_atmos_id)"

    p='{"id":0,"name":"EAC3","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"EAC3","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"EAC3|DD\\+|E-AC-3"}]},{"name":"NOT Atmos","implementation":"ReleaseTitleSpecification","negate":true,"required":true,"fields":[{"name":"value","value":"\\bAtmos\\b"}]}]}'
    local eac3_id; eac3_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: EAC3 (id=$eac3_id)"

    p='{"id":0,"name":"DTS","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"DTS","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bDTS\\b"}]},{"name":"NOT DTS-HD/X","implementation":"ReleaseTitleSpecification","negate":true,"required":true,"fields":[{"name":"value","value":"DTS[-. ]HD|DTS[-. ]?X\\b|DTS:X"}]}]}'
    local dts_id; dts_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: DTS (id=$dts_id)"

    p='{"id":0,"name":"AAC","includeCustomFormatWhenRenaming":false,"specifications":[{"name":"AAC","implementation":"ReleaseTitleSpecification","negate":false,"required":true,"fields":[{"name":"value","value":"\\bAAC\\b"}]}]}'
    local aac_id; aac_id=$(curl -s -X POST -H "X-Api-Key: $key" \
        -H "Content-Type: application/json" -d "$p" "$api/customformat" | jq -r '.id' 2>/dev/null || echo "")
    echo "    + Created CF: AAC (id=$aac_id)"

    # ── Point each indexer at the proxy ───────────────────────────────────────
    local resp; resp=$(mktemp)
    if curl -sf -H "X-Api-Key: $key" "$api/indexer" -o "$resp" && [[ -s "$resp" ]]; then
        while read -r i; do
            local iname; iname=$(echo "$i" | jq -r '.name')
            local base;  base=$(echo "$iname" | sed 's/ \[.*//; s/ {.*//')
            local pid;   pid=$(jq -r --arg n "$base" \
                '.[]|select((.name|ascii_downcase)|startswith($n|ascii_downcase))|.id' \
                "$TMP_JSON" | head -1)
            [[ -z "$pid" || "$pid" == "null" ]] && continue
            local payload; payload=$(echo "$i" | jq \
                --arg u "http://dksubs-proxy:9699/${pid}" \
                --arg n "${base} {DK}" \
                --arg k "$PROWLARR_API_KEY" \
                '(.fields[]|select(.name=="baseUrl")|.value)=$u |
                 (.fields[]|select(.name=="apiKey")|.value)=$k |
                 .name=$n | .enable=true')
            curl -sf -X PUT -H "X-Api-Key: $key" -H "Content-Type: application/json" \
                -d "$payload" "$api/indexer/$(echo "$i" | jq -r .id)?forceSave=true" >/dev/null
            ((linked++)); ((TOTAL_LINKED++))
        done < <(jq -c '.[]' "$resp")
    fi
    rm -f "$resp"

    # ── Delete managed profiles (rebuild fresh) ────────────────────────────────
    local all_profs; all_profs=$(curl -sf -H "X-Api-Key: $key" "$api/qualityprofile")
    if jq -e . <<<"$all_profs" >/dev/null 2>&1; then
        local pdel=0
        while read -r pdel_id; do
            curl -s -X DELETE -H "X-Api-Key: $key" "$api/qualityprofile/$pdel_id" >/dev/null
            ((pdel++)) || true
        done < <(echo "$all_profs" | jq -r \
            '.[]|select(.name=="NORDIC" or .name=="Danish Audio" or .name=="Danish Subtitles")|.id')
        [[ $pdel -gt 0 ]] && { echo "    + Removed $pdel managed profile(s)"; sleep 1; }
    fi

    # ── Update all remaining profiles ─────────────────────────────────────────
    # Strips old CF IDs, adds DKAudio=10000, DKSubs=10000, audio codec tiebreakers.
    if [[ -n "$dkaudio_id" && "$dkaudio_id" != "null" ]]; then
        local profiles; profiles=$(curl -sf -H "X-Api-Key: $key" "$api/qualityprofile")
        echo "$profiles" | jq -c '.[]' | while read -r prof; do
            local updated
            updated=$(echo "$prof" | jq -c \
                --arg dk "$dkaudio_id" --arg ds "$dksubs_id" \
                --arg ta "$truehd_atmos_id" --arg dx "$dtsx_id" \
                --arg th "$truehd_id" --arg dm "$dtshd_id" \
                --arg ea "$eac3_atmos_id" --arg ec "$eac3_id" \
                --arg dt "$dts_id" --arg ac "$aac_id" \
                --argjson old "$old_cf_ids" '
                .formatItems = [.formatItems[] | select(.format as $f | ($old | index($f)) == null)] |
                (if ($dk != "" and $dk != "null") then .formatItems += [{"format":($dk|tonumber),"score":10000}] else . end) |
                (if ($ds != "" and $ds != "null") then .formatItems += [{"format":($ds|tonumber),"score":10000}] else . end) |
                (if ($ta != "" and $ta != "null") then .formatItems += [{"format":($ta|tonumber),"score":2000}]  else . end) |
                (if ($dx != "" and $dx != "null") then .formatItems += [{"format":($dx|tonumber),"score":1800}]  else . end) |
                (if ($th != "" and $th != "null") then .formatItems += [{"format":($th|tonumber),"score":1600}]  else . end) |
                (if ($dm != "" and $dm != "null") then .formatItems += [{"format":($dm|tonumber),"score":1400}]  else . end) |
                (if ($ea != "" and $ea != "null") then .formatItems += [{"format":($ea|tonumber),"score":1200}]  else . end) |
                (if ($ec != "" and $ec != "null") then .formatItems += [{"format":($ec|tonumber),"score":1000}]  else . end) |
                (if ($dt != "" and $dt != "null") then .formatItems += [{"format":($dt|tonumber),"score":500}]   else . end) |
                (if ($ac != "" and $ac != "null") then .formatItems += [{"format":($ac|tonumber),"score":100}]   else . end)')
            curl -sf -X PUT -H "X-Api-Key: $key" -H "Content-Type: application/json" \
                -d "$updated" "$api/qualityprofile/$(echo "$prof" | jq -r .id)" >/dev/null
        done
        echo "    + Updated all profiles: DKAudio/DKSubs=10000 | TrueHD Atmos=2000 … AAC=100"
    fi

    # ── Create Danish Audio profile ─────────────────────────────────────────────
    # minFormatScore=10000: requires DKAudio tag (scores 10000) — audio-dubbed DK releases.
    # DKSubs is NOT scored here — a release with only Danish subs (no audio) would
    # otherwise pass minFormatScore and get grabbed despite being the wrong profile.
    local profiles2; profiles2=$(curl -sf -H "X-Api-Key: $key" "$api/qualityprofile")
    if [[ -n "$dkaudio_id" && "$dkaudio_id" != "null" ]]; then
        local da_prof
        da_prof=$(echo "$profiles2" | jq -c \
            --arg dk "$dkaudio_id" --arg ds "$dksubs_id" \
            --arg ta "$truehd_atmos_id" --arg dx "$dtsx_id" \
            --arg th "$truehd_id" --arg dm "$dtshd_id" \
            --arg ea "$eac3_atmos_id" --arg ec "$eac3_id" \
            --arg dt "$dts_id" --arg ac "$aac_id" '
            .[0] | del(.id) |
            .name = "Danish Audio" |
            .minFormatScore = 10000 |
            .cutoffFormatScore = 0 |
            .language = {"id": -1, "name": "Any"} |
            .formatItems = [] |
            (if ($dk != "" and $dk != "null") then .formatItems += [{"format":($dk|tonumber),"score":10000}] else . end) |
            (if ($ds != "" and $ds != "null") then .formatItems += [{"format":($ds|tonumber),"score":0}] else . end) |
            (if ($ta != "" and $ta != "null") then .formatItems += [{"format":($ta|tonumber),"score":2000}]  else . end) |
            (if ($dx != "" and $dx != "null") then .formatItems += [{"format":($dx|tonumber),"score":1800}]  else . end) |
            (if ($th != "" and $th != "null") then .formatItems += [{"format":($th|tonumber),"score":1600}]  else . end) |
            (if ($dm != "" and $dm != "null") then .formatItems += [{"format":($dm|tonumber),"score":1400}]  else . end) |
            (if ($ea != "" and $ea != "null") then .formatItems += [{"format":($ea|tonumber),"score":1200}]  else . end) |
            (if ($ec != "" and $ec != "null") then .formatItems += [{"format":($ec|tonumber),"score":1000}]  else . end) |
            (if ($dt != "" and $dt != "null") then .formatItems += [{"format":($dt|tonumber),"score":500}]   else . end) |
            (if ($ac != "" and $ac != "null") then .formatItems += [{"format":($ac|tonumber),"score":100}]   else . end)')
        curl -sf -X POST -H "X-Api-Key: $key" -H "Content-Type: application/json" \
            -d "$da_prof" "$api/qualityprofile" >/dev/null \
            && echo "    + Created profile: Danish Audio (minFormatScore=10000, DKAudio=10000)"
    fi

    # ── Create Danish Subtitles profile ─────────────────────────────────────────────
    # minFormatScore=10000: requires DKSubs tag (scores 10000) — English content + DK subs.
    if [[ -n "$dksubs_id" && "$dksubs_id" != "null" ]]; then
        local es_prof
        es_prof=$(echo "$profiles2" | jq -c \
            --arg dk "$dkaudio_id" --arg ds "$dksubs_id" \
            --arg ta "$truehd_atmos_id" --arg dx "$dtsx_id" \
            --arg th "$truehd_id" --arg dm "$dtshd_id" \
            --arg ea "$eac3_atmos_id" --arg ec "$eac3_id" \
            --arg dt "$dts_id" --arg ac "$aac_id" '
            .[0] | del(.id) |
            .name = "Danish Subtitles" |
            .minFormatScore = 10000 |
            .cutoffFormatScore = 0 |
            .language = {"id": -1, "name": "Any"} |
            .formatItems = [] |
            (if ($ds != "" and $ds != "null") then .formatItems += [{"format":($ds|tonumber),"score":10000}] else . end) |
            (if ($dk != "" and $dk != "null") then .formatItems += [{"format":($dk|tonumber),"score":10000}] else . end) |
            (if ($ta != "" and $ta != "null") then .formatItems += [{"format":($ta|tonumber),"score":2000}]  else . end) |
            (if ($dx != "" and $dx != "null") then .formatItems += [{"format":($dx|tonumber),"score":1800}]  else . end) |
            (if ($th != "" and $th != "null") then .formatItems += [{"format":($th|tonumber),"score":1600}]  else . end) |
            (if ($dm != "" and $dm != "null") then .formatItems += [{"format":($dm|tonumber),"score":1400}]  else . end) |
            (if ($ea != "" and $ea != "null") then .formatItems += [{"format":($ea|tonumber),"score":1200}]  else . end) |
            (if ($ec != "" and $ec != "null") then .formatItems += [{"format":($ec|tonumber),"score":1000}]  else . end) |
            (if ($dt != "" and $dt != "null") then .formatItems += [{"format":($dt|tonumber),"score":500}]   else . end) |
            (if ($ac != "" and $ac != "null") then .formatItems += [{"format":($ac|tonumber),"score":100}]   else . end)')
        curl -sf -X POST -H "X-Api-Key: $key" -H "Content-Type: application/json" \
            -d "$es_prof" "$api/qualityprofile" >/dev/null \
            && echo "    + Created profile: Danish Subtitles (minFormatScore=10000, DKSubs=10000, DKAudio=10000)"
    fi

    # ── Profile hardening: disable junk qualities, cutoff=Remux-2160p, upgrades on
    # Cutoff target name differs between Radarr ("Remux-2160p") and Sonarr
    # ("Bluray-2160p Remux"). Looked up by name to be version-agnostic.
    local qd; qd=$(curl -sf -H "X-Api-Key: $key" "$api/qualitydefinition")
    local cutoff_id
    cutoff_id=$(echo "$qd" | jq -r '.[] | select(.quality.name=="Remux-2160p" or .quality.name=="Bluray-2160p Remux") | .quality.id' | head -1)
    if [[ -n "$cutoff_id" && "$cutoff_id" != "null" ]]; then
        local profs3; profs3=$(curl -sf -H "X-Api-Key: $key" "$api/qualityprofile")
        echo "$profs3" | jq -c '.[] | select(.name=="Danish Audio" or .name=="Danish Subtitles")' | while read -r prof; do
            local pid; pid=$(echo "$prof" | jq -r '.id')
            local pname; pname=$(echo "$prof" | jq -r '.name')
            local hardened
            hardened=$(echo "$prof" | jq -c --argjson cut "$cutoff_id" '
                .cutoff = $cut |
                .upgradeAllowed = true |
                .items = [.items[] | (
                    if .quality and (.quality.name | IN(
                        "Unknown","WORKPRINT","CAM","TELESYNC","TELECINE","REGIONAL",
                        "DVDSCR","SDTV","DVD","DVD-R","WEBDL-480p","WEBRip-480p",
                        "Bluray-480p","Bluray-576p","HDTV-720p")) then
                        .allowed = false
                    elif (.name // "") == "WEB 480p" then
                        .allowed = false |
                        .items = [.items[] | .allowed = false]
                    else . end
                )]')
            curl -sf -X PUT -H "X-Api-Key: $key" -H "Content-Type: application/json" \
                -d "$hardened" "$api/qualityprofile/$pid" >/dev/null
            echo "    + Hardened profile: $pname (cutoff=$cutoff_id, upgrades=on, low qualities disabled)"
        done
    fi

    # ── Quality Definition size caps (per-quality MB/min limits) ──────────────
    # min size rejects fake/tiny releases; max prevents 100GB grabs unless desired.
    if jq -e . <<<"$qd" >/dev/null 2>&1; then
        echo "$qd" | jq -c '.[]' | while read -r q; do
            local qname; qname=$(echo "$q" | jq -r '.quality.name')
            local qid;   qid=$(echo "$q" | jq -r '.id')
            local mn pf mx
            case "$qname" in
                "HDTV-720p"|"WEBDL-720p"|"WEBRip-720p")   mn=2;  pf=15;  mx=50  ;;
                "Bluray-720p")                            mn=4;  pf=20;  mx=60  ;;
                "HDTV-1080p"|"WEBDL-1080p"|"WEBRip-1080p") mn=4;  pf=25;  mx=70  ;;
                "Bluray-1080p")                           mn=8;  pf=40;  mx=90  ;;
                "Remux-1080p"|"Bluray-1080p Remux")       mn=12; pf=60;  mx=120 ;;
                "HDTV-2160p"|"WEBDL-2160p"|"WEBRip-2160p") mn=10; pf=60;  mx=150 ;;
                "Bluray-2160p")                           mn=20; pf=150; mx=400 ;;
                "Remux-2160p"|"Bluray-2160p Remux")       mn=30; pf=250; mx=600 ;;
                *) continue ;;
            esac
            local updated; updated=$(echo "$q" | jq -c --argjson mn $mn --argjson pf $pf --argjson mx $mx \
                '.minSize=$mn | .preferredSize=$pf | .maxSize=$mx')
            curl -sf -X PUT -H "X-Api-Key: $key" -H "Content-Type: application/json" \
                -d "$updated" "$api/qualitydefinition/$qid" >/dev/null
        done
        echo "    + Quality Definitions: 720p/1080p/2160p size caps applied"
    fi

    # ── Strip per-indexer category lists ───────────────────────────────────────
    # Categories to drop. Movies (2xxx): Other/SD/3D/DVD. TV (5xxx): SD only.
    # Prowlarr's syncCategories filter alone doesn't rewrite existing indexer
    # entries; we must edit them directly on each Arr.
    local drop_movie='[2020,2030,2060,2070]'
    local drop_tv='[5030]'
    # pick the right drop set based on the Arr label
    local drop_set
    case "$label" in
        radarr*) drop_set="$drop_movie" ;;
        sonarr*) drop_set="$drop_tv"    ;;
        *)       drop_set='[]'          ;;
    esac
    local ix_resp; ix_resp=$(curl -sf -H "X-Api-Key: $key" "$api/indexer" || echo "[]")
    if jq -e . <<<"$ix_resp" >/dev/null 2>&1 && [[ "$drop_set" != "[]" ]]; then
        echo "$ix_resp" | jq -c '.[]' | while read -r ix; do
            local ixid; ixid=$(echo "$ix" | jq -r '.id')
            local stripped
            stripped=$(echo "$ix" | jq -c --argjson drop "$drop_set" '
                (.fields[] | select(.name=="categories") | .value) |= [.[] | select(. as $c | $drop | index($c) | not)]')
            curl -sf -X PUT -H "X-Api-Key: $key" -H "Content-Type: application/json" \
                -d "$stripped" "$api/indexer/$ixid?forceSave=true" >/dev/null
        done
        echo "    + Stripped categories $drop_set from indexers"
    fi

    # ── Clean up legacy release profiles (replaced by minFormatScore) ──────────
    local all_rps; all_rps=$(curl -s -H "X-Api-Key: $key" "$api/releaseprofile")
    if jq -e . <<<"$all_rps" >/dev/null 2>&1; then
        local rp_deleted=0
        while read -r rpid; do
            curl -s -X DELETE -H "X-Api-Key: $key" "$api/releaseprofile/$rpid" >/dev/null
            ((rp_deleted++)) || true
        done < <(echo "$all_rps" | jq -r \
            '.[] | select((.required // []) | any(test("\\[DK"))) | .id')
        [[ $rp_deleted -gt 0 ]] && echo "    + Removed $rp_deleted legacy release profile(s)"
    fi

    echo "    + Linked $linked indexer(s)"
}

# Patch V: set Prowlarr's App Sync level to addOnly so it doesn't re-push
# its own URLs to the arrs and overwrite the dksubs-proxy rewire.
# Without this, Prowlarr re-syncs on every restart and undoes update_arr.
set_prowlarr_app_sync_addonly() {
    local p_url="$1"
    local p_key="$2"
    local apps
    apps=$(curl -sf -H "X-Api-Key: $p_key" "$p_url/api/v1/applications") || {
        echo "  ! could not list Prowlarr apps; skipping syncLevel=addOnly"
        return 1
    }
    local count=0
    while read -r a; do
        local id name arr_key
        id=$(echo "$a" | jq -r '.id')
        name=$(echo "$a" | jq -r '.name')
        local arr_cfg=""
        case "$name" in
            Sonarr)       arr_cfg=/srv/config/sonarr/config.xml    ;;
            Radarr)       arr_cfg=/srv/config/radarr/config.xml    ;;
            *) echo "  ! unknown app name '$name' — skipping"; continue ;;
        esac
        [ -f "$arr_cfg" ] || { echo "  ! $arr_cfg not found — skipping $name"; continue; }
        arr_key=$(grep -oP '(?<=<ApiKey>)[^<]+' "$arr_cfg")
        local payload
        payload=$(echo "$a" | jq \
            --arg sl "addOnly" \
            --arg ak "$arr_key" \
            '.syncLevel = $sl |
             (.fields[] | select(.name=="apiKey") | .value) = $ak')
        local code
        code=$(curl -sS -o /dev/null -w "%{http_code}" \
            -X PUT -H "X-Api-Key: $p_key" -H "Content-Type: application/json" \
            -d "$payload" "$p_url/api/v1/applications/$id?forceSave=true")
        if [ "$code" -ge 200 ] && [ "$code" -lt 300 ]; then
            ((count++)) || true
            echo "  + $name syncLevel=addOnly (HTTP $code)"
        else
            echo "  ! $name PUT failed (HTTP $code)"
        fi
    done < <(echo "$apps" | jq -c '.[]')
    echo "  Prowlarr apps set to addOnly: $count"
}

# Trim each Prowlarr application's syncCategories so SD/Other/3D/DVD movie cats
# (and SD TV cats) never sync down to Radarr/Sonarr from new indexers.
# Note: this filter only affects future indexer syncs; existing indexer category
# lists on each Arr are rewritten directly by update_arr.
trim_prowlarr_sync_categories() {
    local p_url="$1" p_key="$2"
    local apps; apps=$(curl -sf -H "X-Api-Key: $p_key" "$p_url/api/v1/applications") || return 1
    echo "$apps" | jq -c '.[]' | while read -r a; do
        local id name drop
        id=$(echo "$a" | jq -r '.id')
        name=$(echo "$a" | jq -r '.name')
        case "$name" in
            Radarr) drop='[2020,2030,2060,2070]' ;;
            Sonarr) drop='[5030]' ;;
            *) continue ;;
        esac
        local payload
        payload=$(echo "$a" | jq --argjson d "$drop" \
            '(.fields[] | select(.name=="syncCategories") | .value) |= [.[] | select(. as $c | $d | index($c) | not)]')
        curl -sf -X PUT -H "X-Api-Key: $p_key" -H "Content-Type: application/json" \
            -d "$payload" "$p_url/api/v1/applications/$id?forceSave=true" >/dev/null \
            && echo "  + $name syncCategories trimmed (-$drop)"
    done
    # Kick a sync so the change takes effect immediately
    curl -sf -X POST -H "X-Api-Key: $p_key" -H "Content-Type: application/json" \
        -d '{"name":"ApplicationIndexerSync","forceSync":true}' \
        "$p_url/api/v1/command" >/dev/null
}

echo "[6/6] Syncing Radarr and Sonarr..."
update_arr "radarr" "$RADARR_PORT"
update_arr "sonarr" "$SONARR_PORT"

echo "[6.5/6] Setting Prowlarr App Sync level to addOnly..."
set_prowlarr_app_sync_addonly "$P_URL" "$PROWLARR_API_KEY"

echo "[6.6/6] Trimming Prowlarr syncCategories (drop SD/Other/3D/DVD)..."
trim_prowlarr_sync_categories "$P_URL" "$PROWLARR_API_KEY"

# ── 7. Install host-side force-import safety net ──────────────────────────────
# nzbdav uses random hash filenames inside completed folders when the NZB itself
# is obfuscated. Radarr/Sonarr auto-importers parse the *filename* for movie info
# and fail when it's a 32-char hash. The folder name is correct — manualimport
# uses it and succeeds. This script polls the queue every 1 minute, finds items
# stuck in importPending/importBlocked, and force-imports them via manualimport.
echo "[7/7] Installing arr-force-import safety net (host-side systemd timer)..."
if [[ $DRY_RUN -eq 0 ]]; then
cat >/usr/local/bin/arr-force-import.py <<'AFI_PY'
#!/usr/bin/env python3
"""
Force-import items stuck in 'importBlocked' / 'importPending' on Radarr/Sonarr.

nzbdav obfuscated downloads land with random hash filenames; Radarr's auto-importer
can't parse those. We call manualimport (which uses the folder name) instead.

Runs from systemd timer. Idempotent — only acts on currently-stuck items.
"""
import json, urllib.request, ssl, subprocess, time, sys, socket

CONFIG = [
    ("radarr", "/srv/config/radarr/config.xml", 7878, "movie"),
    ("sonarr", "/srv/config/sonarr/config.xml", 8989, "series"),
]

socket.setdefaulttimeout(45)
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE

def call(base, path, api_key, method="GET", body=None, retries=2):
    h = {"X-Api-Key": api_key}
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"]="application/json"
    last_err = None
    for attempt in range(retries+1):
        req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=45) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try: body_resp = json.loads(e.read() or b"{}")
            except: body_resp = {}
            return e.code, body_resp
        except (TimeoutError, urllib.error.URLError, socket.timeout) as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return 0, {"error": last_err}

def process_arr(name, conf_path, port, kind):
    api_key = subprocess.check_output(f"grep -oP '<ApiKey>\\K[^<]+' {conf_path}", shell=True).decode().strip()
    # Use media-stack network specifically — Arrs may be on multiple networks
    # (e.g. cosmos-<arr>-XXX + media-stack); a generic range would concatenate
    # IPs into garbage.
    ip = subprocess.check_output(
        f"docker inspect {name} --format '{{{{(index .NetworkSettings.Networks \"media-stack\").IPAddress}}}}'",
        shell=True).decode().strip()
    base = f"http://{ip}:{port}"
    api = "/api/v3"

    code, q = call(base, f"{api}/queue?pageSize=500&includeMovie=true&includeSeries=true", api_key)
    if code != 200:
        print(f"[{name}] queue fetch failed HTTP {code}")
        return 0, 0
    blocked = [r for r in q.get('records', [])
               if r.get('trackedDownloadState') in ('importBlocked', 'importPending')
               and r.get('status') == 'completed']
    if not blocked:
        print(f"[{name}] no stuck items")
        return 0, 0

    print(f"[{name}] {len(blocked)} stuck items to resolve")
    ok = 0; fail = 0
    for r in blocked:
        title = r.get('title', '?')[:75]
        dlid = r.get('downloadId')
        if not dlid:
            print(f"  ! {title}  (no downloadId)"); fail += 1; continue

        code, items = call(base, f"{api}/manualimport?downloadId={dlid}", api_key)
        if code != 200 or not items:
            print(f"  ! {title}  (manualimport HTTP {code})"); fail += 1; continue

        files = []
        for it in items:
            f = {
                "path": it["path"],
                "quality": it["quality"],
                "languages": it.get("languages", []),
                "releaseGroup": it.get("releaseGroup", ""),
                "downloadId": dlid,
            }
            if kind == "movie":
                mid = (it.get('movie') or {}).get('id') or r.get('movieId')
                if not mid:
                    continue
                f["movieId"] = mid
            else:
                sid = (it.get('series') or {}).get('id') or r.get('seriesId')
                eps = [e.get('id') for e in (it.get('episodes') or []) if e.get('id')]
                if not sid or not eps:
                    continue
                f["seriesId"] = sid
                f["episodeIds"] = eps
                f["episodeFileId"] = 0
            files.append(f)

        if not files:
            print(f"  ! {title}  (no valid file candidates)"); fail += 1; continue

        body = {"name": "ManualImport", "files": files, "importMode": "auto"}
        code, resp = call(base, f"{api}/command", api_key, method="POST", body=body)
        if code in (200, 201):
            print(f"  + {title}  ({len(files)} file(s))")
            ok += 1
        else:
            msg = str(resp)[:120]
            print(f"  ! {title}  (command HTTP {code}: {msg})")
            fail += 1
        time.sleep(0.5)

    return ok, fail

if __name__ == "__main__":
    total_ok = total_fail = 0
    for name, conf_path, port, kind in CONFIG:
        try:
            ok, fail = process_arr(name, conf_path, port, kind)
            total_ok += ok; total_fail += fail
        except Exception as e:
            print(f"[{name}] EXCEPTION: {e}")
            total_fail += 1
    print(f"\nresult: {total_ok} queued, {total_fail} failed")
    sys.exit(0 if total_fail == 0 else 1)
AFI_PY
chmod +x /usr/local/bin/arr-force-import.py

cat >/etc/systemd/system/arr-force-import.service <<'AFI_SVC'
[Unit]
Description=Force-import items stuck in Radarr/Sonarr importBlocked state
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/arr-force-import.py
StandardOutput=journal
StandardError=journal
TimeoutStartSec=600
AFI_SVC

cat >/etc/systemd/system/arr-force-import.timer <<'AFI_TIM'
[Unit]
Description=Periodically resolve stuck Radarr/Sonarr imports

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
AFI_TIM

    systemctl daemon-reload
    systemctl enable --now arr-force-import.timer >/dev/null 2>&1 && \
        echo "  + arr-force-import.timer enabled (runs every 1 min)"
fi

rm -f "$TMP_JSON" /tmp/dksubs_containers.txt

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DKSubs Proxy v$VERSION ready"
echo "  Health:       curl http://localhost:9699/health"
echo "  Metrics:      curl http://localhost:9699/metrics"
echo "  Proxy logs:   docker logs -f dksubs-proxy"
echo "  CFs created:  DKAudio, DKSubs, TrueHD Atmos, DTS-X, TrueHD,"
echo "                DTS-HD MA, EAC3 Atmos, EAC3, DTS, AAC"
echo "  Profiles:     Danish Audio (minScore=10000), Danish Subtitles (minScore=10000)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
