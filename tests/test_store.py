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
    assert episode_path == str(
        tmp_path / "Serries" / "Arabic" / "Show" / "Season 01" / "Show - S01E01.mkv"
    )
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
    assert [(job["kind"], job["ref_id"], job["status"]) for job in jobs] == [
        ("episode", 12, JobStatus.PENDING)
    ]


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
