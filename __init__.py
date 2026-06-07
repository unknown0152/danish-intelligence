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

PROWLARR_URL = os.getenv("PROWLARR_URL", "http://Prowlarr:9696").rstrip("/")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")
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
