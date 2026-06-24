"""File layout, naming, and import finalization for Jellyfin."""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from akwarr.config import Settings
from akwarr.core.tmdb import TMDBClient
from akwarr.library import artwork as art
from akwarr.library import metadata as meta
from akwarr.library.jellyfin import JellyfinClient
from akwarr.scraper.akwam import AkwamScraper

INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
logger = logging.getLogger(__name__)


@dataclass
class MovieImportPlan:
    folder: Path
    video: Path
    nfo: Path
    poster: Path
    fanart: Path


@dataclass
class EpisodeImportPlan:
    folder: Path
    video: Path
    nfo: Path


class MediaOrganizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jellyfin = JellyfinClient(settings)
        self.tmdb = TMDBClient(settings)
        self.scraper = AkwamScraper(settings)

    def ensure_roots(self) -> None:
        for path in (
            self.settings.movies_path,
            self.settings.series_path,
            self.settings.staging_path,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)

    def staging_dir(self) -> Path:
        path = self.settings.staging_path / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)
        return path

    def movie_plan(
        self,
        *,
        title: str,
        year: int | None,
        quality: str,
        extension: str = ".mkv",
    ) -> MovieImportPlan:
        folder_name = self._movie_folder_name(title, year)
        folder = self.settings.movies_path / folder_name
        base = self._sanitize_filename(f"{title} ({year})" if year else title)
        video_name = f"{base} {quality}{extension}"
        return MovieImportPlan(
            folder=folder,
            video=folder / video_name,
            nfo=folder / "movie.nfo",
            poster=folder / "poster.jpg",
            fanart=folder / "fanart.jpg",
        )

    def series_folder(self, *, title: str, year: int | None) -> Path:
        name = self._series_folder_name(title, year)
        return self.settings.series_path / name

    def episode_plan(
        self,
        *,
        series_title: str,
        year: int | None,
        season: int,
        episode: int,
        episode_title: str | None,
        quality: str,
        extension: str = ".mkv",
    ) -> EpisodeImportPlan:
        show_folder = self.series_folder(title=series_title, year=year)
        season_folder = show_folder / f"Season {season:02d}"
        base = self._sanitize_filename(series_title)
        ep_suffix = f" - {self._sanitize_filename(episode_title)}" if episode_title else ""
        video_name = f"{base} - S{season:02d}E{episode:02d}{ep_suffix} {quality}{extension}"
        return EpisodeImportPlan(
            folder=season_folder,
            video=season_folder / video_name,
            nfo=season_folder / f"S{season:02d}E{episode:02d}.nfo",
        )

    async def finalize_movie(
        self,
        staging_file: Path,
        plan: MovieImportPlan,
        *,
        tmdb_id: int,
        title: str,
        original_title: str | None,
        year: int | None,
        overview: str | None,
        imdb_id: str | None,
        poster_url: str | None,
        fanart_url: str | None,
        akwam_url: str | None = None,
        elcinema_id: str | None = None,
        elcinema_url: str | None = None,
        elcinema_title: str | None = None,
    ) -> Path:
        plan.folder.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)
        self._move_file(staging_file, plan.video)

        poster_file: str | None = None
        fanart_file: str | None = None
        if self.settings.save_akwam_artwork:
            resolved_poster, resolved_fanart = await art.resolve_movie_artwork_urls(
                poster_url=poster_url,
                fanart_url=fanart_url,
                tmdb_id=tmdb_id,
                akwam_url=akwam_url,
                tmdb=self.tmdb,
                scraper=self.scraper,
            )
            await art.ensure_movie_artwork(
                plan.folder,
                poster_url=resolved_poster,
                fanart_url=resolved_fanart,
            )
            if plan.poster.exists():
                poster_file = plan.poster.name
            if plan.fanart.exists():
                fanart_file = plan.fanart.name

        meta.write_movie_nfo(
            plan.nfo,
            title=title,
            original_title=original_title,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            elcinema_id=elcinema_id,
            elcinema_url=elcinema_url,
            elcinema_title=elcinema_title,
            overview=overview,
            language=self.settings.metadata_language,
            poster_file=poster_file,
            fanart_file=fanart_file,
        )
        await self.jellyfin.refresh_path(str(plan.folder))
        return plan.video

    async def finalize_episode(
        self,
        staging_file: Path,
        plan: EpisodeImportPlan,
        *,
        series_folder: Path,
        series_title: str,
        original_title: str | None,
        year: int | None,
        tmdb_id: int,
        overview: str | None,
        season: int,
        episode: int,
        episode_title: str,
        poster_url: str | None,
        fanart_url: str | None,
        akwam_url: str | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        elcinema_id: str | None = None,
        elcinema_url: str | None = None,
        elcinema_title: str | None = None,
    ) -> Path:
        series_folder.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)
        plan.folder.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)
        tvshow_nfo = series_folder / "tvshow.nfo"

        poster_file: str | None = None
        fanart_file: str | None = None
        if self.settings.save_akwam_artwork:
            resolved_poster, resolved_fanart = await art.resolve_show_artwork_urls(
                poster_url=poster_url,
                fanart_url=fanart_url,
                tmdb_id=tmdb_id,
                akwam_url=akwam_url,
                tmdb=self.tmdb,
                scraper=self.scraper,
            )
            await art.ensure_series_artwork(
                series_folder,
                season=season,
                poster_url=resolved_poster,
                fanart_url=resolved_fanart,
            )
            poster_path = art.series_poster_path(series_folder)
            fanart_path = art.series_fanart_path(series_folder)
            if poster_path.exists():
                poster_file = poster_path.name
            if fanart_path.exists():
                fanart_file = fanart_path.name

        if not tvshow_nfo.exists():
            meta.write_tvshow_nfo(
                tvshow_nfo,
                title=series_title,
                original_title=original_title,
                year=year,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                elcinema_id=elcinema_id,
                elcinema_url=elcinema_url,
                elcinema_title=elcinema_title,
                overview=overview,
                language=self.settings.metadata_language,
                poster_file=poster_file,
                fanart_file=fanart_file,
            )
        elif poster_file or fanart_file:
            meta.patch_nfo_art(tvshow_nfo, poster_file=poster_file, fanart_file=fanart_file)

        self._move_file(staging_file, plan.video)
        meta.write_episode_nfo(
            plan.nfo,
            title=episode_title,
            season=season,
            episode=episode,
        )
        await self.jellyfin.refresh_path(str(series_folder))
        await self.jellyfin.refresh_path(str(plan.folder))
        return plan.video

    def _move_file(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=self.settings.dir_mode)
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), str(dest))
        try:
            dest.chmod(self.settings.file_mode)
        except PermissionError:
            logger.warning("Could not chmod imported file %s", dest)

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        cleaned = INVALID_CHARS.sub("", value).strip()
        return cleaned or "media"

    def _movie_folder_name(self, title: str, year: int | None) -> str:
        base = self._sanitize_filename(title)
        return f"{base} ({year})" if year else base

    def _series_folder_name(self, title: str, year: int | None) -> str:
        return self._movie_folder_name(title, year)

    @staticmethod
    def extension_from_url(url: str) -> str:
        lower = url.lower().split("?", 1)[0]
        for ext in (".mkv", ".mp4", ".avi", ".mov", ".wmv"):
            if lower.endswith(ext):
                return ext
        return ".mkv"
