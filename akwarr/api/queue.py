"""Radarr/Sonarr queue payloads backed by Akwarr jobs and aria2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from akwarr.core.store import JobStatus, Store
from akwarr.download.aria2 import Aria2Client


async def queue_payload(
    store: Store,
    aria2: Aria2Client,
    *,
    kind: str,
    page: int = 1,
    page_size: int = 20,
    sort_key: str = "timeleft",
    sort_direction: str = "ascending",
) -> dict[str, Any]:
    jobs = [job for job in await store.list_pending_jobs() if job.get("kind") == kind]
    records = [record for job in jobs if (record := await _queue_record(store, aria2, job))]
    total = len(records)
    if page_size > 0:
        start = max(page - 1, 0) * page_size
        records = records[start : start + page_size]
    return {
        "page": page,
        "pageSize": page_size,
        "sortKey": sort_key,
        "sortDirection": sort_direction,
        "totalRecords": total,
        "records": records,
    }


async def _queue_record(store: Store, aria2: Aria2Client, job: dict[str, Any]) -> dict[str, Any] | None:
    media = await _media_for_job(store, job)
    if not media:
        return None

    status = {}
    if job.get("aria2_gid"):
        try:
            status = await aria2.tell_status(job["aria2_gid"])
        except Exception:
            status = {}

    progress = _download_progress(status, fallback_status=str(job.get("status") or "queued"))
    title = _queue_title(media, job)
    record: dict[str, Any] = {
        "id": job["id"],
        "downloadId": job.get("aria2_gid") or f"akwarr-{job['id']}",
        "title": title,
        "status": progress["status"],
        "trackedDownloadState": progress["trackedDownloadState"],
        "trackedDownloadStatus": "ok" if not job.get("error") else "warning",
        "statusMessages": [{"title": job["error"], "messages": [job["error"]]}] if job.get("error") else [],
        "size": progress["size"],
        "sizeleft": progress["sizeleft"],
        "timeleft": progress["timeleft"],
        "estimatedCompletionTime": progress["estimatedCompletionTime"],
        "protocol": "http",
        "downloadClient": "aria2",
        "outputPath": job.get("staging_path") or job.get("dest_path"),
        "errorMessage": job.get("error") or None,
    }
    record.update(media)
    return record


async def _media_for_job(store: Store, job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get("kind") == "movie":
        movie = await store.get_movie(job["ref_id"])
        if not movie:
            return None
        return {
            "movieId": movie["id"],
            "tmdbId": movie.get("tmdb_id"),
            "movie": {
                "id": movie["id"],
                "title": movie["title"],
                "year": movie.get("year"),
                "tmdbId": movie.get("tmdb_id"),
            },
        }

    if job.get("kind") == "episode":
        episode = await store.get_episode(job["ref_id"])
        if not episode:
            return None
        series = await store.get_series(episode["series_id"])
        if not series:
            return None
        return {
            "seriesId": series["id"],
            "episodeId": episode["id"],
            "seasonNumber": episode["season_number"],
            "episodeNumber": episode["episode_number"],
            "series": {
                "id": series["id"],
                "title": series["title"],
                "year": series.get("year"),
                "tmdbId": series.get("tmdb_id"),
                "tvdbId": series.get("tvdb_id") or 0,
            },
            "episode": {
                "id": episode["id"],
                "seriesId": series["id"],
                "seasonNumber": episode["season_number"],
                "episodeNumber": episode["episode_number"],
                "title": episode.get("title"),
            },
        }
    return None


def _queue_title(media: dict[str, Any], job: dict[str, Any]) -> str:
    if job.get("kind") == "movie":
        movie = media["movie"]
        year = f" ({movie['year']})" if movie.get("year") else ""
        return f"{movie['title']}{year}"
    series = media["series"]
    episode = media["episode"]
    return f"{series['title']} - S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d}"


def _download_progress(status: dict[str, Any], *, fallback_status: str) -> dict[str, Any]:
    total = _int(status.get("totalLength"))
    completed = _int(status.get("completedLength"))
    speed = _int(status.get("downloadSpeed"))
    sizeleft = max(total - completed, 0) if total else 0
    eta_seconds = int(sizeleft / speed) if speed > 0 and sizeleft > 0 else None
    state = str(status.get("status") or fallback_status)
    if fallback_status == JobStatus.IMPORTING:
        state = "importing"
        sizeleft = 0
        eta_seconds = 0
    return {
        "status": _servarr_status(state),
        "trackedDownloadState": _tracked_state(state),
        "size": total,
        "sizeleft": sizeleft,
        "timeleft": _format_timeleft(eta_seconds),
        "estimatedCompletionTime": _estimated_completion_time(eta_seconds),
    }


def _servarr_status(status: str) -> str:
    if status in {"active", JobStatus.DOWNLOADING}:
        return "downloading"
    if status == JobStatus.IMPORTING:
        return "importing"
    if status == "complete":
        return "completed"
    if status == "error":
        return "failed"
    return "queued"


def _tracked_state(status: str) -> str:
    if status in {"active", JobStatus.DOWNLOADING}:
        return "downloading"
    if status == JobStatus.IMPORTING:
        return "importing"
    return "queued"


def _format_timeleft(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _estimated_completion_time(seconds: int | None) -> str:
    eta = max(seconds or 0, 0)
    return (_utcnow() + timedelta(seconds=eta)).isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
