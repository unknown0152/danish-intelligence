"""Enrichment: inject extended attrs from direct indexer API queries."""

import aiohttp

from .__init__ import ATTR_RE, GUID_RE, ITEM_RE, log
from .nfo_fetch import _extract_nzb_id, direct_indexer_config


def extract_attrs(item_xml: str) -> dict[str, str]:
    """Extract newznab attrs from an item XML fragment into a dict.
    Multiple values for the same attr name are space-joined."""
    _raw: dict[str, list] = {}
    for m in ATTR_RE.finditer(item_xml):
        _raw.setdefault(m.group(1).lower(), []).append(m.group(2))
    return {k: " ".join(v) for k, v in _raw.items()}


async def enrich_with_extended_attrs(content: str, indexer_id: str, params: dict, session) -> str:
    """Query the indexer directly with extended=1 and inject language/audio/subs attrs into content."""
    cfg = direct_indexer_config(indexer_id)
    apikey = cfg.get("apikey", "")
    baseurl = cfg.get("baseUrl", "")
    if not apikey or not baseurl:
        return content
    direct_params = {k: v for k, v in params.items()
                     if k in ("t", "q", "imdbid", "tvdbid", "tvmazeid", "season", "ep", "limit", "offset", "cat")}
    direct_params["apikey"] = apikey
    direct_params["extended"] = "1"
    try:
        async with session.get(f"{baseurl}/api", params=direct_params,
                               headers={"User-Agent": "danish-intelligence/1.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return content
            direct_xml = await resp.text(errors="replace")
    except Exception as e:
        log(f"ENRICH fetch failed for indexer {indexer_id}: {e!r}", "DEBUG")
        return content
    attr_map: dict[str, dict] = {}
    for item_xml in ITEM_RE.findall(direct_xml):
        guid_m = GUID_RE.search(item_xml)
        if not guid_m:
            continue
        nid = _extract_nzb_id(guid_m.group(1).strip())
        if not nid:
            continue
        attrs = extract_attrs(item_xml)
        if attrs.get("subs") or attrs.get("language") or attrs.get("audio"):
            attr_map[nid] = attrs
    if not attr_map:
        return content
    log(f"ENRICH: injecting extended attrs for {len(attr_map)} items from indexer {indexer_id}", "DEBUG")
    def inject(m):
        item_xml = m.group(1)
        guid_m = GUID_RE.search(item_xml)
        if not guid_m:
            return item_xml
        nid = _extract_nzb_id(guid_m.group(1).strip())
        extra_attrs = attr_map.get(nid, {})
        if not extra_attrs:
            return item_xml
        existing_attrs = extract_attrs(item_xml)
        injected = "".join(
            f'<newznab:attr name="{n}" value="{v}"/>'
            for n, v in extra_attrs.items()
            if n in ("subs", "language", "audio") and v and not existing_attrs.get(n)
        )
        return item_xml.replace("</item>", injected + "</item>") if injected else item_xml
    return ITEM_RE.sub(inject, content)
