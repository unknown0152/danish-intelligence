"""Translate Newznab query parameters into OldBoys (UNIT3D) API parameters."""

from __future__ import annotations

import re
from typing import Mapping

from .categories import CategoryMap

# UNIT3D caps perPage at 100.
MAX_PER_PAGE = 100

_TT = re.compile(r"^tt", re.IGNORECASE)
_TRAILING_NZB = re.compile(r"\s+nzb\s*$", re.IGNORECASE)


def normalize_imdb(value: str | None) -> str | None:
    """Newznab imdb ids may arrive as ``tt0123456``, ``0123456`` or ``123456``.

    UNIT3D wants a bare integer with no ``tt`` prefix and no zero padding.
    Returns the integer as a string, or None if not a valid id.
    """
    if not value:
        return None
    v = _TT.sub("", value.strip())
    if not v.isdigit():
        return None
    n = int(v)
    return str(n) if n > 0 else None


def normalize_title(name: str | None) -> str:
    """Strip OB's trailing ``" nzb"`` marker from release names."""
    if not name:
        return ""
    return _TRAILING_NZB.sub("", name).strip()


def _int_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value.isdigit() else None


def _episode_token(season: str | None, ep: str | None) -> str:
    """Return a SEASON token (``S03``) for TV searches — deliberately NOT
    ``S03E12``.

    OB indexes most TV as season packs (``Show.S03.NORDiC...``) alongside any
    individual episodes. A season-level query (``Show S03``) matches BOTH the
    pack and that season's episodes, whereas ``Show S03E12`` matches only the
    exact episode and misses the pack (and returns nothing for pack-only
    seasons). Sonarr re-parses returned titles and selects the wanted episode
    (or the season pack) itself, so season-level recall is what we want.
    The ``ep`` argument is accepted but intentionally ignored.
    """
    s = _int_or_none(season)
    if s is not None:
        return f"S{int(s):02d}"
    return ""


def build_ob_params(
    query: Mapping[str, str],
    cat_map: CategoryMap,
    default_per_page: int = MAX_PER_PAGE,
) -> dict:
    """Build the OB query dict. We strip most filters to maximize recall."""
    params: dict = {}

    name = (query.get("q") or "").strip()
    if name:
        params["name"] = name

    # Always use max per page to avoid missing results due to OldBoys' internal sorting
    params["perPage"] = MAX_PER_PAGE

    return params


def requested_ob_categories(query: Mapping[str, str], cat_map: CategoryMap) -> set[int]:
    """Return OldBoys category ids requested by the Newznab ``cat`` parameter."""
    cats: list[int] = []
    for raw in (query.get("cat") or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            cats.append(int(raw))
    return set(cat_map.newznab_to_ob(cats))
