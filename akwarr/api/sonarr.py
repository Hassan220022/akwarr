"""Sonarr-compatible API for Jellyseerr."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from akwarr.api.admin import create_admin_router
from akwarr.api.auth import verify_api_key
from akwarr.api.queue import queue_payload
from akwarr.config import get_settings
from akwarr.core.store import Store
from akwarr.core.tmdb import TMDBClient
from akwarr.core.worker import DownloadWorker
from akwarr.download.aria2 import Aria2Client
from akwarr.library.organizer import MediaOrganizer
from akwarr.scraper.akwam import AkwamScraper
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


app = FastAPI(title="Akwarr Sonarr", version="0.1.0", lifespan=lifespan)
router = APIRouter(prefix="/api/v3", dependencies=[Depends(verify_api_key)])


class SeasonSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    seasonNumber: int = Field(validation_alias=AliasChoices("seasonNumber", "season"))
    monitored: bool = True


class SeriesAddBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str
    tmdbId: int | None = Field(default=None, validation_alias=AliasChoices("tmdbId", "tmdbid"))
    tvdbId: int = Field(default=0, validation_alias=AliasChoices("tvdbId", "tvdbid"))
    qualityProfileId: int = Field(default=1, validation_alias=AliasChoices("qualityProfileId", "profileId"))
    languageProfileId: int = 1
    rootFolderPath: str = ""
    monitored: bool = True
    seasonFolder: bool = True
    seasons: list[SeasonSpec] = Field(default_factory=list)
    addOptions: dict[str, Any] = Field(default_factory=dict)
    searchNow: bool | None = None
    year: int | None = None
    originalTitle: str | None = None
    overview: str | None = None


class CommandBody(BaseModel):
    name: str
    seriesId: int | None = None


def _get_store() -> Store:
    if store is None:
        raise HTTPException(503, "Store not ready")
    return store


@router.get("/system/status")
async def system_status() -> dict[str, Any]:
    return {
        "appName": "Akwarr Sonarr",
        "instanceName": "Akwarr Sonarr",
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
    return [
        {
            "id": 1,
            "name": "Arabic 720p",
            "upgradeAllowed": False,
            "cutoff": 1,
            "items": [{"quality": {"id": 1, "name": "720p"}, "allowed": True}],
        }
    ]


@router.get("/languageProfile")
@router.get("/languageprofile")
async def language_profiles() -> list[dict[str, Any]]:
    return [{"id": 1, "name": "Arabic", "upgradeAllowed": False, "cutoff": 1}]


@router.get("/tag")
async def tags() -> list[dict[str, Any]]:
    return []


@router.get("/rootfolder")
async def root_folders() -> list[dict[str, Any]]:
    settings = get_settings()
    path = str(settings.series_path)
    return [
        {
            "id": 1,
            "path": path,
            "accessible": settings.series_path.exists(),
            "freeSpace": 0,
            "unmappedFolders": [],
        }
    ]


@router.get("/series/lookup")
async def series_lookup(term: str = Query(...)) -> list[dict[str, Any]]:
    tmdb = TMDBClient(get_settings())
    return await tmdb.lookup_tv(term)


@router.get("/series")
async def list_series() -> list[dict[str, Any]]:
    items = await _get_store().list_series()
    return [await _series_payload(s) for s in items]


@router.get("/series/{series_id}")
async def get_series(series_id: int) -> dict[str, Any]:
    item = await _get_store().get_series(series_id)
    if not item:
        raise HTTPException(404, "Series not found")
    return await _series_payload(item)


@router.get("/episode")
async def list_episodes(seriesId: int | None = Query(default=None)) -> list[dict[str, Any]]:
    eps = await _get_store().list_episodes(seriesId)
    return [_episode_payload(e) for e in eps]


@router.post("/series")
async def add_series(body: SeriesAddBody) -> dict[str, Any]:
    settings = get_settings()
    s = _get_store()
    tmdb = TMDBClient(settings)
    scraper = AkwamScraper(settings)
    elcinema = ElCinemaScraper(settings)
    organizer = MediaOrganizer(settings)

    tmdb_id = body.tmdbId
    if tmdb_id is None:
        if not body.tvdbId:
            raise HTTPException(400, "tmdbId or tvdbId is required")
        tmdb_data = await tmdb.tv_from_tvdb(body.tvdbId)
        if not tmdb_data:
            raise HTTPException(404, "Series not found")
        tmdb_id = int(tmdb_data["id"])
    else:
        tmdb_data = await tmdb.tv(tmdb_id)

    title = body.title or tmdb_data.get("title") or f"TMDB {tmdb_id}"
    original = body.originalTitle or tmdb_data.get("original_title")
    year = body.year or tmdb_data.get("year")
    overview = body.overview or tmdb_data.get("overview")

    base_queries = _unique_queries(title, original, tmdb_data.get("original_title"))
    arabic_queries: list[str] = []
    if settings.elcinema_enable:
        try:
            arabic_queries = await elcinema.arabic_candidates(*base_queries, year=year, kind="series")
        except Exception:
            logger.exception("ElCinema lookup failed for TMDB %s (%s)", tmdb_id, title)
    alt_queries = _unique_queries(*arabic_queries, *base_queries)
    match = await scraper.best_match(title, section="series", alt_queries=alt_queries)

    poster = TMDBClient.poster_url(tmdb_data.get("poster_path"))
    fanart = TMDBClient.poster_url(tmdb_data.get("backdrop_path"))
    akwam_url = match.url if match else None
    akwam_episodes: list = []

    if match:
        try:
            meta = await scraper.fetch_metadata(match.url, kind="series")
            akwam_episodes = meta.episodes
            if meta.poster:
                poster = meta.poster
            if meta.fanart:
                fanart = meta.fanart
            if meta.overview and not overview:
                overview = meta.overview
        except Exception:
            logger.exception("Akwam metadata fetch failed for %s", match.url)

    show_folder = organizer.series_folder(title=title, year=year)
    record = await s.add_series(
        {
            "tmdb_id": tmdb_id,
            "tvdb_id": body.tvdbId,
            "title": title,
            "original_title": original,
            "year": year,
            "overview": overview,
            "poster_url": poster,
            "fanart_url": fanart,
            "akwam_url": akwam_url,
            "path": str(show_folder),
            "monitored": body.monitored,
            "season_folder": body.seasonFolder,
            "quality_profile_id": body.qualityProfileId,
            "language_profile_id": body.languageProfileId,
            "root_folder_path": str(settings.series_path),
        }
    )

    monitored_seasons = {spec.seasonNumber for spec in body.seasons if spec.monitored}
    if not monitored_seasons:
        monitored_seasons = {1}

    default_season = next(iter(monitored_seasons)) if len(monitored_seasons) == 1 else 1

    search_missing = body.addOptions.get("searchForMissingEpisodes", body.searchNow)
    if search_missing is None:
        search_missing = True

    if search_missing and akwam_episodes:
        for ep in akwam_episodes:
            season = ep.season or default_season
            if monitored_seasons and season not in monitored_seasons:
                continue
            ep_record = await s.upsert_episode(
                {
                    "series_id": record["id"],
                    "season_number": season,
                    "episode_number": ep.number,
                    "title": ep.title,
                    "akwam_url": ep.url,
                    "monitored": True,
                    "has_file": False,
                }
            )
            plan = organizer.episode_plan(
                series_title=title,
                year=year,
                season=season,
                episode=ep.number,
                episode_title=ep.title,
                quality="720p",
            )
            await s.create_job("episode", ep_record["id"], str(plan.video))

    return await _series_payload(record)


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
        kind="episode",
        page=page,
        page_size=pageSize,
        sort_key=sortKey,
        sort_direction=sortDirection,
    )


async def _series_payload(series: dict[str, Any]) -> dict[str, Any]:
    episodes = await _get_store().list_episodes(series["id"])
    has_file = any(e.get("has_file") for e in episodes)
    return {
        "id": series["id"],
        "title": series["title"],
        "originalTitle": series.get("original_title") or series["title"],
        "sortTitle": series["title"],
        "status": "continuing",
        "overview": series.get("overview") or "",
        "year": series.get("year"),
        "seasonCount": max((e["season_number"] for e in episodes), default=1),
        "hasFile": has_file,
        "monitored": series.get("monitored", True),
        "tmdbId": series["tmdb_id"],
        "tvdbId": series.get("tvdb_id") or 0,
        "qualityProfileId": series.get("quality_profile_id", 1),
        "languageProfileId": series.get("language_profile_id", 1),
        "rootFolderPath": series.get("root_folder_path"),
        "path": series.get("path"),
        "added": series.get("added"),
        "seasonFolder": series.get("season_folder", True),
        "statistics": {
            "episodeCount": len(episodes),
            "episodeFileCount": sum(1 for e in episodes if e.get("has_file")),
        },
    }


def _episode_payload(episode: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": episode["id"],
        "seriesId": episode["series_id"],
        "seasonNumber": episode["season_number"],
        "episodeNumber": episode["episode_number"],
        "title": episode.get("title"),
        "hasFile": bool(episode.get("has_file")),
        "monitored": bool(episode.get("monitored", True)),
        "episodeFile": {"path": episode["path"]} if episode.get("path") else None,
    }


def _unique_queries(*values: Any) -> list[str]:
    queries: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    return queries


app.include_router(router)
app.include_router(create_admin_router(_get_store))
