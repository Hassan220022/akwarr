"""Radarr-compatible API for Jellyseerr."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from akwarr.api.admin import create_admin_router
from akwarr.api.auth import verify_api_key
from akwarr.api.matching import best_arabic_akwam_match
from akwarr.api.queue import queue_payload
from akwarr.config import get_settings
from akwarr.core.quality import quality_for_profile_id, quality_profiles_payload
from akwarr.core.store import Store
from akwarr.core.tmdb import TMDBClient
from akwarr.core.worker import DownloadWorker
from akwarr.download.aria2 import Aria2Client
from akwarr.library.organizer import MediaOrganizer
from akwarr.scraper.akwam import AkwamScraper, is_valid_artwork_url
from akwarr.scraper.elcinema import ElCinemaScraper

logger = logging.getLogger(__name__)

store: Store | None = None
worker: DownloadWorker | None = None
worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, worker, worker_task
    settings = get_settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    store = Store(
        settings.db_path,
        movies_path=settings.movies_path,
        series_path=settings.series_path,
        staging_path=settings.staging_path,
    )
    await store.init()
    worker = DownloadWorker(settings, store)
    worker_task = asyncio.create_task(worker.run_forever())
    yield
    if worker:
        worker.stop()
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Akwarr Radarr", version="0.1.0", lifespan=lifespan)
router = APIRouter(prefix="/api/v3", dependencies=[Depends(verify_api_key)])


class MovieAddBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str
    tmdbId: int = Field(validation_alias=AliasChoices("tmdbId", "tmdbid"))
    qualityProfileId: int = Field(default=1, validation_alias=AliasChoices("qualityProfileId", "profileId"))
    rootFolderPath: str = ""
    monitored: bool = True
    addOptions: dict[str, Any] = Field(default_factory=dict)
    year: int | None = None
    originalTitle: str | None = None
    overview: str | None = None


class CommandBody(BaseModel):
    name: str
    movieIds: list[int] = Field(default_factory=list)


def _get_store() -> Store:
    if store is None:
        raise HTTPException(503, "Store not ready")
    return store


@router.get("/system/status")
async def system_status() -> dict[str, Any]:
    return {
        "appName": "Akwarr Radarr",
        "instanceName": "Akwarr Radarr",
        "version": "3.0.0.0",
        "isProduction": True,
        "isAdmin": True,
        "isDebug": False,
        "startupPath": "/app",
        "appData": "/config",
        "osName": "linux",
        "osVersion": "docker",
        "branch": "main",
        "authentication": "apiKey",
    }


@router.get("/qualityProfile")
@router.get("/qualityprofile")
async def quality_profiles() -> list[dict[str, Any]]:
    return quality_profiles_payload(get_settings())


@router.get("/tag")
async def tags() -> list[dict[str, Any]]:
    return []


@router.get("/rootfolder")
async def root_folders() -> list[dict[str, Any]]:
    settings = get_settings()
    path = str(settings.movies_path)
    return [
        {
            "id": 1,
            "path": path,
            "accessible": settings.movies_path.exists(),
            "freeSpace": 0,
            "unmappedFolders": [],
        }
    ]


@router.get("/movie/lookup")
async def movie_lookup(term: str = Query(...)) -> list[dict[str, Any]]:
    tmdb = TMDBClient(get_settings())
    return await tmdb.lookup_movie(term)


@router.get("/movie")
async def list_movies() -> list[dict[str, Any]]:
    movies = await _get_store().list_movies()
    return [_movie_payload(m) for m in movies]


@router.get("/movie/{movie_id}")
async def get_movie(movie_id: int) -> dict[str, Any]:
    movie = await _get_store().get_movie(movie_id)
    if not movie:
        raise HTTPException(404, "Movie not found")
    return _movie_payload(movie)


@router.post("/movie")
async def add_movie(body: MovieAddBody) -> dict[str, Any]:
    settings = get_settings()
    s = _get_store()
    tmdb = TMDBClient(settings)
    scraper = AkwamScraper(settings)
    elcinema = ElCinemaScraper(settings)

    tmdb_data = await tmdb.movie(body.tmdbId)
    title = body.title or tmdb_data.get("title") or f"TMDB {body.tmdbId}"
    original = body.originalTitle or tmdb_data.get("original_title")
    year = body.year or tmdb_data.get("year")
    overview = body.overview or tmdb_data.get("overview")

    base_queries = _unique_queries(title, original, tmdb_data.get("original_title"))
    arabic_queries: list[str] = []
    elcinema_metadata: dict[str, str | None] | None = None
    if settings.elcinema_enable:
        try:
            arabic_queries, elcinema_metadata = await _elcinema_candidates(
                elcinema,
                *base_queries,
                year=year,
                kind="movie",
            )
        except Exception:
            logger.exception("ElCinema lookup failed for TMDB %s (%s)", body.tmdbId, title)
    match = await best_arabic_akwam_match(
        scraper,
        fallback_title=title,
        section="movie",
        arabic_queries=arabic_queries,
        base_queries=base_queries,
    )

    poster = TMDBClient.poster_url(tmdb_data.get("poster_path"))
    fanart = TMDBClient.poster_url(tmdb_data.get("backdrop_path"))
    akwam_url = match.url if match else None
    if match and not poster and match.poster and is_valid_artwork_url(match.poster):
        poster = match.poster

    if match:
        try:
            meta = await scraper.fetch_metadata(match.url, kind="movie")
            if not poster and meta.poster and is_valid_artwork_url(meta.poster):
                poster = meta.poster
            if not fanart and meta.fanart and is_valid_artwork_url(meta.fanart):
                fanart = meta.fanart
            if meta.overview and not overview:
                overview = meta.overview
        except Exception:
            logger.exception("Akwam metadata fetch failed for %s", match.url)

    record = await s.add_movie(
        {
            "tmdb_id": body.tmdbId,
            "title": title,
            "original_title": original,
            "year": year,
            "overview": overview,
            "poster_url": poster,
            "fanart_url": fanart,
            "akwam_url": akwam_url,
            "has_file": False,
            "monitored": body.monitored,
            "quality_profile_id": body.qualityProfileId,
            "root_folder_path": str(settings.movies_path),
            "metadata": _external_metadata(tmdb_data.get("imdb_id"), elcinema_metadata),
        }
    )

    if akwam_url and body.addOptions.get("searchForMovie", True) and not record.get("has_file"):
        quality = quality_for_profile_id(body.qualityProfileId, settings)
        plan = MediaOrganizer(settings).movie_plan(title=title, year=year, quality=quality)
        job_id = await s.create_job("movie", record["id"], str(plan.video), quality=quality)
        logger.info("Queued movie job %s for TMDB %s", job_id, body.tmdbId)
    elif not akwam_url:
        logger.warning("No Akwam match for TMDB %s (%s)", body.tmdbId, title)

    return _movie_payload(record)


@router.post("/command")
async def run_command(body: CommandBody) -> dict[str, Any]:
    return {"name": body.name, "status": "queued"}


@router.get("/queue")
async def get_queue(
    page: int = Query(default=1),
    pageSize: int = Query(default=20),
    sortKey: str = Query(default="timeleft"),
    sortDirection: str = Query(default="ascending"),
) -> dict[str, Any]:
    return await queue_payload(
        _get_store(),
        Aria2Client(get_settings()),
        kind="movie",
        page=page,
        page_size=pageSize,
        sort_key=sortKey,
        sort_direction=sortDirection,
    )


def _movie_payload(movie: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": movie["id"],
        "title": movie["title"],
        "originalTitle": movie.get("original_title") or movie["title"],
        "sortTitle": movie["title"],
        "status": "released",
        "overview": movie.get("overview") or "",
        "year": movie.get("year"),
        "hasFile": movie.get("has_file", False),
        "monitored": movie.get("monitored", True),
        "tmdbId": movie["tmdb_id"],
        "qualityProfileId": movie.get("quality_profile_id", 1),
        "rootFolderPath": movie.get("root_folder_path"),
        "path": movie.get("path"),
        "added": movie.get("added"),
        "isAvailable": movie.get("has_file", False),
        "movieFile": {"path": movie["path"]} if movie.get("path") else None,
    }


def _unique_queries(*values: Any) -> list[str]:
    queries: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    return queries


async def _elcinema_candidates(
    elcinema: Any,
    *queries: str,
    year: int | None,
    kind: str,
) -> tuple[list[str], dict[str, str | None] | None]:
    if hasattr(elcinema, "matched_candidates"):
        results = await elcinema.matched_candidates(*queries, year=year, kind=kind)
        candidates = [result.title for result in results]
        metadata = _elcinema_metadata(results[0]) if results else None
        return candidates, metadata
    return await elcinema.arabic_candidates(*queries, year=year, kind=kind), None


def _elcinema_metadata(result: Any) -> dict[str, str | None]:
    url = result.url
    return {
        "id": _elcinema_id_from_url(url),
        "url": url,
        "title": result.title,
        "english_title": result.english_title,
        "year": result.year,
    }


def _elcinema_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = [part for part in url.split("?")[0].rstrip("/").split("/") if part]
    if len(parts) >= 2 and parts[-2] == "work" and parts[-1].isdigit():
        return parts[-1]
    return None


def _external_metadata(
    imdb_id: str | None,
    elcinema: dict[str, str | None] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if imdb_id:
        metadata["imdb_id"] = imdb_id
    if elcinema:
        metadata["elcinema"] = elcinema
    return metadata


app.include_router(router)
app.include_router(create_admin_router(_get_store))
