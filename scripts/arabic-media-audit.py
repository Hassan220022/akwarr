#!/usr/bin/env python3
"""Audit Arabic series/movies: artwork, NFO, Jellyfin match, akwarr jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SERIES_ROOT = Path("/media/Serries/Arabic")
MOVIES_ROOT = Path("/media/Movie/Arabic")
JELLYFIN_SERIES_LIB = "f088360ebd3d3206bf457c7268835078"
JELLYFIN_MOVIES_LIB = "d95af285f1359f06325593fc15d6623f"
PATH_MAP = ("/media/", "/cc/")
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm"}
from akwarr.library.artwork import is_valid_local_image, poster_matches_episode_thumb
SONARR_DB_CANDIDATES = [
    Path("/config/sonarr/akwarr.db"),
    Path("/var/lib/docker/volumes/akwarr_akwarr-sonarr-config/_data/akwarr.db"),
]
RADARR_DB_CANDIDATES = [
    Path("/config/radarr/akwarr.db"),
    Path("/var/lib/docker/volumes/akwarr_akwarr-radarr-config/_data/akwarr.db"),
]


def resolve_db(candidates: list[Path]) -> Path:
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


@dataclass
class CheckResult:
    name: str
    status: str  # PASS, WARN, FAIL
    detail: str = ""


@dataclass
class TitleAudit:
    kind: str
    folder: str
    checks: list[CheckResult] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    episodes: dict[str, Any] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(c.status == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.status == "WARN" for c in self.checks):
            return "WARN"
        return "PASS"


def load_env(path: str = "/opt/akwarr/.env") -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        env.setdefault(k, v)
    return env


def jellyfin_path(local: Path) -> str:
    return str(local).replace(*PATH_MAP)


def is_valid_image(path: Path) -> tuple[bool, str]:
    return is_valid_local_image(path)


def nfo_art_tags(nfo_path: Path) -> tuple[bool, str]:
    if not nfo_path.exists():
        return False, "nfo missing"
    try:
        root = ET.parse(nfo_path).getroot()
    except ET.ParseError as exc:
        return False, f"parse error: {exc}"
    art = root.find("art")
    if art is None:
        return False, "no <art> block"
    poster = art.findtext("poster", "").strip()
    fanart = art.findtext("fanart", "").strip()
    if not poster:
        return False, "no <poster> in art"
    parts = [f"poster={poster}"]
    if fanart:
        parts.append(f"fanart={fanart}")
    return True, ", ".join(parts)


def resolve_poster_path(folder: Path, kind: str) -> Path | None:
    candidates = [folder / "poster.jpg"]
    if kind == "series":
        candidates.extend([folder / "folder.jpg", folder / "season01-poster.jpg"])
    else:
        candidates.extend([folder / "folder.jpg", folder / "landscape.jpg"])
    for p in candidates:
        ok, _ = is_valid_image(p)
        if ok:
            return p
    return None


def ensure_standard_poster(folder: Path, kind: str) -> str | None:
    dest = folder / "poster.jpg"
    ok, _ = is_valid_image(dest)
    if ok:
        return None
    source = resolve_poster_path(folder, kind)
    if source and source != dest:
        dest.write_bytes(source.read_bytes())
        return f"copied {source.name} -> poster.jpg"
    return None


def resolve_fanart_path(folder: Path, kind: str) -> Path | None:
    candidates = [folder / "fanart.jpg"]
    if kind == "series":
        candidates.append(folder / "backdrop.jpg")
    for p in candidates:
        ok, _ = is_valid_image(p)
        if ok:
            return p
    return None


def ensure_standard_fanart(folder: Path, kind: str) -> str | None:
    dest = folder / "fanart.jpg"
    ok, _ = is_valid_image(dest)
    if ok:
        return None
    source = resolve_fanart_path(folder, kind)
    if source and source != dest:
        dest.write_bytes(source.read_bytes())
        return f"copied {source.name} -> fanart.jpg"
    return None


def md5_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def poster_is_episode_thumb(folder: Path) -> tuple[bool, str]:
    return poster_matches_episode_thumb(folder)


def patch_nfo_if_needed(folder: Path, kind: str) -> str | None:
    from akwarr.library import metadata as meta

    if kind == "series":
        nfo = folder / "tvshow.nfo"
    else:
        nfo = folder / "movie.nfo"
        if not nfo.exists():
            alt = list(folder.glob("*.nfo"))
            nfo = alt[0] if alt else nfo
    if not nfo.exists():
        return None
    ok, _ = nfo_art_tags(nfo)
    if ok:
        return None
    poster_ok, _ = is_valid_image(folder / "poster.jpg")
    fanart_ok, _ = is_valid_image(folder / "fanart.jpg")
    if poster_ok or fanart_ok:
        meta.patch_nfo_art(
            nfo,
            poster_file="poster.jpg" if poster_ok else None,
            fanart_file="fanart.jpg" if fanart_ok else None,
        )
        return f"patched art tags in {nfo.name}"
    return None


def scan_episodes(series_folder: Path) -> dict[str, Any]:
    videos: list[Path] = []
    for p in series_folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            videos.append(p)
    missing_thumb = 0
    for v in videos:
        thumb = v.with_name(f"{v.stem}-thumb.jpg")
        if not thumb.exists():
            missing_thumb += 1
    return {
        "video_count": len(videos),
        "missing_thumb": missing_thumb,
        "videos": [str(v.relative_to(series_folder)) for v in sorted(videos)[:20]],
    }


def db_jobs_for_path(db_path: Path, dest_fragment: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, kind, status, dest_path, error, retry_count, created, updated FROM jobs "
        "WHERE dest_path LIKE ? ORDER BY id DESC LIMIT 20",
        (f"%{dest_fragment}%",),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def db_record_for_folder(db_path: Path, table: str, folder_name: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table} WHERE path LIKE ?", (f"%{folder_name}%",))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


class JellyfinClient:
    def __init__(self, base: str, api_key: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {"X-Emby-Token": api_key}
        self._series_cache: dict[str, dict] | None = None
        self._movie_cache: dict[str, dict] | None = None

    async def _fetch_items(self, parent_id: str, item_type: str) -> list[dict]:
        items: list[dict] = []
        start = 0
        limit = 200
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                r = await client.get(
                    f"{self.base}/Items",
                    headers=self.headers,
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": item_type,
                        "Fields": "Path,ProviderIds,ImageTags",
                        "StartIndex": start,
                        "Limit": limit,
                    },
                )
                r.raise_for_status()
                data = r.json()
                batch = data.get("Items") or []
                items.extend(batch)
                if start + limit >= data.get("TotalRecordCount", len(batch)):
                    break
                start += limit
        return items

    async def series_by_path(self) -> dict[str, dict]:
        if self._series_cache is None:
            items = await self._fetch_items(JELLYFIN_SERIES_LIB, "Series")
            self._series_cache = {}
            for it in items:
                path = it.get("Path") or ""
                self._series_cache[path] = it
                # also index by folder name
                name = Path(path).name if path else it.get("Name", "")
                self._series_cache.setdefault(f"name:{name}", it)
        return self._series_cache

    async def movies_by_path(self) -> dict[str, dict]:
        if self._movie_cache is None:
            items = await self._fetch_items(JELLYFIN_MOVIES_LIB, "Movie")
            self._movie_cache = {}
            for it in items:
                path = it.get("Path") or ""
                self._movie_cache[path] = it
                name = Path(path).parent.name if path else it.get("Name", "")
                self._movie_cache.setdefault(f"name:{name}", it)
        return self._movie_cache

    async def primary_md5(self, item_id: str) -> str | None:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                f"{self.base}/Items/{item_id}/Images/Primary",
                headers=self.headers,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return hashlib.md5(r.content).hexdigest()

    async def refresh_library(self, lib_id: str) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            await client.post(
                f"{self.base}/Library/Refresh",
                headers=self.headers,
                params={"ItemId": lib_id, "Recursive": "true"},
            )


async def try_fix_artwork(
    folder: Path,
    *,
    kind: str,
    record: dict[str, Any] | None,
    fixes: list[str],
) -> None:
    from akwarr.library import artwork as art
    from akwarr.library import metadata as meta

    poster_url = (record or {}).get("poster_url")
    fanart_url = (record or {}).get("fanart_url")
    akwam_url = (record or {}).get("akwam_url")
    tmdb_id = (record or {}).get("tmdb_id")

    from akwarr.config import get_settings
    from akwarr.core.tmdb import TMDBClient
    from akwarr.scraper.akwam import AkwamScraper

    settings = get_settings()
    tmdb = TMDBClient(settings)
    scraper = AkwamScraper(settings)

    if kind == "series":
        ok = await art.refresh_series_artwork(
            folder,
            season=1,
            poster_url=poster_url,
            fanart_url=fanart_url,
            tmdb_id=tmdb_id,
            akwam_url=akwam_url,
            tmdb=tmdb,
            scraper=scraper,
        )
        if ok:
            nfo = folder / "tvshow.nfo"
            if nfo.exists():
                meta.patch_nfo_art(nfo, poster_file="poster.jpg", fanart_file="fanart.jpg")
            fixes.append("refreshed series artwork")
    else:
        ok = await art.refresh_movie_artwork(
            folder,
            poster_url=poster_url,
            fanart_url=fanart_url,
            tmdb_id=tmdb_id,
            akwam_url=akwam_url,
            tmdb=tmdb,
            scraper=scraper,
        )
        if ok:
            nfo = folder / "movie.nfo"
            if nfo.exists():
                meta.patch_nfo_art(nfo, poster_file="poster.jpg", fanart_file="fanart.jpg")
            elif (folder / f"{folder.name}.nfo").exists():
                meta.patch_nfo_art(
                    folder / f"{folder.name}.nfo",
                    poster_file="poster.jpg",
                    fanart_file="fanart.jpg",
                )
            fixes.append("refreshed movie artwork")


async def audit_series(
    folder: Path,
    jf: JellyfinClient,
    *,
    autofix: bool,
) -> TitleAudit:
    name = folder.name
    audit = TitleAudit(kind="series", folder=name)
    record = db_record_for_folder(resolve_db(SONARR_DB_CANDIDATES), "series", name)

    if autofix:
        for msg in filter(None, [
            ensure_standard_poster(folder, "series"),
            ensure_standard_fanart(folder, "series"),
        ]):
            audit.fixes.append(msg)
        if not (folder / "folder.jpg").exists():
            ok, _ = is_valid_image(folder / "poster.jpg")
            if ok:
                (folder / "folder.jpg").write_bytes((folder / "poster.jpg").read_bytes())
                audit.fixes.append("created folder.jpg from poster.jpg")
        patched = patch_nfo_if_needed(folder, "series")
        if patched:
            audit.fixes.append(patched)

    for label, path in [
        ("poster", folder / "poster.jpg"),
        ("fanart", folder / "fanart.jpg"),
        ("folder", folder / "folder.jpg"),
    ]:
        ok, detail = is_valid_image(path)
        audit.checks.append(
            CheckResult(label, "PASS" if ok else "FAIL", detail)
        )

    nfo_ok, nfo_detail = nfo_art_tags(folder / "tvshow.nfo")
    audit.checks.append(CheckResult("tvshow.nfo", "PASS" if nfo_ok else "FAIL", nfo_detail))

    thumb_dup, thumb_name = poster_is_episode_thumb(folder)
    if thumb_dup:
        audit.checks.append(
            CheckResult(
                "poster_source",
                "FAIL",
                f"poster.jpg matches episode thumb {thumb_name}",
            )
        )
    else:
        audit.checks.append(CheckResult("poster_source", "PASS", "not an episode thumb"))

    ep = scan_episodes(folder)
    audit.episodes = ep
    if ep["video_count"] == 0:
        audit.checks.append(CheckResult("episodes", "FAIL", "no video files"))
    elif ep["missing_thumb"] > 0:
        audit.checks.append(
            CheckResult(
                "episodes",
                "WARN",
                f"{ep['video_count']} videos, {ep['missing_thumb']} missing -thumb.jpg",
            )
        )
    else:
        audit.checks.append(
            CheckResult("episodes", "PASS", f"{ep['video_count']} videos, all thumbs present")
        )

    # Jellyfin
    jf_items = await jf.series_by_path()
    jf_path = jellyfin_path(folder)
    jf_item = jf_items.get(jf_path) or jf_items.get(f"name:{name}")
    if not jf_item:
        audit.checks.append(CheckResult("jellyfin", "FAIL", "not found in Jellyfin"))
    else:
        local_md5 = md5_file(folder / "poster.jpg")
        jf_md5 = await jf.primary_md5(jf_item["Id"])
        if not local_md5:
            audit.checks.append(CheckResult("jellyfin", "FAIL", "no local poster to compare"))
        elif not jf_md5:
            audit.checks.append(CheckResult("jellyfin", "WARN", "Jellyfin has no Primary image"))
        elif local_md5 == jf_md5:
            audit.checks.append(CheckResult("jellyfin", "PASS", "poster MD5 match"))
        else:
            audit.checks.append(
                CheckResult("jellyfin", "FAIL", f"poster MD5 mismatch local={local_md5[:8]} jf={jf_md5[:8]}")
            )

    jobs = db_jobs_for_path(resolve_db(SONARR_DB_CANDIDATES), name)
    audit.jobs = jobs
    bad_jobs = [j for j in jobs if j["status"] in ("failed", "pending", "downloading", "paused", "importing")]
    if any(j["status"] == "failed" for j in bad_jobs):
        audit.checks.append(
            CheckResult("jobs", "FAIL", f"{len([j for j in bad_jobs if j['status']=='failed'])} failed")
        )
    elif bad_jobs:
        audit.checks.append(
            CheckResult("jobs", "WARN", f"{len(bad_jobs)} active/pending: {', '.join(j['status'] for j in bad_jobs[:3])}")
        )
    else:
        audit.checks.append(CheckResult("jobs", "PASS", "no failed/stuck jobs"))

    if autofix:
        poster_fail = any(c.name == "poster" and c.status == "FAIL" for c in audit.checks)
        fanart_fail = any(c.name == "fanart" and c.status == "FAIL" for c in audit.checks)
        nfo_fail = any(c.name == "tvshow.nfo" and c.status == "FAIL" for c in audit.checks)
        if poster_fail or fanart_fail or nfo_fail:
            try:
                await try_fix_artwork(folder, kind="series", record=record, fixes=audit.fixes)
                # Re-check after download
                for label, path in [("poster", folder / "poster.jpg"), ("fanart", folder / "fanart.jpg")]:
                    for c in audit.checks:
                        if c.name == label:
                            ok, detail = is_valid_image(path)
                            c.status = "PASS" if ok else "FAIL"
                            c.detail = detail
            except Exception as exc:
                audit.fixes.append(f"autofix error: {exc}")

    return audit


async def audit_movie(
    folder: Path,
    jf: JellyfinClient,
    *,
    autofix: bool,
) -> TitleAudit:
    name = folder.name
    audit = TitleAudit(kind="movie", folder=name)
    record = db_record_for_folder(resolve_db(RADARR_DB_CANDIDATES), "movies", name)

    if autofix:
        for msg in filter(None, [
            ensure_standard_poster(folder, "movie"),
            ensure_standard_fanart(folder, "movie"),
        ]):
            audit.fixes.append(msg)
        patched = patch_nfo_if_needed(folder, "movie")
        if patched:
            audit.fixes.append(patched)

    for label, path in [
        ("poster", folder / "poster.jpg"),
        ("fanart", folder / "fanart.jpg"),
    ]:
        ok, detail = is_valid_image(path)
        audit.checks.append(CheckResult(label, "PASS" if ok else "FAIL", detail))

    nfo_path = folder / "movie.nfo"
    if not nfo_path.exists():
        alt = list(folder.glob("*.nfo"))
        nfo_path = alt[0] if alt else nfo_path
    nfo_ok, nfo_detail = nfo_art_tags(nfo_path)
    audit.checks.append(CheckResult("movie.nfo", "PASS" if nfo_ok else "FAIL", nfo_detail))

    videos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not videos:
        audit.checks.append(CheckResult("video", "FAIL", "no video file"))
    else:
        audit.checks.append(CheckResult("video", "PASS", videos[0].name))

    jf_items = await jf.movies_by_path()
    # movie path is file inside folder
    jf_item = None
    for v in videos:
        jf_item = jf_items.get(jellyfin_path(v))
        if jf_item:
            break
    if not jf_item:
        jf_item = jf_items.get(f"name:{name}")
    if not jf_item:
        audit.checks.append(CheckResult("jellyfin", "FAIL", "not found in Jellyfin"))
    else:
        local_md5 = md5_file(folder / "poster.jpg")
        jf_md5 = await jf.primary_md5(jf_item["Id"])
        if not local_md5:
            audit.checks.append(CheckResult("jellyfin", "FAIL", "no local poster"))
        elif not jf_md5:
            audit.checks.append(CheckResult("jellyfin", "WARN", "Jellyfin has no Primary"))
        elif local_md5 == jf_md5:
            audit.checks.append(CheckResult("jellyfin", "PASS", "poster MD5 match"))
        else:
            audit.checks.append(
                CheckResult("jellyfin", "FAIL", f"poster MD5 mismatch local={local_md5[:8]} jf={jf_md5[:8]}")
            )

    jobs = db_jobs_for_path(resolve_db(RADARR_DB_CANDIDATES), name)
    audit.jobs = jobs
    bad_jobs = [j for j in jobs if j["status"] in ("failed", "pending", "downloading", "paused", "importing")]
    if any(j["status"] == "failed" for j in bad_jobs):
        audit.checks.append(CheckResult("jobs", "FAIL", f"{len([j for j in bad_jobs if j['status']=='failed'])} failed"))
    elif bad_jobs:
        audit.checks.append(CheckResult("jobs", "WARN", f"{len(bad_jobs)} active/pending"))
    else:
        audit.checks.append(CheckResult("jobs", "PASS", "no failed/stuck jobs"))

    if autofix:
        poster_fail = any(c.name == "poster" and c.status == "FAIL" for c in audit.checks)
        fanart_fail = any(c.name == "fanart" and c.status == "FAIL" for c in audit.checks)
        nfo_fail = any(c.name == "movie.nfo" and c.status == "FAIL" for c in audit.checks)
        if poster_fail or fanart_fail or nfo_fail:
            try:
                await try_fix_artwork(folder, kind="movie", record=record, fixes=audit.fixes)
                for label, path in [("poster", folder / "poster.jpg"), ("fanart", folder / "fanart.jpg")]:
                    for c in audit.checks:
                        if c.name == label:
                            ok, detail = is_valid_image(path)
                            c.status = "PASS" if ok else "FAIL"
                            c.detail = detail
            except Exception as exc:
                audit.fixes.append(f"autofix error: {exc}")

    return audit


def print_report(series: list[TitleAudit], movies: list[TitleAudit], *, refreshed: bool) -> dict[str, int]:
    all_audits = series + movies
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for a in all_audits:
        counts[a.overall] += 1

    print("=" * 80)
    print("ARABIC MEDIA AUDIT REPORT")
    print("=" * 80)
    print(f"Series path: {SERIES_ROOT} ({len(series)} folders)")
    print(f"Movies path: {MOVIES_ROOT} ({len(movies)} folders)")
    print(f"Totals: PASS={counts['PASS']} WARN={counts['WARN']} FAIL={counts['FAIL']}")
    if refreshed:
        print("Jellyfin library refresh: triggered for Arabic Series + Arabic Movies")
    print()

    def row(a: TitleAudit) -> None:
        checks = " | ".join(f"{c.name}:{c.status}" for c in a.checks)
        print(f"[{a.overall:4}] {a.kind:6} {a.folder}")
        print(f"       {checks}")
        for c in a.checks:
            if c.status != "PASS" and c.detail:
                print(f"         - {c.name}: {c.detail}")
        if a.fixes:
            print(f"       FIXED: {'; '.join(a.fixes)}")
        failed_jobs = [j for j in a.jobs if j["status"] == "failed"]
        if failed_jobs:
            for j in failed_jobs[:3]:
                print(f"         ! job#{j['id']} {j['status']}: {j.get('error','')[:80]}")

    print("--- SERIES ---")
    for a in sorted(series, key=lambda x: (x.overall != "FAIL", x.overall != "WARN", x.folder)):
        row(a)
    print()
    print("--- MOVIES ---")
    for a in sorted(movies, key=lambda x: (x.overall != "FAIL", x.overall != "WARN", x.folder)):
        row(a)

    fails = [a for a in all_audits if a.overall == "FAIL"]
    if fails:
        print()
        print("--- PRIORITY FIXES (FAIL) ---")
        for a in fails:
            fail_items = [c.name for c in a.checks if c.status == "FAIL"]
            print(f"  {a.kind} '{a.folder}': {', '.join(fail_items)}")

    return counts


async def main() -> int:
    autofix = "--fix" in sys.argv
    env = load_env()
    jf_url = env.get("JELLYFIN_URL", "http://192.168.1.20:8096")
    jf_key = env.get("JELLYFIN_API_KEY", "")
    if not jf_key:
        print("ERROR: JELLYFIN_API_KEY not set", file=sys.stderr)
        return 1

    jf = JellyfinClient(jf_url, jf_key)

    series_dirs = sorted([p for p in SERIES_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name)
    movie_dirs = sorted([p for p in MOVIES_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name)

    series_audits: list[TitleAudit] = []
    for d in series_dirs:
        series_audits.append(await audit_series(d, jf, autofix=autofix))

    movie_audits: list[TitleAudit] = []
    for d in movie_dirs:
        movie_audits.append(await audit_movie(d, jf, autofix=autofix))

    refreshed = False
    if autofix:
        try:
            await jf.refresh_library(JELLYFIN_SERIES_LIB)
            await jf.refresh_library(JELLYFIN_MOVIES_LIB)
            refreshed = True
        except Exception as exc:
            print(f"Jellyfin refresh failed: {exc}", file=sys.stderr)

    counts = print_report(series_audits, movie_audits, refreshed=refreshed)

    # JSON summary for machine use
    summary = {
        "counts": counts,
        "series": [
            {
                "folder": a.folder,
                "overall": a.overall,
                "checks": {c.name: {"status": c.status, "detail": c.detail} for c in a.checks},
                "fixes": a.fixes,
                "episodes": a.episodes,
            }
            for a in series_audits
        ],
        "movies": [
            {
                "folder": a.folder,
                "overall": a.overall,
                "checks": {c.name: {"status": c.status, "detail": c.detail} for c in a.checks},
                "fixes": a.fixes,
            }
            for a in movie_audits
        ],
    }
    out = Path("/tmp/arabic-audit.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nJSON written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
