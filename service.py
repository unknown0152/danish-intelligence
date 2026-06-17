import asyncio
import html
import os
import sys
import secrets
import re
import time
import logging
import subprocess
import xml.etree.ElementTree as ET
import importlib.util
from pathlib import Path

from aiohttp import web
import aiohttp

PACKAGE_NAME = "danish_intelligence"
PACKAGE_DIR = Path(__file__).resolve().parent
ALTMOUNT_SHIM_MAX_UPLOAD_MB = int(os.getenv("ALTMOUNT_SHIM_MAX_UPLOAD_MB", "128"))


def _load_package_alias() -> None:
    """Allow `python3 service.py` from /app while modules use relative imports."""
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not initialize danish_intelligence package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    sys.modules[f"{PACKAGE_NAME}.__init__"] = module
    spec.loader.exec_module(module)


_load_package_alias()

# 1. Import Danish Intelligence modular logic
from danish_intelligence import app as main_proxy
from danish_intelligence.hunt import _handle_learn_imported
from danish_intelligence.marker_preserver import handle_arr_webhook
from danish_intelligence.autopilot import run_autopilot
from danish_intelligence.auto_config import paint as paint_auto_config
from danish_intelligence.diagnostics import dns_state, path_state, record, safe_env, summary as diagnostics_summary

handle = main_proxy.handle
proxy_startup = main_proxy.on_startup
proxy_cleanup = main_proxy.on_cleanup
VERSION = main_proxy.VERSION
_metrics = main_proxy._metrics

# 2. Import ob-proxy core logic
from ob_proxy.config import Config as OBConfig
from ob_proxy.obclient import OBClient
from ob_proxy.sizecache import SizeCache
import ob_proxy.server as ob_server

# Silence noise
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)


CONFIG_KEY_PATHS = {
    "prowlarr": ("/arr-config/prowlarr/config.xml", "/srv/config/prowlarr/config.xml"),
    "radarr": ("/arr-config/radarr/config.xml", "/srv/config/radarr/config.xml"),
    "sonarr": ("/arr-config/sonarr/config.xml", "/srv/config/sonarr/config.xml"),
}


def _clean_env(name: str) -> str:
    value = os.environ.get(name, "")
    return "" if value.startswith("{") and value.endswith("}") else value


def _read_config_key(app_name: str) -> str:
    for path in CONFIG_KEY_PATHS.get(app_name, ()):
        cfg = Path(path)
        if not cfg.exists():
            continue
        try:
            key = ET.parse(cfg).getroot().findtext("ApiKey", default="").strip()
            if key:
                return key
        except Exception as exc:
            print(f"[Core] Could not read {path}: {exc}", flush=True)
    return ""


def ensure_prowlarr_api_key() -> bool:
    key = _clean_env("PROWLARR_API_KEY") or _clean_env("PROWLARR_APIKEY") or _read_config_key("prowlarr")
    if not key:
        record("prowlarr_key.missing")
        return False

    os.environ["PROWLARR_API_KEY"] = key
    main_proxy.PROWLARR_API_KEY = key
    record("prowlarr_key.discovered", source="env_or_config")
    return True


def ensure_proxy_api_key():
    """Cosmos may leave {Passwords.32} empty; persist a fallback key if needed."""
    if _clean_env("PROXY_API_KEY"):
        os.environ["PROXY_API_KEY"] = _clean_env("PROXY_API_KEY")
        return

    key_path = Path(os.getenv("PROXY_API_KEY_FILE", "/config/proxy_api_key"))
    try:
        if key_path.exists():
            key = key_path.read_text().strip()
        else:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_urlsafe(32)
            key_path.write_text(key)
            key_path.chmod(0o600)
        os.environ["PROXY_API_KEY"] = key
        print("[Core] Generated fallback PROXY_API_KEY for OldBoys proxy", flush=True)
        record("proxy_key.ready", persisted=True)
    except Exception as e:
        os.environ["PROXY_API_KEY"] = secrets.token_urlsafe(32)
        print(f"[Core] Generated ephemeral PROXY_API_KEY fallback: {e}", flush=True)
        record("proxy_key.ephemeral", error=str(e))

async def autopilot_loop():
    """Background task for DanskArr autopilot."""
    await asyncio.sleep(60)
    while True:
        try:
            print("[DanskArr] Starting scheduled autopilot run...", flush=True)
            await asyncio.to_thread(run_autopilot, dry_run=False)
            print("[DanskArr] Autopilot run completed.", flush=True)
        except Exception as e:
            print(f"[DanskArr] Error: {e}", flush=True)
        await asyncio.sleep(3600 * 6)

async def auto_config_painter():
    """Paint CFs, profiles, and proxy URLs through Servarr HTTP APIs."""
    print("[Core] Auto-Config: Waiting for Arrs to be ready...", flush=True)
    record("auto_config.waiting", delay_seconds=30)
    await asyncio.sleep(30) # Give Arrs time to start
    
    try:
        _APP_STATE["auto_config"] = {"status": "running", "result": None, "error": None}
        print("[Core] Auto-Config: Painting Custom Formats and Profiles...", flush=True)
        record("auto_config.started")
        totals = await asyncio.to_thread(paint_auto_config)
        _APP_STATE["auto_config"] = {"status": "success", "result": totals, "error": None}
        print(f"[Core] Auto-Config: SUCCESS. {totals}", flush=True)
        record("auto_config.success", totals=totals)
    except Exception as e:
        _APP_STATE["auto_config"] = {"status": "failed", "result": None, "error": str(e)}
        print(f"[Core] Auto-Config: Critical Error: {e}", flush=True)
        record("auto_config.failed", error=str(e), error_type=type(e).__name__)


_APP_STATE = {
    "auto_config": {"status": "not_started", "result": None, "error": None},
    "prowlarr_key_discovered": False,
    "oldboys": {"enabled": False, "configured": False, "error": None},
}

async def on_startup(app):
    print("[Core] Running startup sequence...", flush=True)
    record(
        "startup.begin",
        env=safe_env([
            "PROWLARR_URL",
            "PROWLARR_API_KEY",
            "PROXY_URL",
            "ARR_PROXY_URL",
            "RADARR_URL",
            "SONARR_URL",
            "RADARR_2160P_URL",
            "SONARR_2160P_URL",
            "SEERR_URL",
            "MEDIA_SERVER_TYPE",
            "MEDIA_SERVER_URL",
            "ALTMOUNT_URL",
            "ENABLE_2160P_ARRS",
            "PUID",
            "PGID",
        ]),
        paths=path_state([
            "/config",
            "/arr-config/prowlarr",
            "/arr-config/prowlarr/config.xml",
            "/arr-config/radarr",
            "/arr-config/radarr/config.xml",
            "/arr-config/sonarr",
            "/arr-config/sonarr/config.xml",
            "/arr-config/radarr-2160p",
            "/arr-config/radarr-2160p/config.xml",
            "/arr-config/sonarr-2160p",
            "/arr-config/sonarr-2160p/config.xml",
            "/seerr-config",
            "/media",
            "/mnt",
        ]),
        dns=dns_state({
            "prowlarr": os.getenv("PROWLARR_URL", "http://prowlarr:9696"),
            "radarr": os.getenv("RADARR_URL", "http://radarr:7878"),
            "sonarr": os.getenv("SONARR_URL", "http://sonarr:8989"),
            "radarr-2160p": os.getenv("RADARR_2160P_URL", "http://radarr-2160p:7878"),
            "sonarr-2160p": os.getenv("SONARR_2160P_URL", "http://sonarr-2160p:8989"),
            "seerr": os.getenv("SEERR_URL", "http://seerr:5055"),
            "altmount": os.getenv("ALTMOUNT_URL", "http://altmount:8080/sabnzbd"),
            "danish-intelligence": os.getenv("PROXY_URL", "http://danish-intelligence:9699"),
        }),
    )
    ensure_proxy_api_key()
    _APP_STATE["prowlarr_key_discovered"] = ensure_prowlarr_api_key()
    
    # Init modular proxy
    await proxy_startup(app)
    print("[Core] Danish Intelligence modular core initialized", flush=True)
    record("startup.proxy_initialized")
    
    # Init OldBoys
    try:
        ob_cfg = OBConfig.from_env()
        app['config'] = ob_cfg
        if ob_cfg.ob_api_token and ob_cfg.ob_rid:
            app['client'] = OBClient(
                base_url=ob_cfg.ob_base_url,
                api_token=ob_cfg.ob_api_token,
                rid=ob_cfg.ob_rid,
                search_path=ob_cfg.ob_search_path,
                user_agent=ob_cfg.user_agent,
                max_pages=ob_cfg.max_pages,
            )
            await app['client'].start()
            app['cache'] = SizeCache(ob_cfg.db_path)
            app['warmer'] = type('Dummy', (), {'enqueue': lambda self, x: None})()
            app['oldboys_enabled'] = True
            _APP_STATE["oldboys"] = {"enabled": True, "configured": True, "error": None}
            print("[Core] OldBoys components initialized", flush=True)
            record("oldboys.ready", enabled=True)
        else:
            app['oldboys_enabled'] = False
            _APP_STATE["oldboys"] = {"enabled": False, "configured": False, "error": None}
            print("[Core] OldBoys credentials not set; OldBoys features disabled.", flush=True)
            record("oldboys.disabled", reason="missing_credentials")
    except Exception as e:
        app['oldboys_enabled'] = False
        _APP_STATE["oldboys"] = {"enabled": False, "configured": False, "error": str(e)}
        print(f"[Core] Error initializing OldBoys: {e}", flush=True)
        record("oldboys.failed", error=str(e), error_type=type(e).__name__)

    # Start background tasks
    app['autopilot_task'] = asyncio.create_task(autopilot_loop())
    app['autoconfig_task'] = asyncio.create_task(auto_config_painter())
    print("[Core] Startup sequence complete.", flush=True)
    record("startup.complete")

async def on_cleanup(app):
    print("[Core] Running cleanup sequence...", flush=True)
    for task_key in ['autopilot_task', 'autoconfig_task']:
        if task_key in app:
            app[task_key].cancel()
    
    if 'client' in app:
        await app['client'].close()
    
    if 'cache' in app:
        app['cache'].close()
        
    await proxy_cleanup(app)
    print("[Core] Cleanup sequence complete.", flush=True)


async def _http_ok(app: web.Application, url: str, api_key: str = "") -> bool:
    try:
        headers = {"X-Api-Key": api_key} if api_key else {}
        async with app['session'].get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


async def _status_payload(app: web.Application) -> dict:
    prowlarr_key = _clean_env("PROWLARR_API_KEY") or _clean_env("PROWLARR_APIKEY") or _read_config_key("prowlarr")
    radarr_key = _clean_env("RADARR_API_KEY") or _clean_env("RADARR_APIKEY") or _read_config_key("radarr")
    sonarr_key = _clean_env("SONARR_API_KEY") or _clean_env("SONARR_APIKEY") or _read_config_key("sonarr")
    prowlarr_url = os.getenv("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
    radarr_url = os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/")
    sonarr_url = os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/")
    seerr_url = os.getenv("SEERR_URL", "http://seerr:5055").rstrip("/")
    media_server_type = os.getenv("MEDIA_SERVER_TYPE", "").strip().lower()
    media_server_url = os.getenv("MEDIA_SERVER_URL", "").rstrip("/")
    altmount_url = os.getenv("ALTMOUNT_URL", "http://altmount:8080/sabnzbd").rstrip("?")
    altmount_base_url = altmount_url.split("/sabnzbd", 1)[0].rstrip("/")
    media_server_health_url = ""
    if media_server_url:
        if media_server_type == "plex":
            media_server_health_url = f"{media_server_url}/identity"
        elif media_server_type == "jellyfin":
            media_server_health_url = f"{media_server_url}/System/Info/Public"
        else:
            media_server_health_url = media_server_url

    checks = {
        "prowlarr_key": bool(prowlarr_key),
        "prowlarr_reachable": await _http_ok(app, f"{prowlarr_url}/api/v1/system/status", prowlarr_key),
        "radarr_key": bool(radarr_key),
        "radarr_reachable": await _http_ok(app, f"{radarr_url}/api/v3/system/status", radarr_key),
        "sonarr_key": bool(sonarr_key),
        "sonarr_reachable": await _http_ok(app, f"{sonarr_url}/api/v3/system/status", sonarr_key),
        "seerr_reachable": await _http_ok(app, f"{seerr_url}/api/v1/settings/public"),
        "media_server_reachable": await _http_ok(app, media_server_health_url) if media_server_health_url else True,
        "altmount_reachable": await _http_ok(app, f"{altmount_base_url}/"),
        "oldboys_optional": True,
        "auto_config_success": _APP_STATE["auto_config"]["status"] == "success",
    }
    ready = all(
        checks[name]
        for name in (
            "prowlarr_key",
            "prowlarr_reachable",
            "radarr_key",
            "radarr_reachable",
            "sonarr_key",
            "sonarr_reachable",
            "seerr_reachable",
            "media_server_reachable",
            "altmount_reachable",
            "auto_config_success",
        )
    )
    return {
        "status": "ready" if ready else "attention",
        "checks": checks,
        "auto_config": _APP_STATE["auto_config"],
        "oldboys": _APP_STATE["oldboys"],
        "service": {
            "version": getattr(main_proxy, "VERSION", "unknown"),
            "proxy_url": os.getenv("PROXY_URL", "http://danish-intelligence:9699"),
            "seerr_url": seerr_url,
            "media_server_type": media_server_type or "external",
            "media_server_url": media_server_url,
        },
    }


def _status_html(payload: dict) -> str:
    rows = []
    for name, ok in payload["checks"].items():
        label = name.replace("_", " ").title()
        rows.append(
            f"<tr><td>{html.escape(label)}</td><td class=\"{'ok' if ok else 'bad'}\">"
            f"{'OK' if ok else 'Needs attention'}</td></tr>"
        )
    auto = payload["auto_config"]
    error = html.escape(auto.get("error") or "")
    error_html = f'<p class="bad">{error}</p>' if error else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Danish Intelligence Status</title>"
        "<style>body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;max-width:860px}"
        "table{border-collapse:collapse;width:100%;margin-top:1rem}td{border-bottom:1px solid #ddd;padding:.65rem}"
        ".ok{color:#126b38;font-weight:700}.bad{color:#9f1d1d;font-weight:700}"
        "code{background:#f2f2f2;padding:.15rem .3rem;border-radius:4px}</style></head><body>"
        f"<h1>Danish Intelligence: {html.escape(payload['status'].upper())}</h1>"
        "<p>This page shows whether the Danish media stack is configured and reachable.</p>"
        f"<table>{''.join(rows)}</table>"
        f"<p>Auto-config: <code>{html.escape(auto.get('status', 'unknown'))}</code></p>"
        f"{error_html}"
        "<p>JSON: <a href=\"/status.json\">/status.json</a></p>"
        "</body></html>"
    )


async def handle_status_json(request: web.Request) -> web.Response:
    return web.json_response(await _status_payload(request.app))


async def handle_status(request: web.Request) -> web.Response:
    payload = await _status_payload(request.app)
    return web.Response(text=_status_html(payload), content_type="text/html")


async def handle_install_debug(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "120"))
    except ValueError:
        limit = 120
    return web.json_response(diagnostics_summary(max(1, min(limit, 400))))


async def handle_radarr_webhook(request: web.Request) -> web.Response:
    return await handle_arr_webhook(request, "radarr")


async def handle_sonarr_webhook(request: web.Request) -> web.Response:
    return await handle_arr_webhook(request, "sonarr")


async def handle_arr_instance_webhook(request: web.Request) -> web.Response:
    source = request.match_info.get("source", "").strip().lower()
    if not source.startswith(("radarr", "sonarr")):
        return web.Response(status=404, text="unknown arr source\n")
    return await handle_arr_webhook(request, source)

async def main():
    app = web.Application(client_max_size=ALTMOUNT_SHIM_MAX_UPLOAD_MB * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Routes
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok", "service": "danish-intelligence"}))
    app.router.add_get("/status", handle_status)
    app.router.add_get("/status.json", handle_status_json)
    app.router.add_get("/debug/install", handle_install_debug)
    app.router.add_get("/ob/api", ob_server.handle_api)
    app.router.add_get("/ob/health", ob_server.handle_health)

    # Cosmos Market Endpoints
    async def serve_market(r):
        with open("cosmos-market.json", "r") as f: return web.Response(text=f.read(), content_type="application/json")
    async def serve_compose(r):
        with open("cosmos-compose.json", "r") as f: return web.Response(text=f.read(), content_type="application/json")
    
    app.router.add_get("/cosmos-market.json", serve_market)
    app.router.add_get("/cosmos-compose.json", serve_compose)
    
    # dksubs metrics and learn
    app.router.add_get("/metrics", lambda r: web.json_response(dict(_metrics)))
    app.router.add_post("/learn/imported", _handle_learn_imported)
    app.router.add_post("/arr/radarr", handle_radarr_webhook)
    app.router.add_post("/arr/sonarr", handle_sonarr_webhook)
    app.router.add_post("/arr/{source}", handle_arr_instance_webhook)
    
    # NFO Hunter catch-all
    app.router.add_route('*', '/{tail:.*}', handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9699)
    await site.start()
    
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("  Danish Intelligence Core v6.0 Active on :9699", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
