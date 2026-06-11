"""Cache: SQLite-backed NFO cache, request dedup, indexer probes, scene group learning."""

import re
import time

import aiosqlite

from .__init__ import CACHE_DB, CACHE_TTL_NEGATIVE, DK_AUDIO_TITLE, DK_SUBS_TITLE, INDEXER_SCORING_ENABLED, INDEXER_SCORING_MIN_PROBES, INDEXER_SCORING_WINDOW, SCENE_GROUP_MIN_RELEASES, _metrics, _scene_group_profiles, log
from .classification import normalize_result_tag


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
        await _db.execute("CREATE INDEX IF NOT EXISTS idx_nfo_cache_release_name ON nfo_cache (release_name)")
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
