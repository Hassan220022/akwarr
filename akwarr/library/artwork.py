"""Local artwork paths, validation, and download helpers for Jellyfin."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from akwarr.core.tmdb import TMDBClient
from akwarr.library import metadata as meta
from akwarr.library.metadata import MIN_ARTWORK_BYTES
from akwarr.scraper.akwam import AkwamScraper, is_valid_artwork_url

logger = logging.getLogger(__name__)
EPISODE_THUMB_PATTERN = re.compile(r".+-thumb\.jpe?g$", re.IGNORECASE)


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


def md5_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_local_image(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    size = path.stat().st_size
    if size < MIN_ARTWORK_BYTES:
        return False, f"too small ({size}B, min {MIN_ARTWORK_BYTES})"
    head = path.read_bytes()[:200]
    if head.startswith(b"<") or head.startswith(b"<?xml") or b"svg" in head.lower():
        return False, "svg/xml not image"
    if head[:3] == b"\xff\xd8\xff":
        return True, f"jpeg {size}B"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True, f"png {size}B"
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WEBP":
        return True, f"webp {size}B"
    return False, f"unknown format ({size}B)"


def poster_matches_episode_thumb(series_folder: Path, poster_path: Path | None = None) -> tuple[bool, str]:
    poster = poster_path or series_poster_path(series_folder)
    if not poster.is_file():
        return False, ""
    if EPISODE_THUMB_PATTERN.match(poster.name):
        return True, poster.name
    poster_md5 = md5_file(poster)
    if not poster_md5:
        return False, ""
    for thumb in series_folder.rglob("*-thumb.jpg"):
        if md5_file(thumb) == poster_md5:
            return True, thumb.name
    return False, ""


def local_poster_needs_refresh(folder: Path, *, kind: str) -> bool:
    poster = series_poster_path(folder) if kind == "series" else movie_poster_path(folder)
    ok, _ = is_valid_local_image(poster)
    if not ok:
        return True
    if kind == "series":
        is_thumb, _ = poster_matches_episode_thumb(folder, poster)
        if is_thumb:
            return True
    return False


def remove_invalid_local_image(path: Path) -> None:
    if path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not remove invalid artwork %s: %s", path, exc)


async def ensure_image(url: str, dest: Path, *, series_folder: Path | None = None) -> bool:
    if dest.is_file():
        ok, reason = is_valid_local_image(dest)
        if ok:
            if series_folder and dest.name == "poster.jpg":
                is_thumb, thumb_name = poster_matches_episode_thumb(series_folder, dest)
                if is_thumb:
                    logger.warning(
                        "Removing series poster copied from episode thumb %s",
                        thumb_name,
                    )
                    remove_invalid_local_image(dest)
                else:
                    return True
            elif series_folder is None or dest.name != "poster.jpg":
                return True
        else:
            logger.info("Replacing invalid local artwork %s (%s)", dest, reason)
            remove_invalid_local_image(dest)
    if not is_valid_artwork_url(url):
        return False
    downloaded = await meta.download_image(url, dest)
    if not downloaded:
        return False
    ok, reason = is_valid_local_image(dest)
    if not ok:
        logger.warning("Downloaded artwork rejected for %s (%s)", dest, reason)
        remove_invalid_local_image(dest)
        return False
    if series_folder and dest.name == "poster.jpg":
        is_thumb, thumb_name = poster_matches_episode_thumb(series_folder, dest)
        if is_thumb:
            logger.warning("Rejected poster matching episode thumb %s", thumb_name)
            remove_invalid_local_image(dest)
            return False
    return True


async def _tmdb_artwork_urls(
    tmdb: TMDBClient,
    *,
    tmdb_id: int,
    kind: str,
) -> tuple[str | None, str | None]:
    if kind == "series":
        data = await tmdb.tv(tmdb_id)
    else:
        data = await tmdb.movie(tmdb_id)
    return (
        TMDBClient.poster_url(data.get("poster_path")),
        TMDBClient.poster_url(data.get("backdrop_path")),
    )


async def resolve_show_artwork_urls(
    *,
    poster_url: str | None,
    fanart_url: str | None,
    tmdb_id: int | None,
    akwam_url: str | None,
    tmdb: TMDBClient | None = None,
    scraper: AkwamScraper | None = None,
) -> tuple[str | None, str | None]:
    poster: str | None = None
    fanart: str | None = None

    if tmdb and tmdb_id and tmdb.enabled:
        poster, fanart = await _tmdb_artwork_urls(tmdb, tmdb_id=tmdb_id, kind="series")

    if not poster and is_valid_artwork_url(poster_url):
        poster = poster_url
    if not fanart and is_valid_artwork_url(fanart_url):
        fanart = fanart_url

    if scraper and akwam_url and (not poster or not fanart):
        meta_data = await scraper.fetch_metadata(akwam_url, kind="series")
        if not poster and is_valid_artwork_url(meta_data.poster):
            poster = meta_data.poster
        if not fanart and is_valid_artwork_url(meta_data.fanart):
            fanart = meta_data.fanart

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
    poster: str | None = None
    fanart: str | None = None

    if tmdb and tmdb_id and tmdb.enabled:
        poster, fanart = await _tmdb_artwork_urls(tmdb, tmdb_id=tmdb_id, kind="movie")

    if not poster and is_valid_artwork_url(poster_url):
        poster = poster_url
    if not fanart and is_valid_artwork_url(fanart_url):
        fanart = fanart_url

    if scraper and akwam_url and (not poster or not fanart):
        meta_data = await scraper.fetch_metadata(akwam_url, kind="movie")
        if not poster and is_valid_artwork_url(meta_data.poster):
            poster = meta_data.poster
        if not fanart and is_valid_artwork_url(meta_data.fanart):
            fanart = meta_data.fanart

    if not poster and fanart:
        poster = fanart
    if not fanart and poster:
        fanart = poster
    return poster, fanart


def _sync_folder_art_from_poster(series_folder: Path, poster_path: Path) -> None:
    folder_path = series_folder_art_path(series_folder)
    if not poster_path.is_file():
        return
    ok, _ = is_valid_local_image(poster_path)
    if not ok:
        return
    if not folder_path.is_file() or folder_path.read_bytes() != poster_path.read_bytes():
        folder_path.write_bytes(poster_path.read_bytes())


async def ensure_series_artwork(
    series_folder: Path,
    *,
    season: int,
    poster_url: str | None,
    fanart_url: str | None,
) -> None:
    poster_path = series_poster_path(series_folder)
    fanart_path = series_fanart_path(series_folder)
    season_path = season_poster_path(series_folder, season)

    if poster_url:
        await ensure_image(poster_url, poster_path, series_folder=series_folder)
        if poster_path.is_file():
            _sync_folder_art_from_poster(series_folder, poster_path)
        await ensure_image(poster_url, season_path, series_folder=series_folder)

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


async def refresh_series_artwork(
    series_folder: Path,
    *,
    season: int,
    poster_url: str | None,
    fanart_url: str | None,
    tmdb_id: int | None,
    akwam_url: str | None,
    tmdb: TMDBClient | None = None,
    scraper: AkwamScraper | None = None,
) -> bool:
    if local_poster_needs_refresh(series_folder, kind="series"):
        remove_invalid_local_image(series_poster_path(series_folder))
        remove_invalid_local_image(series_folder_art_path(series_folder))
        remove_invalid_local_image(season_poster_path(series_folder, season))
    fanart_path = series_fanart_path(series_folder)
    fanart_ok, _ = is_valid_local_image(fanart_path)
    if not fanart_ok and fanart_path.is_file():
        remove_invalid_local_image(fanart_path)

    resolved_poster, resolved_fanart = await resolve_show_artwork_urls(
        poster_url=poster_url,
        fanart_url=fanart_url,
        tmdb_id=tmdb_id,
        akwam_url=akwam_url,
        tmdb=tmdb,
        scraper=scraper,
    )
    await ensure_series_artwork(
        series_folder,
        season=season,
        poster_url=resolved_poster,
        fanart_url=resolved_fanart,
    )
    poster_ok, _ = is_valid_local_image(series_poster_path(series_folder))
    return poster_ok


async def refresh_movie_artwork(
    folder: Path,
    *,
    poster_url: str | None,
    fanart_url: str | None,
    tmdb_id: int | None,
    akwam_url: str | None,
    tmdb: TMDBClient | None = None,
    scraper: AkwamScraper | None = None,
) -> bool:
    if local_poster_needs_refresh(folder, kind="movie"):
        remove_invalid_local_image(movie_poster_path(folder))
    fanart_path = movie_fanart_path(folder)
    fanart_ok, _ = is_valid_local_image(fanart_path)
    if not fanart_ok and fanart_path.is_file():
        remove_invalid_local_image(fanart_path)

    resolved_poster, resolved_fanart = await resolve_movie_artwork_urls(
        poster_url=poster_url,
        fanart_url=fanart_url,
        tmdb_id=tmdb_id,
        akwam_url=akwam_url,
        tmdb=tmdb,
        scraper=scraper,
    )
    await ensure_movie_artwork(
        folder,
        poster_url=resolved_poster,
        fanart_url=resolved_fanart,
    )
    poster_ok, _ = is_valid_local_image(movie_poster_path(folder))
    return poster_ok
