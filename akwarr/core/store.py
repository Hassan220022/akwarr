"""SQLite persistence for movies, series, episodes, and download jobs."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _parse_job_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class JobStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class Store:
    def __init__(
        self,
        db_path: Path,
        *,
        movies_path: Path | None = None,
        series_path: Path | None = None,
        staging_path: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.movies_path = movies_path or Path("/media/Movie/Arabic")
        self.series_path = series_path or Path("/media/Serries/Arabic")
        self.staging_path = staging_path or Path("/media/Download/akwarr-staging")

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tmdb_id INTEGER UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    original_title TEXT,
                    year INTEGER,
                    overview TEXT,
                    poster_url TEXT,
                    fanart_url TEXT,
                    akwam_url TEXT,
                    path TEXT,
                    has_file INTEGER NOT NULL DEFAULT 0,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    quality_profile_id INTEGER NOT NULL DEFAULT 1,
                    root_folder_path TEXT NOT NULL,
                    added TEXT NOT NULL,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tmdb_id INTEGER UNIQUE NOT NULL,
                    tvdb_id INTEGER,
                    title TEXT NOT NULL,
                    original_title TEXT,
                    year INTEGER,
                    overview TEXT,
                    poster_url TEXT,
                    fanart_url TEXT,
                    akwam_url TEXT,
                    path TEXT,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    season_folder INTEGER NOT NULL DEFAULT 1,
                    quality_profile_id INTEGER NOT NULL DEFAULT 1,
                    language_profile_id INTEGER NOT NULL DEFAULT 1,
                    root_folder_path TEXT NOT NULL,
                    added TEXT NOT NULL,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    season_number INTEGER NOT NULL,
                    episode_number INTEGER NOT NULL,
                    title TEXT,
                    akwam_url TEXT,
                    path TEXT,
                    has_file INTEGER NOT NULL DEFAULT 0,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(series_id, season_number, episode_number),
                    FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    aria2_gid TEXT,
                    staging_path TEXT,
                    dest_path TEXT,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                );
                """
            )
            await self._migrate_legacy_media_paths(db)
            await self._ensure_jobs_retry_count(db)
            await self._ensure_jobs_active_ref_index(db)
            await db.commit()

    async def _migrate_legacy_media_paths(self, db: aiosqlite.Connection) -> None:
        replacements = [
            ("/media/arabic/movies", str(self.movies_path)),
            ("/media/arabic/series", str(self.series_path)),
            ("/media/arabic/.staging", str(self.staging_path)),
        ]
        for old, new in replacements:
            for table, columns in (
                ("movies", ("path", "root_folder_path")),
                ("series", ("path", "root_folder_path")),
                ("episodes", ("path",)),
                ("jobs", ("staging_path", "dest_path")),
            ):
                for column in columns:
                    await db.execute(
                        f"""
                        UPDATE {table}
                        SET {column} = replace({column}, ?, ?)
                        WHERE {column} LIKE ?
                        """,
                        (old, new, f"{old}%"),
                    )

    async def _ensure_jobs_retry_count(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await cur.fetchall()}
        if "retry_count" not in columns:
            await db.execute("ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")

    async def _ensure_jobs_active_ref_index(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await cur.fetchall()}
        if not {"kind", "ref_id", "status"}.issubset(columns):
            return
        await self._dedupe_active_jobs(db)
        await self._dedupe_pending_when_completed(db)
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_kind_ref
            ON jobs (kind, ref_id)
            WHERE status IN ('pending', 'downloading', 'paused', 'importing', 'failed')
            """
        )

    async def _dedupe_active_jobs(self, db: aiosqlite.Connection) -> None:
        status_priority = {
            JobStatus.DOWNLOADING: 0,
            JobStatus.IMPORTING: 1,
            JobStatus.PENDING: 2,
            JobStatus.PAUSED: 3,
            JobStatus.FAILED: 4,
        }
        placeholders = ", ".join("?" for _ in self._ACTIVE_JOB_STATUSES)
        cur = await db.execute(
            f"""
            SELECT kind, ref_id
            FROM jobs
            WHERE status IN ({placeholders})
            GROUP BY kind, ref_id
            HAVING COUNT(*) > 1
            """,
            self._ACTIVE_JOB_STATUSES,
        )
        duplicates = await cur.fetchall()
        now = utcnow()
        for kind, ref_id in duplicates:
            cur = await db.execute(
                f"""
                SELECT id, status FROM jobs
                WHERE kind = ? AND ref_id = ? AND status IN ({placeholders})
                ORDER BY id
                """,
                (kind, ref_id, *self._ACTIVE_JOB_STATUSES),
            )
            jobs = await cur.fetchall()
            jobs.sort(key=lambda row: (status_priority.get(row[1], 99), row[0]))
            for job_id, _ in jobs[1:]:
                await db.execute(
                    "UPDATE jobs SET status = ?, updated = ? WHERE id = ?",
                    (JobStatus.DELETED, now, job_id),
                )

    async def _dedupe_pending_when_completed(self, db: aiosqlite.Connection) -> None:
        placeholders = ", ".join("?" for _ in self._ACTIVE_JOB_STATUSES)
        cur = await db.execute(
            f"""
            SELECT j_pending.id
            FROM jobs j_done
            JOIN jobs j_pending
              ON j_done.kind = j_pending.kind
             AND j_done.ref_id = j_pending.ref_id
            WHERE j_done.status = ?
              AND j_pending.status IN ({placeholders})
            """,
            (JobStatus.COMPLETED, *self._ACTIVE_JOB_STATUSES),
        )
        now = utcnow()
        for (job_id,) in await cur.fetchall():
            await db.execute(
                "UPDATE jobs SET status = ?, updated = ? WHERE id = ?",
                (JobStatus.DELETED, now, job_id),
            )

    # ── Movies ──

    async def add_movie(self, data: dict[str, Any]) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            now = utcnow()
            await db.execute(
                """
                INSERT INTO movies (
                    tmdb_id, title, original_title, year, overview,
                    poster_url, fanart_url, akwam_url, path, has_file,
                    monitored, quality_profile_id, root_folder_path, added, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    title=excluded.title,
                    original_title=excluded.original_title,
                    year=excluded.year,
                    overview=excluded.overview,
                    poster_url=COALESCE(excluded.poster_url, movies.poster_url),
                    fanart_url=COALESCE(excluded.fanart_url, movies.fanart_url),
                    akwam_url=COALESCE(excluded.akwam_url, movies.akwam_url),
                    monitored=excluded.monitored,
                    quality_profile_id=excluded.quality_profile_id,
                    root_folder_path=excluded.root_folder_path,
                    metadata_json=CASE
                        WHEN excluded.metadata_json = '{}' THEN movies.metadata_json
                        ELSE excluded.metadata_json
                    END
                """,
                (
                    data["tmdb_id"],
                    data["title"],
                    data.get("original_title"),
                    data.get("year"),
                    data.get("overview"),
                    data.get("poster_url"),
                    data.get("fanart_url"),
                    data.get("akwam_url"),
                    data.get("path"),
                    int(data.get("has_file", False)),
                    int(data.get("monitored", True)),
                    data.get("quality_profile_id", 1),
                    data["root_folder_path"],
                    now,
                    json.dumps(data.get("metadata") or {}),
                ),
            )
            await db.commit()
            return await self.get_movie_by_tmdb(data["tmdb_id"])  # type: ignore[return-value]

    async def list_movies(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM movies ORDER BY id")
            rows = await cur.fetchall()
            return [self._movie_row(r) for r in rows]

    async def get_movie(self, movie_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM movies WHERE id = ?", (movie_id,))
            row = await cur.fetchone()
            return self._movie_row(row) if row else None

    async def get_movie_by_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM movies WHERE tmdb_id = ?", (tmdb_id,))
            row = await cur.fetchone()
            return self._movie_row(row) if row else None

    async def set_movie_file(self, movie_id: int, path: str, has_file: bool = True) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE movies SET path = ?, has_file = ? WHERE id = ?",
                (path, int(has_file), movie_id),
            )
            await db.commit()

    async def update_movie_akwam(self, movie_id: int, akwam_url: str, poster: str | None, fanart: str | None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE movies SET akwam_url = ?,
                    poster_url = COALESCE(?, poster_url),
                    fanart_url = COALESCE(?, fanart_url)
                WHERE id = ?
                """,
                (akwam_url, poster, fanart, movie_id),
            )
            await db.commit()

    def _movie_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tmdb_id": row["tmdb_id"],
            "title": row["title"],
            "original_title": row["original_title"],
            "year": row["year"],
            "overview": row["overview"],
            "poster_url": row["poster_url"],
            "fanart_url": row["fanart_url"],
            "akwam_url": row["akwam_url"],
            "path": row["path"],
            "has_file": bool(row["has_file"]),
            "monitored": bool(row["monitored"]),
            "quality_profile_id": row["quality_profile_id"],
            "root_folder_path": row["root_folder_path"],
            "added": row["added"],
            "metadata": _json_dict(row["metadata_json"]),
        }

    # ── Series ──

    async def add_series(self, data: dict[str, Any]) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            now = utcnow()
            await db.execute(
                """
                INSERT INTO series (
                    tmdb_id, tvdb_id, title, original_title, year, overview,
                    poster_url, fanart_url, akwam_url, path, monitored, season_folder,
                    quality_profile_id, language_profile_id, root_folder_path, added, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    tvdb_id=COALESCE(excluded.tvdb_id, series.tvdb_id),
                    title=excluded.title,
                    original_title=excluded.original_title,
                    year=excluded.year,
                    overview=COALESCE(excluded.overview, series.overview),
                    poster_url=COALESCE(excluded.poster_url, series.poster_url),
                    fanart_url=COALESCE(excluded.fanart_url, series.fanart_url),
                    akwam_url=COALESCE(excluded.akwam_url, series.akwam_url),
                    path=COALESCE(excluded.path, series.path),
                    monitored=excluded.monitored,
                    season_folder=excluded.season_folder,
                    quality_profile_id=excluded.quality_profile_id,
                    language_profile_id=excluded.language_profile_id,
                    root_folder_path=excluded.root_folder_path,
                    metadata_json=CASE
                        WHEN excluded.metadata_json = '{}' THEN series.metadata_json
                        ELSE excluded.metadata_json
                    END
                """,
                (
                    data["tmdb_id"],
                    data.get("tvdb_id"),
                    data["title"],
                    data.get("original_title"),
                    data.get("year"),
                    data.get("overview"),
                    data.get("poster_url"),
                    data.get("fanart_url"),
                    data.get("akwam_url"),
                    data.get("path"),
                    int(data.get("monitored", True)),
                    int(data.get("season_folder", True)),
                    data.get("quality_profile_id", 1),
                    data.get("language_profile_id", 1),
                    data["root_folder_path"],
                    now,
                    json.dumps(data.get("metadata") or {}),
                ),
            )
            await db.commit()
            return await self.get_series_by_tmdb(data["tmdb_id"])  # type: ignore[return-value]

    async def list_series(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM series ORDER BY id")
            rows = await cur.fetchall()
            return [self._series_row(r) for r in rows]

    async def get_series(self, series_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM series WHERE id = ?", (series_id,))
            row = await cur.fetchone()
            return self._series_row(row) if row else None

    async def get_series_by_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM series WHERE tmdb_id = ?", (tmdb_id,))
            row = await cur.fetchone()
            return self._series_row(row) if row else None

    async def set_series_path(self, series_id: int, path: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE series SET path = ? WHERE id = ?", (path, series_id))
            await db.commit()

    def _series_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tmdb_id": row["tmdb_id"],
            "tvdb_id": row["tvdb_id"],
            "title": row["title"],
            "original_title": row["original_title"],
            "year": row["year"],
            "overview": row["overview"],
            "poster_url": row["poster_url"],
            "fanart_url": row["fanart_url"],
            "akwam_url": row["akwam_url"],
            "path": row["path"],
            "monitored": bool(row["monitored"]),
            "season_folder": bool(row["season_folder"]),
            "quality_profile_id": row["quality_profile_id"],
            "language_profile_id": row["language_profile_id"],
            "root_folder_path": row["root_folder_path"],
            "added": row["added"],
            "metadata": _json_dict(row["metadata_json"]),
        }

    # ── Episodes ──

    async def upsert_episode(self, data: dict[str, Any]) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO episodes (
                    series_id, season_number, episode_number, title, akwam_url, path, has_file, monitored
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET
                    title=excluded.title,
                    akwam_url=COALESCE(excluded.akwam_url, episodes.akwam_url),
                    monitored=excluded.monitored
                """,
                (
                    data["series_id"],
                    data["season_number"],
                    data["episode_number"],
                    data.get("title"),
                    data.get("akwam_url"),
                    data.get("path"),
                    int(data.get("has_file", False)),
                    int(data.get("monitored", True)),
                ),
            )
            await db.commit()
            cur = await db.execute(
                """
                SELECT * FROM episodes
                WHERE series_id = ? AND season_number = ? AND episode_number = ?
                """,
                (data["series_id"], data["season_number"], data["episode_number"]),
            )
            row = await cur.fetchone()
            return dict(row) if row else data

    async def list_episodes(self, series_id: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if series_id is None:
                cur = await db.execute("SELECT * FROM episodes ORDER BY series_id, season_number, episode_number")
            else:
                cur = await db.execute(
                    "SELECT * FROM episodes WHERE series_id = ? ORDER BY season_number, episode_number",
                    (series_id,),
                )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_episode_file(self, episode_id: int, path: str, has_file: bool = True) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE episodes SET path = ?, has_file = ? WHERE id = ?",
                (path, int(has_file), episode_id),
            )
            await db.commit()

    async def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    # ── Jobs ──

    async def has_blocking_job(self, kind: str, ref_id: int) -> bool:
        """True when a job already exists and should not be duplicated by sync."""
        async with aiosqlite.connect(self.db_path) as db:
            if await self._media_has_file(db, kind, ref_id):
                return True
            if await self._find_active_job_id(db, kind, ref_id) is not None:
                return True
            return False

    _ACTIVE_JOB_STATUSES = ("pending", "downloading", "paused", "importing", "failed")

    async def _media_has_file(self, db: aiosqlite.Connection, kind: str, ref_id: int) -> bool:
        if kind == "episode":
            cur = await db.execute("SELECT has_file FROM episodes WHERE id = ?", (ref_id,))
        elif kind == "movie":
            cur = await db.execute("SELECT has_file FROM movies WHERE id = ?", (ref_id,))
        else:
            return False
        row = await cur.fetchone()
        return bool(row and row[0])

    async def _find_active_job_id(self, db: aiosqlite.Connection, kind: str, ref_id: int) -> int | None:
        placeholders = ", ".join("?" for _ in self._ACTIVE_JOB_STATUSES)
        cur = await db.execute(
            f"""
            SELECT id FROM jobs
            WHERE kind = ?
              AND ref_id = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (kind, ref_id, *self._ACTIVE_JOB_STATUSES),
        )
        existing = await cur.fetchone()
        return int(existing[0]) if existing else None

    async def _find_completed_job_id(self, db: aiosqlite.Connection, kind: str, ref_id: int) -> int | None:
        cur = await db.execute(
            """
            SELECT id FROM jobs
            WHERE kind = ?
              AND ref_id = ?
              AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (kind, ref_id, JobStatus.COMPLETED),
        )
        existing = await cur.fetchone()
        return int(existing[0]) if existing else None

    async def create_job(self, kind: str, ref_id: int, dest_path: str) -> int:
        now = utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if await self._media_has_file(db, kind, ref_id):
                await db.commit()
                return 0

            existing_id = await self._find_active_job_id(db, kind, ref_id)
            if existing_id is not None:
                await db.commit()
                return existing_id

            if await self._find_completed_job_id(db, kind, ref_id) is not None:
                await db.commit()
                return 0

            try:
                cur = await db.execute(
                    """
                    INSERT INTO jobs (kind, ref_id, status, dest_path, created, updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kind, ref_id, JobStatus.PENDING, dest_path, now, now),
                )
                await db.commit()
                return cur.lastrowid or 0
            except aiosqlite.IntegrityError:
                existing_id = await self._find_active_job_id(db, kind, ref_id)
                await db.commit()
                return existing_id or 0

    async def list_pending_jobs(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('pending', 'downloading', 'importing')
                ORDER BY id
                """
            )
            return [dict(r) for r in await cur.fetchall()]

    async def list_retryable_failed_jobs(
        self,
        *,
        max_attempts: int,
        delay_seconds_for: Callable[[dict[str, Any]], int],
    ) -> list[dict[str, Any]]:
        """Failed jobs eligible for retry: per-job delay elapsed and under max attempts."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'failed'
                  AND retry_count < ?
                ORDER BY id
                """,
                (max_attempts,),
            )
            candidates = [dict(r) for r in await cur.fetchall()]
        now = datetime.now(UTC)
        retryable: list[dict[str, Any]] = []
        for job in candidates:
            delay = max(int(delay_seconds_for(job)), 0)
            cutoff = now - timedelta(seconds=delay)
            updated = _parse_job_timestamp(job.get("updated"))
            if updated <= cutoff:
                retryable.append(job)
        return retryable

    async def requeue_job(self, job_id: int) -> None:
        """Reset a failed job back to pending and bump retry_count."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE jobs
                SET status = 'pending',
                    retry_count = retry_count + 1,
                    error = '',
                    aria2_gid = NULL,
                    staging_path = NULL,
                    updated = ?
                WHERE id = ?
                """,
                (utcnow(), job_id),
            )
            await db.commit()

    async def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        aria2_gid: str | None = None,
        staging_path: str | None = None,
        error: str | None = None,
    ) -> None:
        fields: list[str] = ["updated = ?"]
        values: list[Any] = [utcnow()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if aria2_gid is not None:
            fields.append("aria2_gid = ?")
            values.append(aria2_gid)
        if staging_path is not None:
            fields.append("staging_path = ?")
            values.append(staging_path)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        values.append(job_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            await db.commit()

    async def get_job(self, job_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


@dataclass
class MovieRecord:
    tmdb_id: int
    title: str
    year: int | None = None
