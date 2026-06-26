"""Background download and import worker."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from akwarr.config import Settings
from akwarr.core.retry import retry_delay_seconds
from akwarr.core.store import JobStatus, Store
from akwarr.core.tmdb import TMDBClient
from akwarr.download.aria2 import Aria2Client
from akwarr.library import artwork as art
from akwarr.library import metadata as meta
from akwarr.library.organizer import MediaOrganizer
from akwarr.scraper.akwam import AkwamScraper

logger = logging.getLogger(__name__)


class DownloadWorker:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.scraper = AkwamScraper(settings)
        self.aria2 = Aria2Client(settings)
        self.organizer = MediaOrganizer(settings)
        self.tmdb = TMDBClient(settings)
        self._running = False
        self._last_release_scan: float | None = None
        self._last_artwork_validate: float | None = None

    async def run_forever(self) -> None:
        self._running = True
        self.organizer.ensure_roots()
        while self._running:
            try:
                await self._process_jobs()
            except Exception:
                logger.exception("Worker loop error")
            await asyncio.sleep(self.settings.worker_poll_seconds)

    def stop(self) -> None:
        self._running = False

    async def _process_jobs(self) -> None:
        await self._requeue_failed_jobs()
        await self._maybe_sync_missing_releases()
        await self._maybe_validate_artwork()
        jobs = await self.store.list_pending_jobs()
        for job in jobs:
            status = job["status"]
            if status == JobStatus.DOWNLOADING:
                await self._check_download(job)
            elif status == JobStatus.IMPORTING:
                await self._import_job(job)
        jobs = await self.store.list_pending_jobs()
        active_count = await self._active_download_count(jobs)
        max_active = max(int(self.settings.max_active_downloads), 1)
        for job in jobs:
            if job["status"] != JobStatus.PENDING:
                continue
            if active_count >= max_active:
                break
            await self._start_job(job)
            active_count += 1

    async def _requeue_failed_jobs(self) -> None:
        """Requeue failed jobs that are eligible for retry (older than retry interval, under max attempts)."""
        retry_after = max(int(self.settings.retry_failed_after_seconds), 60)
        transient_after = max(int(self.settings.retry_transient_after_seconds), 30)
        transient_max = max(int(self.settings.retry_transient_max_seconds), transient_after)
        max_attempts = max(int(self.settings.max_retry_attempts), 1)

        def delay_for(job: dict) -> int:
            return retry_delay_seconds(
                error=job.get("error"),
                retry_count=int(job.get("retry_count") or 0),
                default_after_seconds=retry_after,
                transient_base_seconds=transient_after,
                transient_max_seconds=transient_max,
            )

        try:
            retryable = await self.store.list_retryable_failed_jobs(
                max_attempts=max_attempts,
                delay_seconds_for=delay_for,
            )
        except Exception:
            logger.exception("Failed to list retryable jobs")
            return
        for job in retryable:
            delay = delay_for(job)
            logger.info(
                "Requeuing failed job %s (attempt %s -> %s) after %ss retry interval",
                job["id"],
                job.get("retry_count", 0),
                job.get("retry_count", 0) + 1,
                delay,
            )
            try:
                await self.store.requeue_job(job["id"])
            except Exception:
                logger.exception("Failed to requeue job %s", job["id"])

    async def _maybe_sync_missing_releases(self) -> None:
        interval = max(int(self.settings.monitor_missing_interval_seconds), 300)
        now = time.monotonic()
        last_scan = getattr(self, "_last_release_scan", None)
        if last_scan is not None and (now - last_scan) < interval:
            return
        self._last_release_scan = now
        try:
            if self.settings.is_sonarr:
                await self._sync_missing_series_episodes()
            else:
                await self._sync_missing_movies()
        except Exception:
            logger.exception("Missing release sync failed")

    async def _maybe_validate_artwork(self) -> None:
        if not getattr(self.settings, "save_akwam_artwork", True):
            return
        interval = max(int(getattr(self.settings, "artwork_validate_interval_seconds", 604800)), 3600)
        now = time.monotonic()
        last = getattr(self, "_last_artwork_validate", None)
        if last is not None and (now - last) < interval:
            return
        self._last_artwork_validate = now
        try:
            if self.settings.is_sonarr:
                await self._validate_series_artwork()
            else:
                await self._validate_movie_artwork()
        except Exception:
            logger.exception("Periodic artwork validation failed")

    async def _validate_series_artwork(self) -> None:
        refreshed = 0
        for series in await self.store.list_series():
            if not series.get("monitored"):
                continue
            folder = self._series_folder(series)
            if not folder.is_dir():
                continue
            if not art.local_poster_needs_refresh(folder, kind="series"):
                continue
            ok = await art.refresh_series_artwork(
                folder,
                season=1,
                poster_url=series.get("poster_url"),
                fanart_url=series.get("fanart_url"),
                tmdb_id=series.get("tmdb_id"),
                akwam_url=series.get("akwam_url"),
                tmdb=self.tmdb,
                scraper=self.scraper,
            )
            if ok:
                tvshow_nfo = folder / "tvshow.nfo"
                if tvshow_nfo.exists():
                    meta.patch_nfo_art(
                        tvshow_nfo,
                        poster_file="poster.jpg",
                        fanart_file="fanart.jpg" if art.series_fanart_path(folder).is_file() else None,
                    )
                await self.organizer.jellyfin.refresh_path(str(folder))
                refreshed += 1
        if refreshed:
            logger.info("Periodic artwork validation refreshed %s series", refreshed)

    async def _validate_movie_artwork(self) -> None:
        refreshed = 0
        for movie in await self.store.list_movies():
            if not movie.get("monitored") or not movie.get("has_file"):
                continue
            folder = self._movie_folder(movie)
            if not folder.is_dir():
                continue
            if not art.local_poster_needs_refresh(folder, kind="movie"):
                continue
            ok = await art.refresh_movie_artwork(
                folder,
                poster_url=movie.get("poster_url"),
                fanart_url=movie.get("fanart_url"),
                tmdb_id=movie.get("tmdb_id"),
                akwam_url=movie.get("akwam_url"),
                tmdb=self.tmdb,
                scraper=self.scraper,
            )
            if ok:
                nfo = folder / "movie.nfo"
                if nfo.exists():
                    meta.patch_nfo_art(
                        nfo,
                        poster_file="poster.jpg",
                        fanart_file="fanart.jpg" if art.movie_fanart_path(folder).is_file() else None,
                    )
                await self.organizer.jellyfin.refresh_path(str(folder))
                refreshed += 1
        if refreshed:
            logger.info("Periodic artwork validation refreshed %s movies", refreshed)

    def _series_folder(self, series: dict) -> Path:
        if series.get("path"):
            return Path(series["path"])
        return self.organizer.series_folder(title=series["title"], year=series.get("year"))

    def _movie_folder(self, movie: dict) -> Path:
        if movie.get("path"):
            return Path(movie["path"]).parent
        return self.organizer.movie_plan(
            title=movie["title"],
            year=movie.get("year"),
            quality="720p",
        ).folder

    async def _sync_missing_series_episodes(self) -> None:
        for series in await self.store.list_series():
            if not series.get("monitored") or not series.get("akwam_url"):
                continue
            try:
                meta = await self.scraper.fetch_metadata(series["akwam_url"], kind="series")
            except Exception:
                logger.exception("Series metadata refresh failed for %s", series.get("title"))
                continue
            episodes = await self.store.list_episodes(series["id"])
            by_key = {(e["season_number"], e["episode_number"]): e for e in episodes}
            for ep in meta.episodes:
                season = ep.season or 1
                record = by_key.get((season, ep.number))
                if record and record.get("has_file"):
                    continue
                ep_record = await self.store.upsert_episode(
                    {
                        "series_id": series["id"],
                        "season_number": season,
                        "episode_number": ep.number,
                        "title": ep.title,
                        "akwam_url": ep.url,
                        "monitored": True,
                        "has_file": False,
                    }
                )
                if await self.store.has_blocking_job("episode", ep_record["id"]):
                    continue
                plan = self.organizer.episode_plan(
                    series_title=series["title"],
                    year=series.get("year"),
                    season=season,
                    episode=ep.number,
                    episode_title=ep.title,
                    quality="720p",
                )
                job_id = await self.store.create_job("episode", ep_record["id"], str(plan.video))
                logger.info(
                    "Queued missing episode S%02dE%02d for %s (job %s)",
                    season,
                    ep.number,
                    series["title"],
                    job_id,
                )

    async def _sync_missing_movies(self) -> None:
        for movie in await self.store.list_movies():
            if not movie.get("monitored") or movie.get("has_file"):
                continue
            if await self.store.has_blocking_job("movie", movie["id"]):
                continue
            akwam_url = movie.get("akwam_url")
            if not akwam_url:
                alt = movie.get("original_title") or ""
                match = await self.scraper.best_match(
                    movie["title"], section="movie", alt_queries=[alt] if alt else []
                )
                if not match:
                    continue
                akwam_url = match.url
                await self.store.update_movie_akwam(movie["id"], akwam_url, match.poster, None)
            plan = self.organizer.movie_plan(
                title=movie["title"], year=movie.get("year"), quality="720p"
            )
            job_id = await self.store.create_job("movie", movie["id"], str(plan.video))
            logger.info("Queued missing movie %s (job %s)", movie["title"], job_id)

    async def _start_job(self, job: dict) -> None:
        job_id = job["id"]
        kind = job["kind"]
        ref_id = job["ref_id"]

        try:
            if kind == "episode" and self.settings.is_radarr:
                await self.store.update_job(
                    job_id,
                    status=JobStatus.FAILED,
                    error="episode jobs are not supported on the radarr shim",
                )
                return
            if kind == "movie":
                movie = await self.store.get_movie(ref_id)
                if not movie:
                    await self.store.update_job(job_id, status=JobStatus.FAILED, error="movie not found")
                    return
                if not movie.get("akwam_url"):
                    await self.store.update_job(job_id, status=JobStatus.FAILED, error="no akwam url")
                    return
                meta = await self.scraper.fetch_metadata(movie["akwam_url"], kind="movie")
                quality, link = await self.scraper.pick_download(meta)
                direct = await self.scraper.resolve_direct_url(link)
                staging = self.organizer.staging_dir()
                ext = MediaOrganizer.extension_from_url(direct)
                filename = Aria2Client.safe_filename(f"movie-{movie['tmdb_id']}{ext}")
                gid = await self.aria2.add_uri(direct, str(staging), filename)
                await self.store.update_job(
                    job_id,
                    status=JobStatus.DOWNLOADING,
                    aria2_gid=gid,
                    staging_path=str(staging / filename),
                    error="",
                )
            elif kind == "episode":
                episode = await self.store.get_episode(ref_id)
                if not episode or not episode.get("akwam_url"):
                    await self.store.update_job(job_id, status=JobStatus.FAILED, error="episode not found")
                    return
                quality, direct = await self.scraper.episode_download_url(episode["akwam_url"])
                staging = self.organizer.staging_dir()
                ext = MediaOrganizer.extension_from_url(direct)
                filename = Aria2Client.safe_filename(
                    f"s{episode['season_number']}e{episode['episode_number']}{ext}"
                )
                gid = await self.aria2.add_uri(direct, str(staging), filename)
                await self.store.update_job(
                    job_id,
                    status=JobStatus.DOWNLOADING,
                    aria2_gid=gid,
                    staging_path=str(staging / filename),
                    error="",
                )
            else:
                await self.store.update_job(job_id, status=JobStatus.FAILED, error=f"unknown kind {kind}")
        except Exception as exc:
            logger.exception("Failed to start job %s", job_id)
            await self.store.update_job(job_id, status=JobStatus.FAILED, error=str(exc))

    async def _check_download(self, job: dict) -> None:
        gid = job.get("aria2_gid")
        if not gid:
            await self.store.update_job(job["id"], status=JobStatus.FAILED, error="missing gid")
            return
        try:
            status = await self.aria2.tell_status(gid)
            state = status.get("status")
            if state == "waiting" and self._is_stale_waiting(job, status):
                await self._release_stalled_download(
                    job,
                    reason="stalled in aria2 waiting queue with no metadata",
                )
                return
            actual_path = self._aria2_file_path(status)
            staging_path = str(job.get("staging_path") or "")
            if actual_path and self._is_legacy_staging_path(actual_path):
                await self.aria2.remove(gid)
                await self.store.update_job(
                    job["id"],
                    status=JobStatus.PENDING,
                    staging_path=str(self._legacy_requeue_path(job, actual_path)),
                    error="requeued legacy staging download",
                )
                return
            if actual_path and actual_path != staging_path:
                await self.store.update_job(job["id"], staging_path=actual_path)
            if state == "complete":
                await self.store.update_job(job["id"], status=JobStatus.IMPORTING, staging_path=actual_path or None)
            elif state == "error":
                await self.store.update_job(
                    job["id"],
                    status=JobStatus.FAILED,
                    error=status.get("errorMessage", "aria2 error"),
                )
        except Exception as exc:
            if self._is_orphan_gid_error(exc):
                await self._release_stalled_download(job, reason=str(exc))
                return
            await self.store.update_job(job["id"], status=JobStatus.FAILED, error=str(exc))

    async def _active_download_count(self, jobs: list[dict]) -> int:
        count = 0
        for job in jobs:
            status = job.get("status")
            if status == JobStatus.IMPORTING:
                count += 1
                continue
            if status != JobStatus.DOWNLOADING:
                continue
            gid = job.get("aria2_gid")
            if not gid:
                count += 1
                continue
            try:
                aria2_status = await self.aria2.tell_status(str(gid))
            except Exception as exc:
                if self._is_orphan_gid_error(exc):
                    continue
                count += 1
                continue
            state = aria2_status.get("status")
            if state in {"active", "paused"}:
                count += 1
        return count

    async def _release_stalled_download(self, job: dict, *, reason: str) -> None:
        gid = job.get("aria2_gid")
        if gid:
            try:
                await self.aria2.force_remove(str(gid))
            except Exception:
                logger.warning("Failed to remove aria2 gid %s for job %s", gid, job["id"])

        # Enforce the retry cap on the stalled-waiting path too: requeue_job
        # bumps retry_count unconditionally, and stalled downloads never pass
        # through status=failed (so list_retryable_failed_jobs' max_attempts
        # guard never sees them). Without this cap a download that can never
        # reach aria2 metadata spins forever, retrying every stale_waiting
        # interval (observed retry_count > 100 in production).
        max_attempts = max(int(self.settings.max_retry_attempts), 1)
        retry_count = int(job.get("retry_count") or 0)
        if retry_count >= max_attempts:
            logger.warning(
                "Marking job %s failed after %s stalled requeues: %s",
                job["id"],
                retry_count,
                reason,
            )
            await self.store.update_job(
                job["id"],
                status=JobStatus.FAILED,
                error=f"stalled download exhausted {retry_count} retries: {reason}",
            )
            return

        logger.info("Requeuing stalled job %s: %s", job["id"], reason)
        await self.store.requeue_job(job["id"])

    @staticmethod
    def _is_orphan_gid_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "is not found" in message or "not found" in message

    def _is_stale_waiting(self, job: dict, status: dict) -> bool:
        if status.get("status") != "waiting":
            return False
        if self._aria2_int(status.get("totalLength")) > 0:
            return False
        if self._aria2_int(status.get("completedLength")) > 0:
            return False
        stale_after = max(int(self.settings.stale_waiting_seconds), 300)
        since = job.get("updated") or job.get("created")
        if not since:
            return False
        parsed = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()
        return age >= stale_after

    @staticmethod
    def _aria2_int(value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _aria2_file_path(status: dict) -> str | None:
        files = status.get("files") or []
        if not files:
            return None
        path = files[0].get("path")
        return str(path) if path else None

    @staticmethod
    def _is_legacy_staging_path(path: str) -> bool:
        return path.startswith("/media/arabic/.staging/")

    def _legacy_requeue_path(self, job: dict, path: str) -> Path:
        filename = Path(path).name or "download.mkv"
        return self.settings.staging_path / "requeued" / f"job-{job['id']}" / filename

    async def _import_job(self, job: dict) -> None:
        staging_path = job.get("staging_path")
        if not staging_path or not Path(staging_path).exists():
            await self.store.update_job(job["id"], status=JobStatus.FAILED, error="staging file missing")
            return

        try:
            if job["kind"] == "movie":
                await self._import_movie(job, Path(staging_path))
            elif job["kind"] == "episode":
                await self._import_episode(job, Path(staging_path))
            await self._post_import_artwork(job)
            await self.store.update_job(job["id"], status=JobStatus.COMPLETED)
        except Exception as exc:
            logger.exception("Import failed for job %s", job["id"])
            await self.store.update_job(job["id"], status=JobStatus.FAILED, error=str(exc))

    async def _import_movie(self, job: dict, staging_file: Path) -> None:
        movie = await self.store.get_movie(job["ref_id"])
        if not movie:
            raise RuntimeError("movie missing")
        quality = "720p"
        plan = self.organizer.movie_plan(
            title=movie["title"],
            year=movie.get("year"),
            quality=quality,
            extension=staging_file.suffix or ".mkv",
        )
        final = await self.organizer.finalize_movie(
            staging_file,
            plan,
            tmdb_id=movie["tmdb_id"],
            title=movie["title"],
            original_title=movie.get("original_title"),
            year=movie.get("year"),
            overview=movie.get("overview"),
            imdb_id=_metadata_value(movie, "imdb_id"),
            poster_url=movie.get("poster_url"),
            fanart_url=movie.get("fanart_url"),
            elcinema_id=_elcinema_metadata(movie, "id"),
            elcinema_url=_elcinema_metadata(movie, "url"),
            elcinema_title=_elcinema_metadata(movie, "title"),
            akwam_url=movie.get("akwam_url"),
        )
        await self.store.set_movie_file(movie["id"], str(final), has_file=True)

    async def _import_episode(self, job: dict, staging_file: Path) -> None:
        episode = await self.store.get_episode(job["ref_id"])
        if not episode:
            raise RuntimeError("episode missing")
        series = await self.store.get_series(episode["series_id"])
        if not series:
            raise RuntimeError("series missing")

        quality = "720p"
        plan = self.organizer.episode_plan(
            series_title=series["title"],
            year=series.get("year"),
            season=episode["season_number"],
            episode=episode["episode_number"],
            episode_title=episode.get("title"),
            quality=quality,
            extension=staging_file.suffix or ".mkv",
        )
        show_folder = self.organizer.series_folder(title=series["title"], year=series.get("year"))
        final = await self.organizer.finalize_episode(
            staging_file,
            plan,
            series_folder=show_folder,
            series_title=series["title"],
            original_title=series.get("original_title"),
            year=series.get("year"),
            tmdb_id=series["tmdb_id"],
            overview=series.get("overview"),
            season=episode["season_number"],
            episode=episode["episode_number"],
            episode_title=episode.get("title") or f"Episode {episode['episode_number']}",
            poster_url=series.get("poster_url"),
            fanart_url=series.get("fanart_url"),
            imdb_id=_metadata_value(series, "imdb_id"),
            tvdb_id=series.get("tvdb_id"),
            elcinema_id=_elcinema_metadata(series, "id"),
            elcinema_url=_elcinema_metadata(series, "url"),
            elcinema_title=_elcinema_metadata(series, "title"),
            akwam_url=series.get("akwam_url"),
        )
        await self.store.set_episode_file(episode["id"], str(final), has_file=True)
        if not series.get("path"):
            await self.store.set_series_path(series["id"], str(show_folder))

    async def _post_import_artwork(self, job: dict) -> None:
        if not self.settings.save_akwam_artwork:
            return
        try:
            if job["kind"] == "movie":
                movie = await self.store.get_movie(job["ref_id"])
                if not movie:
                    return
                folder = self._movie_folder(movie)
                if not folder.is_dir():
                    return
                await art.refresh_movie_artwork(
                    folder,
                    poster_url=movie.get("poster_url"),
                    fanart_url=movie.get("fanart_url"),
                    tmdb_id=movie.get("tmdb_id"),
                    akwam_url=movie.get("akwam_url"),
                    tmdb=self.tmdb,
                    scraper=self.scraper,
                )
                nfo = folder / "movie.nfo"
                if nfo.exists():
                    meta.patch_nfo_art(nfo, poster_file="poster.jpg", fanart_file="fanart.jpg")
                await self.organizer.jellyfin.refresh_path(str(folder))
            elif job["kind"] == "episode":
                episode = await self.store.get_episode(job["ref_id"])
                if not episode:
                    return
                series = await self.store.get_series(episode["series_id"])
                if not series:
                    return
                folder = self._series_folder(series)
                if not folder.is_dir():
                    return
                await art.refresh_series_artwork(
                    folder,
                    season=episode["season_number"],
                    poster_url=series.get("poster_url"),
                    fanart_url=series.get("fanart_url"),
                    tmdb_id=series.get("tmdb_id"),
                    akwam_url=series.get("akwam_url"),
                    tmdb=self.tmdb,
                    scraper=self.scraper,
                )
                tvshow_nfo = folder / "tvshow.nfo"
                if tvshow_nfo.exists():
                    meta.patch_nfo_art(tvshow_nfo, poster_file="poster.jpg", fanart_file="fanart.jpg")
                await self.organizer.jellyfin.refresh_path(str(folder))
        except Exception:
            logger.exception("Post-import artwork refresh failed for job %s", job.get("id"))


def _metadata_value(item: dict, key: str) -> str | None:
    metadata = item.get("metadata") or {}
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return str(value) if value else None


def _elcinema_metadata(item: dict, key: str) -> str | None:
    metadata = item.get("metadata") or {}
    elcinema = metadata.get("elcinema") if isinstance(metadata, dict) else None
    value = elcinema.get(key) if isinstance(elcinema, dict) else None
    return str(value) if value else None
