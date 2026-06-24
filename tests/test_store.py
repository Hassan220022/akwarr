import aiosqlite
import pytest

from akwarr.core.store import JobStatus, Store


@pytest.mark.asyncio
async def test_store_init_migrates_legacy_arabic_media_paths(tmp_path):
    db_path = tmp_path / "akwarr.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE movies (
                id INTEGER PRIMARY KEY,
                path TEXT,
                root_folder_path TEXT
            );
            CREATE TABLE series (
                id INTEGER PRIMARY KEY,
                path TEXT,
                root_folder_path TEXT
            );
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY,
                path TEXT
            );
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                staging_path TEXT,
                dest_path TEXT
            );
            INSERT INTO movies VALUES (
                1,
                '/media/arabic/movies/Film/Film.mkv',
                '/media/arabic/movies'
            );
            INSERT INTO series VALUES (
                1,
                '/media/arabic/series/Show',
                '/media/arabic/series'
            );
            INSERT INTO episodes VALUES (
                1,
                '/media/arabic/series/Show/Season 01/Show - S01E01.mkv'
            );
            INSERT INTO jobs VALUES (
                1,
                '/media/arabic/.staging/movie.mkv',
                '/media/arabic/movies/Film/Film.mkv'
            );
            """
        )
        await db.commit()

    store = Store(
        db_path,
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
        staging_path=tmp_path / "Download" / "akwarr-staging",
    )
    await store.init()

    async with aiosqlite.connect(db_path) as db:
        movie_path = await _value(db, "SELECT path FROM movies WHERE id = 1")
        series_path = await _value(db, "SELECT path FROM series WHERE id = 1")
        episode_path = await _value(db, "SELECT path FROM episodes WHERE id = 1")
        staging_path = await _value(db, "SELECT staging_path FROM jobs WHERE id = 1")
        dest_path = await _value(db, "SELECT dest_path FROM jobs WHERE id = 1")

    assert movie_path == str(tmp_path / "Movie" / "Arabic" / "Film" / "Film.mkv")
    assert series_path == str(tmp_path / "Serries" / "Arabic" / "Show")
    assert episode_path == str(tmp_path / "Serries" / "Arabic" / "Show" / "Season 01" / "Show - S01E01.mkv")
    assert staging_path == str(tmp_path / "Download" / "akwarr-staging" / "movie.mkv")
    assert dest_path == str(tmp_path / "Movie" / "Arabic" / "Film" / "Film.mkv")


async def _value(db: aiosqlite.Connection, query: str):
    cursor = await db.execute(query)
    row = await cursor.fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_store_round_trips_external_metadata_for_import(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    movie = await store.add_movie(
        {
            "tmdb_id": 2019662,
            "title": "الفيل الأزرق",
            "root_folder_path": str(tmp_path / "Movie" / "Arabic"),
            "metadata": {
                "imdb_id": "tt3461252",
                "elcinema": {"id": "2019662", "url": "https://elcinema.com/work/2019662"},
            },
        }
    )
    series = await store.add_series(
        {
            "tmdb_id": 2058052,
            "title": "ما وراء الطبيعة",
            "root_folder_path": str(tmp_path / "Serries" / "Arabic"),
            "metadata": {
                "imdb_id": "tt12411074",
                "elcinema": {"id": "2058052", "url": "https://elcinema.com/work/2058052"},
            },
        }
    )

    assert movie["metadata"]["imdb_id"] == "tt3461252"
    assert movie["metadata"]["elcinema"]["url"] == "https://elcinema.com/work/2019662"
    assert series["metadata"]["imdb_id"] == "tt12411074"
    assert series["metadata"]["elcinema"]["url"] == "https://elcinema.com/work/2058052"


@pytest.mark.asyncio
async def test_create_job_reuses_active_job_for_same_media_ref(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    first = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E12.mkv")
    second = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E13.mkv")

    jobs = await store.list_pending_jobs()

    assert second == first
    assert [(job["kind"], job["ref_id"], job["status"]) for job in jobs] == [("episode", 12, JobStatus.PENDING)]


@pytest.mark.asyncio
async def test_create_job_reuses_paused_job_for_same_media_ref(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    first = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E12.mkv")
    await store.update_job(first, status=JobStatus.PAUSED)
    second = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E12.mkv")

    assert second == first


@pytest.mark.asyncio
async def test_create_job_reuses_failed_job_for_same_media_ref(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    first = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E12.mkv")
    await store.update_job(first, status=JobStatus.FAILED, error="temporary")
    second = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E13.mkv")

    jobs = await store.list_jobs()
    active = [job for job in jobs if job["status"] != JobStatus.DELETED]

    assert second == first
    assert len(active) == 1
    assert active[0]["status"] == JobStatus.FAILED


@pytest.mark.asyncio
async def test_create_job_skips_when_episode_has_file(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    series = await store.add_series(
        {
            "tmdb_id": 1,
            "title": "Show",
            "root_folder_path": str(tmp_path / "Serries" / "Arabic"),
        }
    )
    episode = await store.upsert_episode(
        {
            "series_id": series["id"],
            "season_number": 1,
            "episode_number": 1,
            "title": "Pilot",
            "has_file": True,
            "path": "/media/ep.mkv",
        }
    )

    job_id = await store.create_job("episode", episode["id"], "/media/ep.mkv")

    assert job_id == 0
    assert await store.list_pending_jobs() == []


@pytest.mark.asyncio
async def test_create_job_skips_when_movie_has_file(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    movie = await store.add_movie(
        {
            "tmdb_id": 42,
            "title": "Film",
            "root_folder_path": str(tmp_path / "Movie" / "Arabic"),
            "has_file": True,
            "path": "/media/film.mkv",
        }
    )

    job_id = await store.create_job("movie", movie["id"], "/media/film.mkv")

    assert job_id == 0
    assert await store.list_pending_jobs() == []


@pytest.mark.asyncio
async def test_create_job_skips_when_completed_job_exists(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    first = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E01.mkv")
    await store.update_job(first, status=JobStatus.COMPLETED)

    second = await store.create_job("episode", 12, "/media/Serries/Arabic/Show/Season 01/Show - S01E01.mkv")

    assert second == 0
    jobs = await store.list_jobs()
    active = [job for job in jobs if job["status"] != JobStatus.DELETED]
    assert len(active) == 1
    assert active[0]["status"] == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_init_dedupes_pending_jobs_when_completed_exists(tmp_path):
    from akwarr.core.store import utcnow

    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    completed = await store.create_job("episode", 12, "/media/ep-a.mkv")
    await store.update_job(completed, status=JobStatus.COMPLETED)

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP INDEX IF EXISTS idx_jobs_active_kind_ref")
        await db.execute(
            "INSERT INTO jobs (kind, ref_id, status, dest_path, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
            ("episode", 12, JobStatus.PENDING, "/media/ep-b.mkv", utcnow(), utcnow()),
        )
        await db.commit()

    await store.init()

    jobs = await store.list_jobs()
    active = [job for job in jobs if job["status"] in store._ACTIVE_JOB_STATUSES]
    assert active == []
    completed_jobs = [job for job in jobs if job["status"] == JobStatus.COMPLETED]
    assert len(completed_jobs) == 1
    assert completed_jobs[0]["id"] == completed


@pytest.mark.asyncio
async def test_init_dedupes_active_jobs_before_unique_index(tmp_path):
    from akwarr.core.store import utcnow

    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    first = await store.create_job("episode", 12, "/media/ep-a.mkv")

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP INDEX IF EXISTS idx_jobs_active_kind_ref")
        await db.execute(
            "INSERT INTO jobs (kind, ref_id, status, dest_path, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
            ("episode", 12, JobStatus.PENDING, "/media/ep-c.mkv", utcnow(), utcnow()),
        )
        await db.commit()

    await store.init()

    jobs = await store.list_jobs()
    active = [job for job in jobs if job["status"] in store._ACTIVE_JOB_STATUSES]
    assert len(active) == 1
    assert active[0]["id"] == first


@pytest.mark.asyncio
async def test_list_retryable_failed_jobs_returns_failed_jobs_under_max_attempts(tmp_path):
    """Failed jobs past the retry interval and under max attempts are retryable."""
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    job_id = await store.create_job("episode", 50, "/media/ep.mkv")
    await store.update_job(job_id, status=JobStatus.FAILED, error="No download links")

    retryable = await store.list_retryable_failed_jobs(
        max_attempts=5,
        delay_seconds_for=lambda _job: 0,
    )
    assert len(retryable) == 1
    assert retryable[0]["id"] == job_id
    assert retryable[0]["retry_count"] == 0


@pytest.mark.asyncio
async def test_list_retryable_failed_jobs_excludes_jobs_at_max_attempts(tmp_path):
    """Jobs at max retry attempts are not retryable."""
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    job_id = await store.create_job("episode", 50, "/media/ep.mkv")
    await store.update_job(job_id, status=JobStatus.FAILED, error="No download links")
    for _ in range(5):
        await store.requeue_job(job_id)
        await store.update_job(job_id, status=JobStatus.FAILED, error="still failing")

    retryable = await store.list_retryable_failed_jobs(
        max_attempts=5,
        delay_seconds_for=lambda _job: 0,
    )
    assert retryable == []


@pytest.mark.asyncio
async def test_requeue_job_resets_to_pending_and_increments_retry_count(tmp_path):
    """requeue_job sets status=pending, clears error/gid/staging, bumps retry_count."""
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    job_id = await store.create_job("episode", 50, "/media/ep.mkv")
    await store.update_job(
        job_id,
        status=JobStatus.FAILED,
        error="No download links",
        aria2_gid="gid-x",
        staging_path="/tmp/staging.mkv",
    )

    await store.requeue_job(job_id)

    pending = await store.list_pending_jobs()
    assert len(pending) == 1
    assert pending[0]["id"] == job_id
    assert pending[0]["status"] == JobStatus.PENDING
    assert pending[0]["retry_count"] == 1
    assert pending[0]["error"] == ""
    assert pending[0]["aria2_gid"] is None
    assert pending[0]["staging_path"] is None


@pytest.mark.asyncio
async def test_list_retryable_failed_jobs_respects_retry_interval(tmp_path):
    """Jobs updated too recently are not retryable until the interval passes."""
    from datetime import UTC, datetime, timedelta

    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    job_id = await store.create_job("episode", 50, "/media/ep.mkv")
    recent = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE jobs SET status='failed', error='x', updated=? WHERE id=?",
            (recent, job_id),
        )
        await db.commit()

    retryable = await store.list_retryable_failed_jobs(
        max_attempts=5,
        delay_seconds_for=lambda _job: 60,
    )
    assert retryable == []

    retryable = await store.list_retryable_failed_jobs(
        max_attempts=5,
        delay_seconds_for=lambda _job: 5,
    )
    assert len(retryable) == 1


@pytest.mark.asyncio
async def test_has_blocking_job_detects_active_and_failed_jobs(tmp_path):
    store = Store(
        tmp_path / "akwarr.db",
        movies_path=tmp_path / "Movie" / "Arabic",
        series_path=tmp_path / "Serries" / "Arabic",
    )
    await store.init()

    job_id = await store.create_job("episode", 50, "/media/ep.mkv")
    assert await store.has_blocking_job("episode", 50) is True

    await store.update_job(job_id, status=JobStatus.COMPLETED)
    assert await store.has_blocking_job("episode", 50) is False

    await store.update_job(job_id, status=JobStatus.FAILED, error="temporary")
    assert await store.has_blocking_job("episode", 50) is True
