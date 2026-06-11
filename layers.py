"""Layers: request dedup, inflight locks, session, background tasks, verdicts."""

import asyncio
import hashlib
import os
import time

import aiohttp

from .__init__ import DKSUBS_PROXY_V56_FEATURES, GLOBAL_CONCURRENCY, MOVIE_VERDICT_TRIGGER, MOVIE_VERDICT_TTL, MOVIE_VERDICT_WINDOW, NFO_TIMEOUT, REQUEST_DEDUP_TTL, VERSION, _metrics, log
from .cache import _db


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
    from .cache import _db
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
    from .cache import _db
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
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=GLOBAL_CONCURRENCY), headers={"User-Agent": f"Danish-Intelligence/{VERSION}"})
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
    from .cache import _db
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
    from .cache import _db
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
