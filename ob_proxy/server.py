"""aiohttp application: Newznab front end wired to the OB client + size cache."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from aiohttp import web

from .categories import estimate_size
from .config import Config
from .newznab import build_caps, build_results
from .nzbparse import parse_nzb
from .obclient import OBClient, OBError
from .sizecache import SizeCache, Warmer
from .translate import build_ob_params, normalize_title, requested_ob_categories

log = logging.getLogger("ob_proxy.server")

# Newznab search "modes" we accept on t=.
_SEARCH_MODES = {"search", "tvsearch", "tv-search", "movie", "movie-search"}


def _safe_filename(base: str, password: str | None) -> str:
    base = "".join(c for c in base if c.isalnum() or c in " ._-").strip() or "release"
    if password:
        return f"{base}{{{{{password}}}}}.nzb"
    return f"{base}.nzb"


def _enclosure_base(request: web.Request, cfg: Config) -> str:
    if cfg.public_url:
        return cfg.public_url
    # Derive from how the caller reached us (honours reverse proxies via Host).
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"


def _require_apikey(request: web.Request) -> web.Response | None:
    if not request.app.get("oldboys_enabled"):
        return web.Response(status=503, text="oldboys disabled")
    cfg: Config = request.app["config"]
    if request.query.get("apikey") != cfg.proxy_api_key:
        return web.Response(status=401, text="unauthorized")
    return None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def handle_health(request: web.Request) -> web.Response:
    if not request.app.get("oldboys_enabled"):
        return web.json_response({"status": "disabled", "service": "oldboys"})
    return web.json_response({"status": "ok", "service": "oldboys"})


async def handle_api(request: web.Request) -> web.Response:
    denied = _require_apikey(request)
    if denied is not None:
        return denied

    t = request.query.get("t", "")
    if t == "caps":
        return web.Response(body=build_caps().encode(), content_type="application/xml")
    if t in _SEARCH_MODES:
        return await _handle_search(request)
    if t == "get":
        return await _handle_get(request)
    return web.Response(status=400, text=f"unsupported t={t!r}")


async def _handle_search(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    client: OBClient = request.app["client"]
    cache: SizeCache = request.app["cache"]
    warmer: Warmer = request.app["warmer"]

    ob_params = build_ob_params(request.query, cfg.cat_map, default_per_page=100)
    try:
        releases = await client.search(ob_params)
    except OBError as exc:
        log.warning("OB search failed: %s", exc)
        return web.Response(status=502, text="upstream error")

    requested_categories = requested_ob_categories(request.query, cfg.cat_map)
    if requested_categories:
        releases = [
            r for r in releases if _int_or_none(r.get("category_id")) in requested_categories
        ]

    # Resolve sizes: real (cached) or provisional, enqueueing misses for warming.
    sizes: dict[str, int] = {}
    for r in releases:
        rid = str(r.get("id"))
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            sizes[rid] = 0
            continue
        meta = cache.get(rid_int)
        if meta and meta.real_size > 0:
            sizes[rid] = meta.real_size
        else:
            sizes[rid] = estimate_size(r.get("resolution"), r.get("category_id"), cfg.cat_map.ob_tv)
            # warmer.enqueue(rid_int) # Disabled to save bandwidth

    base = _enclosure_base(request, cfg)

    def download_url(rid: str) -> str:
        qs = urlencode({"t": "get", "id": rid, "apikey": cfg.proxy_api_key})
        return f"{base}/api?{qs}"

    xml = build_results(
        releases,
        download_url=download_url,
        ob_to_newznab=cfg.cat_map.ob_to_newznab,
        get_size=lambda r: sizes.get(str(r.get("id")), 0),
    )
    return web.Response(body=xml.encode(), content_type="application/xml")


async def _handle_get(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    client: OBClient = request.app["client"]
    cache: SizeCache = request.app["cache"]

    raw_id = request.query.get("id", "")
    if not raw_id.isdigit():
        return web.Response(status=400, text="invalid id")
    rid = int(raw_id)

    try:
        data, ob_filename = await client.download(rid)
    except OBError as exc:
        log.warning("OB download failed for id=%s: %s", rid, exc)
        return web.Response(status=502, text="upstream error")

    # Parse + cache real size/password while we have the bytes.
    password = parse_nzb(data).password
    cache.store_from_nzb(rid, data)

    base = ob_filename.rsplit(".nzb", 1)[0] if ob_filename else str(rid)
    filename = _safe_filename(normalize_title(base) or str(rid), password)

    return web.Response(
        body=data,
        content_type="application/x-nzb",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def create_app(
    config: Config,
    client: OBClient | None = None,
    cache: SizeCache | None = None,
    warmer: Warmer | None = None,
) -> web.Application:
    """Build the aiohttp app. Dependencies may be injected for testing."""
    app = web.Application()
    app["config"] = config
    app["oldboys_enabled"] = bool(config.ob_api_token and config.ob_rid)

    owns_client = client is None
    if client is None:
        client = OBClient(
            base_url=config.ob_base_url,
            api_token=config.ob_api_token,
            rid=config.ob_rid,
            search_path=config.ob_search_path,
            user_agent=config.user_agent,
            max_pages=config.max_pages,
        )
    if cache is None:
        cache = SizeCache(config.db_path)
    if warmer is None:
        async def _fetch_bytes(rid: int) -> bytes:
            data, _ = await client.download(rid)
            return data
        warmer = Warmer(cache, _fetch_bytes, per_min=config.warmer_per_min)

    app["client"] = client
    app["cache"] = cache
    app["warmer"] = warmer

    async def _on_startup(_app: web.Application) -> None:
        if owns_client:
            await client.start()
        warmer.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await warmer.stop()
        if owns_client:
            await client.close()
        cache.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", handle_health)
    app.router.add_get("/api", handle_api)
    return app
