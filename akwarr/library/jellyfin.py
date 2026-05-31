"""Jellyfin library scan notifications."""

from __future__ import annotations

import logging

import httpx

from akwarr.config import Settings

logger = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.jellyfin_url.rstrip("/")
        self.api_key = settings.jellyfin_api_key
        self.movies_library = settings.jellyfin_movies_library_name
        self.series_library = settings.jellyfin_series_library_name

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base)

    def _headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.api_key}

    async def refresh_path(self, path: str) -> None:
        if not self.enabled:
            logger.debug("Jellyfin refresh skipped (no API key)")
            return
        payload = {"Updates": [{"Path": path, "UpdateType": "Created"}]}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.post(
                    f"{self.base}/Library/Media/Updated",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                logger.info("Jellyfin refresh triggered for %s", path)
            except httpx.HTTPError as exc:
                logger.warning("Jellyfin refresh failed: %s", exc)

    async def refresh_library(self, *, movies: bool = False, series: bool = False) -> None:
        if not self.enabled:
            return
        name = self.movies_library if movies else self.series_library if series else None
        if not name:
            return
        lib_id = await self._find_library_id(name)
        if not lib_id:
            logger.warning("Jellyfin library not found: %s", name)
            return
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.post(
                    f"{self.base}/Library/Refresh",
                    headers=self._headers(),
                    params={"ItemId": lib_id, "Recursive": "true"},
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Jellyfin library refresh failed: %s", exc)

    async def _find_library_id(self, name: str) -> str | None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.base}/Library/VirtualFolders",
                headers=self._headers(),
            )
            r.raise_for_status()
            for lib in r.json():
                if lib.get("Name") == name:
                    locations = lib.get("Locations") or []
                    item_id = lib.get("ItemId") or lib.get("Id")
                    if item_id:
                        return str(item_id)
                    if locations:
                        return locations[0]
        return None
