"""Newznab <-> OldBoys (UNIT3D) category mapping and provisional size estimates.

Newznab uses numeric category ids (2000 = Movies, 5000 = TV, with sub-categories
like 2040 = Movies/HD). UNIT3D uses its own small integer ``category_id`` per
torrent. From live data we have only observed TV = 2; Movie defaults to UNIT3D's
standard id 1. Both are configurable via env so they can be corrected without a
code change.
"""

from __future__ import annotations

# Newznab top-level buckets we advertise.
NEWZNAB_MOVIES = 2000
NEWZNAB_TV = 5000
NEWZNAB_XXX = 6000
NEWZNAB_BOOKS = 7000

# Newznab sub-categories advertised in caps (kept small / standard).
MOVIE_SUBCATS = [
    (2030, "SD"),
    (2040, "HD"),
    (2045, "UHD"),
    (2050, "BluRay"),
    (2010, "Foreign"),
]
TV_SUBCATS = [
    (5030, "SD"),
    (5040, "HD"),
    (5045, "UHD"),
    (5070, "Anime"),
]


class CategoryMap:
    """Bidirectional Newznab <-> OB category mapping.

    ``ob_movie`` / ``ob_tv`` are the UNIT3D ``category_id`` values on OB.
    """

    def __init__(
        self, ob_movie: int = 1, ob_tv: int = 2, ob_xxx: int = 8, ob_books: int = 6
    ) -> None:
        self.ob_movie = ob_movie
        self.ob_tv = ob_tv
        self.ob_xxx = ob_xxx
        self.ob_books = ob_books

    def newznab_to_ob(self, newznab_cats: list[int]) -> list[int]:
        """Map requested Newznab category ids to OB category_id values.

        2xxx -> Movies, 5xxx -> TV, 6xxx -> XXX, 7xxx -> Books. Unknown/empty
        input yields an empty list (search everything). De-duplicated, ordered.
        """
        buckets = [
            (2000, 3000, self.ob_movie),
            (5000, 6000, self.ob_tv),
            (6000, 7000, self.ob_xxx),
            (7000, 8000, self.ob_books),
        ]
        out: list[int] = []
        for c in newznab_cats:
            for lo, hi, ob in buckets:
                if lo <= c < hi:
                    if ob not in out:
                        out.append(ob)
                    break
        return out

    def ob_to_newznab(self, ob_category_id: int | None) -> int:
        """Map an OB category_id to a Newznab top-level bucket.

        Defaults to Movies for anything not explicitly TV/XXX/Books.
        """
        if ob_category_id == self.ob_tv:
            return NEWZNAB_TV
        if ob_category_id == self.ob_xxx:
            return NEWZNAB_XXX
        if ob_category_id == self.ob_books:
            return NEWZNAB_BOOKS
        return NEWZNAB_MOVIES


# Provisional size estimates (bytes) used only until the real size is parsed
# from the NZB and cached. Keyed by a coarse resolution bucket; TV episodes are
# assumed smaller than movies.
_GB = 1024 ** 3
_MOVIE_SIZE = {"2160": 25 * _GB, "1080": 8 * _GB, "720": 3 * _GB, "sd": 1 * _GB}
_TV_SIZE = {"2160": 6 * _GB, "1080": 2 * _GB, "720": 900 * 1024 * 1024, "sd": 350 * 1024 * 1024}


def _resolution_bucket(resolution: str | None) -> str:
    r = (resolution or "").lower()
    if "2160" in r or "4k" in r:
        return "2160"
    if "1080" in r:
        return "1080"
    if "720" in r:
        return "720"
    return "sd"


def estimate_size(resolution: str | None, ob_category_id: int | None, ob_tv: int) -> int:
    """Coarse provisional size used before the real NZB size is known."""
    bucket = _resolution_bucket(resolution)
    table = _TV_SIZE if ob_category_id == ob_tv else _MOVIE_SIZE
    return table[bucket]
