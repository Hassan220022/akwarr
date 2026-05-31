"""Admin monitoring UI and Akwam diagnostics."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from akwarr.api.auth import verify_api_key
from akwarr.config import get_settings
from akwarr.core.store import Store
from akwarr.download.aria2 import Aria2Client
from akwarr.scraper.akwam import AkwamMetadata, AkwamScraper
from akwarr.scraper.elcinema import ElCinemaScraper

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".wmv"}
SIDE_CAR_SUFFIXES = {".nfo", ".jpg", ".jpeg", ".png", ".webp"}


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
        return {
            "total": len(jobs),
            "jobs": jobs,
            "counts": _job_counts(jobs),
        }

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

    @router.get("/api/v3/akwam/resolve")
    async def akwam_resolve(linkUrl: str = Query(..., min_length=1)) -> dict[str, str]:
        settings = get_settings()
        _validate_akwam_url(linkUrl, settings.akwam_base)
        scraper = AkwamScraper(settings)
        direct_url = await scraper.resolve_direct_url(linkUrl)
        return {"directUrl": direct_url}

    return router


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


async def _with_download_progress(jobs: list[dict[str, Any]], aria2: Aria2Client) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for job in jobs:
        item = dict(job)
        if item.get("status") == "downloading" and item.get("aria2_gid"):
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
      --bg: #111315;
      --panel: #1a1f23;
      --panel-2: #22282d;
      --line: #394149;
      --text: #f3f1e8;
      --muted: #a9b0ad;
      --green: #61d394;
      --amber: #e3b34b;
      --red: #e56b6f;
      --cyan: #76c7d7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 18px;
      padding: 22px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: #15191c;
    }
    h1 {
      margin: 0;
      font-size: 26px;
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
    }
    button:hover { border-color: var(--cyan); }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 440px) minmax(0, 1fr);
      min-height: calc(100vh - 76px);
    }
    aside {
      border-right: 1px solid var(--line);
      padding: 18px;
      background: #14181b;
    }
    section {
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0 0 12px;
      font-size: 14px;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }
    .row { display: flex; gap: 8px; align-items: center; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
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
    .list {
      display: grid;
      gap: 8px;
      max-height: 410px;
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
    .tag.pending, .tag.downloading, .tag.importing { background: var(--amber); }
    .tag.failed { background: var(--red); }
    .tag.metadata { background: var(--cyan); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
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
    }
    code {
      color: var(--cyan);
      word-break: break-word;
    }
    .error { color: var(--red); }
    .ok { color: var(--green); }
    @media (max-width: 900px) {
      header, main { display: block; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .grid { grid-template-columns: 1fr; }
      .auth { margin-top: 14px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Akwarr Monitor</h1>
      <div class="subtitle">Akwam search, metadata, download queue, imported Arabic media</div>
    </div>
    <div class="auth">
      <input id="apiKey" type="password" placeholder="Akwarr API key">
      <button id="saveKey">Save</button>
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <aside>
      <section>
        <h2>Akwam Search</h2>
        <div class="row">
          <select id="section">
            <option value="movie">Movie</option>
            <option value="series">Series</option>
          </select>
          <input id="term" placeholder="Arabic title">
          <button id="search">Search</button>
        </div>
      </section>
      <section>
        <h2>ElCinema Bridge</h2>
        <div class="row">
          <input id="elcinemaTerm" placeholder="English or Arabic title">
          <input id="elcinemaYear" placeholder="Year" style="max-width:82px">
          <button id="elcinemaSearch">Find</button>
        </div>
        <div id="elcinemaResults" class="list" style="margin-top:10px"></div>
      </section>
      <section>
        <h2>Search Results</h2>
        <div id="results" class="list"></div>
      </section>
    </aside>
    <div>
      <section>
        <h2>Status</h2>
        <div class="grid">
          <div class="metric"><strong id="jobTotal">0</strong><span>Jobs</span></div>
          <div class="metric"><strong id="movieFiles">0</strong><span>Movie files</span></div>
          <div class="metric"><strong id="seriesCount">0</strong><span>Series files</span></div>
        </div>
      </section>
      <section>
        <h2>Download Jobs</h2>
        <div class="pane"><table><thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Progress</th><th>Destination</th><th>Error</th></tr></thead><tbody id="jobs"></tbody></table></div>
      </section>
      <section>
        <h2>Akwam Metadata</h2>
        <div id="metadata" class="list"></div>
      </section>
      <section>
        <h2>Imported Files</h2>
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
      </section>
    </div>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const keyInput = document.querySelector('#apiKey');
    const trustedLanHost = location.hostname === 'akwam.mikawi.org';
    const keyFromUrl = trustedLanHost ? '' : params.get('apikey') || params.get('apiKey') || '';
    keyInput.value = trustedLanHost ? '' : keyFromUrl || localStorage.getItem('akwarrApiKey') || '';

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
    async function refresh() {
      try {
        const jobs = await loadJson('/api/v3/monitor/jobs');
        document.querySelector('#jobTotal').textContent = jobs.total;
        document.querySelector('#jobs').innerHTML = jobs.jobs.map(job => `
          <tr>
            <td>${job.id}</td>
            <td>${text(job.kind)}</td>
            <td><span class="tag ${text(job.status)}">${text(job.status)}</span></td>
            <td>${progress(job)}</td>
            <td><code>${text(job.dest_path || job.staging_path || '')}</code></td>
            <td class="error">${text(job.error || '')}</td>
          </tr>`).join('');
      } catch (error) {
        document.querySelector('#jobs').innerHTML = `<tr><td colspan="6" class="error">${text(error.message)}</td></tr>`;
      }
      try {
        const files = await loadJson('/api/v3/monitor/files');
        document.querySelector('#movieFiles').textContent = files.movies.length;
        document.querySelector('#seriesCount').textContent = files.series.length;
        document.querySelector('#movies').innerHTML = files.movies.map(file =>
          item(file.name, `<code>${text(file.relativePath)}</code>`, file.kind)
        ).join('');
        document.querySelector('#seriesList').innerHTML = files.series.map(file =>
          item(file.name, `<code>${text(file.relativePath)}</code>`, file.kind)
        ).join('');
      } catch (error) {
        document.querySelector('#movies').innerHTML = item('Files unavailable', text(error.message), 'failed');
      }
    }
    async function search() {
      const term = document.querySelector('#term').value.trim();
      const section = document.querySelector('#section').value;
      if (!term) return;
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
      const data = await loadJson(`/api/v3/akwam/metadata?url=${encodeURIComponent(url)}&kind=${kind}`);
      const downloads = data.downloads.map(download =>
        item(download.quality, `<code>${text(download.link_url)}</code><div>${text(download.size || '')}</div><div class="row" style="margin-top:8px"><button class="resolveBtn" data-url="${text(download.link_url)}">Resolve</button></div>`, 'download')
      ).join('');
      const episodes = data.episodes.map(ep =>
        item(`S${String(ep.season || 1).padStart(2, '0')}E${String(ep.number).padStart(2, '0')}`, `<code>${text(ep.url)}</code><div>${text(ep.title)}</div>`, 'episode')
      ).join('');
      document.querySelector('#metadata').innerHTML =
        item(data.title, `<code>${text(data.url)}</code><div>${text(data.overview || '')}</div>`, data.kind) + downloads + episodes;
      document.querySelectorAll('.resolveBtn').forEach(button => {
        button.addEventListener('click', () => resolve(button.dataset.url));
      });
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
    document.querySelector('#saveKey').addEventListener('click', () => localStorage.setItem('akwarrApiKey', key()));
    document.querySelector('#refresh').addEventListener('click', refresh);
    document.querySelector('#search').addEventListener('click', search);
    document.querySelector('#elcinemaSearch').addEventListener('click', elcinemaSearch);
    keyInput.addEventListener('keydown', event => { if (event.key === 'Enter') refresh(); });
    refresh();
  </script>
</body>
</html>"""
