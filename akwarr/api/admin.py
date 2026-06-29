"""Admin monitoring UI and Akwam diagnostics."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from akwarr.api.auth import verify_api_key
from akwarr.config import get_settings
from akwarr.core.quality import quality_for_profile_id
from akwarr.core.store import JobStatus, Store
from akwarr.download.aria2 import Aria2Client
from akwarr.library.organizer import MediaOrganizer
from akwarr.scraper.akwam import AkwamMetadata, AkwamScraper
from akwarr.scraper.elcinema import ElCinemaScraper

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".wmv"}
SIDE_CAR_SUFFIXES = {".nfo", ".jpg", ".jpeg", ".png", ".webp"}


class AkwamEpisodeDownloadSelection(BaseModel):
    season: int | None = Field(default=None, ge=0)
    number: int = Field(ge=1)
    title: str | None = None
    url: str = Field(min_length=1)


class AkwamSeriesDownloadBody(BaseModel):
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    year: int | None = None
    episodes: list[AkwamEpisodeDownloadSelection] = Field(min_length=1)


def create_admin_router(get_store: Callable[[], Store]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_api_key)])

    @router.get("/ui", response_class=HTMLResponse)
    async def admin_ui() -> HTMLResponse:
        return HTMLResponse(ADMIN_HTML)

    @router.get("/api/v3/monitor/files")
    async def monitor_files(limit: int = Query(default=80, ge=1, le=500)) -> dict[str, Any]:
        settings = get_settings()
        return {
            "moviesRoot": str(settings.movies_path),
            "seriesRoot": str(settings.series_path),
            "movies": _media_files(settings.movies_path, limit=limit),
            "series": _media_files(settings.series_path, limit=limit),
        }

    @router.get("/api/v3/monitor/jobs")
    async def monitor_jobs(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        settings = get_settings()
        jobs = await get_store().list_jobs(limit=limit)
        jobs = await _with_download_progress(jobs, Aria2Client(settings))
        jobs = _tag_jobs(jobs, settings.mode)
        peer_error = None
        if settings.peer_monitor_url:
            try:
                jobs.extend(await _peer_monitor_jobs(settings.peer_monitor_url, settings.api_key, limit=limit))
            except Exception as exc:
                peer_error = str(exc)
        jobs.sort(key=lambda job: str(job.get("created") or ""), reverse=True)
        jobs = jobs[:limit]
        return {
            "total": len(jobs),
            "jobs": jobs,
            "counts": _job_counts(jobs),
            "peerError": peer_error,
        }

    @router.post("/api/v3/monitor/jobs/{job_ref}/pause")
    async def pause_job(job_ref: str) -> dict[str, Any]:
        settings = get_settings()
        job_id = _local_job_id_or_proxy(job_ref, settings, "pause")
        if job_id is None:
            return await _proxy_peer_job_command(settings, job_ref, "pause", "POST")
        job = await _get_job_or_404(get_store(), job_id)
        gid = _job_gid_or_400(job)
        await Aria2Client(settings).pause(gid)
        await get_store().update_job(job_id, status=JobStatus.PAUSED, error="")
        return {"id": job_id, "controlId": _control_id(settings.mode, job_id), "status": JobStatus.PAUSED}

    @router.post("/api/v3/monitor/jobs/{job_ref}/resume")
    async def resume_job(job_ref: str) -> dict[str, Any]:
        settings = get_settings()
        job_id = _local_job_id_or_proxy(job_ref, settings, "resume")
        if job_id is None:
            return await _proxy_peer_job_command(settings, job_ref, "resume", "POST")
        job = await _get_job_or_404(get_store(), job_id)
        gid = _job_gid_or_400(job)
        await Aria2Client(settings).unpause(gid)
        await get_store().update_job(job_id, status=JobStatus.DOWNLOADING, error="")
        return {"id": job_id, "controlId": _control_id(settings.mode, job_id), "status": JobStatus.DOWNLOADING}

    @router.delete("/api/v3/monitor/jobs/{job_ref}")
    async def delete_job(job_ref: str) -> dict[str, Any]:
        settings = get_settings()
        job_id = _local_job_id_or_proxy(job_ref, settings, "delete")
        if job_id is None:
            return await _proxy_peer_job_command(settings, job_ref, "", "DELETE")
        job = await _get_job_or_404(get_store(), job_id)
        gid = str(job.get("aria2_gid") or "")
        if gid:
            await Aria2Client(settings).force_remove(gid)
        await get_store().update_job(job_id, status=JobStatus.DELETED, error="deleted from monitor")
        return {"id": job_id, "controlId": _control_id(settings.mode, job_id), "status": JobStatus.DELETED}

    @router.get("/api/v3/akwam/search")
    async def akwam_search(
        term: str = Query(..., min_length=1),
        section: str = Query(default="movie", pattern="^(movie|series)$"),
    ) -> dict[str, Any]:
        scraper = AkwamScraper(get_settings())
        results = await scraper.search(term, section=section)
        return {
            "term": term,
            "section": section,
            "count": len(results),
            "results": [item.__dict__ for item in results],
        }

    @router.get("/api/v3/elcinema/search")
    async def elcinema_search(
        term: str = Query(..., min_length=1),
        kind: str = Query(default="movie", pattern="^(movie|series)$"),
        year: int | None = Query(default=None),
    ) -> dict[str, Any]:
        scraper = ElCinemaScraper(get_settings())
        results = await scraper.search(term, kind=kind)
        candidates = await scraper.arabic_candidates(term, year=year, kind=kind)
        return {
            "term": term,
            "kind": kind,
            "year": year,
            "count": len(results),
            "candidates": candidates,
            "results": [_result_payload(item) for item in results],
        }

    @router.get("/api/v3/akwam/metadata")
    async def akwam_metadata(
        url: str = Query(..., min_length=1),
        kind: str = Query(default="movie", pattern="^(movie|series)$"),
    ) -> dict[str, Any]:
        settings = get_settings()
        _validate_akwam_url(url, settings.akwam_base)
        scraper = AkwamScraper(settings)
        metadata = await scraper.fetch_metadata(url, kind=kind)
        return _metadata_payload(metadata)

    @router.post("/api/v3/akwam/series/download")
    async def akwam_series_download(body: AkwamSeriesDownloadBody) -> dict[str, Any]:
        settings = get_settings()
        if not settings.is_sonarr:
            raise HTTPException(
                status_code=400,
                detail="Series episode downloads must be queued on the sonarr shim",
            )
        _validate_akwam_url(body.url, settings.akwam_base)
        for episode in body.episodes:
            _validate_akwam_url(episode.url, settings.akwam_base)

        store = get_store()
        organizer = MediaOrganizer(settings)
        series = await store.add_series(
            {
                "tmdb_id": _synthetic_tmdb_id(body.url),
                "tvdb_id": 0,
                "title": body.title,
                "original_title": body.title,
                "year": body.year,
                "overview": None,
                "poster_url": None,
                "fanart_url": None,
                "akwam_url": body.url,
                "path": str(organizer.series_folder(title=body.title, year=body.year)),
                "monitored": True,
                "season_folder": True,
                "quality_profile_id": 1,
                "language_profile_id": 1,
                "root_folder_path": str(settings.series_path),
                "metadata": {"source": "akwam", "source_url": body.url},
            }
        )

        queued: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for episode in body.episodes:
            season = episode.season or 1
            key = (season, episode.number)
            if key in seen:
                continue
            seen.add(key)
            ep_record = await store.upsert_episode(
                {
                    "series_id": series["id"],
                    "season_number": season,
                    "episode_number": episode.number,
                    "title": episode.title,
                    "akwam_url": episode.url,
                    "monitored": True,
                    "has_file": False,
                }
            )
            if ep_record.get("has_file"):
                continue
            quality = quality_for_profile_id(series.get("quality_profile_id", 1), settings)
            plan = organizer.episode_plan(
                series_title=body.title,
                year=body.year,
                season=season,
                episode=episode.number,
                episode_title=episode.title,
                quality=quality,
            )
            job_id = await store.create_job("episode", ep_record["id"], str(plan.video), quality=quality)
            queued.append(
                {
                    "jobId": job_id,
                    "episodeId": ep_record["id"],
                    "season": season,
                    "number": episode.number,
                    "title": episode.title,
                    "destination": str(plan.video),
                }
            )

        return {
            "seriesId": series["id"],
            "title": series["title"],
            "requested": len(body.episodes),
            "queued": len(queued),
            "episodes": queued,
        }

    @router.get("/api/v3/akwam/resolve")
    async def akwam_resolve(linkUrl: str = Query(..., min_length=1)) -> dict[str, str]:
        settings = get_settings()
        _validate_akwam_url(linkUrl, settings.akwam_base)
        scraper = AkwamScraper(settings)
        direct_url = await scraper.resolve_direct_url(linkUrl)
        return {"directUrl": direct_url}

    return router


def _tag_jobs(jobs: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for job in jobs:
        item = dict(job)
        item["source"] = source
        item["controlId"] = _control_id(source, item["id"])
        tagged.append(item)
    return tagged


def _control_id(source: str, job_id: int | str) -> str:
    return f"{source}:{job_id}"


async def _peer_monitor_jobs(peer_url: str, api_key: str, *, limit: int) -> list[dict[str, Any]]:
    url = f"{peer_url.rstrip('/')}/api/v3/monitor/jobs"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params={"limit": limit}, headers={"X-Api-Key": api_key})
        response.raise_for_status()
    data = response.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return [dict(job) for job in jobs if isinstance(job, dict)]


def _local_job_id_or_proxy(job_ref: str, settings: Any, action: str) -> int | None:
    source, job_id = _split_job_ref(job_ref)
    if source and source != settings.mode:
        if not settings.peer_monitor_url:
            raise HTTPException(status_code=404, detail=f"No peer monitor configured for {source} job {action}")
        return None
    return job_id


def _split_job_ref(job_ref: str) -> tuple[str, int]:
    if ":" in job_ref:
        source, raw_id = job_ref.split(":", 1)
    else:
        source, raw_id = "", job_ref
    try:
        return source, int(raw_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid job id") from exc


async def _proxy_peer_job_command(settings: Any, job_ref: str, action: str, method: str) -> dict[str, Any]:
    source, job_id = _split_job_ref(job_ref)
    suffix = f"/{action}" if action else ""
    url = f"{settings.peer_monitor_url.rstrip('/')}/api/v3/monitor/jobs/{job_id}{suffix}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, url, headers={"X-Api-Key": settings.api_key})
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    data = response.json()
    data["controlId"] = _control_id(source, job_id)
    return data


def _media_files(root: Path, *, limit: int) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    suffixes = VIDEO_SUFFIXES | SIDE_CAR_SUFFIXES
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "relativePath": str(path.relative_to(root)),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "kind": "video" if path.suffix.lower() in VIDEO_SUFFIXES else "metadata",
            }
        )
    files.sort(key=lambda item: item["modified"], reverse=True)
    return files[:limit]


def _job_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


async def _get_job_or_404(store: Store, job_id: int) -> dict[str, Any]:
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_gid_or_400(job: dict[str, Any]) -> str:
    gid = str(job.get("aria2_gid") or "")
    if not gid:
        raise HTTPException(status_code=400, detail="Job has no aria2 download id")
    return gid


async def _with_download_progress(jobs: list[dict[str, Any]], aria2: Aria2Client) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for job in jobs:
        item = dict(job)
        if item.get("status") in {"downloading", "paused"} and item.get("aria2_gid"):
            try:
                status = await aria2.tell_status(str(item["aria2_gid"]))
                item.update(_download_progress(status))
            except Exception as exc:
                item["downloadError"] = str(exc)
        enriched.append(item)
    return enriched


def _download_progress(status: dict[str, Any]) -> dict[str, Any]:
    total = _to_int(status.get("totalLength"))
    completed = _to_int(status.get("completedLength"))
    speed = _to_int(status.get("downloadSpeed"))
    remaining = max(total - completed, 0) if total > 0 else 0
    percent = round((completed / total) * 100, 2) if total > 0 else None
    eta_seconds = int(remaining / speed) if speed > 0 and remaining > 0 else None
    return {
        "aria2Status": status.get("status"),
        "progressBytes": completed,
        "totalBytes": total,
        "downloadSpeed": speed,
        "progressPercent": percent,
        "etaSeconds": eta_seconds,
    }


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_akwam_url(url: str, base_url: str) -> None:
    parsed = urlparse(url)
    base = urlparse(base_url)
    host = parsed.hostname or ""
    base_host = base.hostname or ""
    allowed_host = host == base_host or host.endswith(f".{base_host}")
    if parsed.scheme not in {"http", "https"} or not allowed_host:
        raise HTTPException(status_code=400, detail="URL must be on configured Akwam host")


def _synthetic_tmdb_id(url: str) -> int:
    digest = hashlib.blake2s(url.encode("utf-8"), digest_size=4).digest()
    value = int.from_bytes(digest, "big") % 2_000_000_000
    return -(value or 1)


def _metadata_payload(metadata: AkwamMetadata) -> dict[str, Any]:
    return {
        "title": metadata.title,
        "url": metadata.url,
        "kind": metadata.kind,
        "poster": metadata.poster,
        "fanart": metadata.fanart,
        "overview": metadata.overview,
        "year": metadata.year,
        "downloads": [download.__dict__ for download in metadata.downloads],
        "episodes": [episode.__dict__ for episode in metadata.episodes],
    }


def _result_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(item.__dict__)


ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Akwarr Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1314;
      --surface: #151a1d;
      --panel: #1b2226;
      --panel-2: #232c31;
      --line: #334149;
      --line-strong: #4b5d66;
      --text: #f5f1e7;
      --muted: #a8b1ad;
      --green: #6edb9a;
      --amber: #efc85d;
      --red: #ef7779;
      --cyan: #7bc9d9;
      --blue: #8ea8ff;
      --shadow: 0 18px 52px rgba(0,0,0,.26);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      letter-spacing: 0;
      min-width: 320px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 22px 28px 14px;
      border-bottom: 1px solid var(--line);
      background: #131719;
      position: sticky;
      top: 0;
      z-index: 20;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      font-weight: 750;
    }
    .subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }
    .auth {
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: min(460px, 100%);
    }
    .auth[hidden] { display: none; }
    .lan-pill {
      border: 1px solid #2f634c;
      background: #16251e;
      color: var(--green);
      border-radius: 999px;
      padding: 8px 11px;
      font-size: 12px;
      font-weight: 800;
    }
    input, select, button {
      height: 36px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
    }
    input { width: 100%; }
    button {
      cursor: pointer;
      background: #263138;
      white-space: nowrap;
      font-weight: 650;
    }
    button:hover { border-color: var(--cyan); background: #2b3840; }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 2px solid var(--cyan);
      outline-offset: 2px;
    }
    .tabs {
      display: flex;
      gap: 6px;
      padding: 10px 28px;
      border-bottom: 1px solid var(--line);
      background: #101415;
      position: sticky;
      top: 81px;
      z-index: 19;
      overflow-x: auto;
    }
    .tab {
      min-width: 112px;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
    }
    .tab.active {
      background: var(--text);
      border-color: var(--text);
      color: #101415;
    }
    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px 28px 32px;
    }
    section {
      padding: 0;
      border-bottom: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 14px;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .view { display: none; }
    .view.active { display: block; }
    .layout {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .stack { display: grid; gap: 14px; }
    .card {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .metric {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 12px;
      min-height: 72px;
    }
    .metric strong {
      display: block;
      font-size: 28px;
      line-height: 1.1;
    }
    .metric span {
      color: var(--muted);
      font-size: 12px;
    }
    .pane {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      overflow: hidden;
    }
    .table-wrap {
      overflow: auto;
      max-height: min(70vh, 760px);
    }
    .jobs-cards {
      display: none;
    }
    .filters {
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .filter {
      height: 30px;
      border-radius: 999px;
      padding: 0 11px;
      color: var(--muted);
      background: transparent;
    }
    .filter.active {
      color: #101415;
      border-color: var(--cyan);
      background: var(--cyan);
    }
    .list {
      display: grid;
      gap: 8px;
      max-height: min(62vh, 560px);
      overflow: auto;
      padding-right: 4px;
    }
    .item {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 10px;
    }
    .title {
      font-weight: 700;
      word-break: break-word;
    }
    .meta, .path {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      word-break: break-word;
    }
    .tag {
      display: inline-block;
      color: #111315;
      background: var(--green);
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 11px;
      margin-right: 5px;
      font-weight: 800;
    }
    .tag.ok { background: var(--green); }
    .tag.pending, .tag.downloading, .tag.importing { background: var(--amber); }
    .tag.paused { background: var(--cyan); }
    .tag.failed { background: var(--red); }
    .tag.deleted { background: var(--muted); }
    .tag.metadata { background: var(--cyan); }
    .tag.download, .tag.episode { background: var(--blue); }
    .actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      min-width: 142px;
    }
    .actions button {
      min-width: 40px;
      padding: 0;
      font-size: 13px;
      border-color: var(--line-strong);
    }
    .actions button.danger {
      border-color: #6a3438;
      background: #3a2023;
      color: #ffd8d8;
    }
    .actions button:disabled {
      cursor: not-allowed;
      opacity: .45;
    }
    .series-download-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .series-download-actions button {
      min-width: 148px;
      padding: 0 12px;
    }
    .episode-select {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      margin-top: 8px;
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
    }
    .episode-select input {
      width: auto;
      height: auto;
      accent-color: var(--green);
    }
    .job-card {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 10px;
    }
    .job-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .job-card-head strong {
      line-height: 1.25;
      word-break: break-word;
    }
    .job-card .actions {
      min-width: 0;
    }
    .job-card .actions button {
      flex: 1 1 76px;
      min-width: 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      min-width: 980px;
    }
    th, td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      position: sticky;
      top: 0;
      background: var(--panel);
      z-index: 2;
    }
    tbody tr:hover { background: rgba(123, 201, 217, .06); }
    .mono-path {
      display: block;
      max-width: 560px;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    code {
      color: var(--cyan);
      word-break: break-word;
    }
    .error { color: var(--red); }
    .ok { color: var(--green); }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 18px;
      color: var(--muted);
      background: rgba(255,255,255,.02);
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .section-head h2 { margin: 0; }
    @media (max-width: 900px) {
      header {
        display: block;
        padding: 18px;
      }
      h1 { font-size: 30px; }
      main { padding: 14px; }
      .tabs {
        top: 0;
        padding: 10px 14px;
      }
      .layout, .grid { grid-template-columns: 1fr; }
      .auth { margin-top: 14px; }
      .auth button { flex: 0 0 auto; }
      input, select, button { height: 42px; }
      .row > input { min-width: 0; flex: 1 1 160px; }
      .table-wrap { max-height: 70vh; }
    }
    @media (max-width: 700px) {
      .jobs-table { display: none; }
      .jobs-cards {
        display: grid;
        gap: 10px;
      }
      .section-head {
        align-items: flex-start;
        flex-direction: column;
      }
      .section-head button { width: 100%; }
      .filters .filter {
        flex: 1 1 calc(50% - 7px);
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Akwarr Monitor</h1>
      <div class="subtitle">Akwam search, metadata, download queue, imported Arabic media</div>
    </div>
    <div id="keyAuth" class="auth">
      <input id="apiKey" type="password" placeholder="Akwarr API key">
      <button id="saveKey" type="button">Save</button>
      <button id="refresh" type="button">Refresh</button>
    </div>
    <div id="lanAuth" class="auth" hidden>
      <span class="lan-pill">LAN auth active</span>
      <button id="refreshLan" type="button">Refresh</button>
    </div>
  </header>
  <nav class="tabs" aria-label="Monitor pages">
    <button class="tab active" type="button" data-tab="overview">Overview</button>
    <button class="tab" type="button" data-tab="search">Search</button>
    <button class="tab" type="button" data-tab="downloads">Downloads</button>
    <button class="tab" type="button" data-tab="library">Library</button>
    <button class="tab" type="button" data-tab="diagnostics">Diagnostics</button>
  </nav>
  <main>
    <section id="overview" class="view active">
      <div class="stack">
        <div class="grid">
          <div class="metric"><strong id="jobTotal">0</strong><span>Total jobs</span></div>
          <div class="metric"><strong id="activeJobs">0</strong><span>Active downloads</span></div>
          <div class="metric"><strong id="failedJobs">0</strong><span>Failed jobs</span></div>
          <div class="metric"><strong id="movieFiles">0</strong><span>Movie files</span></div>
          <div class="metric"><strong id="seriesCount">0</strong><span>Series files</span></div>
          <div class="metric"><strong id="lastRefresh">--</strong><span>Last refresh</span></div>
        </div>
        <div class="layout">
          <div class="card">
            <h2>Recent Jobs</h2>
            <div id="recentJobs" class="list"></div>
          </div>
          <div class="card">
            <h2>Latest Imported Files</h2>
            <div id="recentFiles" class="list"></div>
          </div>
        </div>
      </div>
    </section>

    <section id="searchView" class="view">
      <div class="layout">
        <div class="stack">
          <div class="card">
            <h2>Akwam Search</h2>
            <div class="row">
              <select id="section" aria-label="Akwam section">
                <option value="movie">Movie</option>
                <option value="series">Series</option>
              </select>
              <input id="term" placeholder="Arabic title">
              <button id="search" type="button">Search</button>
            </div>
          </div>
          <div class="card">
            <h2>ElCinema Bridge</h2>
            <div class="row">
              <input id="elcinemaTerm" placeholder="English or Arabic title">
              <input id="elcinemaYear" placeholder="Year" inputmode="numeric" style="max-width:96px">
              <button id="elcinemaSearch" type="button">Find</button>
            </div>
            <div id="elcinemaResults" class="list" style="margin-top:10px"></div>
          </div>
        </div>
        <div class="card">
          <div class="section-head">
            <h2>Search Results</h2>
            <button id="clearResults" type="button">Clear</button>
          </div>
          <div id="results" class="list"></div>
        </div>
      </div>
    </section>

    <section id="downloadsView" class="view">
      <div class="card">
        <div class="section-head">
          <h2>Download Jobs</h2>
          <button id="refreshDownloads" type="button">Refresh</button>
        </div>
        <div class="filters" aria-label="Download filters">
          <button class="filter active" type="button" data-filter="all">All</button>
          <button class="filter" type="button" data-filter="downloading">Downloading</button>
          <button class="filter" type="button" data-filter="completed">Completed</button>
          <button class="filter" type="button" data-filter="failed">Failed</button>
          <button class="filter" type="button" data-filter="deleted">Deleted</button>
        </div>
        <div class="pane table-wrap jobs-table"><table><thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Progress</th><th>Destination</th><th>Error</th><th>Actions</th></tr></thead><tbody id="jobs"></tbody></table></div>
        <div id="jobsCards" class="jobs-cards"></div>
      </div>
    </section>

    <section id="libraryView" class="view">
      <div class="card">
        <div class="section-head">
          <h2>Imported Files</h2>
          <button id="refreshFiles" type="button">Refresh</button>
        </div>
        <div class="grid">
          <div>
            <h2>Movies</h2>
            <div id="movies" class="list"></div>
          </div>
          <div>
            <h2>Series</h2>
            <div id="seriesList" class="list"></div>
          </div>
          <div>
            <h2>Download Links</h2>
            <div id="resolved" class="list"></div>
          </div>
        </div>
      </div>
    </section>

    <section id="diagnosticsView" class="view">
      <div class="layout">
        <div class="card">
          <h2>Akwam Metadata</h2>
          <div id="metadata" class="list"></div>
        </div>
        <div class="card">
          <h2>Service Checks</h2>
          <div id="diagnostics" class="list"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const keyInput = document.querySelector('#apiKey');
    const trustedLanHost = location.hostname === 'akwam.mikawi.org';
    const keyFromUrl = trustedLanHost ? '' : params.get('apikey') || params.get('apiKey') || '';
    keyInput.value = trustedLanHost ? '' : keyFromUrl || localStorage.getItem('akwarrApiKey') || '';
    if (trustedLanHost) {
      document.querySelector('#keyAuth').hidden = true;
      document.querySelector('#lanAuth').hidden = false;
    }
    let allJobs = [];
    let allFiles = { movies: [], series: [] };
    let jobFilter = 'all';

    function key() { return keyInput.value.trim(); }
    function endpoint(path) {
      if (trustedLanHost) return path;
      const apiKey = key();
      if (!apiKey) return path;
      const sep = path.includes('?') ? '&' : '?';
      return `${path}${sep}apikey=${encodeURIComponent(apiKey)}`;
    }
    async function loadJson(path) {
      const response = await fetch(endpoint(path));
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return response.json();
    }
    function text(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }
    function item(title, body, tag = '') {
      return `<div class="item">${tag ? `<span class="tag ${text(tag)}">${text(tag)}</span>` : ''}<div class="title">${text(title)}</div><div class="meta">${body}</div></div>`;
    }
    function empty(label) {
      return `<div class="empty">${text(label)}</div>`;
    }
    function bytes(value) {
      const size = Number(value || 0);
      if (!size) return '';
      const units = ['B', 'KB', 'MB', 'GB'];
      let n = size;
      let i = 0;
      while (n >= 1024 && i < units.length - 1) { n = n / 1024; i += 1; }
      return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
    }
    function eta(seconds) {
      const s = Number(seconds || 0);
      if (!s) return '';
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m`;
      return `${s}s`;
    }
    function progress(job) {
      if (job.progressPercent == null) {
        return [job.aria2Status, job.downloadError].filter(Boolean).map(text).join('<br>');
      }
      const speed = bytes(job.downloadSpeed);
      const etaText = eta(job.etaSeconds);
      return [
        `${job.progressPercent}%`,
        `${bytes(job.progressBytes)} / ${bytes(job.totalBytes)}`,
        speed ? `${speed}/s` : '',
        etaText ? `ETA ${etaText}` : ''
      ].filter(Boolean).map(text).join('<br>');
    }
    function controls(job) {
      const status = String(job.status || '');
      const hasGid = Boolean(job.aria2_gid);
      const controlId = text(job.controlId || job.id);
      const canPause = hasGid && status === 'downloading';
      const canResume = hasGid && status === 'paused';
      const canDelete = !['completed', 'failed', 'deleted'].includes(status);
      return `<div class="actions">
        <button title="Pause download" aria-label="Pause download" ${canPause ? '' : 'disabled'} onclick="pauseJob('${controlId}')">Pause</button>
        <button title="Resume download" aria-label="Resume download" ${canResume ? '' : 'disabled'} onclick="resumeJob('${controlId}')">Run</button>
        <button class="danger" title="Delete download" aria-label="Delete download" ${canDelete ? '' : 'disabled'} onclick="deleteJob('${controlId}')">Delete</button>
      </div>`;
    }
    function jobRow(job) {
      return `
        <tr>
          <td>${text(job.controlId || job.id)}</td>
          <td>${text(job.source ? `${job.kind} / ${job.source}` : job.kind)}</td>
          <td><span class="tag ${text(job.status)}">${text(job.status)}</span></td>
          <td>${progress(job)}</td>
          <td><code class="mono-path">${text(job.dest_path || job.staging_path || '')}</code></td>
          <td class="error">${text(job.error || '')}</td>
          <td>${controls(job)}</td>
        </tr>`;
    }
    function jobCard(job) {
      const path = job.dest_path || job.staging_path || '';
      return `<div class="job-card">
        <div class="job-card-head">
          <strong>#${text(job.controlId || job.id)} ${text(job.source ? `${job.kind} / ${job.source}` : job.kind)}</strong>
          <span class="tag ${text(job.status)}">${text(job.status)}</span>
        </div>
        <div class="meta">${progress(job) || 'No active progress'}</div>
        <code class="mono-path">${text(path)}</code>
        ${job.error ? `<div class="error">${text(job.error)}</div>` : ''}
        ${controls(job)}
      </div>`;
    }
    function renderJobs() {
      const filtered = jobFilter === 'all'
        ? allJobs
        : allJobs.filter(job => String(job.status || '') === jobFilter);
      document.querySelector('#jobs').innerHTML = filtered.length
        ? filtered.map(jobRow).join('')
        : `<tr><td colspan="7">${empty('No jobs for this filter.')}</td></tr>`;
      document.querySelector('#jobsCards').innerHTML = filtered.length
        ? filtered.map(jobCard).join('')
        : empty('No jobs for this filter.');
      document.querySelector('#recentJobs').innerHTML = allJobs.slice(0, 6).map(job =>
        item(`#${job.id} ${job.kind}`, `<span class="tag ${text(job.status)}">${text(job.status)}</span><code>${text(job.dest_path || job.staging_path || '')}</code>`, job.status)
      ).join('') || empty('No download jobs yet.');
    }
    function renderFiles() {
      document.querySelector('#movies').innerHTML = allFiles.movies.map(file =>
        item(file.name, `<code>${text(file.relativePath)}</code><div>${text(bytes(file.size))}</div>`, file.kind)
      ).join('') || empty('No movie files found.');
      document.querySelector('#seriesList').innerHTML = allFiles.series.map(file =>
        item(file.name, `<code>${text(file.relativePath)}</code><div>${text(bytes(file.size))}</div>`, file.kind)
      ).join('') || empty('No series files found.');
      document.querySelector('#recentFiles').innerHTML = [...allFiles.movies, ...allFiles.series].slice(0, 8).map(file =>
        item(file.name, `<code>${text(file.relativePath)}</code>`, file.kind)
      ).join('') || empty('No imported files found.');
    }
    function renderDiagnostics() {
      document.querySelector('#diagnostics').innerHTML = [
        item('Monitor API', 'Jobs and file endpoints responded.', 'ok'),
        item('Auth mode', trustedLanHost ? 'akwam.mikawi.org LAN header injection' : 'Explicit API key', 'ok'),
        item('Movies root', `<code>${text(allFiles.moviesRoot || '')}</code>`, 'metadata'),
        item('Series root', `<code>${text(allFiles.seriesRoot || '')}</code>`, 'metadata')
      ].join('');
    }
    function showTab(tab) {
      document.querySelectorAll('.tab').forEach(button => {
        button.classList.toggle('active', button.dataset.tab === tab);
      });
      document.querySelectorAll('.view').forEach(view => view.classList.remove('active'));
      document.querySelector(`#${tab === 'overview' ? 'overview' : `${tab}View`}`).classList.add('active');
    }
    async function command(path, method = 'POST') {
      const response = await fetch(endpoint(path), { method });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return response.json();
    }
    async function postJson(path, payload) {
      const response = await fetch(endpoint(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail.slice(0, 180)}` : ''}`);
      }
      return response.json();
    }
    async function pauseJob(id) {
      await command(`/api/v3/monitor/jobs/${id}/pause`);
      await refresh();
    }
    async function resumeJob(id) {
      await command(`/api/v3/monitor/jobs/${id}/resume`);
      await refresh();
    }
    async function deleteJob(id) {
      await command(`/api/v3/monitor/jobs/${id}`, 'DELETE');
      await refresh();
    }
    async function refresh() {
      try {
        const jobs = await loadJson('/api/v3/monitor/jobs');
        allJobs = jobs.jobs || [];
        const counts = jobs.counts || {};
        document.querySelector('#jobTotal').textContent = jobs.total;
        document.querySelector('#activeJobs').textContent = Number(counts.downloading || 0) + Number(counts.importing || 0);
        document.querySelector('#failedJobs').textContent = counts.failed || 0;
        renderJobs();
      } catch (error) {
        document.querySelector('#jobs').innerHTML = `<tr><td colspan="7" class="error">${text(error.message)}</td></tr>`;
        document.querySelector('#jobsCards').innerHTML = `<div class="empty error">${text(error.message)}</div>`;
        document.querySelector('#recentJobs').innerHTML = item('Jobs unavailable', text(error.message), 'failed');
      }
      try {
        const files = await loadJson('/api/v3/monitor/files');
        allFiles = files;
        document.querySelector('#movieFiles').textContent = files.movies.length;
        document.querySelector('#seriesCount').textContent = files.series.length;
        document.querySelector('#lastRefresh').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        renderFiles();
        renderDiagnostics();
      } catch (error) {
        document.querySelector('#movies').innerHTML = item('Files unavailable', text(error.message), 'failed');
        document.querySelector('#recentFiles').innerHTML = item('Files unavailable', text(error.message), 'failed');
      }
    }
    async function search() {
      const term = document.querySelector('#term').value.trim();
      const section = document.querySelector('#section').value;
      if (!term) return;
      document.querySelector('#results').innerHTML = empty('Searching Akwam...');
      const data = await loadJson(`/api/v3/akwam/search?term=${encodeURIComponent(term)}&section=${section}`);
      document.querySelector('#results').innerHTML = data.results.map(result =>
        item(result.title, `<code>${text(result.url)}</code><div class="row" style="margin-top:8px"><button data-url="${text(result.url)}" data-kind="${section}" class="metadataBtn">Metadata</button></div>`, result.kind)
      ).join('') || item('No Akwam results', 'Try another Arabic title.', 'failed');
      document.querySelectorAll('.metadataBtn').forEach(button => {
        button.addEventListener('click', () => metadata(button.dataset.url, button.dataset.kind));
      });
    }
    async function elcinemaSearch() {
      const term = document.querySelector('#elcinemaTerm').value.trim();
      const year = document.querySelector('#elcinemaYear').value.trim();
      const kind = document.querySelector('#section').value;
      if (!term) return;
      document.querySelector('#elcinemaResults').innerHTML = empty('Searching ElCinema...');
      const path = `/api/v3/elcinema/search?term=${encodeURIComponent(term)}&kind=${kind}${year ? `&year=${encodeURIComponent(year)}` : ''}`;
      const data = await loadJson(path);
      const rows = data.results.map(result =>
        item(result.title, `<div>${text(result.english_title || '')} ${text(result.year || '')}</div><code>${text(result.url)}</code><div class="row" style="margin-top:8px"><button class="akwamFromElcinema" data-title="${text(result.title)}">Search Akwam</button></div>`, result.kind)
      ).join('');
      const candidates = data.candidates.length
        ? item('Arabic candidates', data.candidates.map(value => `<button class="akwamFromElcinema" data-title="${text(value)}">${text(value)}</button>`).join(' '), 'ok')
        : '';
      document.querySelector('#elcinemaResults').innerHTML = candidates + rows || item('No ElCinema results', 'Try the TMDB original title.', 'failed');
      document.querySelectorAll('.akwamFromElcinema').forEach(button => {
        button.addEventListener('click', () => {
          document.querySelector('#term').value = button.dataset.title || '';
          search();
        });
      });
    }
    async function metadata(url, kind) {
      showTab('diagnostics');
      document.querySelector('#metadata').innerHTML = empty('Loading Akwam metadata...');
      const data = await loadJson(`/api/v3/akwam/metadata?url=${encodeURIComponent(url)}&kind=${kind}`);
      const downloads = data.downloads.map(download =>
        item(download.quality, `<code>${text(download.link_url)}</code><div>${text(download.size || '')}</div><div class="row" style="margin-top:8px"><button class="resolveBtn" data-url="${text(download.link_url)}">Resolve</button></div>`, 'download')
      ).join('');
      const seriesDownload = kind === 'series' && data.episodes.length ? renderSeriesDownloadControls(data) : '';
      const episodes = data.episodes.map((ep, index) =>
        item(
          `S${String(ep.season || 1).padStart(2, '0')}E${String(ep.number).padStart(2, '0')}`,
          `<code>${text(ep.url)}</code><div>${text(ep.title)}</div><label class="episode-select"><input class="episodeSelect" type="checkbox" data-index="${index}" checked> Select episode</label>`,
          'episode'
        )
      ).join('');
      document.querySelector('#metadata').innerHTML =
        seriesDownload + item(data.title, `<code>${text(data.url)}</code><div>${text(data.overview || '')}</div>`, data.kind) + downloads + episodes;
      document.querySelectorAll('.resolveBtn').forEach(button => {
        button.addEventListener('click', () => resolve(button.dataset.url));
      });
      document.querySelector('#downloadAllEpisodes')?.addEventListener('click', () => {
        queueSeriesEpisodes(data, data.episodes.map(episodePayload));
      });
      document.querySelector('#downloadSelectedEpisodes')?.addEventListener('click', () => {
        const selected = [...document.querySelectorAll('.episodeSelect:checked')]
          .map(input => data.episodes[Number(input.dataset.index)])
          .filter(Boolean)
          .map(episodePayload);
        queueSeriesEpisodes(data, selected);
      });
    }
    function renderSeriesDownloadControls(data) {
      return item('Download series', `
        <div>${text(data.episodes.length)} episodes found from Akwam metadata.</div>
        <div class="series-download-actions">
          <button id="downloadAllEpisodes" type="button">Download all episodes</button>
          <button id="downloadSelectedEpisodes" type="button">Download selected</button>
        </div>
        <div id="seriesDownloadStatus" style="margin-top:10px"></div>
      `, 'download');
    }
    function episodePayload(ep) {
      return {
        season: Number(ep.season || 1),
        number: Number(ep.number),
        title: ep.title || '',
        url: ep.url
      };
    }
    async function queueSeriesEpisodes(metadata, episodes) {
      const status = document.querySelector('#seriesDownloadStatus');
      if (!episodes.length) {
        status.innerHTML = item('No episodes selected', 'Select at least one episode first.', 'failed');
        return;
      }
      status.innerHTML = item('Queueing episodes', `${episodes.length} episode jobs requested.`, 'pending');
      try {
        const data = await postJson('/api/v3/akwam/series/download', {
          title: metadata.title,
          url: metadata.url,
          year: metadata.year,
          episodes
        });
        status.innerHTML = item('Queued series download', `${data.queued} of ${data.requested} episode jobs ready.`, 'ok');
        await refresh();
        showTab('downloads');
      } catch (error) {
        status.innerHTML = item('Series download failed', text(error.message), 'failed');
      }
    }
    async function resolve(url) {
      const box = document.querySelector('#resolved');
      box.innerHTML = item('Resolving link', `<code>${text(url)}</code>`, 'pending') + box.innerHTML;
      try {
        const data = await loadJson(`/api/v3/akwam/resolve?linkUrl=${encodeURIComponent(url)}`);
        box.innerHTML = item('Direct URL', `<code>${text(data.directUrl)}</code>`, 'ok') + box.innerHTML;
      } catch (error) {
        box.innerHTML = item('Resolve failed', text(error.message), 'failed') + box.innerHTML;
      }
    }
    document.querySelectorAll('.tab').forEach(button => {
      button.addEventListener('click', () => showTab(button.dataset.tab));
    });
    document.querySelectorAll('.filter').forEach(button => {
      button.addEventListener('click', () => {
        jobFilter = button.dataset.filter || 'all';
        document.querySelectorAll('.filter').forEach(item => item.classList.toggle('active', item === button));
        renderJobs();
      });
    });
    document.querySelector('#saveKey').addEventListener('click', () => localStorage.setItem('akwarrApiKey', key()));
    document.querySelector('#refresh').addEventListener('click', refresh);
    document.querySelector('#refreshLan')?.addEventListener('click', refresh);
    document.querySelector('#refreshDownloads').addEventListener('click', refresh);
    document.querySelector('#refreshFiles').addEventListener('click', refresh);
    document.querySelector('#clearResults').addEventListener('click', () => {
      document.querySelector('#results').innerHTML = '';
      document.querySelector('#elcinemaResults').innerHTML = '';
    });
    document.querySelector('#search').addEventListener('click', search);
    document.querySelector('#elcinemaSearch').addEventListener('click', elcinemaSearch);
    keyInput.addEventListener('keydown', event => { if (event.key === 'Enter') refresh(); });
    document.querySelector('#term').addEventListener('keydown', event => { if (event.key === 'Enter') search(); });
    document.querySelector('#elcinemaTerm').addEventListener('keydown', event => { if (event.key === 'Enter') elcinemaSearch(); });
    refresh();
  </script>
</body>
</html>"""
