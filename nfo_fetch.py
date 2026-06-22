"""NFO fetching: rate limiting, indexer config loading, NFO retrieval."""

import asyncio
import collections
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
from urllib.parse import urlparse

import aiohttp

from .__init__ import NFO_TIMEOUT, PROWLARR_API_KEY, PROWLARR_URL, _metrics, log, scrub


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


def _merge_indexer_config(configs: dict[str, dict], iid: str, values: dict) -> None:
    current = configs.setdefault(str(iid), {})
    for key in ("apikey", "baseUrl"):
        value = (values.get(key) or "").strip()
        if value and not current.get(key):
            current[key] = value.rstrip("/") if key == "baseUrl" else value


def _load_indexer_configs_from_prowlarr_db(configs: dict[str, dict]) -> int:
    """Fill direct indexer API keys from the read-only Prowlarr config mount."""
    db_path = os.getenv("PROWLARR_DB_PATH", "/arr-config/prowlarr/prowlarr.db")
    if not os.path.exists(db_path):
        return 0
    loaded = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("select Id, Settings from Indexers where Enable = 1").fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                settings = json.loads(row["Settings"] or "{}")
            except Exception:
                continue
            before = dict(configs.get(str(row["Id"]), {}))
            _merge_indexer_config(
                configs,
                str(row["Id"]),
                {
                    "apikey": settings.get("apiKey", ""),
                    "baseUrl": settings.get("baseUrl", ""),
                },
            )
            after = configs.get(str(row["Id"]), {})
            if after != before and after.get("apikey") and after.get("baseUrl"):
                loaded += 1
    except Exception as e:
        log(f"Could not load direct indexer configs from Prowlarr DB: {e!r}", "WARN")
    return loaded


def has_direct_indexer_config(indexer_id: str) -> bool:
    cfg = _indexer_configs.get(str(indexer_id), {})
    return bool(cfg.get("apikey") and cfg.get("baseUrl"))


def direct_indexer_config(indexer_id: str) -> dict:
    return dict(_indexer_configs.get(str(indexer_id), {}))

async def load_indexer_configs(session) -> None:
    """Load per-indexer API keys/base URLs from env or Prowlarr REST."""
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

    db_loaded = _load_indexer_configs_from_prowlarr_db(configs)
    if configs:
        _indexer_configs = configs
        source = "env/Prowlarr DB" if db_loaded else "env"
        log(f"Loaded direct API configs for {len(configs)} indexers from {source}")
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
            log(f"Loaded baseUrls for {len(configs)} indexers from Prowlarr REST API (apikeys masked)", "WARN")
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
