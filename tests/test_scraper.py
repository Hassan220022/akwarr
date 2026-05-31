import json
from pathlib import Path

import pytest

from akwarr.config import Settings
from akwarr.scraper.akwam import AkwamScraper
from akwarr.scraper.elcinema import ElCinemaScraper
from akwarr.scraper.flaresolverr import FetchResponse


class FakeFetcher:
    async def get(self, target_url: str) -> FetchResponse:
        html = """
        <html>
          <body>
            <h1>مسلسل تجريبي</h1>
            <div class="bg-primary2">
              <h2><a href="/series/episode-1">مسلسل تجريبي الموسم الثاني الحلقة ١</a></h2>
            </div>
            <div class="bg-primary2">
              <h2><a href="/series/episode-2">مسلسل تجريبي الموسم 2 الحلقة 2</a></h2>
            </div>
          </body>
        </html>
        """
        return FetchResponse(text=html, status_code=200, url=target_url)


class FakeSearchFetcher:
    async def get(self, target_url: str) -> FetchResponse:
        html = """
        <html>
          <body>
            <a href="/movie/3082/%D9%88%D9%84%D8%A7%D8%AF-%D8%B1%D8%B2%D9%82-11">مشاهدة</a>
            <a href="/movie/3082/%D9%88%D9%84%D8%A7%D8%AF-%D8%B1%D8%B2%D9%82-11">ولاد رزق</a>
          </body>
        </html>
        """
        return FetchResponse(text=html, status_code=200, url=target_url)


class FakeElCinemaFetcher:
    async def get(self, target_url: str) -> FetchResponse:
        html = json.dumps(
            [
                """
                <div data-url="/work/2054468" data-entity="Work" data-text="الفيل الأزرق 2">
                  <div class="left" dir="rtl">الفيل الأزرق 2</div>
                  <div class="right" dir="ltr">The Blue Elephant 2</div>
                  <ul class="list-separator"><li>فيلم</li><li>تم عرضه</li><li>2019</li></ul>
                </div>
                """,
                """
                <div data-url="/work/2019662" data-entity="Work" data-text="الفيل الأزرق">
                  <div class="left" dir="rtl">الفيل الأزرق</div>
                  <div class="right" dir="ltr">The Blue Elephant</div>
                  <ul class="list-separator"><li>فيلم</li><li>تم عرضه</li><li>2014</li></ul>
                </div>
                """,
                '<div data-url="/video/1" data-entity="video" data-text="Trailer">ignored</div>',
            ]
        )
        return FetchResponse(text=html, status_code=200, url=target_url)


class FakeShortenerFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, target_url: str) -> FetchResponse:
        self.urls.append(target_url)
        if "/link/" in target_url:
            html = """
            <html>
              <body>
                <a href="https://akwam.it/">Home</a>
                <a href="/download/179594/11140/%D8%A7%D9%84%D8%B3%D8%AA"
                   class="download-link"
                   style="font-size: 20px;color: #a3aaae;">
                  <span style="color: #ff5e1d;">Click here</span> to go for your link ...
                </a>
              </body>
            </html>
            """
            return FetchResponse(text=html, status_code=200, url=target_url)
        if "cdn.example.test" in target_url:
            raise AssertionError("direct media URL should not be fetched by Akwarr")
        return FetchResponse(
            text="https://cdn.example.test/direct/%D8%A7%D9%84%D8%B3%D8%AA.mp4",
            status_code=200,
            url=target_url,
        )


@pytest.mark.asyncio
async def test_series_metadata_parses_arabic_episode_and_season_numbers(tmp_path: Path) -> None:
    settings = Settings(
        mode="sonarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )
    scraper = AkwamScraper(settings)
    scraper.fetcher = FakeFetcher()  # type: ignore[assignment]

    metadata = await scraper.fetch_metadata("https://akwam.it/series/test", kind="series")

    assert [(episode.season, episode.number) for episode in metadata.episodes] == [(2, 1), (2, 2)]


@pytest.mark.asyncio
async def test_search_deduplicates_generic_watch_links(tmp_path: Path) -> None:
    settings = Settings(
        mode="radarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )
    scraper = AkwamScraper(settings)
    scraper.fetcher = FakeSearchFetcher()  # type: ignore[assignment]

    results = await scraper.search("ولاد رزق", section="movie")

    assert len(results) == 1
    assert results[0].title == "ولاد رزق"


@pytest.mark.asyncio
async def test_elcinema_search_extracts_arabic_work_titles_and_prioritizes_year(tmp_path: Path) -> None:
    settings = Settings(
        mode="radarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )
    scraper = ElCinemaScraper(settings)
    scraper.fetcher = FakeElCinemaFetcher()  # type: ignore[assignment]

    results = await scraper.search("The Blue Elephant", kind="movie")
    candidates = await scraper.arabic_candidates("The Blue Elephant", year=2014, kind="movie")

    assert [result.title for result in results] == ["الفيل الأزرق 2", "الفيل الأزرق"]
    assert results[1].english_title == "The Blue Elephant"
    assert results[1].url == "https://elcinema.com/work/2019662"
    assert candidates[0] == "الفيل الأزرق"


@pytest.mark.asyncio
async def test_resolve_direct_url_follows_akwam_shortener_download_link(tmp_path: Path) -> None:
    settings = Settings(
        mode="radarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )
    scraper = AkwamScraper(settings)
    fetcher = FakeShortenerFetcher()
    scraper.fetcher = fetcher  # type: ignore[assignment]

    direct = await scraper.resolve_direct_url("https://akwam.it/link/11140")

    assert direct == "https://cdn.example.test/direct/%D8%A7%D9%84%D8%B3%D8%AA.mp4"
    assert fetcher.urls == [
        "https://akwam.it/link/11140",
        "https://akwam.it/download/179594/11140/%D8%A7%D9%84%D8%B3%D8%AA",
    ]


@pytest.mark.asyncio
async def test_resolve_direct_url_returns_direct_media_url_without_fetching_it(tmp_path: Path) -> None:
    settings = Settings(
        mode="radarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )
    scraper = AkwamScraper(settings)
    fetcher = FakeShortenerFetcher()
    scraper.fetcher = fetcher  # type: ignore[assignment]

    direct = await scraper.resolve_direct_url("https://cdn.example.test/direct/movie.mp4")

    assert direct == "https://cdn.example.test/direct/movie.mp4"
    assert fetcher.urls == []
