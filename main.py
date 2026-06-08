#!/usr/bin/env python3
"""dksubs-proxy v6.0 — auto-bundled from src/ modules. DO NOT EDIT directly."""

########################################################################
# Module: __init__
########################################################################

"""
DKSubs NFO-Hunter Proxy  v6.0
==============================
Universal Translator: Extended-attr enrichment for both NFO and enrich-only
indexers, plus v6.0 API-spend reduction layers (request dedup with
single-flight lock, movie-verdict cache, cross-indexer release-name dedup +
NFO early-exit, per-indexer NFO budget scaled by rolling hit-rate).

All v6.0 layers are individually env-flag gated, with DKSUBS_PROXY_V56_FEATURES=0
as the global kill switch that reverts to v5.5 behavior.
"""

import asyncio
import collections
import contextvars
import datetime
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()                              # /app/.env when running under `docker compose` w/ env_file
load_dotenv("/config/.env", override=True) # /config/.env when installed via Cosmos Market (bind mount)

VERSION = "6.0"


def _clean_env_value(name: str) -> str:
    value = os.getenv(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _read_arr_api_key(app_name: str) -> str:
    for path in (f"/arr-config/{app_name}/config.xml", f"/srv/config/{app_name}/config.xml"):
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            return ET.parse(cfg).getroot().findtext("ApiKey", default="").strip()
        except Exception:
            continue
    return ""


PROWLARR_URL = os.getenv("PROWLARR_URL", "http://Prowlarr:9696").rstrip("/")
PROWLARR_API_KEY = (
    _clean_env_value("PROWLARR_API_KEY")
    or _clean_env_value("PROWLARR_APIKEY")
    or _read_arr_api_key("prowlarr")
)
ALTMOUNT_URL = os.getenv("ALTMOUNT_URL", "http://altmount:8080/sabnzbd").rstrip("?")
ALTMOUNT_API_KEY = _clean_env_value("ALTMOUNT_APIKEY") or _clean_env_value("ALTMOUNT_API_KEY")
RADARR_URL = os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = _clean_env_value("RADARR_APIKEY") or _clean_env_value("RADARR_API_KEY") or _read_arr_api_key("radarr")
SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = _clean_env_value("SONARR_APIKEY") or _clean_env_value("SONARR_API_KEY") or _read_arr_api_key("sonarr")
LISTEN_HOST  = os.getenv("LISTEN_HOST",  "0.0.0.0")
LISTEN_PORT  = int(os.getenv("LISTEN_PORT",  "9699"))
CACHE_DB              = os.getenv("CACHE_DB",              "proxy_cache.db")
NFO_TIMEOUT           = float(os.getenv("NFO_TIMEOUT",           "5.0"))
CACHE_TTL_NEGATIVE    = float(os.getenv("CACHE_TTL_NEGATIVE",    str(30 * 86400)))
GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", "50"))
MAX_NFO_CANDIDATES = int(os.getenv("MAX_NFO_CANDIDATES", "20"))

# v5.7: query-type-aware NFO budget. RSS sweeps return ~50 candidates per
# call but most are already cached, so probing all of them wastes indexer
# quota. Each query type ('t' parameter) gets its own budget. RSS is
# detected by t=search with no q AND no external IDs.
NFO_BUDGET = {
    "rss":      int(os.getenv("NFO_BUDGET_RSS",      "3")),
    "search":   int(os.getenv("NFO_BUDGET_SEARCH",   "15")),
    "movie":    int(os.getenv("NFO_BUDGET_MOVIE",    "20")),
    "tvsearch": int(os.getenv("NFO_BUDGET_TV",       "12")),
}


def nfo_budget_for(params: dict) -> int:
    """Return the NFO probe budget for this request. Increments a
    per-budget-class metric as a side effect."""
    t = (params.get("t") or "").lower()
    has_id = any(params.get(k) for k in
                 ("imdbid", "tmdbid", "tvdbid", "tvmazeid", "tvrageid"))
    if t == "search" and not params.get("q") and not has_id:
        _metrics["nfo_budget_rss_count"] += 1
        return NFO_BUDGET["rss"]
    if t == "movie":
        _metrics["nfo_budget_movie_count"] += 1
    elif t == "tvsearch":
        _metrics["nfo_budget_tv_count"] += 1
    else:
        _metrics["nfo_budget_search_count"] += 1
    return NFO_BUDGET.get(t, NFO_BUDGET["search"])


# v5.7: per-indexer cost penalty for probe_score. Higher = more expensive
# to query → less preferred as an NFO candidate. Defaults align with the
# Prowlarr-side query/grab caps configured for each indexer.
INDEXER_COST: dict[str, float] = {}
for _ix_key, _default in [
    ("1", 1.0),  # abnzb — unlimited
    ("2", 1.5),  # altHUB — 50k warning threshold
    ("3", 1.0),  # DrunkenSlug — unlimited
    ("4", 1.0),  # NinjaCentral
    ("5", 1.5),  # Nzb.life — 10k/day
    ("6", 1.0),  # NZBgeek — gold language attr
    ("7", 3.0),  # omgwtfnzbs — 300/5min ban risk
    ("8", 1.0),  # NZBFinder
    ("9", 2.0),  # msgnews — 5k/day strict
]:
    INDEXER_COST[_ix_key] = float(os.getenv(f"INDEXER_{_ix_key}_COST", str(_default)))


# Pre-compiled signal regex for probe_score
_MULTI_AUDIO_TITLE_RE = re.compile(
    r"NORDiC\.ENG|\.MULTI\.|-BANDOLEROS|-PiTBULL|-CiNEMiX|-DRAUGR|-RAPiDCOWS",
    re.I,
)


def probe_score(title: str, indexer_id: str,
                subs_from_title: bool, indexer_hit_rate: float) -> float:
    """Rank a candidate for NFO probing. Higher = better.

    Components:
      + 5.0 for subs_from_title (NORDiC etc. matched the title)
      + 3.0 for a strong multi-audio scene-group signal in the title
      + indexer_hit_rate × 2.0 (existing v6.0 per-indexer score)
      - per-indexer cost penalty (default 1.0)
    """
    score = 0.0
    if subs_from_title:
        score += 5.0
    if _MULTI_AUDIO_TITLE_RE.search(title):
        score += 3.0
    score += indexer_hit_rate * 2.0
    score -= INDEXER_COST.get(indexer_id, 1.0)
    return score


DEBUG_LOGGING      = os.getenv("DEBUG_LOGGING", os.getenv("DEBUG", "0")) == "1"

# ── v6.0 feature flags ───────────────────────────────────────────────────────
# All flags can be disabled individually by setting to 0/false. The global
# DKSUBS_PROXY_V56_FEATURES kill switch overrides them all when set to 0.

def _env_bool(name: str, default: bool) -> bool:
    """Parse boolean env var. Empty string and unset → default."""
    raw = os.getenv(name, "")
    if raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


DKSUBS_PROXY_V56_FEATURES  = _env_bool("DKSUBS_PROXY_V56_FEATURES", True)
REQUEST_DEDUP_TTL          = float(os.getenv("REQUEST_DEDUP_TTL",          "30"))
MOVIE_VERDICT_TTL          = int(os.getenv("MOVIE_VERDICT_TTL",            str(14 * 86400)))
MOVIE_VERDICT_TRIGGER      = int(os.getenv("MOVIE_VERDICT_TRIGGER",        "3"))
MOVIE_VERDICT_WINDOW       = int(os.getenv("MOVIE_VERDICT_WINDOW",         str(7 * 86400)))
INDEXER_SCORING_ENABLED    = _env_bool("INDEXER_SCORING_ENABLED", True)
INDEXER_SCORING_WINDOW     = int(os.getenv("INDEXER_SCORING_WINDOW",       "100"))
INDEXER_SCORING_THRESHOLD  = float(os.getenv("INDEXER_SCORING_THRESHOLD",  "0.05"))
INDEXER_SCORING_MIN_PROBES = int(os.getenv("INDEXER_SCORING_MIN_PROBES",   "10"))
NFO_EARLY_EXIT_HITS        = int(os.getenv("NFO_EARLY_EXIT_HITS",          "2"))
# Opt-in enrichment: when ON, parse already-fetched NFOs for media properties
# (Dolby Vision, HDR10, Atmos, ...) and append proxy-owned `.NFOxxx` tags to
# the release title alongside the existing `.DKOK` / `.DKaudio` tag. Default
# OFF — zero behaviour change until explicitly enabled. The proxy makes no
# extra HTTP/NFO calls when this is on; the patterns only run against NFO
# text that hunt_danish() already retrieved for DK detection.
DKSUBS_PROXY_NFO_MEDIA_TAGS = _env_bool("DKSUBS_PROXY_NFO_MEDIA_TAGS", False)

# Used by Layer 1 (request_dedup) for hashing search params into a stable key.
import hashlib  # noqa: E402

def _max_nfo_candidates(indexer_id: str) -> int:
    """Per-indexer NFO candidate cap; falls back to the global MAX_NFO_CANDIDATES."""
    return int(os.getenv(f"INDEXER_{indexer_id}_MAX_NFO_CANDIDATES", str(MAX_NFO_CANDIDATES)))

# Indexer routing sets — IDs that skip NFO fetching entirely (title + attr scan only)
_title_only_ids: set[str] = set(filter(None, os.getenv("TITLE_ONLY_INDEXERS", "").split(",")))
_nfo_ids: set[str]        = set(filter(None, os.getenv("NFO_INDEXERS",        "").split(",")))
# Enrich-only: gets extended-attr enrichment but stays title-only (no NFO fetch)
_enrich_ids: set[str]     = set(filter(None, os.getenv("ENRICH_INDEXERS",     "").split(",")))
# v5.7 PR B: indexers whose <description> RSS field contains useful
# MediaInfo / language hints. The proxy will classify their description
# text and skip the NFO probe if the description alone is decisive.
_desc_classifier_ids: set[str] = set(filter(None,
    os.getenv("DESC_CLASSIFIER_INDEXERS", "").split(",")))
# Filter mode: drop items not tagged as Danish from the response.
# 0 = enrich only (default, safe);  1 = drop non-DK items.
DROP_NON_DK = os.getenv("DROP_NON_DK", "0") == "1"

# ── Scene group intelligence ─────────────────────────────────────────────────
# Loaded from scene-groups.json (generated by cache analysis). Each group has
# an audio_rate (0.0-1.0) based on historical ffprobe + proxy data.
# Groups above SCENE_GROUP_AUDIO_THRESHOLD are shortcut to .DKaudio when NORDiC
# title detected. Groups below SCENE_GROUP_SUBS_THRESHOLD skip NFO probes.
SCENE_GROUP_AUDIO_THRESHOLD = float(os.getenv("SCENE_GROUP_AUDIO_THRESHOLD", "0.90"))
SCENE_GROUP_SUBS_THRESHOLD  = float(os.getenv("SCENE_GROUP_SUBS_THRESHOLD",  "0.10"))
SCENE_GROUP_MIN_RELEASES    = int(os.getenv("SCENE_GROUP_MIN_RELEASES",      "10"))
SCENE_GROUP_ENABLED         = _env_bool("SCENE_GROUP_ENABLED", True)

_scene_group_profiles: dict[str, dict] = {}
_SCENE_GROUP_RE = re.compile(r'-([A-Za-z0-9]+?)(?:\.DK|\.nzb|$)')

def _load_scene_groups():
    global _scene_group_profiles
    for path in ["/config/scene-groups.json", "scene-groups.json"]:
        p = Path(path)
        if p.is_file():
            try:
                _scene_group_profiles = json.loads(p.read_text())
                return len(_scene_group_profiles)
            except Exception:
                pass
    return 0

_sg_count = _load_scene_groups()

def scene_group_verdict(title: str) -> str | None:
    """Return 'audio', 'subs', or None based on scene group history.
    Only applies when title has a NORDiC/subs hint (otherwise title scan handles it)."""
    if not SCENE_GROUP_ENABLED or not _scene_group_profiles:
        return None
    m = _SCENE_GROUP_RE.search(title)
    if not m:
        return None
    group = m.group(1)
    profile = _scene_group_profiles.get(group)
    if not profile or profile.get("total", 0) < SCENE_GROUP_MIN_RELEASES:
        return None
    rate = profile.get("audio_rate", 0.5)
    if rate >= SCENE_GROUP_AUDIO_THRESHOLD:
        return "audio"
    # The 'subs' verdict makes hunt_danish SKIP the authoritative NFO and tag
    # .DKOK (score 0), which PERMANENTLY blocks a release under DanishAudio
    # profiles. That is only safe for PURE-subs groups: a mixed group that has
    # ever produced a Danish-audio release (e.g. one that subs adult shows but
    # DUBS kids cartoons — ROCKETRACCOON, DUKTiGPOJK) would have its dubbed
    # releases wrongly blocked, even though its aggregate audio_rate is tiny.
    # For those, return None so the NFO decides (false-subs is far costlier than
    # the extra NFO fetch). See CLAUDE.md: NORDiC titles must go through the NFO.
    if rate <= SCENE_GROUP_SUBS_THRESHOLD and profile.get("audio", 0) == 0:
        return "subs"
    return None

_metrics = collections.Counter({
    "requests_total": 0, "hunt_total": 0, "dk_hits": 0, "nfo_fetches": 0,
    "nfo_direct_fetches": 0, "nfo_direct_hits": 0,
    "cache_hits": 0, "cache_misses": 0, "upstream_errors": 0, "hunt_errors": 0,
    # Search/grab forwards skipped because the indexer's per-window rate budget
    # was spent (ban prevention for rate-pinned indexers, e.g. omgwtfnzbs).
    "search_rate_skipped": 0,
    # v6.0 metrics
    "dedup_hits": 0, "dedup_inflight_waits": 0,
    "verdict_suppressions": 0, "verdict_writes": 0,
    "indexer_score_demotions": 0, "nfo_early_exits": 0,
    "crossindex_dedup_skips": 0,
    # Opt-in NFO media tags — gated by DKSUBS_PROXY_NFO_MEDIA_TAGS. Always
    # exposed in /metrics (zero when disabled) so dashboards stay stable.
    "nfo_media_tags_injected": 0,
    "nfo_media_tag_dv": 0, "nfo_media_tag_hdr10p": 0,
    "nfo_media_tag_hdr10": 0, "nfo_media_tag_atmos": 0,
    "nfo_media_tag_truehd": 0, "nfo_media_tag_dtshdma": 0,
    "nfo_media_tag_remux": 0,
    # v5.7 PR B: description classifier hits
    "desc_classifier_hits":      0,
    # v5.7: per-budget-class routing counters for nfo_budget_for()
    "nfo_budget_rss_count":      0,
    "nfo_budget_search_count":   0,
    "nfo_budget_movie_count":    0,
    "nfo_budget_tv_count":       0,
    # Scene group shortcuts
    "scene_group_audio_shortcuts":       0,
    "scene_group_subs_skips":            0,
    # PR C: /learn/imported endpoint
    "learn_imported_total":              0,
    "learn_unauthorized":                0,
    "learn_mismatch_agreement":          0,
    "learn_mismatch_upgrade":            0,
    "learn_mismatch_missed_dkaudio":     0,
    "learn_mismatch_false_dkaudio":      0,
    "learn_mismatch_false_dkok":         0,
})

# ── Logging ───────────────────────────────────────────────────────────────────

_req_id: contextvars.ContextVar[str] = contextvars.ContextVar("req_id", default="INIT")

def scrub(msg: str) -> str:
    msg = re.sub(r"apikey=[a-zA-Z0-9]+", "apikey=********", msg)
    msg = re.sub(r"X-Api-Key: [a-zA-Z0-9]+", "X-Api-Key: ********", msg)
    return msg

def log(msg: str, level: str = "INFO") -> None:
    if level == "DEBUG" and not DEBUG_LOGGING: return
    rid = _req_id.get(); ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(scrub(f"[{ts}] [dksubs] [{rid}] [{level}] {msg}\n"))

# ── Regexes ───────────────────────────────────────────────────────────────────

ITEM_RE  = re.compile(r"(<item>.*?</item>)", re.DOTALL)
TITLE_RE = re.compile(r"(<title>)(.*?)(</title>)", re.DOTALL)
# v5.7 PR B: extract <description> body for the description classifier.
# Non-greedy. Matches RSS-standard <description>...</description>.
DESC_RE = re.compile(r"<description[^>]*>([^<]*)</description>", re.I)

# Servarr disables an indexer when a test probe returns 0 items. With
# DROP_NON_DK=1 most generic probes get filtered to empty → circuit breaker
# trips → indexer stays disabled. PROBE_FILLER_ITEM is a synthetic <item>
# injected when the filtered response would be empty, so Servarr's count>0
# check passes. The title is junk that won't match any monitored movie/show,
# so RSS sync receiving it ignores it.
PROBE_FILLER_ITEM = (
    '<item>'
    '<title>DKSubs.Proxy.Probe.Filler.DoNotImport.0000.DKOK</title>'
    '<guid isPermaLink="false">dksubs-proxy-probe-filler</guid>'
    '<link>http://127.0.0.1/dksubs-probe-filler</link>'
    '<pubDate>Thu, 01 Jan 1970 00:00:00 +0000</pubDate>'
    '<category>2000</category>'
    '<size>1</size>'
    '<enclosure url="http://127.0.0.1/dksubs-probe-filler" length="1" type="application/x-nzb"/>'
    '<newznab:attr name="category" value="2000"/>'
    '<newznab:attr name="size" value="1"/>'
    '</item>'
)

_EMPTY_RSS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
    '<channel><newznab:response offset="0" total="0"/></channel>'
    '</rss>'
)

_CAPS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<caps>'
    '<server appversion="6.0" version="1.0" title="Danish Intelligence"/>'
    '<limits max="100" default="100"/>'
    '<registration available="no" open="no"/>'
    '<searching>'
    '<search available="yes" supportedParams="q"/>'
    '<movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>'
    '<tv-search available="yes" supportedParams="q,tvdbid,rid,season,ep"/>'
    '</searching>'
    '<categories>'
    '<category id="2000" name="Movies">'
    '<subcat id="2010" name="Movies/Foreign"/>'
    '<subcat id="2040" name="Movies/HD"/>'
    '<subcat id="2045" name="Movies/UHD"/>'
    '<subcat id="2050" name="Movies/Other"/>'
    '</category>'
    '<category id="5000" name="TV">'
    '<subcat id="5030" name="TV/SD"/>'
    '<subcat id="5040" name="TV/HD"/>'
    '</category>'
    '</categories>'
    '</caps>'
)

GUID_RE  = re.compile(r"<guid[^>]*>([^<]+)</guid>")
ATTR_RE  = re.compile(r'<newznab:attr\s+name="(\w+)"\s+value="([^"]*)"', re.I)
SIZE_RE  = re.compile(r"<size>(\d+)</size>", re.I)
SIZE_ATTR_RE = re.compile(r'<newznab:attr\s+name="size"\s+value="(\d+)"', re.I)
CATEGORY_RE = re.compile(r"<category>(\d+)</category>")
CATEGORY_ATTR_RE = re.compile(r'<newznab:attr\s+name="category"\s+value="(\d+)"', re.I)
MIN_RELEASE_SIZE       = int(os.getenv("MIN_RELEASE_SIZE",       "0") or "0")
MIN_RELEASE_SIZE_MOVIE = int(os.getenv("MIN_RELEASE_SIZE_MOVIE", "0") or "0")
MIN_RELEASE_SIZE_TV    = int(os.getenv("MIN_RELEASE_SIZE_TV",    "0") or "0")

ATTR_DK_RE = re.compile(r"\b(danish|dansk|nordic|dan|da)\b", re.I)

# ── v6.0 Layer 3: release-name normalization for cross-indexer dedup ─────────
_PROXY_TAG_RE = re.compile(r'\.(DKOK|DKaudio)\b', re.I)
_EXT_RE = re.compile(r'\.(mkv|mp4|avi|nfo|nzb)$', re.I)
_WS_RE = re.compile(r'\s+')

# Granular tags so Sonarr/Radarr can distinguish audio vs subs (per-tag release
# profiles). Audio tags signal Danish dub present; subs tags signal Danish
# subtitles present. Audio takes priority when both are detected.
DK_AUDIO_TITLE = ".DKaudio"
DK_AUDIO_NFO   = ".DKaudio"
DK_SUBS_TITLE  = ".DKOK"
DK_SUBS_NFO    = ".DKOK"

# Aliases retained for any external callers; pre-v6 cache values map to subs
# (most common pre-split case). Wipe the cache after upgrade for clean state.
DK_TAG_TITLE = DK_SUBS_TITLE
DK_TAG_NFO   = DK_SUBS_NFO

########################################################################
# Module: cache
########################################################################

"""Cache: SQLite-backed NFO cache, request dedup, indexer probes, scene group learning."""





# Module-level DB connection
_db: aiosqlite.Connection | None = None


async def _ensure_nfo_cache_media_tags_column(db) -> None:
    """Idempotent additive migration: add the nullable `media_tags` column to
    nfo_cache if it isn't already there."""
    try:
        async with db.execute("PRAGMA table_info(nfo_cache)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "media_tags" not in cols:
            await db.execute("ALTER TABLE nfo_cache ADD COLUMN media_tags TEXT")
            await db.commit()
            log("nfo_cache: added media_tags column (additive migration)")
    except Exception as e:
        log(f"nfo_cache media_tags migration: {e!r}", "WARN")


async def _ensure_nfo_cache_source_column(db) -> None:
    """Additive migration: add nullable `source` column to nfo_cache."""
    try:
        async with db.execute("PRAGMA table_info(nfo_cache)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "source" not in cols:
            await db.execute(
                "ALTER TABLE nfo_cache ADD COLUMN source TEXT DEFAULT 'unknown'"
            )
            await db.commit()
    except Exception as e:
        log(f"_ensure_nfo_cache_source_column failed: {e!r}", "WARN")


async def _ensure_classifier_audit_table(db) -> None:
    """Create classifier_audit table if absent."""
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS classifier_audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "release_name TEXT NOT NULL, "
            "predicted_tag TEXT NOT NULL, "
            "actual_tag TEXT NOT NULL, "
            "predicted_source TEXT NOT NULL, "
            "audio_languages TEXT, "
            "subtitle_languages TEXT, "
            "mismatch_type TEXT NOT NULL, "
            "created_at REAL NOT NULL"
            ")"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_mismatch "
            "ON classifier_audit(mismatch_type, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_release "
            "ON classifier_audit(release_name)"
        )
        await db.commit()
    except Exception as e:
        log(f"_ensure_classifier_audit_table failed: {e!r}", "WARN")


async def cache_init() -> None:
    global _db
    try:
        _db = await aiosqlite.connect(CACHE_DB)
        await _db.execute("CREATE TABLE IF NOT EXISTS nfo_cache (nzb_id TEXT PRIMARY KEY, result_tag TEXT NOT NULL, release_name TEXT, scanned_at REAL NOT NULL, media_tags TEXT)")
        await _ensure_nfo_cache_media_tags_column(_db)
        await _ensure_nfo_cache_source_column(_db)
        await _ensure_classifier_audit_table(_db)
        # v5.6 layer 1: request dedup
        await _db.execute(
            "CREATE TABLE IF NOT EXISTS request_dedup ("
            " request_key TEXT PRIMARY KEY, response_xml BLOB NOT NULL, "
            " completed_at REAL NOT NULL)"
        )
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_dedup_completed "
            "ON request_dedup(completed_at)"
        )
        # v5.6 layer 2: movie verdicts
        await _db.execute(
            "CREATE TABLE IF NOT EXISTS movie_verdicts ("
            " external_id TEXT NOT NULL, external_id_type TEXT NOT NULL, "
            " media_type TEXT NOT NULL, verdict TEXT NOT NULL, "
            " zero_dk_searches INTEGER NOT NULL DEFAULT 0, "
            " first_search_at REAL NOT NULL, last_search_at REAL NOT NULL, "
            " suppress_until REAL, "
            " PRIMARY KEY (external_id, external_id_type, media_type))"
        )
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_movie_verdicts_suppress "
            "ON movie_verdicts(suppress_until)"
        )
        # v5.6 layer 4: per-indexer probe ring buffer
        await _db.execute(
            "CREATE TABLE IF NOT EXISTS indexer_probes ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, "
            " indexer_id TEXT NOT NULL, was_dk_hit INTEGER NOT NULL, "
            " probed_at REAL NOT NULL)"
        )
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_indexer_probes_lookup "
            "ON indexer_probes(indexer_id, id DESC)"
        )
        # v6.0: scene group learning
        await _db.execute(
            "CREATE TABLE IF NOT EXISTS scene_group_stats ("
            " group_name TEXT PRIMARY KEY, "
            " audio_count INTEGER NOT NULL DEFAULT 0, "
            " subs_count INTEGER NOT NULL DEFAULT 0, "
            " none_count INTEGER NOT NULL DEFAULT 0, "
            " last_seen REAL NOT NULL)"
        )
        await _db.commit()
        log(f"Cache ready: {CACHE_DB} (v5.6 schema)")
    except Exception as e:
        log(f"Cache error: {e!r}. Fallback to memory.", "ERROR")
        _db = await aiosqlite.connect(":memory:")
        await _db.execute("CREATE TABLE nfo_cache (nzb_id TEXT PRIMARY KEY, result_tag TEXT NOT NULL, release_name TEXT, scanned_at REAL NOT NULL, media_tags TEXT)")

def _decode_media_tags(raw) -> list[str]:
    """Cache-row media_tags column → list of `.NFOxxx` tags."""
    if not raw:
        return []
    parts = [p for p in str(raw).split() if p.startswith(".NFO")]
    return parts


def _encode_media_tags(tags: list[str]) -> str:
    """Encode a list of `.NFOxxx` tags for storage."""
    return " ".join(sorted(t for t in tags if t.startswith(".NFO")))


async def cache_get(nzb_id: str, release_name: str = "") -> tuple[str | None, list[str]]:
    """Returns (dk_tag, media_tags). `dk_tag` is None on cache miss or
    expired negative entry."""
    if not _db:
        return None, []
    try:
        async with _db.execute(
            "SELECT result_tag, scanned_at, media_tags FROM nfo_cache WHERE nzb_id = ?",
            (nzb_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                _metrics["cache_hits"] += 1
                tag, scanned_at, mt_raw = row
                if tag == "NONE" and time.time() - scanned_at > CACHE_TTL_NEGATIVE:
                    return None, []
                return normalize_result_tag(tag), _decode_media_tags(mt_raw)
        if release_name:
            # Cross-nzb_id (same release_name) fallback. .DKaudio is content-
            # identifying and always safe to propagate. A .DKOK only propagates
            # when it was AUTHORITATIVELY sourced (real media inspection: NFO /
            # attr / description / ffprobe). A title- or group-derived .DKOK is
            # only a *guess* and must NOT propagate by name — otherwise a
            # title-only indexer's subs tag suppresses an NFO-capable indexer
            # that could upgrade the SAME release to .DKaudio (the cross-indexer
            # race that blocks Danish-dubbed shows like Ed, Edd n Eddy). Prefer
            # audio when both exist. Exact nzb_id hits above are unaffected, so
            # same-indexer re-polls keep their API savings.
            async with _db.execute(
                "SELECT result_tag, media_tags FROM nfo_cache "
                "WHERE release_name = ? AND ("
                "result_tag = ? OR "
                "(result_tag = ? AND source IN ('nfo','attr','description','ffprobe'))"
                ") ORDER BY (result_tag = ?) DESC LIMIT 1",
                (release_name, DK_AUDIO_TITLE, DK_SUBS_TITLE, DK_AUDIO_TITLE),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    _metrics["cache_hits"] += 1
                    return normalize_result_tag(row[0]), _decode_media_tags(row[1])
        _metrics["cache_misses"] += 1
    except Exception:
        pass
    return None, []


async def cache_set(nzb_id: str, tag: str, release_name: str = "",
                    media_tags: list[str] | None = None,
                    source: str = "nfo") -> None:
    """Persist a DK probe outcome and (optionally) the NFO-derived media
    tags found in the same probe."""
    if not _db:
        return
    mt_blob = _encode_media_tags(media_tags or [])
    try:
        async with _db.execute("PRAGMA table_info(nfo_cache)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "source" in cols:
            await _db.execute(
                "INSERT OR REPLACE INTO nfo_cache "
                "(nzb_id, result_tag, release_name, scanned_at, media_tags, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    nzb_id,
                    normalize_result_tag(tag),
                    release_name,
                    time.time(),
                    mt_blob,
                    source,
                ),
            )
        else:
            await _db.execute(
                "INSERT OR REPLACE INTO nfo_cache "
                "(nzb_id, result_tag, release_name, scanned_at, media_tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    nzb_id,
                    normalize_result_tag(tag),
                    release_name,
                    time.time(),
                    mt_blob,
                ),
            )
        await _db.commit()
    except Exception as e:
        log(f"cache_set failed: {e!r}", "WARN")


# ── v5.6 Layer 4: per-indexer hit-rate scoring ───────────────────────────────

async def record_indexer_probes(indexer_id: str, probe_results: dict) -> None:
    """Append probe outcomes to the ring buffer; trim to INDEXER_SCORING_WINDOW * 2."""
    if not _db or not probe_results:
        return
    now = time.time()
    try:
        await _db.executemany(
            "INSERT INTO indexer_probes (indexer_id, was_dk_hit, probed_at) "
            "VALUES (?, ?, ?)",
            [(indexer_id, 1 if (tag and tag != "NONE") else 0, now)
             for tag in probe_results.values()],
        )
        # Cap at 2x window so we always have full window after pruning.
        cap = INDEXER_SCORING_WINDOW * 2
        await _db.execute(
            "DELETE FROM indexer_probes WHERE indexer_id = ? "
            "AND id NOT IN (SELECT id FROM indexer_probes "
            "               WHERE indexer_id = ? ORDER BY id DESC LIMIT ?)",
            (indexer_id, indexer_id, cap),
        )
        await _db.commit()
    except Exception as e:
        log(f"record_indexer_probes failed for {indexer_id}: {e!r}", "WARN")


async def get_indexer_score(indexer_id: str) -> float:
    """Return rolling DK hit rate over the last INDEXER_SCORING_WINDOW probes."""
    if not INDEXER_SCORING_ENABLED or not _db:
        return 1.0
    try:
        async with _db.execute(
            "SELECT was_dk_hit FROM indexer_probes WHERE indexer_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (indexer_id, INDEXER_SCORING_WINDOW),
        ) as cur:
            rows = [r[0] for r in await cur.fetchall()]
        if len(rows) < INDEXER_SCORING_MIN_PROBES:
            return 1.0
        return sum(rows) / len(rows)
    except Exception as e:
        log(f"get_indexer_score failed for {indexer_id}: {e!r}", "WARN")
        return 1.0


# ── Scene group learning ─────────────────────────────────────────────────────

_SCENE_GROUP_RE = re.compile(r'-([A-Za-z0-9]+?)(?:\.DK|\.nzb|$)')


async def record_scene_group(release_name: str, tag: str) -> None:
    """Increment scene group stats for a classification result."""
    if not _db or not release_name:
        return
    m = _SCENE_GROUP_RE.search(release_name)
    if not m:
        return
    group = m.group(1)
    now = time.time()
    try:
        col = "audio_count" if tag == ".DKaudio" else "subs_count" if tag == ".DKOK" else "none_count"
        await _db.execute(
            f"INSERT INTO scene_group_stats (group_name, audio_count, subs_count, none_count, last_seen) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(group_name) DO UPDATE SET "
            f"{col} = {col} + 1, last_seen = ?",
            (group,
             1 if tag == ".DKaudio" else 0,
             1 if tag == ".DKOK" else 0,
             1 if tag not in (".DKaudio", ".DKOK") else 0,
             now, now),
        )
        await _db.commit()
    except Exception as e:
        log(f"record_scene_group failed: {e!r}", "DEBUG")


async def rebuild_scene_group_profiles() -> int:
    """Rebuild in-memory scene group profiles from the stats table.
    Returns the number of groups loaded."""
    global _scene_group_profiles
    if not _db:
        return 0
    try:
        async with _db.execute(
            "SELECT group_name, audio_count, subs_count, none_count "
            "FROM scene_group_stats "
            "WHERE (audio_count + subs_count) >= ?",
            (SCENE_GROUP_MIN_RELEASES,),
        ) as cur:
            rows = await cur.fetchall()
        new_profiles = {}
        for group, audio, subs, none_count in rows:
            total = audio + subs
            if total < SCENE_GROUP_MIN_RELEASES:
                continue
            new_profiles[group] = {
                "audio": audio,
                "subs": subs,
                "total": total,
                "audio_rate": round(audio / total, 3),
            }
        _scene_group_profiles.clear()
        _scene_group_profiles.update(new_profiles)
        return len(new_profiles)
    except Exception as e:
        log(f"rebuild_scene_group_profiles failed: {e!r}", "WARN")
        return 0


async def backfill_scene_groups_from_cache() -> int:
    """One-time: populate scene_group_stats from existing nfo_cache entries.
    Safe to run multiple times — uses INSERT OR IGNORE style accumulation."""
    if not _db:
        return 0
    try:
        async with _db.execute(
            "SELECT release_name, result_tag FROM nfo_cache "
            "WHERE result_tag IN ('.DKaudio', '.DKOK') AND release_name IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

        groups: dict[str, dict] = {}
        for name, tag in rows:
            m = _SCENE_GROUP_RE.search(name)
            if not m:
                continue
            g = m.group(1)
            if g not in groups:
                groups[g] = {"audio": 0, "subs": 0}
            if tag == ".DKaudio":
                groups[g]["audio"] += 1
            else:
                groups[g]["subs"] += 1

        now = time.time()
        for g, counts in groups.items():
            await _db.execute(
                "INSERT INTO scene_group_stats (group_name, audio_count, subs_count, none_count, last_seen) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(group_name) DO UPDATE SET "
                "audio_count = MAX(audio_count, ?), subs_count = MAX(subs_count, ?), last_seen = ?",
                (g, counts["audio"], counts["subs"], now,
                 counts["audio"], counts["subs"], now),
            )
        await _db.commit()
        return len(groups)
    except Exception as e:
        log(f"backfill_scene_groups failed: {e!r}", "WARN")
        return 0

########################################################################
# Module: classification
########################################################################

"""Classification: regex patterns, NFO scanning, title/attr classification."""




# ── Scandinavian spelling-fold ───────────────────────────────────────────────
# Scene posters ASCII-fold Danish titles (Ørkenens -> Oerkenens), so a Radarr
# text query carrying ø/æ/å never matches those releases. We drop the words
# containing those chars so a SINGLE upstream query matches every spelling
# variant (cost-neutral — no extra calls). A guard keeps the original query
# when too few distinctive words would survive (avoids over-broadening).
_SCANDI_RE = re.compile(r"[øæåØÆÅ]")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_DK_STOPWORDS = frozenset({
    "i", "en", "et", "og", "til", "af", "for",
    "med", "de", "den", "det", "på",
})
_FOLD_PUNCT = ".,:;!?-_'\"()[]"


def fold_scandi_query(q: str) -> str:
    """Rewrite a Newznab text query so one upstream call matches both the
    diacritic spelling ('Ørkenens Sønner') and the ASCII-folded spelling
    scene posters use ('Oerkenens Soenner').

    Drops the words containing ø/æ/å, leaving the shared ASCII words (+ year)
    that appear in every variant. Only folds when >= 2 *distinctive* ASCII
    words survive (excluding Danish stop words and the year); otherwise
    returns q unchanged so we never over-broaden (e.g. 'Den skaldede frisør
    2012' must not collapse to 'Den 2012')."""
    if not q or not _SCANDI_RE.search(q):
        return q

    kept = [w for w in q.split() if not _SCANDI_RE.search(w)]

    distinctive = 0
    for w in kept:
        bare = w.strip(_FOLD_PUNCT).lower()
        if bare and bare not in _DK_STOPWORDS and not _YEAR_RE.match(bare):
            distinctive += 1

    if distinctive < 2:
        return q
    return " ".join(kept)


def _is_status_probe(params: dict) -> bool:
    """A "status probe" is the kind of generic search Servarr's
    IndexerStatusCheck sends with no specific identifier — those are what
    we need to keep non-empty so the circuit breaker doesn't disable the
    indexer. Real searches (with q/imdbid/tmdbid/tvdbid/tvmazeid/season/ep)
    should be allowed to legitimately return empty results."""
    if params.get("t") not in ("search", "movie", "tvsearch"):
        return False
    for k in ("q", "imdbid", "tmdbid", "tvdbid", "tvmazeid", "season", "ep"):
        if params.get(k):
            return False
    return True


def _inject_probe_filler_if_empty(content: str) -> str:
    """Inject PROBE_FILLER_ITEM into content if it contains zero <item>s,
    and also bump newznab:response total="0" → "1" so consumers that trust
    total don't disagree with the item count.
    No-op when the response already has items or is malformed (no </channel>).
    Callers must have already decided the request is a status probe — this
    function does not gate on params itself."""
    if ITEM_RE.search(content):
        return content
    if '</channel>' not in content:
        return content
    _metrics["probe_filler_injected"] += 1
    out = content.replace('</channel>', PROBE_FILLER_ITEM + '</channel>', 1)
    # Best-effort total bump. If the upstream omitted the response tag or
    # used different formatting, this leaves it alone (still better than
    # before, where injection always left total="0").
    out = re.sub(
        r'(<newznab:response[^>]*\btotal=")0(")',
        r'\g<1>1\2',
        out,
        count=1,
    )
    return out

def _empty_or_filler_response(params: dict):
    """Used by error paths in _handle_inner. For status-probe-shaped queries
    returns an empty RSS with the probe filler injected so Servarr's circuit
    breaker stays happy even when upstream Prowlarr is rate-limited / down.
    For real searches (movie/tv with a specific id, or text search) returns
    the bare empty RSS — propagates the upstream error condition honestly
    instead of fabricating a result Servarr might try to grab."""
    from aiohttp import web as _web
    if params.get("t") == "caps":
        return _web.Response(text=_CAPS_XML, content_type='application/xml')

    body = _EMPTY_RSS
    if _is_status_probe(params):
        body = _inject_probe_filler_if_empty(body)
    return _web.Response(text=body, content_type='application/xml')


def _min_size_for(item_xml: str) -> int:
    """Pick the size threshold for a release based on its Newznab category."""
    cats = [int(m.group(1)) for m in CATEGORY_RE.finditer(item_xml)]
    cats += [int(m.group(1)) for m in CATEGORY_ATTR_RE.finditer(item_xml)]
    if any(2000 <= c < 3000 for c in cats):
        return MIN_RELEASE_SIZE_MOVIE or MIN_RELEASE_SIZE
    if any(5000 <= c < 6000 for c in cats):
        return MIN_RELEASE_SIZE_TV or MIN_RELEASE_SIZE
    return MIN_RELEASE_SIZE


def normalize_release_name(title: str) -> str:
    s = title.lower()
    s = _PROXY_TAG_RE.sub('', s)
    s = _EXT_RE.sub('', s)
    s = _WS_RE.sub(' ', s).strip()
    return s

LOWQ_RE      = re.compile(r"\b(CAM|CAMRIP|HDCAM|HDTS|TELESYNC|TELECINE|DVDSCR|XViD|DivX|TS|SD|480p)\b", re.I)
AUDIO_DK_RE  = re.compile(
    r"\b("
    r"danish[\.\-_\s]*audio|"
    r"danish[\.\-_\s]*dub|"
    r"(dk|dan)[\.\-_\s]*multi|"
    # Bare DANISH/DANSK: only count as AUDIO when NOT immediately followed by
    # a SUBS/SUBTITLES qualifier. Prevents `Movie.DANISH.SUBS.1080p` from
    # being tagged `.DKaudio` when it's actually subs-only.
    r"(danish|dansk)(?![\.\-_\s]*subs?\b|[\.\-_\s]*subtitles?\b)"
    r")\b",
    re.I,
)
SUBS_DK_RE   = re.compile(r"\b(nordic|nordic[\.\-_\s]*subs?|danish[\.\-_\s]*(subs?|subtitles?)|dk[\.\-_\s]*subs?|dksubs?|dansubs?|dk|da)\b", re.I)
MI_AUDIO_DK  = re.compile(r"Audio\s*#\d+[\s\S]{1,1500}?Language\s*:\s*(Danish|da|dan)\b", re.I)
MI_SUBS_DK   = re.compile(r"(Text\s*#\d+[\s\S]{1,600}?Language\s*:\s*(Danish|da|dan)\b|S_TEXT[\s\S]{1,300}?Language\s*:\s*(Danish|da|dan)\b)", re.I)

# Scene-NFO header format (BANDOLEROS, NORDiC.MULTI groups, etc.):
#   LANGUAGE.....: Danish, English, Finnish, Norwegian, Swedish
#   SUBTiTLES....: Retail -> Danish, English, Finnish, Norwegian, Swedish
# LANGUAGE = audio tracks; SUBTiTLES = subtitle tracks. Distinct lines, so the
# audio classifier must not also pick up Danish from the SUBTiTLES line. Anchor
# each regex to start-of-line and stop at end-of-line.
SCENE_LANG_DK = re.compile(
    r"^[\s\W]*LANGUAGE\b[\s\.\-:_>]*[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)
SCENE_SUBS_DK = re.compile(
    r"^[\s\W]*SUB(?:TITLES?|S)\b[\s\.\-:_>]*[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)

# Scene-NFO AUDIO-label line with Danish in the value.
SCENE_AUDIO_DK = re.compile(
    r"^[\s\W]*"
    r"(?:(?:AUD[A-ZΘиИ\d]*[A-ZΘиИ]\w*"
    r"(?:[\s\.\-_]*(?:track|lang(?:uage)?|i?nfo)[\s\.\-_]*\d*)?)"
    r"|LYD(?:SPOR)?)"
    r"[\s\.\-:_>|]+"
    r"[^\n]*\b(danish|dansk)\b",
    re.I | re.M,
)


# ── Smart NFO classifier (section-aware + signal-proximity) ──────────────────

_DANISH_WORD_RE = re.compile(r"\b(danish|dansk)\b", re.I)

_AUDIO_STRONG_RE = re.compile(
    r"\b("
    r"audio|sound|dub(?:s|bed|bing)?|spoken|voice|"
    r"lyd(?:spor)?|tale|tonspur|"
    r"ac-?3|aac|dts(?:-?hd)?|truehd|atmos|ddp|e-?ac-?3|"
    r"flac|mp3|opus|lpcm|hd[\s-]?ma|mlp"
    r")\b",
    re.I,
)
_AUDIO_WEAK_RE = re.compile(
    r"\b("
    r"language|lang(?:s|uage)?|track|channels?|"
    r"kbps|kbit|kb/s|khz"
    r")\b",
    re.I,
)

_SUBS_TOKEN_RE = re.compile(
    r"\b("
    r"sub(?:s|title?s?|titled)?|"
    r"caption(?:s|ing)?|cc|srt|vtt|sup|ssa|ass|forced|sdh|"
    r"tekst(?:er|ning|et)?|undertekst(?:er)?|untertitel"
    r")\b",
    re.I,
)

_AUDIO_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"audio(?:\s*#?\s*\d+)?|sound(?:track)?|lyd(?:spor)?|tonspur|tale|"
    r"voice(?:\s*cast)?|dub(?:s|bed|bing|s?\s*track)?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)
_SUBS_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"sub(?:s|titles?)?(?:\s*#?\s*\d+)?|text(?:\s*#?\s*\d+)?|"
    r"caption(?:s|ing)?|tekst(?:er|ning)?|undertekst(?:er)?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)
_OTHER_HEADER_LINE_RE = re.compile(
    r"^[\s\W]*("
    r"general|video(?:\s*#?\s*\d+)?|menu|chapters?|cover|file|"
    r"complete[\s_-]?name|format|container|"
    r"plot|synopsis|description|story|imdb|name|title|genre|cast|"
    r"actors?|director|year|released|source|notes?"
    r")\s*[#:\-]?\s*\d*\s*$",
    re.I,
)

_PROXIMITY_WINDOW = 12


def _classify_dk_proximity(text: str) -> tuple[bool, bool]:
    """Walk NFO lines; for each Danish/Dansk match, find the nearest
    audio- or subs-context signal in the current line + preceding window,
    plus the active section header. Returns (audio_hit, subs_hit)."""
    audio_hit = False
    subs_hit = False
    lines = text.splitlines()
    current_section: str | None = None
    section_age = 0

    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped:
            if _SUBS_HEADER_LINE_RE.match(ln):
                current_section = "subs"; section_age = 0
            elif _AUDIO_HEADER_LINE_RE.match(ln):
                current_section = "audio"; section_age = 0
            elif _OTHER_HEADER_LINE_RE.match(ln):
                current_section = None; section_age = 0
            else:
                section_age += 1
            if section_age > _PROXIMITY_WINDOW:
                current_section = None

        if not _DANISH_WORD_RE.search(ln):
            continue

        nearest_audio_strong = -1
        nearest_audio_weak = -1
        nearest_subs = -1
        for d in range(_PROXIMITY_WINDOW + 1):
            j = i - d
            if j < 0:
                break
            l2 = lines[j]
            if not l2.strip():
                continue
            if nearest_audio_strong == -1 and _AUDIO_STRONG_RE.search(l2):
                nearest_audio_strong = d
            if nearest_audio_weak == -1 and _AUDIO_WEAK_RE.search(l2):
                nearest_audio_weak = d
            if nearest_subs == -1 and _SUBS_TOKEN_RE.search(l2):
                nearest_subs = d
            if (nearest_audio_strong != -1 and nearest_subs != -1):
                break

        if current_section == "subs":
            subs_hit = True
            continue
        if nearest_subs != -1 and (
            (nearest_audio_strong == -1 or nearest_subs <= nearest_audio_strong)
            and (nearest_audio_weak == -1 or nearest_subs <= nearest_audio_weak)
        ):
            subs_hit = True
            continue
        if nearest_audio_strong != -1 or nearest_audio_weak != -1:
            audio_hit = True
            continue
        if current_section == "audio":
            audio_hit = True

    return audio_hit, subs_hit


def classify_nfo_text(text: str) -> str:
    """Classify NFO text into DK_AUDIO_NFO / DK_SUBS_NFO / "NONE"."""
    if not text:
        return "NONE"
    audio_hit, subs_hit = _classify_dk_proximity(text)
    if audio_hit:
        return DK_AUDIO_NFO
    if subs_hit:
        return DK_SUBS_NFO
    return "NONE"


# ISO 639 + colloquial variants that count as Danish in ffprobe output.
_DANISH_LANG_CODES = frozenset({"dan", "dansk", "danish", "da"})


# v5.7: strip the proxy's appended DK + NFO media tags from a release name
_PROXY_SUFFIX_STRIP_RE = re.compile(
    r"(?:\.DKaudio|\.DKOK)(?:\.NFO[A-Za-z0-9]+)*\s*$",
    re.I,
)


def strip_proxy_suffix(release_name: str) -> str:
    """Remove the proxy's appended .DKaudio/.DKOK + .NFOxxx tags from the
    end of a release name. Returns the canonical original title."""
    return _PROXY_SUFFIX_STRIP_RE.sub("", release_name or "")


def compute_actual_tag(audio_languages: list[str],
                       subtitle_languages: list[str]) -> str:
    """Compute the authoritative DK tag from ffprobe output."""
    def has_dk(langs):
        return any(
            (str(l) or "").strip().lower() in _DANISH_LANG_CODES
            for l in (langs or [])
        )
    if has_dk(audio_languages):
        return ".DKaudio"
    if has_dk(subtitle_languages):
        return ".DKOK"
    return "NONE"


def classify_mismatch(predicted: str, actual: str) -> str:
    """Return one of: agreement / upgrade / missed_dkaudio / false_dkaudio /
    false_dkok. Used by /learn/imported to label audit rows."""
    if predicted == actual:
        return "agreement"
    if actual == ".DKaudio" and predicted == ".DKOK":
        return "upgrade"
    if actual in (".DKaudio", ".DKOK") and predicted == "NONE":
        return "missed_dkaudio"
    if predicted == ".DKaudio" and actual in (".DKOK", "NONE"):
        return "false_dkaudio"
    if predicted == ".DKOK" and actual == "NONE":
        return "false_dkok"
    return "other"


# ── NFO-derived media tags (opt-in, gated by DKSUBS_PROXY_NFO_MEDIA_TAGS) ─────
_MEDIA_NFO_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    ".NFODV": [
        re.compile(r"\bDolby\s+Vision\b", re.I),
        re.compile(r"\bdvhe\.0[5-8]\.0[5-8]\b", re.I),
    ],
    ".NFOHDR10P": [
        re.compile(r"\bHDR10\s*\+", re.I),
        re.compile(r"\bHDR10\s*Plus\b", re.I),
        re.compile(r"\bSMPTE\s*ST\s*2094", re.I),
    ],
    ".NFOHDR10": [
        re.compile(r"\bHDR10\b", re.I),
        re.compile(r"\bSMPTE\s*ST\s*2086", re.I),
    ],
    ".NFOAtmos": [
        re.compile(r"\bDolby\s+Atmos\b", re.I),
    ],
    ".NFOTrueHD": [
        re.compile(r"\bDolby\s+TrueHD\b", re.I),
        re.compile(r"\bMLP\s+FBA\b", re.I),
    ],
    ".NFODTSHDMA": [
        re.compile(r"\bDTS-HD\s+Master\s+Audio\b", re.I),
        re.compile(r"\bDTS-HD\s+MA\b", re.I),
    ],
    ".NFORemux": [
        re.compile(r"\b(?:UHD\s+)?(?:Blu-?ray|BD)\s+Remux\b", re.I),
        re.compile(r"^\s*Source\s*:.*\bRemux\b", re.I | re.M),
        re.compile(r"\bRemuxed\s+from\b", re.I),
    ],
}


def _extract_nfo_media_tags(nfo_text: str) -> list[str]:
    """Run _MEDIA_NFO_PATTERNS against NFO text and return the matching
    tags as a sorted list (HDR10+ suppresses HDR10). Returns [] for empty
    or missing input."""
    if not nfo_text:
        return []
    tags = set()
    for tag, patterns in _MEDIA_NFO_PATTERNS.items():
        if any(p.search(nfo_text) for p in patterns):
            tags.add(tag)
    if ".NFOHDR10P" in tags:
        tags.discard(".NFOHDR10")
    return sorted(tags)


# Native-Danish title list.
NATIVE_DK_FILE = Path(os.environ.get("NATIVE_DK_FILE", "/cache/native-dk-titles.txt"))
_native_dk_cache: tuple = (0.0, [])     # (mtime, [compiled patterns])


def _load_native_dk():
    """Load native-Danish show titles, cached by file mtime. Returns list of compiled patterns."""
    global _native_dk_cache
    try:
        st = NATIVE_DK_FILE.stat()
    except FileNotFoundError:
        return []
    cached_mtime, cached_pats = _native_dk_cache
    if st.st_mtime == cached_mtime:
        return cached_pats
    try:
        titles = []
        with NATIVE_DK_FILE.open() as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    titles.append(t)
        pats = [re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in titles]
        _native_dk_cache = (st.st_mtime, pats)
        return pats
    except Exception:
        return cached_pats


NON_DK_LANG_RE = re.compile(
    r'\b('
    r'GERMAN|ITALIAN|SPANISH|FRENCH|RUSSIAN|HINDI|TAMIL|TELUGU|'
    r'JAPANESE|JAP|KOREAN|CHINESE|MANDARIN|CANTONESE|'
    r'POLISH|TURKISH|PORTUGUESE|HEBREW|ARABIC|UKRAINIAN|GREEK|'
    r'CZECH|HUNGARIAN|DUTCH|ROMANIAN|THAI|VIETNAMESE|'
    r'GER\.DUBBED|ITA\.DUBBED|FRE\.DUBBED|SPA\.DUBBED'
    r')\b',
    re.I,
)


def is_native_dk_title(title: str) -> bool:
    """True if release title contains a known native-Danish show/movie name
    AND does NOT explicitly advertise a foreign-language audio."""
    if NON_DK_LANG_RE.search(title):
        return False
    return any(p.search(title) for p in _load_native_dk())


def normalize_result_tag(tag: str) -> str:
    # Pass-through for new granular tags; legacy [DKOK:*] entries collapse to subs.
    if tag == "[DKOK:Title]":
        return DK_SUBS_TITLE
    if tag == "[DKOK:NFO]":
        return DK_SUBS_NFO
    return tag

########################################################################
# Module: nfo_fetch
########################################################################

"""NFO fetching: rate limiting, indexer config loading, NFO retrieval."""

from urllib.parse import urlparse




def _extract_nzb_id(url: str) -> str:
    """Extract the NZB release ID from a GUID URL.
    Handles ?id=NNN, ?guid=HEX, and path-based /details/{ID} formats."""
    from urllib.parse import parse_qs, urlparse as _urlparse
    qs = parse_qs(_urlparse(url).query)
    nid = (qs.get("id") or qs.get("guid") or [""])[0]
    return nid or url.split("/")[-1]


# ── SSRF guard ────────────────────────────────────────────────────────────────

_INTERNAL_HOSTNAMES = frozenset({
    "localhost",
    "host.docker.internal",
    "gateway.docker.internal",
    "host-gateway",
    "metadata.google.internal",
})


def _host_is_private(host: str) -> bool:
    """True if `host` resolves to (or literally is) a non-public address."""
    if not host:
        return True
    h = host.strip("[]").lower()
    if h in _INTERNAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, socket.herror, UnicodeError):
        return True
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            return True
    return False


def _hosts_share_indexer_domain(url_host: str, indexer_host: str) -> bool:
    """Same-domain check that handles `api.example.com` ↔ `example.com`."""
    if not url_host or not indexer_host:
        return False
    if url_host == indexer_host:
        return True
    if url_host.endswith("." + indexer_host):
        return True
    if indexer_host.endswith("." + url_host):
        return True
    return False


def _is_safe_indexer_url(url: str, indexer_id: str) -> bool:
    """Strict SSRF guard for indexer-supplied URLs."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return False
    url_host = (parsed.hostname or "").lower()
    if not url_host:
        return False
    cfg = _indexer_configs.get(indexer_id, {})
    base_url = cfg.get("baseUrl", "")
    base_host = (urlparse(base_url).hostname or "").lower() if base_url else ""
    if not base_host:
        return False
    if not _hosts_share_indexer_domain(url_host, base_host):
        return False
    if _host_is_private(url_host):
        return False
    return True

# ── Rate limiting for direct NFO calls ────────────────────────────────────────

NFO_DIRECT_RATE_CALLS  = int(os.getenv("NFO_DIRECT_RATE_CALLS",  "8"))   # max requests
NFO_DIRECT_RATE_WINDOW = float(os.getenv("NFO_DIRECT_RATE_WINDOW", "10")) # per N seconds

class SlidingWindowRateLimiter:
    """Async context manager: allows at most `calls` entries in any `window`-second window."""
    def __init__(self, calls: int, window: float):
        self._calls = calls; self._window = window
        self._timestamps: collections.deque = collections.deque()
        self._lock = asyncio.Lock()
    async def __aenter__(self):
        async with self._lock:
            now = time.time()
            while self._timestamps and now - self._timestamps[0] > self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                sleep_for = self._window - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.time()
                while self._timestamps and now - self._timestamps[0] > self._window:
                    self._timestamps.popleft()
            self._timestamps.append(now)
        return self
    async def __aexit__(self, *_): pass

    async def try_acquire(self) -> bool:
        """Non-blocking acquire: record a slot and return True if one is free in
        the current window, else return False WITHOUT sleeping or recording.

        Used by the search/grab forward path, where blocking (__aenter__) would
        hang the Arr request — we'd rather skip the upstream this window than
        stall, and never exceed the budget that triggers an indexer ban."""
        async with self._lock:
            now = time.time()
            while self._timestamps and now - self._timestamps[0] > self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                return False
            self._timestamps.append(now)
            return True

_nfo_rate_limiters: dict[str, SlidingWindowRateLimiter] = {}

def _nfo_rate_limiter(indexer_id: str) -> SlidingWindowRateLimiter:
    if indexer_id not in _nfo_rate_limiters:
        calls = int(os.getenv(f"INDEXER_{indexer_id}_RATE_CALLS", str(NFO_DIRECT_RATE_CALLS)))
        window = float(os.getenv(f"INDEXER_{indexer_id}_RATE_WINDOW", str(NFO_DIRECT_RATE_WINDOW)))
        _nfo_rate_limiters[indexer_id] = SlidingWindowRateLimiter(
            calls, window)
    return _nfo_rate_limiters[indexer_id]

def _has_explicit_rate(indexer_id: str) -> bool:
    """True only when an INDEXER_{id}_RATE_CALLS is explicitly configured.

    Without this opt-in, the search gate is open (unchanged behavior) for the
    bulk of indexers; the per-window throttle applies only to indexers we've
    deliberately rate-pinned (e.g. omgwtfnzbs, which bans at 300 calls/5min)."""
    return os.getenv(f"INDEXER_{indexer_id}_RATE_CALLS") is not None

async def _search_rate_limit_ok(indexer_id: str) -> bool:
    """Gate the search/grab forward path. Returns True if the request may be
    forwarded upstream, False if the indexer's per-window budget is spent.

    Shares the SAME limiter instance as the NFO path (_nfo_rate_limiter) so the
    combined call count — searches + NFO fetches — stays under one budget, which
    is exactly how omgwtfnzbs counts its 300-calls/5-min ban ceiling."""
    if not _has_explicit_rate(indexer_id):
        return True
    return await _nfo_rate_limiter(indexer_id).try_acquire()

# ── Indexer config (API keys + base URLs loaded from Prowlarr at startup) ─────

_indexer_configs: dict[str, dict] = {}  # str(indexer_id) -> {apikey, baseUrl}

async def load_indexer_configs(session) -> None:
    """Load per-indexer API keys and base URLs from env vars written by setup-proxy.sh."""
    global _indexer_configs
    configs: dict[str, dict] = {}

    for k, v in os.environ.items():
        m = re.match(r'^INDEXER_(\d+)_APIKEY$', k)
        if m:
            iid = m.group(1)
            configs.setdefault(iid, {})["apikey"] = v
        m = re.match(r'^INDEXER_(\d+)_BASEURL$', k)
        if m:
            iid = m.group(1)
            configs.setdefault(iid, {})["baseUrl"] = v.rstrip("/")

    if configs:
        _indexer_configs = configs
        log(f"Loaded direct API configs for {len(configs)} indexers from env")
        return

    try:
        async with session.get(f"{PROWLARR_URL}/api/v1/indexer",
                               params={"apikey": PROWLARR_API_KEY},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200: return
            data = json.loads(await resp.text())
            for ix in data:
                iid = str(ix["id"])
                baseurl = next((f["value"] for f in ix.get("fields", []) if f["name"] == "baseUrl"), "")
                configs[iid] = {"apikey": "", "baseUrl": baseurl.rstrip("/")}
            _indexer_configs = configs
            log(f"Loaded baseUrls for {len(configs)} indexers from Prowlarr REST API (apikeys masked — run setup-proxy.sh)", "WARN")
    except Exception as e:
        log(f"Could not load indexer configs: {e!r}", "WARN")

# ── Logic ─────────────────────────────────────────────────────────────────────

async def fetch_nfo(session, indexer_id, nzb_id, apikey) -> str | None:
    """Stage 1: try Prowlarr t=getnfo proxy. Returns NFO text, or None if unsupported/failed."""
    _metrics["nfo_fetches"] += 1
    params = {"t": "getnfo", "apikey": apikey, "id": nzb_id, "raw": "1"}
    for base in [f"{PROWLARR_URL}/{indexer_id}/api", f"{PROWLARR_URL}/api/v1/indexer/{indexer_id}/torznab"]:
        try:
            async with session.get(base, params=params, allow_redirects=False) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    if "<error" in text[:500]:
                        log(f"getnfo unsupported via Prowlarr for indexer {indexer_id}", "DEBUG")
                        return None
                    if _nfo_is_valid_text(text):
                        return text
                    log(f"getnfo via Prowlarr returned non-NFO content for indexer {indexer_id}", "DEBUG")
        except: continue
    return None

# Per-indexer API quirks for getnfo (matched by substring of baseUrl)
_NFO_QUIRKS: dict[str, dict] = {
    "drunkenslug": {"t": "info", "o": "file"},
}

def _nfo_is_valid_text(text: str) -> bool:
    """Return True if response looks like NFO/MediaInfo content, not XML/RSS/HTML."""
    head = text[:500].lower()
    return (len(text) > 50 and
            "<error" not in head and
            "<rss" not in head and
            "<channel>" not in head and
            "<html" not in head and
            "<!doctype" not in head)

async def fetch_nfo_direct(session, indexer_id: str, nzb_id: str, details_url: str, info_url: str = "") -> str | None:
    """Stage 2: call the indexer's API directly (bypassing Prowlarr) using its own API key."""
    async with _nfo_rate_limiter(indexer_id):
        return await _fetch_nfo_direct_inner(session, indexer_id, nzb_id, details_url, info_url)

async def _fetch_nfo_direct_inner(session, indexer_id: str, nzb_id: str, details_url: str, info_url: str = "") -> str | None:
    _metrics["nfo_direct_fetches"] += 1
    cfg = _indexer_configs.get(indexer_id, {})
    ix_key = cfg.get("apikey", "")
    ix_base = cfg.get("baseUrl", "")
    base_host = urlparse(ix_base).hostname or "" if ix_base else ""
    details_host = urlparse(details_url).hostname or ""

    def _same_domain(h1: str, h2: str) -> bool:
        """True if both hostnames share the same registered domain (handles api.X vs X)."""
        return h1 == h2 or h1.endswith("." + h2) or h2.endswith("." + h1)

    # 2a: direct getnfo call on the indexer's own API endpoint
    tried_2a = False
    got_definitive_no = False
    if ix_key and ix_base:
        tried_2a = True
        quirk = next((v for k, v in _NFO_QUIRKS.items() if k in ix_base.lower()), None)
        if quirk is not None:
            params = {"apikey": ix_key, "id": nzb_id, **quirk}
        else:
            params = {"t": "getnfo", "apikey": ix_key, "id": nzb_id, "raw": "1"}
        try:
            log(f"NFO direct getnfo: indexer {indexer_id} id={nzb_id}", "DEBUG")
            async with session.get(f"{ix_base}/api", params=params,
                                   allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=NFO_TIMEOUT)) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    if _nfo_is_valid_text(text):
                        return text
                    got_definitive_no = True
                    log(f"NFO direct getnfo returned non-NFO content for indexer {indexer_id}", "DEBUG")
                else:
                    log(f"NFO direct getnfo got HTTP {resp.status} for indexer {indexer_id}", "DEBUG")
        except Exception as e:
            log(f"NFO direct getnfo failed for indexer {indexer_id}: {e!r}", "DEBUG")

    # 2b: info attr URL
    if info_url and _is_safe_indexer_url(info_url, indexer_id) and not got_definitive_no:
        info_host = urlparse(info_url).hostname or ""
        if _same_domain(info_host, base_host) or not tried_2a:
            try:
                log(f"NFO direct info_url: {scrub(info_url)}", "DEBUG")
                async with session.get(info_url, allow_redirects=True,
                                       timeout=aiohttp.ClientTimeout(total=NFO_TIMEOUT)) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="replace")
                        if _nfo_is_valid_text(text):
                            return text
                        got_definitive_no = True
            except Exception as e:
                log(f"NFO direct info_url failed for {scrub(info_url)}: {e!r}", "DEBUG")

    # 2c: details page URL
    skip_2c = got_definitive_no and tried_2a and _same_domain(details_host, base_host)
    if _is_safe_indexer_url(details_url, indexer_id) and not skip_2c:
        auth_url = details_url + ("&" if "?" in details_url else "?") + f"apikey={ix_key}" if ix_key else details_url
        try:
            log(f"NFO direct details: {scrub(auth_url)}", "DEBUG")
            async with session.get(auth_url, allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=NFO_TIMEOUT)) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    if _nfo_is_valid_text(text):
                        return text
                    log(f"NFO direct details returned non-NFO content for {scrub(auth_url)}", "DEBUG")
                else:
                    log(f"NFO direct details got HTTP {resp.status} for {scrub(auth_url)}", "DEBUG")
        except Exception as e:
            log(f"NFO direct details failed for {scrub(auth_url)}: {e!r}", "DEBUG")

    return None

########################################################################
# Module: enrichment
########################################################################

"""Enrichment: inject extended attrs from direct indexer API queries."""




def extract_attrs(item_xml: str) -> dict[str, str]:
    """Extract newznab attrs from an item XML fragment into a dict.
    Multiple values for the same attr name are space-joined."""
    _raw: dict[str, list] = {}
    for m in ATTR_RE.finditer(item_xml):
        _raw.setdefault(m.group(1).lower(), []).append(m.group(2))
    return {k: " ".join(v) for k, v in _raw.items()}


async def enrich_with_extended_attrs(content: str, indexer_id: str, params: dict, session) -> str:
    """Query the indexer directly with extended=1 and inject subs/language attrs into content."""
    cfg = _indexer_configs.get(indexer_id, {})
    apikey = cfg.get("apikey", "")
    baseurl = cfg.get("baseUrl", "")
    if not apikey or not baseurl:
        return content
    direct_params = {k: v for k, v in params.items()
                     if k in ("t", "q", "imdbid", "tvdbid", "tvmazeid", "season", "ep", "limit", "offset", "cat")}
    direct_params["apikey"] = apikey
    direct_params["extended"] = "1"
    try:
        async with session.get(f"{baseurl}/api", params=direct_params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return content
            direct_xml = await resp.text(errors="replace")
    except Exception as e:
        log(f"ENRICH fetch failed for indexer {indexer_id}: {e!r}", "DEBUG")
        return content
    attr_map: dict[str, dict] = {}
    for item_xml in ITEM_RE.findall(direct_xml):
        guid_m = GUID_RE.search(item_xml)
        if not guid_m:
            continue
        nid = _extract_nzb_id(guid_m.group(1).strip())
        if not nid:
            continue
        attrs = extract_attrs(item_xml)
        if attrs.get("subs") or attrs.get("language"):
            attr_map[nid] = attrs
    if not attr_map:
        return content
    log(f"ENRICH: injecting extended attrs for {len(attr_map)} items from indexer {indexer_id}", "DEBUG")
    def inject(m):
        item_xml = m.group(1)
        guid_m = GUID_RE.search(item_xml)
        if not guid_m:
            return item_xml
        nid = _extract_nzb_id(guid_m.group(1).strip())
        extra_attrs = attr_map.get(nid, {})
        if not extra_attrs:
            return item_xml
        injected = "".join(
            f'<newznab:attr name="{n}" value="{v}"/>'
            for n, v in extra_attrs.items()
            if n in ("subs", "language") and v
        )
        return item_xml.replace("</item>", injected + "</item>") if injected else item_xml
    return ITEM_RE.sub(inject, content)

########################################################################
# Module: layers
########################################################################

"""Layers: request dedup, inflight locks, session, background tasks, verdicts."""

import hashlib




def extract_external_id(params: dict):
    """Extract (id, id_type) from search params. tmdbid wins over imdbid."""
    tmdb = params.get("tmdbid") or params.get("tmdb_id")
    if tmdb:
        return (str(tmdb), "tmdb")
    imdb = params.get("imdbid") or params.get("imdb_id")
    if imdb:
        return (str(imdb), "imdb")
    return (None, None)


# ── v5.6 Layer 1: in-flight request dedup ────────────────────────────────────

_DEDUP_KEY_PARAMS = ("t", "q", "imdbid", "tmdbid", "tvdbid",
                     "tvmazeid", "season", "ep", "cat",
                     "offset", "limit")


def request_key(indexer_id: str, params: dict) -> str:
    """Stable hash of the search-relevant params."""
    parts = [indexer_id]
    for p in _DEDUP_KEY_PARAMS:
        v = params.get(p)
        if v is not None:
            parts.append(f"{p}={v}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


async def dedup_get(key: str):
    """Return the cached response_xml (bytes) within TTL, else None."""
    if REQUEST_DEDUP_TTL <= 0 or not _db:
        return None
    try:
        async with _db.execute(
            "SELECT response_xml, completed_at FROM request_dedup "
            "WHERE request_key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        xml, completed_at = row
        if time.time() - completed_at > REQUEST_DEDUP_TTL:
            await _db.execute(
                "DELETE FROM request_dedup WHERE request_key = ?", (key,)
            )
            await _db.commit()
            return None
        _metrics["dedup_hits"] += 1
        return xml
    except Exception as e:
        log(f"dedup_get failed: {e!r}", "WARN")
        return None


async def dedup_set(key: str, xml) -> None:
    """Persist the tagged response. GCs rows older than 5x TTL on each write."""
    if REQUEST_DEDUP_TTL <= 0 or not _db:
        return
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        now = time.time()
        await _db.execute(
            "INSERT OR REPLACE INTO request_dedup "
            "(request_key, response_xml, completed_at) VALUES (?, ?, ?)",
            (key, xml, now),
        )
        await _db.execute(
            "DELETE FROM request_dedup WHERE completed_at < ?",
            (now - REQUEST_DEDUP_TTL * 5,),
        )
        await _db.commit()
    except Exception as e:
        log(f"dedup_set failed: {e!r}", "WARN")


# Per-request-key locks. Lazy-init `_inflight_locks_lock` because a
# module-level `asyncio.Lock()` binds to whichever event loop is current at
# import time — which breaks under pytest-asyncio's per-test loop.
_inflight_locks: dict = {}
_inflight_locks_lock: asyncio.Lock | None = None


def _get_inflight_locks_lock() -> asyncio.Lock:
    global _inflight_locks_lock
    if _inflight_locks_lock is None:
        _inflight_locks_lock = asyncio.Lock()
    return _inflight_locks_lock


class _InflightContext:
    def __init__(self, key: str):
        self.key = key
        self.is_leader = False
        self._lock = None

    async def __aenter__(self):
        async with _get_inflight_locks_lock():
            existing = _inflight_locks.get(self.key)
            if existing is None:
                self._lock = asyncio.Lock()
                await self._lock.acquire()
                _inflight_locks[self.key] = self._lock
                self.is_leader = True
            else:
                self._lock = existing
                self.is_leader = False
        if not self.is_leader:
            _metrics["dedup_inflight_waits"] += 1
            try:
                await asyncio.wait_for(self._lock.acquire(),
                                       timeout=NFO_TIMEOUT * 2)
                self._lock.release()
            except asyncio.TimeoutError:
                log(f"Inflight wait timeout for {self.key[:8]}", "WARN")
        return self.is_leader

    async def __aexit__(self, *exc):
        if self.is_leader:
            try:
                self._lock.release()
            finally:
                async with _get_inflight_locks_lock():
                    if _inflight_locks.get(self.key) is self._lock:
                        _inflight_locks.pop(self.key, None)


def dedup_inflight_lock(key: str) -> _InflightContext:
    return _InflightContext(key)


# Reset module-level lock when re-imported (test isolation).
_inflight_locks.clear()
_inflight_locks_lock = None


# ── Shared Session ────────────────────────────────────────────────────────────

_session: aiohttp.ClientSession | None = None
async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=GLOBAL_CONCURRENCY), headers={"User-Agent": f"DKSubs-Proxy/{VERSION}"})
    return _session


# ── Background-task registry ─────────────────────────────────────────────────

_background_tasks: set = set()
_BACKGROUND_TASKS_MAX = int(os.getenv("BACKGROUND_TASKS_MAX", "500"))


def _bg_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, asyncio.CancelledError):
        log(f"background NFO task raised: {exc!r}", "DEBUG")


def register_background_task(task: asyncio.Task) -> None:
    """Track an asyncio task so it survives until completion."""
    # Clean up already-done tasks before checking capacity.
    _background_tasks.difference_update(
        {t for t in _background_tasks if t.done()}
    )
    while len(_background_tasks) >= _BACKGROUND_TASKS_MAX:
        # Try to discard a done task first, else cancel a live one.
        removed = False
        for old in list(_background_tasks):
            if old.done():
                _background_tasks.discard(old)
                removed = True
                break
        if not removed:
            for old in list(_background_tasks):
                if not old.done():
                    old.cancel()
                    _background_tasks.discard(old)
                    break
            else:
                break  # nothing left to remove
    _background_tasks.add(task)
    task.add_done_callback(_bg_task_done)


# ── v5.6 Layer 2: movie-verdict cache ────────────────────────────────────────

async def verdict_says_no_dk(external_id: str, external_id_type: str,
                             media_type: str) -> bool:
    """True if an active 'no_dk' verdict exists for this media."""
    if MOVIE_VERDICT_TTL <= 0 or not _db:
        return False
    try:
        now = time.time()
        async with _db.execute(
            "SELECT suppress_until FROM movie_verdicts "
            "WHERE external_id = ? AND external_id_type = ? AND media_type = ? "
            "AND verdict = 'no_dk'",
            (external_id, external_id_type, media_type),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        suppress_until = row[0]
        return suppress_until is not None and suppress_until > now
    except Exception as e:
        log(f"verdict_says_no_dk failed: {e!r}", "WARN")
        return False


async def update_movie_verdict(external_id: str, external_id_type: str,
                               media_type: str, had_dk_hit: bool) -> None:
    """Increment the zero-DK counter (or reset on a DK hit). When the counter
    crosses MOVIE_VERDICT_TRIGGER within MOVIE_VERDICT_WINDOW seconds, set
    suppress_until = now + MOVIE_VERDICT_TTL.

    On a DK hit, reset zero_dk_searches=0 and clear suppress_until."""
    if MOVIE_VERDICT_TTL <= 0 or not _db:
        return
    try:
        now = time.time()
        async with _db.execute(
            "SELECT zero_dk_searches, first_search_at, suppress_until "
            "FROM movie_verdicts "
            "WHERE external_id = ? AND external_id_type = ? AND media_type = ?",
            (external_id, external_id_type, media_type),
        ) as cur:
            row = await cur.fetchone()

        if had_dk_hit:
            if row is not None:
                await _db.execute(
                    "UPDATE movie_verdicts "
                    "SET zero_dk_searches = 0, suppress_until = NULL, "
                    "    last_search_at = ?, verdict = 'no_dk' "
                    "WHERE external_id = ? AND external_id_type = ? "
                    "AND media_type = ?",
                    (now, external_id, external_id_type, media_type),
                )
            await _db.commit()
            return

        if row is None:
            await _db.execute(
                "INSERT INTO movie_verdicts "
                "(external_id, external_id_type, media_type, verdict, "
                " zero_dk_searches, first_search_at, last_search_at) "
                "VALUES (?, ?, ?, 'no_dk', 1, ?, ?)",
                (external_id, external_id_type, media_type, now, now),
            )
            await _db.commit()
            return

        prev_count, first_at, prev_suppress = row
        if now - first_at > MOVIE_VERDICT_WINDOW:
            new_count = 1
            new_first = now
        else:
            new_count = prev_count + 1
            new_first = first_at

        new_suppress = prev_suppress
        if new_count >= MOVIE_VERDICT_TRIGGER and (prev_suppress is None or prev_suppress < now):
            new_suppress = now + MOVIE_VERDICT_TTL
            _metrics["verdict_writes"] += 1
            log(f"Verdict 'no_dk' set for {external_id_type}:{external_id} "
                f"({media_type}); suppress for {MOVIE_VERDICT_TTL}s", "DEBUG")

        await _db.execute(
            "UPDATE movie_verdicts "
            "SET zero_dk_searches = ?, first_search_at = ?, "
            "    last_search_at = ?, suppress_until = ? "
            "WHERE external_id = ? AND external_id_type = ? AND media_type = ?",
            (new_count, new_first, now, new_suppress,
             external_id, external_id_type, media_type),
        )
        await _db.commit()
    except Exception as e:
        log(f"update_movie_verdict failed: {e!r}", "WARN")

########################################################################
# Module: hunt
########################################################################

"""Hunt: main NFO-hunter logic and /learn/imported endpoint."""





async def hunt_danish(content, indexer_id, apikey, session,
                      title_only: bool = False, params: dict | None = None):
    """Returns (xml, probe_results_dict). probe_results maps nzb_id -> tag
    (or 'NONE') for each NFO probed in this search."""
    params = params or {}
    _metrics["hunt_total"] += 1; found_hits = {}; candidates = []; items = ITEM_RE.findall(content)
    log(f"HUNT: parsed {len(items)} items from indexer {indexer_id}", "INFO")
    probe_results: dict = {}
    media_tags_by_nid: dict = {}
    for item_xml in items:
        title_m = TITLE_RE.search(item_xml); guid_m = GUID_RE.search(item_xml)
        if not title_m or not guid_m:
            continue
        title = title_m.group(2); g_url = guid_m.group(1).strip()
        nid = _extract_nzb_id(g_url)
        if not nid: continue
        
        # Quality Rejection
        if LOWQ_RE.search(title):
            continue
        
        skip_nfo_for_size = False
        min_sz = _min_size_for(item_xml)
        if min_sz > 0:
            size_m = SIZE_RE.search(item_xml) or SIZE_ATTR_RE.search(item_xml)
            if size_m and int(size_m.group(1)) < min_sz:
                skip_nfo_for_size = True

        # Native DK Check
        if is_native_dk_title(title):
            found_hits[nid] = DK_AUDIO_TITLE
            await cache_set(nid, DK_AUDIO_TITLE, title, source="title"); continue
        
        # Audio Check
        if AUDIO_DK_RE.search(title):
            found_hits[nid] = DK_AUDIO_TITLE
            await cache_set(nid, DK_AUDIO_TITLE, title, source="title"); continue

        subs_from_title = bool(SUBS_DK_RE.search(title))
        # Scene group shortcut: if NORDiC in title + known group, skip NFO
        if subs_from_title:
            sg_verdict = scene_group_verdict(title)
            if sg_verdict == "audio":
                found_hits[nid] = DK_AUDIO_TITLE
                _metrics["scene_group_audio_shortcuts"] += 1
                await cache_set(nid, DK_AUDIO_TITLE, title, source="group")
                log(f"Scene group shortcut → .DKaudio for {title[:50]}", "DEBUG")
                continue
            elif sg_verdict == "subs":
                found_hits[nid] = DK_SUBS_TITLE
                _metrics["scene_group_subs_skips"] += 1
                await cache_set(nid, DK_SUBS_TITLE, title, source="group")
                log(f"Scene group shortcut → .DKOK for {title[:50]}", "DEBUG")
                continue
        # v5.7 PR B: description classifier
        if indexer_id in _desc_classifier_ids:
            desc_m = DESC_RE.search(item_xml)
            if desc_m and len(desc_m.group(1)) >= 100:
                desc_tag = classify_nfo_text(desc_m.group(1))
                if desc_tag != "NONE":
                    found_hits[nid] = desc_tag
                    _metrics["desc_classifier_hits"] += 1
                    await cache_set(nid, desc_tag, title, source="description")
                    continue
        if subs_from_title and skip_nfo_for_size:
            skip_nfo_for_size = False
        attrs = extract_attrs(item_xml)
        if ATTR_DK_RE.search(attrs.get("language", "")):
            if not subs_from_title:
                found_hits[nid] = DK_AUDIO_TITLE
                await cache_set(nid, DK_AUDIO_TITLE, title, source="attr"); continue
            # else: NORDiC + language=Danish is ambiguous, fall through
        if ATTR_DK_RE.search(attrs.get("subs", "")):
            if not subs_from_title:
                found_hits[nid] = DK_SUBS_TITLE
                await cache_set(nid, DK_SUBS_TITLE, title, source="attr"); continue
        if not title_only and not skip_nfo_for_size:
            if attrs.get("nfo") == "0":
                if subs_from_title:
                    found_hits[nid] = DK_SUBS_TITLE
                    await cache_set(nid, DK_SUBS_TITLE, title, source="title")
                log(f"nfo=0 skip for {nid} ({title[:50]})", "DEBUG"); continue
            info_url = attrs.get("info", "")
            candidates.append((nid, title, g_url, info_url, subs_from_title))
        elif subs_from_title:
            found_hits[nid] = DK_SUBS_TITLE
            await cache_set(nid, DK_SUBS_TITLE, title, source="title")

    to_fetch = [c for c in candidates if c[0] not in found_hits]
    _indexer_hit_rate = await get_indexer_score(indexer_id)
    to_fetch.sort(
        key=lambda c: -probe_score(
            title=c[1], indexer_id=indexer_id,
            subs_from_title=c[4], indexer_hit_rate=_indexer_hit_rate,
        )
    )
    seen_names: set = set()
    deduped = []
    for c in to_fetch:
        rn_key = normalize_release_name(c[1])
        if rn_key in seen_names:
            _metrics["crossindex_dedup_skips"] += 1
            continue
        seen_names.add(rn_key)
        deduped.append(c)
    to_fetch = deduped
    base_cap = nfo_budget_for(params)
    if _indexer_hit_rate < INDEXER_SCORING_THRESHOLD:
        cap = max(1, int(base_cap * 0.5))
        _metrics["indexer_score_demotions"] += 1
        log(f"Indexer {indexer_id} hit_rate={_indexer_hit_rate:.3f} below "
            f"threshold; cap halved {base_cap}→{cap}", "DEBUG")
    else:
        cap = base_cap
    if len(to_fetch) > cap:
        log(f"Limiting NFO probes from {len(to_fetch)} to {cap} (indexer {indexer_id})", "DEBUG")
        to_fetch = to_fetch[:cap]
    if to_fetch and not title_only:
        async def fetch_one(nid, title, g_url, info_url, subs_fallback=False):
            cached, cached_media = await cache_get(nid, title)
            if cached:
                if cached_media and DKSUBS_PROXY_NFO_MEDIA_TAGS:
                    media_tags_by_nid[nid] = cached_media
                return nid, cached if cached != "NONE" else (DK_SUBS_TITLE if subs_fallback else "NONE")

            text = await fetch_nfo(session, indexer_id, nid, apikey)

            via_direct = False
            if text is None:
                text = await fetch_nfo_direct(session, indexer_id, nid, g_url, info_url)
                via_direct = text is not None

            if text is None:
                return nid, DK_SUBS_TITLE if subs_fallback else "NONE"

            tag = classify_nfo_text(text)
            if via_direct and tag != "NONE":
                _metrics["nfo_direct_hits"] += 1
                log(f"NFO direct HIT [{tag}] for {nid} ({title[:60]})", "DEBUG")
            media_tags: list[str] = []
            if DKSUBS_PROXY_NFO_MEDIA_TAGS:
                media_tags = _extract_nfo_media_tags(text)
                if media_tags:
                    media_tags_by_nid[nid] = media_tags
                    for mt in media_tags:
                        _metrics["nfo_media_tag_" + mt[4:].lower()] += 1
                    _metrics["nfo_media_tags_injected"] += 1
                    log(f"NFO media tags for {nid} ({title[:50]}): "
                        f"{','.join(media_tags)}", "DEBUG")
            if tag == "NONE" and subs_fallback:
                tag = DK_SUBS_TITLE
            await cache_set(nid, tag, title, media_tags=media_tags)
            return nid, tag

        foreground_budget = NFO_TIMEOUT
        tasks = [asyncio.create_task(fetch_one(n, t, g, i, s)) for n, t, g, i, s in to_fetch]
        dk_hits_this_search = 0
        try:
            for fut in asyncio.as_completed(tasks, timeout=foreground_budget):
                try:
                    r = await fut
                except asyncio.TimeoutError:
                    raise
                except Exception as e:
                    log(f"fetch_one failed (indexer {indexer_id}): {e!r}", "WARN")
                    continue
                if isinstance(r, tuple):
                    nid, tag = r
                    probe_results[nid] = tag
                    if tag and tag != "NONE":
                        found_hits[nid] = tag
                        dk_hits_this_search += 1
                        if (NFO_EARLY_EXIT_HITS > 0
                                and dk_hits_this_search >= NFO_EARLY_EXIT_HITS):
                            _metrics["nfo_early_exits"] += 1
                            log(f"Early-exit at {dk_hits_this_search} DK hits "
                                f"(indexer {indexer_id})", "DEBUG")
                            break
        except asyncio.TimeoutError:
            pass
        pending = [t for t in tasks if not t.done()]
        if pending:
            log(f"{len(pending)} NFO fetch(es) running in background for next poll", "DEBUG")
            for t in pending:
                register_background_task(t)

    _metrics["dk_hits"] += len(found_hits)
    # Record scene group stats for every classified release
    for _nid, _tag in found_hits.items():
        # Find the title for this nid from items
        for _it in items:
            _tm = TITLE_RE.search(_it)
            _gm = GUID_RE.search(_it)
            if _tm and _gm and _extract_nzb_id(_gm.group(1).strip()) == _nid:
                await record_scene_group(_tm.group(2), _tag)
                break
    if not found_hits and not DROP_NON_DK:
        return content, probe_results
    def replacer(m):
        xml = m.group(1); g_m = GUID_RE.search(xml)
        if not g_m:
            return "" if DROP_NON_DK else xml
        nid = _extract_nzb_id(g_m.group(1).strip())
        tag = found_hits.get(nid)
        if tag:
            media_suffix = "".join(media_tags_by_nid.get(nid, []))
            return TITLE_RE.sub(rf"\1\2{tag}{media_suffix}\3", xml, 1)
        return "" if DROP_NON_DK else xml
    return ITEM_RE.sub(replacer, content), probe_results


async def _handle_learn_imported(request) -> "web.Response":
    """POST /learn/imported — receive ffprobe ground truth, update cache,
    write audit row. Auth: X-Api-Key must equal PROWLARR_API_KEY."""

    api_key = request.headers.get("X-Api-Key", "")
    if not PROWLARR_API_KEY:
        return web.Response(status=500, text="proxy not configured")
    if not secrets.compare_digest(api_key, PROWLARR_API_KEY):
        _metrics["learn_unauthorized"] += 1
        return web.Response(status=401, text="Unauthorized")

    try:
        body_text = await request.text()
        body = json.loads(body_text or "{}")
    except (json.JSONDecodeError, ValueError):
        return web.Response(status=400, text="invalid JSON")

    release_name = body.get("release_name", "").strip()
    if not release_name:
        return web.Response(status=400, text="release_name required")

    lookup_name = strip_proxy_suffix(release_name)

    audio_langs = body.get("audio_languages") or []
    subs_langs  = body.get("subtitle_languages") or []
    actual_tag = compute_actual_tag(audio_langs, subs_langs)

    previous_tag = "NONE"
    previous_source = "unknown"
    previous_nzb_id = ""
    if _db:
        try:
            async with _db.execute(
                "SELECT nzb_id, result_tag, source FROM nfo_cache WHERE release_name=? LIMIT 1",
                (lookup_name,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    previous_nzb_id = row[0] or ""
                    previous_tag = row[1] or "NONE"
                    previous_source = row[2] or "unknown"
        except Exception as e:
            log(f"/learn/imported lookup failed: {e!r}", "WARN")

    mismatch = classify_mismatch(previous_tag, actual_tag)

    audit_id = None
    if _db:
        try:
            async with _db.execute(
                "INSERT INTO classifier_audit "
                "(release_name, predicted_tag, actual_tag, predicted_source, "
                "audio_languages, subtitle_languages, mismatch_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                (release_name, previous_tag, actual_tag, previous_source,
                 json.dumps(audio_langs), json.dumps(subs_langs), mismatch),
            ) as cur:
                audit_id = cur.lastrowid
            await _db.commit()
        except Exception as e:
            log(f"/learn/imported audit insert failed: {e!r}", "WARN")

    cache_nzb_id = previous_nzb_id or (
        "ffp:" + hashlib.sha256(lookup_name.encode("utf-8")).hexdigest()[:16]
    )
    try:
        await cache_set(cache_nzb_id, actual_tag, lookup_name, source="ffprobe")
    except Exception as e:
        log(f"/learn/imported cache_set failed: {e!r}", "WARN")

    _metrics["learn_imported_total"] += 1
    _metrics[f"learn_mismatch_{mismatch}"] = _metrics.get(f"learn_mismatch_{mismatch}", 0) + 1

    return web.json_response({
        "previous_tag": previous_tag,
        "previous_source": previous_source,
        "new_tag": actual_tag,
        "audit_id": audit_id,
        "mismatch_type": mismatch,
    })

########################################################################
# Module: app
########################################################################

"""App: HTTP handler, lifecycle hooks, main entry point."""






# ── Handler ───────────────────────────────────────────────────────────────────

async def handle_altmount(request: web.Request) -> web.Response:
    """Shim for AltMount/SABnzbd: translates mode=qstatus -> mode=status."""
    params = dict(request.rel_url.query)
    if params.get("mode") == "qstatus":
        params["mode"] = "status"
    if ALTMOUNT_API_KEY:
        params["apikey"] = ALTMOUNT_API_KEY
    elif "ma_username" not in params and "ma_password" not in params:
        if RADARR_API_KEY:
            params["ma_username"] = RADARR_URL
            params["ma_password"] = RADARR_API_KEY
        elif SONARR_API_KEY:
            params["ma_username"] = SONARR_URL
            params["ma_password"] = SONARR_API_KEY

    # Forward to AltMount container
    alt_url = f"{ALTMOUNT_URL}?{urlencode(params)}"
    session = request.app['session']
    try:
        # Forward everything (method, body, headers)
        data = await request.read()
        headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
        async with session.request(request.method, alt_url, data=data,
                                    headers=headers, timeout=10) as resp:
            body = await resp.read()
            return web.Response(body=body, status=resp.status,
                                headers={"Content-Type": "application/json"})
    except Exception as e:
        log(f"altmount-shim: forward failed: {e!r}", "WARN")
        return web.json_response({"status": False, "error": str(e)}, status=502)


def _redacted_query(query) -> str:
    secret_names = {"apikey", "api_key", "x-api-key", "token", "key", "password"}
    return urlencode([
        (name, "***" if name.lower() in secret_names else value)
        for name, value in query.items()
    ])


async def handle(request: web.Request) -> web.Response:
    _metrics["requests_total"] += 1
    _req_id.set(secrets.token_hex(4))
    path = request.path.lstrip("/")
    log(f"REQ-START: /{path} {_redacted_query(request.rel_url.query)}", "INFO")

    if path.startswith("altmount"):
        return await handle_altmount(request)

    match = re.match(r"^(\d+)", path)
    indexer_id = match.group(1) if match else None
    if not indexer_id:
        return web.Response(text="Invalid Indexer ID", status=400)

    params = dict(request.rel_url.query)
    incoming_key = params.get("apikey") or request.headers.get("X-Api-Key", "")
    if not PROWLARR_API_KEY:
        _metrics["auth_misconfigured"] += 1
        log("auth: PROWLARR_API_KEY not configured; refusing request", "WARN")
        return web.Response(text="Proxy not configured: PROWLARR_API_KEY missing",
                            status=500)
    if not secrets.compare_digest(incoming_key, PROWLARR_API_KEY):
        _metrics["auth_rejected"] += 1
        return web.Response(text="Unauthorized", status=401)
    apikey = PROWLARR_API_KEY

    # v5.6 Layer 1: in-flight request dedup
    dedup_key = None
    if DKSUBS_PROXY_V56_FEATURES and params.get("t") in ("search", "movie", "tvsearch"):
        dedup_key = request_key(indexer_id, params)
        cached = await dedup_get(dedup_key)
        if cached is not None:
            return web.Response(body=cached,
                                headers={"Content-Type": "application/xml"})
        async with dedup_inflight_lock(dedup_key) as is_leader:
            if not is_leader:
                cached = await dedup_get(dedup_key)
                if cached is not None:
                    return web.Response(body=cached,
                                        headers={"Content-Type": "application/xml"})
            return await _handle_inner(request, indexer_id, params,
                                       apikey, dedup_key)

    return await _handle_inner(request, indexer_id, params, apikey, dedup_key)


async def _handle_inner(request, indexer_id, params, apikey, dedup_key):
    """Original handle body: fetch upstream, enrich, hunt, return response."""
    params["extended"] = "1"

    # Scandinavian spelling fold (cost-neutral, single query): Radarr text-
    # searches with the diacritic title (Ørkenens Sønner); many alive releases
    # are ASCII-folded (Oerkenens Soenner) and won't match. Mutating params["q"]
    # here covers BOTH the Prowlarr forward AND the direct
    # enrich_with_extended_attrs() call (which reuses params["q"]).
    if params.get("t") in ("search", "movie", "tvsearch") and params.get("q"):
        _folded = fold_scandi_query(params["q"])
        if _folded != params["q"]:
            log(f"scandi-fold: {params['q']!r} -> {_folded!r}", "DEBUG")
            params["q"] = _folded

    session = request.app['session']
    content = None
    EMPTY_XML = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><newznab:response offset="0" total="0"/></channel></rss>'

    # v6.2: Support direct upstream URLs for specialized bridges (ob-proxy)
    direct_url = os.getenv(f"INDEXER_{indexer_id}_BASEURL")
    if direct_url:
        upstream_url = f"{direct_url.rstrip('/')}/api"
        log(f"DIRECT ROUTE: talking to bridge at {upstream_url}", "DEBUG")
        # Ensure correct bridge apikey is in params
        bridge_key = os.getenv(f"INDEXER_{indexer_id}_APIKEY")
        if bridge_key:
            params["apikey"] = bridge_key
    else:
        upstream_url = f"{PROWLARR_URL}/{indexer_id}/api"

    # Ban-prevention gate: for rate-pinned indexers...

    # forwards within the per-window budget shared with the NFO path. When the
    # budget is spent we return an empty response instead of forwarding, so the
    # upstream indexer never sees > N calls/window (omgwtfnzbs bans at 300/5min).
    # Skipped for t=get (downloads must never be dropped) and for indexers with
    # no explicit INDEXER_{id}_RATE_CALLS (behavior unchanged for those).
    if params.get("t") in ("search", "movie", "tvsearch"):
        if not await _search_rate_limit_ok(indexer_id):
            _metrics["search_rate_skipped"] += 1
            log(f"rate-limit: search budget spent for indexer {indexer_id}; "
                f"skipping upstream forward this window", "WARN")
            return _empty_or_filler_response(params)

    try:
        headers = {"X-Api-Key": apikey} if apikey else {}
        async with session.get(upstream_url, params=params, headers=headers,
                               allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                text = await resp.text(errors="replace")
                if "<?xml" in text[:500] or "<rss" in text[:500].lower():
                    content = text
                    log(f"DEBUG: Upstream returned XML ({len(content)} bytes) for indexer {indexer_id}", "DEBUG")
                else:
                    log(f"REJECT: returned non-XML body from {upstream_url}", "WARN")
            else:
                log(f"Upstream returned non-200 status: {resp.status}", "WARN")
                _metrics["upstream_errors"] += 1
                return _empty_or_filler_response(params)
    except Exception as e:
        log(f"FETCH ERROR: {upstream_url} -> {e!r}", "DEBUG")
        _metrics["upstream_errors"] += 1
        return _empty_or_filler_response(params)

    if content is None:
        _metrics["upstream_errors"] += 1
        log(f"UPSTREAM FAILURE for indexer {indexer_id}", "WARN")
        return _empty_or_filler_response(params)

    if params.get("t") in ["search", "movie", "tvsearch"]:
        is_enrich_only = (indexer_id in _enrich_ids
                          and indexer_id not in _nfo_ids)
        is_title_only = (indexer_id in _title_only_ids) or is_enrich_only
        if is_title_only:
            log(f"TITLE-ONLY mode for indexer {indexer_id}"
                f"{' (enrich-only)' if is_enrich_only else ''}", "DEBUG")

        ext_id, ext_id_type = extract_external_id(params)
        media_type = "tv" if params.get("t") == "tvsearch" else "movie"
        verdict_suppressed = False
        if ext_id and DKSUBS_PROXY_V56_FEATURES:
            verdict_suppressed = await verdict_says_no_dk(
                ext_id, ext_id_type, media_type
            )
            if verdict_suppressed:
                _metrics["verdict_suppressions"] += 1
                log(f"Verdict suppressed NFO for {ext_id_type}:{ext_id}", "DEBUG")

        if indexer_id in _nfo_ids or indexer_id in _enrich_ids:
            try:
                content = await enrich_with_extended_attrs(
                    content, indexer_id, params, session
                )
            except Exception as e:
                log(f"ENRICH ERROR: {e!r}", "WARN")

        try:
            content, probe_results = await hunt_danish(
                content, indexer_id, apikey, session,
                title_only=is_title_only or verdict_suppressed,
                params=params,
            )
            if probe_results and DKSUBS_PROXY_V56_FEATURES:
                await record_indexer_probes(indexer_id, probe_results)
            if ext_id and DKSUBS_PROXY_V56_FEATURES:
                # A "DK hit" is ANY Danish tag applied to the response, not just
                # NFO-probe outcomes. Releases tagged .DKaudio/.DKOK via the
                # title / scene-group / attr shortcuts never enter probe_results,
                # so keying the verdict off probe_results alone wrongly counts a
                # reliably-Danish title (e.g. a NORDiC kids show shortcut-tagged
                # .DKOK) as a zero-DK search and suppresses its NFO probing for
                # days — the bug that blocked Ed, Edd n Eddy. Scan the tagged
                # output instead. We also run this even when verdict_suppressed,
                # so a poisoned 'no_dk' verdict self-heals the instant Danish
                # content shows up again (a suppressed search still title-tags).
                had_dk_hit = (DK_AUDIO_TITLE in content) or (DK_SUBS_TITLE in content)
                await update_movie_verdict(
                    ext_id, ext_id_type, media_type, had_dk_hit
                )
        except Exception as e:
            log(f"HUNT ERROR: {e!r}", "ERROR")

        if _is_status_probe(params):
            content = _inject_probe_filler_if_empty(content)

    if dedup_key and DKSUBS_PROXY_V56_FEATURES:
        await dedup_set(dedup_key, content)
    return web.Response(body=content.encode("utf-8"),
                        headers={"Content-Type": "application/xml"})

# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def on_startup(app):
    await cache_init()
    app['session'] = await get_session()
    await load_indexer_configs(app['session'])
    # Scene group learning: backfill from cache on first run, then rebuild profiles
    if SCENE_GROUP_ENABLED:
        backfilled = await backfill_scene_groups_from_cache()
        if backfilled:
            log(f"Scene groups: backfilled {backfilled} groups from cache history")
        rebuilt = await rebuild_scene_group_profiles()
        log(f"Scene group intelligence: {rebuilt} groups loaded from learned data")
        # Start periodic refresh task
        app['scene_group_refresh'] = asyncio.create_task(_scene_group_refresh_loop())


SCENE_GROUP_REFRESH_INTERVAL = int(os.getenv("SCENE_GROUP_REFRESH_HOURS", "6")) * 3600


async def _scene_group_refresh_loop():
    """Periodically rebuild scene group profiles from accumulated data."""
    while True:
        await asyncio.sleep(SCENE_GROUP_REFRESH_INTERVAL)
        try:
            count = await rebuild_scene_group_profiles()
            log(f"Scene groups refreshed: {count} groups")
        except Exception as e:
            log(f"Scene group refresh failed: {e!r}", "WARN")


async def on_cleanup(app):
    """Drain background NFO tasks, close aiohttp session, close aiosqlite DB.
    Without this, aiohttp shutdown can hang waiting on pending tasks and
    aiosqlite leaves its worker thread alive."""
    global _session, _db
    # 0. Cancel scene group refresh loop
    refresh_task = app.get('scene_group_refresh')
    if refresh_task and not refresh_task.done():
        refresh_task.cancel()
    # 1. Cancel and await any in-flight background fetches.
    pending = [t for t in _background_tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log(f"on_cleanup: {len(pending)} background tasks did not drain "
                "within 5s; abandoning them", "WARN")
    _background_tasks.clear()
    # 2. Close the HTTP session.
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as e:
            log(f"on_cleanup: closing aiohttp session: {e!r}", "WARN")
    _session = None
    # 3. Close the sqlite cache.
    if _db is not None:
        try:
            await _db.close()
        except Exception as e:
            log(f"on_cleanup: closing aiosqlite cache: {e!r}", "WARN")
    _db = None


async def main():
    app = web.Application(client_max_size=10*1024*1024); app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok", "version": VERSION}))
    app.router.add_get("/metrics", lambda r: web.json_response(dict(_metrics)))
    app.router.add_post("/learn/imported", _handle_learn_imported)
    app.router.add_route('*', '/{tail:.*}', handle)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT); await site.start()
    log(f"Proxy v{VERSION} active on {LISTEN_HOST}:{LISTEN_PORT}"); await asyncio.Event().wait()

if __name__ == "__main__": asyncio.run(main())
