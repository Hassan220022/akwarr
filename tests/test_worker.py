import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from akwarr.config import Settings
from akwarr.core.store import JobStatus
from akwarr.core.worker import DownloadWorker
from akwarr.library.organizer import MediaOrganizer


class FakeAria2:
    async def tell_status(self, gid: str) -> dict:
        assert gid == "gid-1"
        return {
            "status": "complete",
            "files": [{"path": "/media/Download/akwarr-staging/actual/movie.mp4"}],
        }


class FakeStore:
    def __init__(self) -> None:
        self.updates: list[tuple[int, dict]] = []

    async def update_job(self, job_id: int, **kwargs) -> None:
        self.updates.append((job_id, kwargs))


class FakeEpisodeStartStore(FakeStore):
    async def get_episode(self, episode_id: int) -> dict:
        return {
            "id": episode_id,
            "series_id": 10,
            "season_number": 1,
            "episode_number": 2,
            "akwam_url": "https://akwam.it/episode/1/show/الحلقة-2",
        }


class FakeEpisodeScraper:
    async def episode_download_url(self, url: str) -> tuple[str, str]:
        return "720p", "https://cdn.example/show-s01e02.mp4"


class FakeEpisodeAria2:
    def __init__(self) -> None:
        self.adds: list[tuple[str, str, str]] = []

    async def add_uri(self, uri: str, dest_dir: str, filename: str) -> str:
        self.adds.append((uri, dest_dir, filename))
        return "gid-episode"


class FixedStagingOrganizer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def staging_dir(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path


class FakeProcessStore(FakeStore):
    def __init__(self, jobs: list[dict]) -> None:
        super().__init__()
        self.jobs = jobs
        self.started: list[int] = []
        self.checked: list[int] = []
        self.requeued: list[int] = []
        self.retryable: list[dict] = []

    async def list_pending_jobs(self) -> list[dict]:
        return self.jobs

    async def list_retryable_failed_jobs(self, *, retry_after_seconds: int, max_attempts: int) -> list[dict]:
        return self.retryable

    async def requeue_job(self, job_id: int) -> None:
        self.requeued.append(job_id)

    async def has_blocking_job(self, kind: str, ref_id: int) -> bool:
        return False

    async def list_series(self) -> list[dict]:
        return []


class ProcessWorker(DownloadWorker):
    async def _start_job(self, job: dict) -> None:
        self.store.started.append(job["id"])

    async def _check_download(self, job: dict) -> None:
        self.store.checked.append(job["id"])

    async def _import_job(self, job: dict) -> None:
        self.store.checked.append(job["id"])


@pytest.mark.asyncio
async def test_check_download_uses_aria2_actual_file_path_before_import() -> None:
    worker = object.__new__(DownloadWorker)
    worker.aria2 = FakeAria2()
    worker.store = FakeStore()

    await worker._check_download(
        {
            "id": 5,
            "aria2_gid": "gid-1",
            "staging_path": "/media/Download/akwarr-staging/job/movie.mp4",
        }
    )

    assert worker.store.updates == [
        (5, {"staging_path": "/media/Download/akwarr-staging/actual/movie.mp4"}),
        (
            5,
            {
                "status": JobStatus.IMPORTING,
                "staging_path": "/media/Download/akwarr-staging/actual/movie.mp4",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_process_jobs_respects_max_active_downloads() -> None:
    worker = object.__new__(ProcessWorker)
    worker.store = FakeProcessStore(
        [
            {"id": 1, "status": JobStatus.DOWNLOADING},
            {"id": 2, "status": JobStatus.PENDING},
            {"id": 3, "status": JobStatus.PENDING},
        ]
    )
    worker.settings = SimpleNamespace(
        max_active_downloads=2,
        retry_failed_after_seconds=3600,
        max_retry_attempts=5,
        monitor_missing_interval_seconds=3600,
        is_sonarr=True,
    )

    await worker._process_jobs()

    assert worker.store.checked == [1]
    assert worker.store.started == [2]
    assert worker.store.requeued == []


@pytest.mark.asyncio
async def test_requeue_failed_jobs_requeues_eligible_failed_jobs() -> None:
    """Failed jobs past the retry interval and under max attempts get requeued to pending."""
    worker = object.__new__(ProcessWorker)
    store = FakeProcessStore([])
    store.retryable = [
        {"id": 100, "status": JobStatus.FAILED, "retry_count": 1, "error": "No download links"},
        {"id": 101, "status": JobStatus.FAILED, "retry_count": 0, "error": "DNS error"},
    ]
    worker.store = store
    worker.settings = SimpleNamespace(
        max_active_downloads=2,
        retry_failed_after_seconds=3600,
        max_retry_attempts=5,
        monitor_missing_interval_seconds=3600,
        is_sonarr=True,
    )

    await worker._requeue_failed_jobs()

    assert worker.store.requeued == [100, 101]


@pytest.mark.asyncio
async def test_requeue_failed_jobs_skips_when_none_eligible() -> None:
    """No requeue when there are no retryable failed jobs."""
    worker = object.__new__(ProcessWorker)
    worker.store = FakeProcessStore([])
    worker.settings = SimpleNamespace(
        max_active_downloads=2,
        retry_failed_after_seconds=3600,
        max_retry_attempts=5,
        monitor_missing_interval_seconds=3600,
        is_sonarr=True,
    )

    await worker._requeue_failed_jobs()

    assert worker.store.requeued == []


@pytest.mark.asyncio
async def test_sync_missing_series_episodes_queues_new_episodes(tmp_path: Path) -> None:
    worker = object.__new__(DownloadWorker)
    worker.settings = SimpleNamespace(
        monitor_missing_interval_seconds=0,
        is_sonarr=True,
    )
    worker._last_release_scan = None
    worker.organizer = MediaOrganizer(
        Settings(
            mode="sonarr",
            movies_path=tmp_path / "movies",
            series_path=tmp_path / "series",
            staging_path=tmp_path / "staging",
            data_path=tmp_path / "config",
        )
    )

    class SyncStore:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, int, str]] = []

        async def list_series(self) -> list[dict]:
            return [
                {
                    "id": 7,
                    "title": "هي كيميا؟!",
                    "year": 2026,
                    "akwam_url": "https://akwam.it/series/5304/test",
                    "monitored": True,
                }
            ]

        async def list_episodes(self, series_id: int) -> list[dict]:
            assert series_id == 7
            return [{"id": 50, "season_number": 1, "episode_number": 1, "has_file": True}]

        async def upsert_episode(self, data: dict) -> dict:
            return {"id": 51, **data}

        async def has_blocking_job(self, kind: str, ref_id: int) -> bool:
            return False

        async def create_job(self, kind: str, ref_id: int, dest_path: str) -> int:
            self.jobs.append((kind, ref_id, dest_path))
            return 99

    class SyncScraper:
        async def fetch_metadata(self, url: str, *, kind: str):
            assert kind == "series"

            class Ep:
                season = 1
                number = 2
                title = "الحلقة 2"
                url = "https://akwam.it/episode/93876/test"

            class Meta:
                episodes = [Ep()]

            return Meta()

    worker.store = SyncStore()
    worker.scraper = SyncScraper()

    await worker._sync_missing_series_episodes()

    assert worker.store.jobs == [
        (
            "episode",
            51,
            str(
                worker.organizer.episode_plan(
                    series_title="هي كيميا؟!",
                    year=2026,
                    season=1,
                    episode=2,
                    episode_title="الحلقة 2",
                    quality="720p",
                ).video
            ),
        )
    ]


class LegacyStagingAria2:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def tell_status(self, gid: str) -> dict:
        return {
            "status": "active",
            "files": [{"path": "/media/arabic/.staging/old/movie.mp4"}],
        }

    async def remove(self, gid: str) -> None:
        self.removed.append(gid)


@pytest.mark.asyncio
async def test_check_download_requeues_legacy_staging_downloads() -> None:
    worker = object.__new__(DownloadWorker)
    worker.aria2 = LegacyStagingAria2()
    worker.store = FakeStore()
    worker.settings = SimpleNamespace(staging_path=Path("/media/Download/akwarr-staging"))

    await worker._check_download(
        {
            "id": 5,
            "aria2_gid": "gid-1",
            "staging_path": "/media/Download/akwarr-staging/old/movie.mp4",
        }
    )

    assert worker.aria2.removed == ["gid-1"]
    assert worker.store.updates == [
        (
            5,
            {
                "status": JobStatus.PENDING,
                "staging_path": "/media/Download/akwarr-staging/requeued/job-5/movie.mp4",
                "error": "requeued legacy staging download",
            },
        )
    ]


@pytest.mark.asyncio
async def test_start_episode_job_downloads_to_current_staging_root(tmp_path: Path) -> None:
    worker = object.__new__(DownloadWorker)
    worker.store = FakeEpisodeStartStore()
    worker.scraper = FakeEpisodeScraper()
    worker.aria2 = FakeEpisodeAria2()
    worker.organizer = FixedStagingOrganizer(tmp_path / "Download" / "akwarr-staging" / "job")

    await worker._start_job({"id": 9, "kind": "episode", "ref_id": 123})

    assert worker.aria2.adds == [
        (
            "https://cdn.example/show-s01e02.mp4",
            str(tmp_path / "Download" / "akwarr-staging" / "job"),
            "s1e2.mp4",
        )
    ]
    assert worker.store.updates == [
        (
            9,
            {
                "status": JobStatus.DOWNLOADING,
                "aria2_gid": "gid-episode",
                "staging_path": str(tmp_path / "Download" / "akwarr-staging" / "job" / "s1e2.mp4"),
                "error": "",
            },
        )
    ]


class FakeEpisodeImportStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.episode_file: tuple[int, str, bool] | None = None
        self.series_path: tuple[int, str] | None = None

    async def get_episode(self, episode_id: int) -> dict:
        return {
            "id": episode_id,
            "series_id": 7,
            "season_number": 1,
            "episode_number": 2,
            "title": "الحلقة 2",
        }

    async def get_series(self, series_id: int) -> dict:
        return {
            "id": series_id,
            "title": "سيد الناس",
            "original_title": "سيد الناس",
            "year": 2025,
            "tmdb_id": 2087914,
            "tvdb_id": 431975,
            "overview": "اختبار",
            "poster_url": None,
            "fanart_url": None,
            "path": None,
            "metadata": {
                "imdb_id": "tt32006014",
                "elcinema": {
                    "id": "2087914",
                    "url": "https://elcinema.com/work/2087914",
                    "title": "سيد الناس",
                },
            },
        }

    async def set_episode_file(self, episode_id: int, path: str, has_file: bool = True) -> None:
        self.episode_file = (episode_id, path, has_file)

    async def set_series_path(self, series_id: int, path: str) -> None:
        self.series_path = (series_id, path)


class FakeJellyfin:
    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def refresh_path(self, path: str) -> None:
        self.refreshed.append(path)


@pytest.mark.asyncio
async def test_import_episode_writes_jellyfin_metadata_under_series_root(tmp_path: Path) -> None:
    settings = Settings(
        series_path=tmp_path / "Serries" / "Arabic",
        staging_path=tmp_path / "Download" / "akwarr-staging",
        movies_path=tmp_path / "Movie" / "Arabic",
        data_path=tmp_path / "config",
        save_akwam_artwork=False,
    )
    staging_file = settings.staging_path / "episode.mp4"
    staging_file.parent.mkdir(parents=True)
    staging_file.write_text("video")

    worker = object.__new__(DownloadWorker)
    worker.store = FakeEpisodeImportStore()
    worker.organizer = MediaOrganizer(settings)
    worker.organizer.jellyfin = FakeJellyfin()

    await worker._import_episode({"id": 9, "kind": "episode", "ref_id": 123}, staging_file)

    final = settings.series_path / "سيد الناس (2025)" / "Season 01" / "سيد الناس - S01E02 - الحلقة 2 720p.mp4"
    assert final.read_text() == "video"
    tvshow_nfo = settings.series_path / "سيد الناس (2025)" / "tvshow.nfo"
    assert tvshow_nfo.exists()
    assert (settings.series_path / "سيد الناس (2025)" / "Season 01" / "S01E02.nfo").exists()
    root = ET.parse(tvshow_nfo).getroot()
    assert root.find("uniqueid[@type='tmdb'][@default='true']").text == "2087914"
    assert root.find("uniqueid[@type='imdb']").text == "tt32006014"
    assert root.find("uniqueid[@type='tvdb']").text == "431975"
    assert root.find("uniqueid[@type='elcinema']").text == "2087914"
    assert root.findtext("elcinemaurl") == "https://elcinema.com/work/2087914"
    assert worker.store.episode_file == (123, str(final), True)
    assert worker.store.series_path == (7, str(settings.series_path / "سيد الناس (2025)"))
    assert worker.organizer.jellyfin.refreshed == [str(final.parent)]
