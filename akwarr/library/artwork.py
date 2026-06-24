"""Local artwork paths and download helpers for Jellyfin."""

from __future__ import annotations

from pathlib import Path

from akwarr.core.tmdb import TMDBClient
from akwarr.library import metadata as meta
from akwarr.scraper.akwam import AkwamScraper, is_valid_artwork_url


def series_poster_path(series_folder: Path) -> Path:
    return series_folder / "poster.jpg"


def series_fanart_path(series_folder: Path) -> Path:
    return series_folder / "fanart.jpg"


def series_folder_art_path(series_folder: Path) -> Path:
    return series_folder / "folder.jpg"


def season_poster_path(series_folder: Path, season: int) -> Path:
    return series_folder / f"season{season:02d}-poster.jpg"


def movie_poster_path(folder: Path) -> Path:
    return folder / "poster.jpg"


def movie_fanart_path(folder: Path) -> Path:
    return folder / "fanart.jpg"


def episode_thumb_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}-thumb.jpg")


async def ensure_image(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 500:
        return True
    if not is_valid_artwork_url(url):
        return False
    return await meta.download_image(url, dest)


async def resolve_show_artwork_urls(
    *,
    poster_url: str | None,
    fanart_url: str | None,
    tmdb_id: int | None,
    akwam_url: str | None,
    tmdb: TMDBClient | None = None,
    scraper: AkwamScraper | None = None,
) -> tuple[str | None, str | None]:
    poster = poster_url if is_valid_artwork_url(poster_url) else None
    fanart = fanart_url if is_valid_artwork_url(fanart_url) else None

    if tmdb and tmdb_id and tmdb.enabled and (not poster or not fanart):
        data = await tmdb.tv(tmdb_id)
        poster = poster or TMDBClient.poster_url(data.get("poster_path"))
        fanart = fanart or TMDBClient.poster_url(data.get("backdrop_path"))

    if scraper and akwam_url and (not poster or not fanart):
        meta_data = await scraper.fetch_metadata(akwam_url, kind="series")
        poster = poster or (meta_data.poster if is_valid_artwork_url(meta_data.poster) else None)
        fanart = fanart or (meta_data.fanart if is_valid_artwork_url(meta_data.fanart) else None)

    if not poster and fanart:
        poster = fanart
    if not fanart and poster:
        fanart = poster
    return poster, fanart


async def resolve_movie_artwork_urls(
    *,
    poster_url: str | None,
    fanart_url: str | None,
    tmdb_id: int | None,
    akwam_url: str | None,
    tmdb: TMDBClient | None = None,
    scraper: AkwamScraper | None = None,
) -> tuple[str | None, str | None]:
    poster = poster_url if is_valid_artwork_url(poster_url) else None
    fanart = fanart_url if is_valid_artwork_url(fanart_url) else None

    if tmdb and tmdb_id and tmdb.enabled and (not poster or not fanart):
        data = await tmdb.movie(tmdb_id)
        poster = poster or TMDBClient.poster_url(data.get("poster_path"))
        fanart = fanart or TMDBClient.poster_url(data.get("backdrop_path"))

    if scraper and akwam_url and (not poster or not fanart):
        meta_data = await scraper.fetch_metadata(akwam_url, kind="movie")
        poster = poster or (meta_data.poster if is_valid_artwork_url(meta_data.poster) else None)
        fanart = fanart or (meta_data.fanart if is_valid_artwork_url(meta_data.fanart) else None)

    if not poster and fanart:
        poster = fanart
    if not fanart and poster:
        fanart = poster
    return poster, fanart


async def ensure_series_artwork(
    series_folder: Path,
    *,
    season: int,
    poster_url: str | None,
    fanart_url: str | None,
) -> None:
    poster_path = series_poster_path(series_folder)
    fanart_path = series_fanart_path(series_folder)
    folder_path = series_folder_art_path(series_folder)
    season_path = season_poster_path(series_folder, season)

    if poster_url:
        await ensure_image(poster_url, poster_path)
        if poster_path.exists() and not folder_path.exists():
            folder_path.write_bytes(poster_path.read_bytes())
        await ensure_image(poster_url, season_path)

    if fanart_url:
        await ensure_image(fanart_url, fanart_path)


async def ensure_movie_artwork(
    folder: Path,
    *,
    poster_url: str | None,
    fanart_url: str | None,
) -> None:
    if poster_url:
        await ensure_image(poster_url, movie_poster_path(folder))
    if fanart_url:
        await ensure_image(fanart_url, movie_fanart_path(folder))
