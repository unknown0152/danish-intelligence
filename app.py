"""App: HTTP handler, lifecycle hooks, main entry point."""

import asyncio
import json
import os
import re
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from aiohttp import web

from urllib.parse import parse_qs, urlencode, urlparse

from .__init__ import DK_AUDIO_TITLE, DK_SUBS_TITLE, DKSUBS_PROXY_V56_FEATURES, LISTEN_HOST, LISTEN_PORT, PROWLARR_API_KEY, PROWLARR_URL, SCENE_GROUP_ENABLED, VERSION, _enrich_ids, _metrics, _nfo_ids, _req_id, _scene_group_profiles, _title_only_ids, log
from .cache import _db, backfill_scene_groups_from_cache, cache_init, rebuild_scene_group_profiles, record_indexer_probes
from .classification import _empty_or_filler_response, _inject_probe_filler_if_empty, _is_status_probe, fold_scandi_query
from .enrichment import enrich_with_extended_attrs
from .hunt import _handle_learn_imported, hunt_danish
from .layers import _background_tasks, _session, dedup_get, dedup_inflight_lock, dedup_set, extract_external_id, get_session, request_key, update_movie_verdict, verdict_says_no_dk
from .nfo_fetch import _search_rate_limit_ok, load_indexer_configs


# ── Handler ───────────────────────────────────────────────────────────────────

ALTMOUNT_VISIBLE_ROOT = os.getenv("ALTMOUNT_VISIBLE_ROOT", "/mnt/altmount").rstrip("/")
ALTMOUNT_URL = os.getenv("ALTMOUNT_URL", "http://altmount:8080/sabnzbd").rstrip("?")
ALTMOUNT_API_KEY = os.getenv("ALTMOUNT_API_KEY") or os.getenv("ALTMOUNT_APIKEY") or ""
RADARR_URL = os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/")
SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/")


def _clean_env(name: str) -> str:
    value = os.getenv(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _read_config_key(app_name: str) -> str:
    for path in (f"/arr-config/{app_name}/config.xml", f"/srv/config/{app_name}/config.xml"):
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            key = ET.parse(cfg).getroot().findtext("ApiKey", default="").strip()
            if key:
                return key
        except Exception as exc:
            log(f"altmount-shim: could not read {path}: {exc}", "WARN")
    return ""


RADARR_API_KEY = _clean_env("RADARR_API_KEY") or _clean_env("RADARR_APIKEY") or _read_config_key("radarr")
SONARR_API_KEY = _clean_env("SONARR_API_KEY") or _clean_env("SONARR_APIKEY") or _read_config_key("sonarr")


def _normalize_altmount_path(value: str) -> str:
    if not value.startswith(f"{ALTMOUNT_VISIBLE_ROOT}/"):
        return value
    duplicate = f"/{ALTMOUNT_VISIBLE_ROOT.lstrip('/')}/"
    duplicate_at = value.find(duplicate, len(ALTMOUNT_VISIBLE_ROOT))
    if duplicate_at != -1:
        return value[duplicate_at:]
    return value


def _normalize_altmount_response(obj):
    if isinstance(obj, dict):
        return {
            key: _normalize_altmount_path(value) if key in {"path", "storage", "filename"} and isinstance(value, str)
            else _normalize_altmount_response(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_normalize_altmount_response(item) for item in obj]
    return obj


def _redacted_query(query) -> str:
    secret_names = {"apikey", "api_key", "x-api-key", "token", "key", "password", "ma_password"}
    return urlencode([
        (name, "***" if name.lower() in secret_names else value)
        for name, value in query.items()
    ], doseq=True)


async def handle_altmount(request: web.Request) -> web.Response:
    """Shim for AltMount/SABnzbd: translates mode=qstatus -> mode=status."""
    params = dict(request.rel_url.query)
    if params.get("mode") == "qstatus":
        params["mode"] = "status"
    if ALTMOUNT_API_KEY:
        params["apikey"] = ALTMOUNT_API_KEY
    elif "ma_username" not in params and "ma_password" not in params:
        params.pop("apikey", None)
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
            if params.get("output") == "json" and params.get("mode") in {"history", "queue"}:
                try:
                    payload = _normalize_altmount_response(json.loads(body.decode("utf-8")))
                    body = json.dumps(payload).encode("utf-8")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            return web.Response(body=body, status=resp.status,
                                headers={"Content-Type": "application/json"})
    except Exception as e:
        log(f"altmount-shim: forward failed: {e!r}", "WARN")
        return web.json_response({"status": False, "error": str(e)}, status=502)


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
                # NFO-probe outcomes. Releases tagged by title / scene-group /
                # attr shortcuts never enter probe_results, so keying the verdict
                # off probe_results alone wrongly counts a reliably-Danish title
                # as a zero-DK search and suppresses its NFO probing for days.
                # Scan the tagged output instead. We also run this even when
                # verdict_suppressed, so a poisoned 'no_dk' verdict self-heals
                # the instant Danish content shows up again.
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
