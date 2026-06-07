"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .categories import CategoryMap


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"required environment variable {name} is not set")
    return val or ""


@dataclass
class Config:
    ob_base_url: str
    ob_api_token: str
    ob_rid: str
    ob_search_path: str    # OB's real NZB search endpoint (not the dead UNIT3D /api/torrents)
    proxy_api_key: str
    cat_map: CategoryMap
    host: str
    port: int
    db_path: str
    public_url: str        # override base for download links; empty = derive from request
    warmer_per_min: int    # background NZB fetches per minute (OB download budget is 30/min)
    max_pages: int         # cap pages followed per search
    user_agent: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            ob_base_url=_env("OB_BASE_URL", "https://oldboys.pw").rstrip("/"),
            ob_api_token=_env("OB_API_TOKEN", required=True),
            ob_rid=_env("OB_RID", required=True),
            ob_search_path=_env("OB_SEARCH_PATH", "/api/nzbs/filter"),
            proxy_api_key=_env("PROXY_API_KEY", required=True),
            cat_map=CategoryMap(
                ob_movie=int(_env("OB_CAT_MOVIE", "1")),
                ob_tv=int(_env("OB_CAT_TV", "2")),
                ob_xxx=int(_env("OB_CAT_XXX", "8")),
                ob_books=int(_env("OB_CAT_BOOKS", "6")),
            ),
            host=_env("OB_PROXY_HOST", "0.0.0.0"),
            port=int(_env("OB_PROXY_PORT", "9700")),
            db_path=_env("OB_PROXY_DB", "/data/nzb_meta.db"),
            public_url=_env("PROXY_PUBLIC_URL", "").rstrip("/"),
            warmer_per_min=int(_env("OB_WARMER_PER_MIN", "20")),
            max_pages=int(_env("OB_MAX_PAGES", "5")),
            user_agent=_env("OB_PROXY_UA", "ob-proxy/0.1"),
        )
