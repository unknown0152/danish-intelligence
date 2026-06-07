import asyncio
import os
import sys
import secrets
import re
import time
import logging
import subprocess
from pathlib import Path

from aiohttp import web
import aiohttp

# 1. Import dksubs-proxy logic
import main as main_proxy

# 2. Import ob-proxy core logic
from ob_proxy.config import Config as OBConfig
from ob_proxy.obclient import OBClient
from ob_proxy.sizecache import SizeCache
import ob_proxy.server as ob_server

# 3. Import danskarr logic
from autopilot import run_autopilot
from auto_config import paint as paint_auto_config

# Silence noise
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)


def ensure_proxy_api_key():
    """Cosmos may leave {Passwords.32} empty; persist a fallback key if needed."""
    if os.environ.get("PROXY_API_KEY"):
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
    except Exception as e:
        os.environ["PROXY_API_KEY"] = secrets.token_urlsafe(32)
        print(f"[Core] Generated ephemeral PROXY_API_KEY fallback: {e}", flush=True)

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
    await asyncio.sleep(30) # Give Arrs time to start
    
    try:
        print("[Core] Auto-Config: Painting Custom Formats and Profiles...", flush=True)
        totals = await asyncio.to_thread(paint_auto_config)
        print(f"[Core] Auto-Config: SUCCESS. {totals}", flush=True)
    except Exception as e:
        print(f"[Core] Auto-Config: Critical Error: {e}", flush=True)

async def on_startup(app):
    print("[Core] Running startup sequence...", flush=True)
    ensure_proxy_api_key()
    
    # Init dksubs
    await main_proxy.on_startup(app)
    print("[Core] dksubs initialized", flush=True)
    
    # Init OldBoys
    try:
        ob_cfg = OBConfig.from_env()
        app['config'] = ob_cfg
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
        print("[Core] OldBoys components initialized", flush=True)
    except Exception as e:
        print(f"[Core] Error initializing OldBoys: {e}", flush=True)

    # Start background tasks
    app['autopilot_task'] = asyncio.create_task(autopilot_loop())
    app['autoconfig_task'] = asyncio.create_task(auto_config_painter())
    print("[Core] Startup sequence complete.", flush=True)

async def on_cleanup(app):
    print("[Core] Running cleanup sequence...", flush=True)
    for task_key in ['autopilot_task', 'autoconfig_task']:
        if task_key in app:
            app[task_key].cancel()
    
    if 'client' in app:
        await app['client'].close()
    
    if 'cache' in app:
        app['cache'].close()
        
    await main_proxy.on_cleanup(app)
    print("[Core] Cleanup sequence complete.", flush=True)

async def main():
    app = web.Application(client_max_size=10*1024*1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Routes
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok", "service": "danish-intelligence"}))
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
    app.router.add_get("/metrics", lambda r: web.json_response(dict(main_proxy._metrics)))
    app.router.add_post("/learn/imported", main_proxy._handle_learn_imported)
    
    # NFO Hunter catch-all
    app.router.add_route('*', '/{tail:.*}', main_proxy.handle)

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
