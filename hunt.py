"""Hunt: main NFO-hunter logic and /learn/imported endpoint."""

import asyncio
import hashlib
import json
import secrets

from aiohttp import web

from .__init__ import ATTR_DK_RE, DKSUBS_PROXY_NFO_MEDIA_TAGS, DK_AUDIO_TITLE, DK_SUBS_TITLE, DROP_NON_DK, DESC_RE, GUID_RE, INDEXER_SCORING_THRESHOLD, ITEM_RE, NFO_EARLY_EXIT_HITS, NFO_TIMEOUT, PROWLARR_API_KEY, SIZE_ATTR_RE, SIZE_RE, TITLE_RE, _desc_classifier_ids, _metrics, log, nfo_budget_for, probe_score, scene_group_verdict
from .cache import cache_get, cache_set, get_indexer_score, record_scene_group
from .classification import AUDIO_DK_RE, LOWQ_RE, SUBS_DK_RE, _extract_nfo_media_tags, _min_size_for, classify_mismatch, classify_nfo_text, compute_actual_tag, is_native_dk_title, normalize_release_name, strip_proxy_suffix
from .enrichment import extract_attrs
from .layers import register_background_task
from .nfo_fetch import _extract_nzb_id, fetch_nfo, fetch_nfo_direct
from .tags import NO_DK_TAG


def _attr_has_value(attrs: dict[str, str], name: str) -> bool:
    return bool((attrs.get(name) or "").strip())


async def hunt_danish(content, indexer_id, apikey, session,
                      title_only: bool = False, params: dict | None = None,
                      native_titles: list[str] | None = None):
    """Returns (xml, probe_results_dict). probe_results maps nzb_id -> tag
    (or NO_DK_TAG) for each NFO probed in this search."""
    params = params or {}
    _metrics["hunt_total"] += 1
    found_hits = {}
    candidates = []
    items = ITEM_RE.findall(content)
    log(f"HUNT: parsed {len(items)} items from indexer {indexer_id}", "INFO")
    probe_results: dict = {}
    media_tags_by_nid: dict = {}
    for item_xml in items:
        title_m = TITLE_RE.search(item_xml)
        guid_m = GUID_RE.search(item_xml)
        if not title_m or not guid_m:
            continue
        title = title_m.group(2)
        g_url = guid_m.group(1).strip()
        nid = _extract_nzb_id(g_url)
        if not nid:
            continue
        
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
        if is_native_dk_title(title, native_titles):
            found_hits[nid] = DK_AUDIO_TITLE
            await cache_set(nid, DK_AUDIO_TITLE, title, source="native-title")
            continue
        
        # Audio Check
        if AUDIO_DK_RE.search(title):
            found_hits[nid] = DK_AUDIO_TITLE
            await cache_set(nid, DK_AUDIO_TITLE, title, source="title")
            continue

        subs_from_title = bool(SUBS_DK_RE.search(title))
        # Scene group shortcut: if NORDiC in title + known group, skip NFO
        if subs_from_title:
            sg_verdict = scene_group_verdict(title)
            if sg_verdict == "audio":
                found_hits[nid] = DK_AUDIO_TITLE
                _metrics["scene_group_audio_shortcuts"] += 1
                await cache_set(nid, DK_AUDIO_TITLE, title, source="group")
                log(f"Scene group shortcut -> {DK_AUDIO_TITLE} for {title[:50]}", "DEBUG")
                continue
            elif sg_verdict == "subs":
                found_hits[nid] = DK_SUBS_TITLE
                _metrics["scene_group_subs_skips"] += 1
                await cache_set(nid, DK_SUBS_TITLE, title, source="group")
                log(f"Scene group shortcut -> {DK_SUBS_TITLE} for {title[:50]}", "DEBUG")
                continue
        # v5.7 PR B: description classifier
        if indexer_id in _desc_classifier_ids:
            desc_m = DESC_RE.search(item_xml)
            if desc_m and len(desc_m.group(1)) >= 100:
                desc_tag = classify_nfo_text(desc_m.group(1))
                if desc_tag != NO_DK_TAG:
                    found_hits[nid] = desc_tag
                    _metrics["desc_classifier_hits"] += 1
                    await cache_set(nid, desc_tag, title, source="description")
                    continue
        if subs_from_title and skip_nfo_for_size:
            skip_nfo_for_size = False
        attrs = extract_attrs(item_xml)
        language_attr = attrs.get("language", "")
        audio_attr = attrs.get("audio", "")
        subs_attr = attrs.get("subs", "")
        language_has_value = _attr_has_value(attrs, "language")
        audio_has_value = _attr_has_value(attrs, "audio")
        language_is_dk = bool(ATTR_DK_RE.search(language_attr))
        audio_is_dk = bool(ATTR_DK_RE.search(audio_attr))
        subs_attr_is_dk = bool(ATTR_DK_RE.search(subs_attr))
        if audio_is_dk:
            found_hits[nid] = DK_AUDIO_TITLE
            await cache_set(nid, DK_AUDIO_TITLE, title, source="attr")
            continue
        if language_is_dk:
            if not subs_from_title:
                found_hits[nid] = DK_AUDIO_TITLE
                await cache_set(nid, DK_AUDIO_TITLE, title, source="attr")
                continue
            # else: NORDiC + language=Danish is ambiguous, fall through
        if subs_attr_is_dk:
            if not subs_from_title:
                found_hits[nid] = DK_SUBS_TITLE
                await cache_set(nid, DK_SUBS_TITLE, title, source="attr")
                continue
        if language_has_value and not language_is_dk and (
            subs_from_title or subs_attr_is_dk
        ):
            found_hits[nid] = DK_SUBS_TITLE
            await cache_set(nid, DK_SUBS_TITLE, title, source="attr")
            log(f"Attribute shortcut -> {DK_SUBS_TITLE} for {title[:50]}", "DEBUG")
            continue
        if audio_has_value and not audio_is_dk and (
            subs_from_title or subs_attr_is_dk
        ):
            found_hits[nid] = DK_SUBS_TITLE
            await cache_set(nid, DK_SUBS_TITLE, title, source="attr")
            log(f"Attribute shortcut -> {DK_SUBS_TITLE} for {title[:50]}", "DEBUG")
            continue
        if language_has_value and not language_is_dk and not subs_from_title and not subs_attr_is_dk:
            probe_results[nid] = NO_DK_TAG
            log(f"Attribute shortcut -> {NO_DK_TAG} for {title[:50]}", "DEBUG")
            continue
        if audio_has_value and not audio_is_dk and not subs_from_title and not subs_attr_is_dk:
            probe_results[nid] = NO_DK_TAG
            log(f"Attribute shortcut -> {NO_DK_TAG} for {title[:50]}", "DEBUG")
            continue
        if not title_only and not skip_nfo_for_size:
            if attrs.get("nfo") == "0":
                if subs_from_title:
                    found_hits[nid] = DK_SUBS_TITLE
                    await cache_set(nid, DK_SUBS_TITLE, title, source="title")
                log(f"nfo=0 skip for {nid} ({title[:50]})", "DEBUG")
                continue
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
                return nid, cached if cached != NO_DK_TAG else (DK_SUBS_TITLE if subs_fallback else NO_DK_TAG)

            text = await fetch_nfo(session, indexer_id, nid, apikey)

            via_direct = False
            if text is None:
                text = await fetch_nfo_direct(session, indexer_id, nid, g_url, info_url)
                via_direct = text is not None

            if text is None:
                return nid, DK_SUBS_TITLE if subs_fallback else NO_DK_TAG

            tag = classify_nfo_text(text)
            if via_direct and tag != NO_DK_TAG:
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
            if tag == NO_DK_TAG and subs_fallback:
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
                    if tag and tag != NO_DK_TAG:
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
        xml = m.group(1)
        g_m = GUID_RE.search(xml)
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
    from .cache import _db

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

    previous_tag = NO_DK_TAG
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
                    previous_tag = row[1] or NO_DK_TAG
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
