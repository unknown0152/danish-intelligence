"""SQLite-backed real-size cache plus a rate-limited background warmer.

The OB API's reported size is the NZB-file size, not the media size. The real
size is obtained by downloading each release's NZB once and summing its segment
bytes (see :mod:`ob_proxy.nzbparse`). That is too slow to do inline during a
search, so searches enqueue cache-miss ids and a background worker fetches them
politely (token-bucket) on OB's separate download rate-limit budget.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from typing import Awaitable, Callable, NamedTuple

from .nzbparse import parse_nzb

log = logging.getLogger("ob_proxy.sizecache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nzb_meta (
    release_id   INTEGER PRIMARY KEY,
    real_size    INTEGER NOT NULL,
    has_password INTEGER NOT NULL DEFAULT 0,
    segments     INTEGER NOT NULL DEFAULT 0,
    fetched_at   INTEGER NOT NULL
);
"""


class Meta(NamedTuple):
    real_size: int
    has_password: bool
    segments: int


# A coroutine that, given a release id, returns the raw NZB bytes.
Fetcher = Callable[[int], Awaitable[bytes]]


class SizeCache:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # --- synchronous store (cheap; guarded by a lock) ---

    def get(self, release_id: int) -> Meta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT real_size, has_password, segments FROM nzb_meta WHERE release_id=?",
                (int(release_id),),
            ).fetchone()
        if not row:
            return None
        return Meta(real_size=row[0], has_password=bool(row[1]), segments=row[2])

    def put(self, release_id: int, real_size: int, has_password: bool, segments: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nzb_meta (release_id, real_size, has_password, segments, fetched_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(release_id) DO UPDATE SET "
                "real_size=excluded.real_size, has_password=excluded.has_password, "
                "segments=excluded.segments, fetched_at=excluded.fetched_at",
                (int(release_id), int(real_size), 1 if has_password else 0, int(segments), int(time.time())),
            )
            self._conn.commit()

    def store_from_nzb(self, release_id: int, data: bytes) -> Meta:
        """Parse NZB bytes and upsert. Returns the stored Meta."""
        parsed = parse_nzb(data)
        self.put(release_id, parsed.size, parsed.password is not None, parsed.segments)
        return Meta(parsed.size, parsed.password is not None, parsed.segments)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Warmer:
    """Background task that fills the size cache, rate-limited per minute."""

    def __init__(self, cache: SizeCache, fetcher: Fetcher, per_min: int = 20) -> None:
        self._cache = cache
        self._fetcher = fetcher
        self._interval = 60.0 / max(1, per_min)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._enqueued: set[int] = set()
        self._task: asyncio.Task | None = None
        self._stop = False

    def enqueue(self, release_id: int) -> None:
        rid = int(release_id)
        if rid in self._enqueued or self._cache.get(rid) is not None:
            return
        self._enqueued.add(rid)
        self._queue.put_nowait(rid)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ob-size-warmer")

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop:
            release_id = await self._queue.get()
            try:
                if self._cache.get(release_id) is None:
                    data = await self._fetcher(release_id)
                    meta = self._cache.store_from_nzb(release_id, data)
                    log.info(
                        "warmed size for id=%s: %.2f GB (password=%s)",
                        release_id, meta.real_size / 1e9, meta.has_password,
                    )
            except Exception as exc:  # noqa: BLE001 - warmer must never die
                log.warning("warmer failed for id=%s: %s", release_id, exc)
            finally:
                self._enqueued.discard(release_id)
            await asyncio.sleep(self._interval)
