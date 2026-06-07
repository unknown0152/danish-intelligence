"""Build Newznab XML responses (caps + search results)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Callable, Iterable
from xml.sax.saxutils import escape, quoteattr

from . import __version__
from .categories import MOVIE_SUBCATS, NEWZNAB_MOVIES, NEWZNAB_TV, TV_SUBCATS
from .translate import normalize_imdb, normalize_title

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def build_caps(server_title: str = "OldBoys") -> str:
    """Static capabilities document advertised to Prowlarr."""
    def subcats(pairs):
        return "".join(
            f'      <subcat id="{cid}" name="{escape(name)}"/>\n' for cid, name in pairs
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<caps>\n"
        f'  <server version="{__version__}" title={quoteattr(server_title)}/>\n'
        '  <limits max="100" default="100"/>\n'
        "  <registration available=\"no\" open=\"no\"/>\n"
        "  <searching>\n"
        '    <search available="yes" supportedParams="q"/>\n'
        '    <tv-search available="yes" supportedParams="q,season,ep"/>\n'
        '    <movie-search available="yes" supportedParams="q,imdbid"/>\n'
        "  </searching>\n"
        "  <categories>\n"
        f'    <category id="{NEWZNAB_MOVIES}" name="Movies">\n'
        f"{subcats(MOVIE_SUBCATS)}"
        "    </category>\n"
        f'    <category id="{NEWZNAB_TV}" name="TV">\n'
        f"{subcats(TV_SUBCATS)}"
        "    </category>\n"
        "  </categories>\n"
        "</caps>\n"
    )


def _attr(name: str, value) -> str:
    return f'    <newznab:attr name="{name}" value={quoteattr(str(value))}/>\n'


def _pubdate(created_at: str | None) -> str:
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return format_datetime(dt)
        except ValueError:
            pass
    return format_datetime(datetime(1970, 1, 1, tzinfo=timezone.utc))


def build_results(
    releases: Iterable[dict],
    download_url: Callable[[str], str],
    ob_to_newznab: Callable[[int | None], int],
    get_size: Callable[[dict], int],
    server_title: str = "OldBoys",
) -> str:
    """Render OB releases as a Newznab RSS feed.

    ``download_url(id)`` returns the proxied enclosure URL; ``get_size(release)``
    returns the size in bytes to advertise (real if cached, else provisional).
    """
    items: list[str] = []
    for r in releases:
        rid = str(r.get("id"))
        title = normalize_title(r.get("name"))
        size = int(get_size(r))
        dl = download_url(rid)
        nz_cat = ob_to_newznab(r.get("category_id"))
        pubdate = _pubdate(r.get("created_at"))
        details = r.get("details_link") or ""

        parts = [
            "  <item>\n",
            f"    <title>{escape(title)}</title>\n",
            f"    <guid isPermaLink=\"false\">{escape(rid)}</guid>\n",
            f"    <link>{escape(dl)}</link>\n",
            f"    <comments>{escape(details)}</comments>\n",
            f"    <pubDate>{pubdate}</pubDate>\n",
            f"    <category>{nz_cat}</category>\n",
            f'    <enclosure url={quoteattr(dl)} length="{size}" type="application/x-nzb"/>\n',
            _attr("category", nz_cat),
            _attr("size", size),
            _attr("guid", rid),
        ]

        imdb = normalize_imdb(str(r.get("imdb_id"))) if r.get("imdb_id") else None
        if imdb:
            parts.append(_attr("imdb", f"{int(imdb):07d}"))
        if r.get("tmdb_id"):
            parts.append(_attr("tmdbid", r["tmdb_id"]))
        if r.get("tvdb_id"):
            parts.append(_attr("tvdbid", r["tvdb_id"]))
        # password attr: 0 to prevent automatic rejection by Arrs. 
        # AltMount handles the password natively via the {{password}} filename tag.
        parts.append(_attr("password", 0))
        parts.append("  </item>\n")
        items.append("".join(parts))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rss version="2.0" xmlns:newznab="{NEWZNAB_NS}">\n'
        "  <channel>\n"
        f"    <title>{escape(server_title)}</title>\n"
        "    <description>OldBoys Newznab proxy</description>\n"
        f"{''.join(items)}"
        "  </channel>\n"
        "</rss>\n"
    )
