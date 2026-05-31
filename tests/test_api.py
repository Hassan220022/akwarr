from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from akwarr.api import queue as queue_module
from akwarr.api import radarr, sonarr
from akwarr.api.admin import _download_progress, _validate_akwam_url
from akwarr.api.radarr import app as radarr_app
from akwarr.api.sonarr import app as sonarr_app
from akwarr.config import get_settings
from akwarr.core import tmdb as tmdb_module
from akwarr.core.store import JobStatus


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "AKWARR_API_KEY",
        "TMDB_API_KEY",
        "JELLYFIN_API_KEY",
        "JELLYFIN_URL",
        "JELLYSEERR_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_system_status() -> None:
    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/system/status")
        assert r.status_code == 200
        data = r.json()
        assert data["appName"] == "Akwarr Radarr"


def test_download_progress_calculates_percent_speed_and_eta() -> None:
    progress = _download_progress(
        {
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "50",
        }
    )

    assert progress == {
        "aria2Status": "active",
        "progressBytes": 250,
        "totalBytes": 1000,
        "downloadSpeed": 50,
        "progressPercent": 25.0,
        "etaSeconds": 15,
    }


def test_validate_akwam_url_allows_shortener_subdomain() -> None:
    _validate_akwam_url("http://go.akwam.it/link/162163", "https://akwam.it")


def test_validate_akwam_url_rejects_non_akwam_host() -> None:
    with pytest.raises(Exception):
        _validate_akwam_url("https://example.com/link/162163", "https://akwam.it")


@pytest.mark.asyncio
async def test_rootfolder() -> None:
    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/rootfolder")
        assert r.status_code == 200
        folders = r.json()
        assert len(folders) == 1
        assert folders[0]["path"] == "/media/Movie/Arabic"


@pytest.mark.asyncio
async def test_sonarr_tag_endpoint_is_jellyseerr_compatible() -> None:
    transport = ASGITransport(app=sonarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/tag")

    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_tmdb_lookup_tv_resolves_tvdb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "tv_results": [
                    {
                        "id": 249826,
                        "name": "Baba Almgal",
                        "original_name": "بابا المجال",
                        "first_air_date": "2023-03-23",
                        "overview": "Arabic series",
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setenv("TMDB_API_KEY", "tmdb-key")
    get_settings.cache_clear()
    monkeypatch.setattr(tmdb_module.httpx, "AsyncClient", FakeAsyncClient)

    results = await tmdb_module.TMDBClient(get_settings()).lookup_tv("tvdb:431975")

    assert captured["url"].endswith("/find/431975")
    assert captured["params"]["external_source"] == "tvdb_id"
    assert results[0]["tmdbId"] == 249826
    assert results[0]["tvdbId"] == 431975
    assert results[0]["title"] == "Baba Almgal"


@pytest.mark.asyncio
async def test_radarr_accepts_jellyseerr_query_api_key_and_camel_quality_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/qualityProfile?apikey=secret")

    assert r.status_code == 200
    assert r.json()[0]["name"] == "Arabic 720p"


@pytest.mark.asyncio
async def test_radarr_queue_endpoint_is_jellyseerr_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyStore:
        async def list_pending_jobs(self):
            return []

    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(radarr, "store", EmptyStore())

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/queue?apikey=secret&includeEpisode=true")

    assert r.status_code == 200
    assert r.json() == {
        "page": 1,
        "pageSize": 20,
        "sortKey": "timeleft",
        "sortDirection": "ascending",
        "totalRecords": 0,
        "records": [],
    }


@pytest.mark.asyncio
async def test_sonarr_accepts_jellyseerr_query_api_key_and_camel_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()

    transport = ASGITransport(app=sonarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        quality = await client.get("/api/v3/qualityProfile?apikey=secret")
        language = await client.get("/api/v3/languageProfile?apikey=secret")

    assert quality.status_code == 200
    assert quality.json()[0]["name"] == "Arabic 720p"
    assert language.status_code == 200
    assert language.json()[0]["name"] == "Arabic"


@pytest.mark.asyncio
async def test_sonarr_queue_endpoint_is_jellyseerr_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyStore:
        async def list_pending_jobs(self):
            return []

    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(sonarr, "store", EmptyStore())

    transport = ASGITransport(app=sonarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/queue?apikey=secret&includeEpisode=true")

    assert r.status_code == 200
    assert r.json()["records"] == []
    assert r.json()["totalRecords"] == 0


@pytest.mark.asyncio
async def test_radarr_queue_reports_movie_download_eta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def list_pending_jobs(self):
            return [
                {
                    "id": 7,
                    "kind": "movie",
                    "ref_id": 4,
                    "status": JobStatus.DOWNLOADING,
                    "aria2_gid": "gid-movie",
                    "staging_path": "/media/Download/akwarr-staging/job/movie.mp4",
                    "dest_path": "/media/Movie/Arabic/الفيل الأزرق/الفيل الأزرق 720p.mp4",
                    "error": "",
                }
            ]

        async def get_movie(self, movie_id):
            return {
                "id": movie_id,
                "title": "الفيل الأزرق",
                "year": 2014,
                "tmdb_id": 289510,
            }

    class FakeAria2Client:
        def __init__(self, settings):
            pass

        async def tell_status(self, gid):
            assert gid == "gid-movie"
            return {
                "status": "active",
                "totalLength": "1000",
                "completedLength": "250",
                "downloadSpeed": "50",
            }

    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(radarr, "store", FakeStore())
    monkeypatch.setattr(radarr, "Aria2Client", FakeAria2Client)
    monkeypatch.setattr(queue_module, "_utcnow", lambda: datetime(2026, 5, 31, 18, 0, tzinfo=UTC))

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/queue?apikey=secret")

    assert r.status_code == 200
    data = r.json()
    assert data["totalRecords"] == 1
    record = data["records"][0]
    assert record["title"] == "الفيل الأزرق (2014)"
    assert record["status"] == "downloading"
    assert record["downloadId"] == "gid-movie"
    assert record["movieId"] == 4
    assert record["tmdbId"] == 289510
    assert record["size"] == 1000
    assert record["sizeleft"] == 750
    assert record["timeleft"] == "00:00:15"
    assert record["estimatedCompletionTime"] == "2026-05-31T18:00:15+00:00"


@pytest.mark.asyncio
async def test_sonarr_queue_reports_episode_download_eta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def list_pending_jobs(self):
            return [
                {
                    "id": 9,
                    "kind": "episode",
                    "ref_id": 12,
                    "status": JobStatus.DOWNLOADING,
                    "aria2_gid": "gid-episode",
                    "staging_path": "/media/Download/akwarr-staging/job/s1e2.mp4",
                    "dest_path": "/media/Serries/Arabic/بابا المجال (2023)/Season 01/بابا المجال - S01E02.mp4",
                    "error": "",
                }
            ]

        async def get_episode(self, episode_id):
            return {
                "id": episode_id,
                "series_id": 5,
                "season_number": 1,
                "episode_number": 2,
                "title": "الحلقة 2",
            }

        async def get_series(self, series_id):
            return {
                "id": series_id,
                "title": "بابا المجال",
                "year": 2023,
                "tmdb_id": 218323,
                "tvdb_id": 431975,
            }

    class FakeAria2Client:
        def __init__(self, settings):
            pass

        async def tell_status(self, gid):
            assert gid == "gid-episode"
            return {
                "status": "active",
                "totalLength": "1200",
                "completedLength": "600",
                "downloadSpeed": "100",
            }

    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(sonarr, "store", FakeStore())
    monkeypatch.setattr(sonarr, "Aria2Client", FakeAria2Client)
    monkeypatch.setattr(queue_module, "_utcnow", lambda: datetime(2026, 5, 31, 18, 0, tzinfo=UTC))

    transport = ASGITransport(app=sonarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/queue?apikey=secret&includeEpisode=true")

    assert r.status_code == 200
    record = r.json()["records"][0]
    assert record["title"] == "بابا المجال - S01E02"
    assert record["status"] == "downloading"
    assert record["seriesId"] == 5
    assert record["episodeId"] == 12
    assert record["seasonNumber"] == 1
    assert record["episodeNumber"] == 2
    assert record["series"]["tvdbId"] == 431975
    assert record["sizeleft"] == 600
    assert record["timeleft"] == "00:00:06"
    assert record["estimatedCompletionTime"] == "2026-05-31T18:00:06+00:00"


@pytest.mark.asyncio
async def test_admin_ui_is_served_and_api_key_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/ui")
        allowed = await client.get("/ui?apikey=secret")

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert "Akwarr Monitor" in allowed.text
    assert "/api/v3/akwam/search" in allowed.text


@pytest.mark.asyncio
async def test_admin_ui_does_not_append_empty_api_key_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ui?apikey=secret")

    assert response.status_code == 200
    assert "const trustedLanHost = location.hostname === 'akwam.mikawi.org';" in response.text
    assert "if (trustedLanHost) return path;" in response.text
    assert "if (!apiKey) return path;" in response.text
    assert "apikey=${encodeURIComponent(key())}" not in response.text
    assert "/api/v3/elcinema/search" in response.text


@pytest.mark.asyncio
async def test_elcinema_search_endpoint_returns_arabic_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    get_settings.cache_clear()

    class FakeElCinemaScraper:
        def __init__(self, settings):
            pass

        async def search(self, term, *, kind):
            return [
                {
                    "title": "الفيل الأزرق",
                    "url": "https://elcinema.com/work/2019662",
                    "kind": kind,
                    "year": "2014",
                    "english_title": "The Blue Elephant",
                    "poster": None,
                }
            ]

        async def arabic_candidates(self, *queries, year, kind):
            return ["الفيل الأزرق"]

    monkeypatch.setattr("akwarr.api.admin.ElCinemaScraper", FakeElCinemaScraper)

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/api/v3/elcinema/search?apikey=secret&term=The%20Blue%20Elephant&kind=movie&year=2014"
        )

    assert r.status_code == 200
    assert r.json()["candidates"] == ["الفيل الأزرق"]
    assert r.json()["results"][0]["title"] == "الفيل الأزرق"


@pytest.mark.asyncio
async def test_radarr_add_movie_uses_elcinema_arabic_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, list[str] | None] = {"alt_queries": None}

    class FakeStore:
        async def add_movie(self, data):
            return {"id": 1, "path": None, "added": None, **data}

    class FakeTMDBClient:
        def __init__(self, settings):
            pass

        async def movie(self, tmdb_id):
            return {
                "title": "The Blue Elephant",
                "original_title": "The Blue Elephant",
                "year": 2014,
                "overview": "",
                "poster_path": None,
                "backdrop_path": None,
            }

        @staticmethod
        def poster_url(path):
            return None

    class FakeAkwamScraper:
        def __init__(self, settings):
            pass

        async def best_match(self, query, *, section, alt_queries):
            captured["alt_queries"] = alt_queries
            return SimpleNamespace(url="https://akwam.it/movie/test", poster=None)

        async def fetch_metadata(self, url, *, kind):
            return SimpleNamespace(poster=None, fanart=None, overview=None)

    class FakeElCinemaScraper:
        def __init__(self, settings):
            pass

        async def arabic_candidates(self, *queries, year, kind):
            return ["الفيل الأزرق"]

    monkeypatch.setenv("MOVIES_PATH", str(tmp_path / "movies"))
    get_settings.cache_clear()
    monkeypatch.setattr(radarr, "store", FakeStore())
    monkeypatch.setattr(radarr, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(radarr, "AkwamScraper", FakeAkwamScraper)
    monkeypatch.setattr(radarr, "ElCinemaScraper", FakeElCinemaScraper)

    await radarr.add_movie(
        radarr.MovieAddBody(
            title="The Blue Elephant",
            tmdbId=2019662,
            rootFolderPath=str(tmp_path / "movies"),
            addOptions={"searchForMovie": False},
        )
    )

    assert captured["alt_queries"] == ["الفيل الأزرق", "The Blue Elephant"]


@pytest.mark.asyncio
async def test_sonarr_add_series_uses_elcinema_arabic_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, list[str] | None] = {"alt_queries": None}

    class FakeStore:
        async def add_series(self, data):
            return {"id": 1, "path": None, "added": None, **data}

        async def list_episodes(self, series_id):
            return []

    class FakeTMDBClient:
        def __init__(self, settings):
            pass

        async def tv(self, tmdb_id):
            return {
                "title": "Paranormal",
                "original_title": "Paranormal",
                "year": 2020,
                "overview": "",
                "poster_path": None,
                "backdrop_path": None,
            }

        @staticmethod
        def poster_url(path):
            return None

    class FakeAkwamScraper:
        def __init__(self, settings):
            pass

        async def best_match(self, query, *, section, alt_queries):
            captured["alt_queries"] = alt_queries
            return SimpleNamespace(url="https://akwam.it/series/test", poster=None)

        async def fetch_metadata(self, url, *, kind):
            return SimpleNamespace(poster=None, fanart=None, overview=None, episodes=[])

    class FakeElCinemaScraper:
        def __init__(self, settings):
            pass

        async def arabic_candidates(self, *queries, year, kind):
            return ["ما وراء الطبيعة"]

    monkeypatch.setenv("SERIES_PATH", str(tmp_path / "series"))
    get_settings.cache_clear()
    monkeypatch.setattr(sonarr, "store", FakeStore())
    monkeypatch.setattr(sonarr, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(sonarr, "AkwamScraper", FakeAkwamScraper)
    monkeypatch.setattr(sonarr, "ElCinemaScraper", FakeElCinemaScraper)

    await sonarr.add_series(
        sonarr.SeriesAddBody(
            title="Paranormal",
            tmdbId=2058052,
            rootFolderPath=str(tmp_path / "series"),
            addOptions={"searchForMissingEpisodes": False},
        )
    )

    assert captured["alt_queries"] == ["ما وراء الطبيعة", "Paranormal"]


@pytest.mark.asyncio
async def test_sonarr_add_series_accepts_jellyseerr_tvdb_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class FakeStore:
        async def add_series(self, data):
            captured["series"] = data
            return {"id": 1, "path": None, "added": None, **data}

        async def list_episodes(self, series_id):
            return []

    class FakeTMDBClient:
        def __init__(self, settings):
            pass

        async def tv_from_tvdb(self, tvdb_id):
            captured["tvdb_id"] = tvdb_id
            return {
                "id": 249826,
                "title": "Baba Almgal",
                "original_title": "بابا المجال",
                "year": 2023,
                "overview": "",
                "poster_path": None,
                "backdrop_path": None,
            }

        @staticmethod
        def poster_url(path):
            return None

    class FakeAkwamScraper:
        def __init__(self, settings):
            pass

        async def best_match(self, query, *, section, alt_queries):
            return None

    class FakeElCinemaScraper:
        def __init__(self, settings):
            pass

        async def arabic_candidates(self, *queries, year, kind):
            return []

    series_root = tmp_path / "Serries" / "Arabic"
    monkeypatch.setenv("SERIES_PATH", str(series_root))
    get_settings.cache_clear()
    monkeypatch.setattr(sonarr, "store", FakeStore())
    monkeypatch.setattr(sonarr, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(sonarr, "AkwamScraper", FakeAkwamScraper)
    monkeypatch.setattr(sonarr, "ElCinemaScraper", FakeElCinemaScraper)

    body = sonarr.SeriesAddBody.model_validate(
        {
            "title": "Baba Almgal",
            "tvdbid": 431975,
            "profileId": 6,
            "rootFolderPath": "/data/Serries/English",
            "searchNow": False,
            "tags": [1, 7, 6],
        }
    )

    payload = await sonarr.add_series(body)

    assert captured["tvdb_id"] == 431975
    assert captured["series"]["tmdb_id"] == 249826
    assert captured["series"]["tvdb_id"] == 431975
    assert captured["series"]["quality_profile_id"] == 6
    assert captured["series"]["root_folder_path"] == str(series_root)
    assert payload["tmdbId"] == 249826
    assert payload["rootFolderPath"] == str(series_root)


@pytest.mark.asyncio
async def test_monitor_files_reports_arabic_media_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    movie_file = movies / "فيلم" / "فيلم 720p.mkv"
    series_file = series / "مسلسل" / "Season 01" / "مسلسل - S01E01.mkv"
    movie_file.parent.mkdir(parents=True)
    series_file.parent.mkdir(parents=True)
    movie_file.write_text("movie")
    series_file.write_text("series")

    monkeypatch.setenv("AKWARR_API_KEY", "secret")
    monkeypatch.setenv("MOVIES_PATH", str(movies))
    monkeypatch.setenv("SERIES_PATH", str(series))
    get_settings.cache_clear()

    transport = ASGITransport(app=radarr_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v3/monitor/files?apikey=secret")

    assert r.status_code == 200
    data = r.json()
    assert data["movies"][0]["path"].endswith("فيلم 720p.mkv")
    assert data["series"][0]["path"].endswith("مسلسل - S01E01.mkv")
