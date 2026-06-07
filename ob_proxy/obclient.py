"""Async HTTP client for the OldBoys (UNIT3D-fork) API."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

log = logging.getLogger("ob_proxy.obclient")


class OBError(Exception):
    """Raised when the OB upstream returns an error or unexpected response."""


class OBClient:
    """Talks to OB: search via /api/torrents/filter, download via /nzbs/download.

    Only ever contacts ``base_url`` (no user-controlled hosts) — SSRF-safe.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        rid: str,
        search_path: str = "/api/nzbs/filter",
        user_agent: str = "ob-proxy/0.1",
        max_pages: int = 5,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.rid = rid
        self.search_path = "/" + search_path.strip("/")
        self.user_agent = user_agent
        self.max_pages = max_pages
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": self.user_agent},
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise OBError("client session not started")
        return self._session

    async def search(self, params: dict) -> list[dict]:
        """Run a filter search, following links.next up to ``max_pages``.

        Returns a flat list of release dicts ``{"id": str, **attributes}``.
        """
        url = f"{self.base_url}{self.search_path}"
        headers = {"Authorization": f"Bearer {self.api_token}"}
        results: list[dict] = []
        query = dict(params)
        
        # Local Filter Logic:
        # UNIT3D (OldBoys) search is notoriously bad at matching season/ep tokens.
        # We strip the token from the upstream request to maximize recall,
        # then filter the results locally.
        name_query = query.get("name", "").lower()
        clean_name = name_query
        token = ""
        m = re.search(r"s\d{1,2}(?:e\d{1,3})?", name_query, re.I)
        if m:
            token = m.group(0).lower()
            clean_name = name_query.replace(m.group(0), "").strip()
            query["name"] = clean_name
            log.info(f"OB Broad Search: '{name_query}' -> '{clean_name}' (local filter: {token})")

        for _ in range(self.max_pages):
            async with self.session.get(url, params=query, headers=headers) as resp:
                if resp.status == 429:
                    log.warning("OB search rate-limited (429); stopping pagination")
                    break
                if resp.status != 200:
                    raise OBError(f"OB search returned HTTP {resp.status}")
                payload: dict[str, Any] = await resp.json()

            for item in payload.get("data", []):
                attrs = item.get("attributes", {}) or {}
                rel_name = attrs.get("name", "").lower().replace(".", " ")
                
                # Apply local token filter (e.g. s01 must be in title)
                if token and token not in rel_name:
                    continue
                
                # Double check clean_name is in rel_name to avoid unrelated results
                if clean_name and clean_name not in rel_name:
                    continue
                    
                log.info(f"Local Filter MATCH: '{attrs.get('name')}'")
                results.append({"id": str(item.get("id")), **attrs})

            if not payload.get("links", {}).get("next"):
                break
            cur = payload.get("meta", {}).get("current_page", query.get("page", 1))
            query["page"] = int(cur) + 1
        return results

    async def download(self, release_id: int) -> tuple[bytes, str | None]:
        """Fetch the raw .nzb bytes for a release id.

        Uses the RID-in-URL download route. Returns (data, suggested_filename).
        ``release_id`` must already be validated as a positive integer.
        """
        url = f"{self.base_url}/nzbs/download/{int(release_id)}.{self.rid}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                raise OBError(f"OB download returned HTTP {resp.status}")
            data = await resp.read()
            filename = None
            disp = resp.headers.get("Content-Disposition", "")
            if "filename=" in disp:
                filename = disp.split("filename=", 1)[1].strip().strip('"')
            return data, filename
