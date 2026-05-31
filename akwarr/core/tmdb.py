"""TMDB metadata client."""

from __future__ import annotations

from typing import Any

import httpx

from akwarr.config import Settings


class TMDBClient:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.tmdb_api_key
        self.language = settings.metadata_language

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def movie(self, tmdb_id: int) -> dict[str, Any]:
        if not self.enabled:
            return {"id": tmdb_id, "title": f"TMDB {tmdb_id}", "year": None}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.BASE}/movie/{tmdb_id}",
                params={"api_key": self.api_key, "language": self.language},
            )
            r.raise_for_status()
            data = r.json()
            year = None
            if data.get("release_date"):
                year = int(data["release_date"][:4])
            return {
                "id": data["id"],
                "title": data.get("title") or data.get("original_title"),
                "original_title": data.get("original_title"),
                "year": year,
                "overview": data.get("overview"),
                "imdb_id": data.get("imdb_id"),
                "poster_path": data.get("poster_path"),
                "backdrop_path": data.get("backdrop_path"),
            }

    async def tv(self, tmdb_id: int) -> dict[str, Any]:
        if not self.enabled:
            return {"id": tmdb_id, "title": f"TMDB {tmdb_id}", "year": None, "seasons": []}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.BASE}/tv/{tmdb_id}",
                params={
                    "api_key": self.api_key,
                    "language": self.language,
                    "append_to_response": "external_ids",
                },
            )
            r.raise_for_status()
            data = r.json()
            year = None
            if data.get("first_air_date"):
                year = int(data["first_air_date"][:4])
            external_ids = data.get("external_ids") or {}
            return {
                "id": data["id"],
                "title": data.get("name") or data.get("original_name"),
                "original_title": data.get("original_name"),
                "year": year,
                "overview": data.get("overview"),
                "imdb_id": external_ids.get("imdb_id"),
                "tvdb_id": external_ids.get("tvdb_id"),
                "poster_path": data.get("poster_path"),
                "backdrop_path": data.get("backdrop_path"),
                "seasons": data.get("seasons") or [],
            }

    async def lookup_movie(self, term: str) -> list[dict[str, Any]]:
        if term.startswith("tmdb:"):
            tmdb_id = int(term.split(":", 1)[1])
            m = await self.movie(tmdb_id)
            return [self._movie_lookup_payload(m)]
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.BASE}/search/movie",
                params={"api_key": self.api_key, "language": self.language, "query": term},
            )
            r.raise_for_status()
            return [self._movie_lookup_payload(item) for item in r.json().get("results", [])]

    async def lookup_tv(self, term: str) -> list[dict[str, Any]]:
        if term.startswith("tmdb:"):
            tmdb_id = int(term.split(":", 1)[1])
            s = await self.tv(tmdb_id)
            return [self._series_lookup_payload(s)]
        if term.startswith("tvdb:"):
            tvdb_id = int(term.split(":", 1)[1])
            s = await self.tv_from_tvdb(tvdb_id)
            return [self._series_lookup_payload(s)] if s else []
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.BASE}/search/tv",
                params={"api_key": self.api_key, "language": self.language, "query": term},
            )
            r.raise_for_status()
            return [self._series_lookup_payload(item) for item in r.json().get("results", [])]

    async def tv_from_tvdb(self, tvdb_id: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.BASE}/find/{tvdb_id}",
                params={
                    "api_key": self.api_key,
                    "language": self.language,
                    "external_source": "tvdb_id",
                },
            )
            r.raise_for_status()
            results = r.json().get("tv_results", [])
            if not results:
                return None
            data = results[0]
        data = await self.tv(int(data["id"]))
        data["tvdb_id"] = tvdb_id
        return data

    def _movie_lookup_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        year = data.get("year")
        if not year and data.get("release_date"):
            year = int(str(data["release_date"])[:4])
        title = data.get("title") or data.get("name") or "Unknown"
        return {
            "title": title,
            "originalTitle": data.get("original_title") or data.get("original_name") or title,
            "sortTitle": title,
            "status": "released",
            "overview": data.get("overview") or "",
            "inCinemas": f"{year}-01-01" if year else None,
            "physicalRelease": f"{year}-01-01" if year else None,
            "digitalRelease": f"{year}-01-01" if year else None,
            "year": year,
            "hasFile": False,
            "isAvailable": False,
            "monitored": True,
            "tmdbId": data.get("id"),
            "imdbId": data.get("imdb_id"),
            "images": [],
        }

    def _series_lookup_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        year = data.get("year")
        if not year and data.get("first_air_date"):
            year = int(str(data["first_air_date"])[:4])
        title = data.get("title") or data.get("name") or "Unknown"
        seasons = self._season_lookup_payloads(data.get("seasons"))
        return {
            "title": title,
            "sortTitle": title,
            "originalTitle": data.get("original_title") or data.get("original_name") or title,
            "status": "continuing",
            "overview": data.get("overview") or "",
            "year": year,
            "seasonCount": len(seasons),
            "seasons": seasons,
            "hasFile": False,
            "monitored": True,
            "tmdbId": data.get("id"),
            "tvdbId": data.get("tvdb_id") or data.get("tvdbId") or 0,
            "imdbId": data.get("imdb_id") or data.get("imdbId"),
            "images": [],
        }

    @staticmethod
    def _season_lookup_payloads(seasons: Any) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for raw in seasons or []:
            if not isinstance(raw, dict):
                continue
            season_number = raw.get("season_number", raw.get("seasonNumber"))
            if season_number in (None, 0):
                continue
            episode_count = int(raw.get("episode_count", raw.get("episodeCount") or 0) or 0)
            payloads.append(
                {
                    "seasonNumber": int(season_number),
                    "monitored": True,
                    "statistics": {
                        "episodeFileCount": 0,
                        "episodeCount": episode_count,
                        "totalEpisodeCount": episode_count,
                        "sizeOnDisk": 0,
                        "percentOfEpisodes": 0,
                    },
                }
            )
        if not payloads:
            payloads.append(
                {
                    "seasonNumber": 1,
                    "monitored": True,
                    "statistics": {
                        "episodeFileCount": 0,
                        "episodeCount": 0,
                        "totalEpisodeCount": 0,
                        "sizeOnDisk": 0,
                        "percentOfEpisodes": 0,
                    },
                }
            )
        return sorted(payloads, key=lambda season: season["seasonNumber"])

    @staticmethod
    def poster_url(path: str | None) -> str | None:
        if not path:
            return None
        return f"https://image.tmdb.org/t/p/original{path}"
