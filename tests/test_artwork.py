from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from akwarr.library import artwork as art
from akwarr.library.metadata import MIN_ARTWORK_BYTES
from akwarr.scraper.akwam import is_valid_artwork_url


def _jpeg_bytes(size: int = MIN_ARTWORK_BYTES) -> bytes:
    payload = b"x" * max(size - 3, 0)
    return b"\xff\xd8\xff" + payload


def test_is_valid_artwork_url_rejects_site_logo() -> None:
    assert not is_valid_artwork_url("https://akwam.it/style/assets/images/logo-white.svg")
    assert is_valid_artwork_url("https://img.downet.net/uploads/BH0T9.jpg")


def test_series_artwork_paths() -> None:
    root = Path("/media/Serries/Arabic/Show (2026)")
    assert art.series_poster_path(root).name == "poster.jpg"
    assert art.series_fanart_path(root).name == "fanart.jpg"
    assert art.season_poster_path(root, 1).name == "season01-poster.jpg"
    assert art.episode_thumb_path(root / "Season 01" / "Show - S01E01 720p.mp4").name.endswith(
        "-thumb.jpg"
    )


def test_movie_artwork_paths() -> None:
    root = Path("/media/Movie/Arabic/Movie (2026)")
    assert art.movie_poster_path(root).name == "poster.jpg"
    assert art.movie_fanart_path(root).name == "fanart.jpg"


def test_is_valid_local_image_rejects_small_and_svg(tmp_path: Path) -> None:
    small = tmp_path / "small.jpg"
    small.write_bytes(b"\xff\xd8\xffabc")
    ok, reason = art.is_valid_local_image(small)
    assert not ok
    assert "too small" in reason

    svg = tmp_path / "logo.jpg"
    svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" + b"x" * MIN_ARTWORK_BYTES)
    ok, reason = art.is_valid_local_image(svg)
    assert not ok
    assert "svg" in reason

    good = tmp_path / "good.jpg"
    good.write_bytes(_jpeg_bytes())
    ok, _ = art.is_valid_local_image(good)
    assert ok


def test_poster_matches_episode_thumb_by_md5(tmp_path: Path) -> None:
    series = tmp_path / "Show"
    season = series / "Season 01"
    season.mkdir(parents=True)
    thumb = season / "Show - S01E01 720p-thumb.jpg"
    poster = series / "poster.jpg"
    thumb.write_bytes(_jpeg_bytes())
    poster.write_bytes(thumb.read_bytes())
    is_thumb, name = art.poster_matches_episode_thumb(series)
    assert is_thumb
    assert name == thumb.name


@pytest.mark.asyncio
async def test_ensure_series_artwork_downloads_missing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_folder = tmp_path / "Show (2026)"
    series_folder.mkdir()
    poster_bytes = _jpeg_bytes()
    fanart_bytes = _jpeg_bytes(MIN_ARTWORK_BYTES + 1000)

    async def fake_download(url: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "poster" in url:
            dest.write_bytes(poster_bytes)
        else:
            dest.write_bytes(fanart_bytes)
        return True

    monkeypatch.setattr(art.meta, "download_image", fake_download)

    await art.ensure_series_artwork(
        series_folder,
        season=1,
        poster_url="https://cdn.example/poster.jpg",
        fanart_url="https://cdn.example/fanart.jpg",
    )

    assert art.series_poster_path(series_folder).read_bytes() == poster_bytes
    assert art.series_fanart_path(series_folder).read_bytes() == fanart_bytes
    assert art.series_folder_art_path(series_folder).read_bytes() == poster_bytes
    assert art.season_poster_path(series_folder, 1).read_bytes() == poster_bytes


@pytest.mark.asyncio
async def test_resolve_show_artwork_urls_prefers_tmdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmdb = AsyncMock()
    tmdb.enabled = True
    tmdb.tv = AsyncMock(
        return_value={
            "poster_path": "/abc.jpg",
            "backdrop_path": "/def.jpg",
        }
    )

    poster, fanart = await art.resolve_show_artwork_urls(
        poster_url="https://akwam.it/style/assets/images/logo-white.svg",
        fanart_url="https://img.downet.net/uploads/BH0T9.jpg",
        tmdb_id=302546,
        akwam_url="https://akwam.it/series/example",
        tmdb=tmdb,
        scraper=None,
    )

    assert poster == "https://image.tmdb.org/t/p/original/abc.jpg"
    assert fanart == "https://image.tmdb.org/t/p/original/def.jpg"
    tmdb.tv.assert_awaited_once_with(302546)


@pytest.mark.asyncio
async def test_resolve_show_artwork_urls_uses_akwam_only_when_tmdb_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmdb = AsyncMock()
    tmdb.enabled = True
    tmdb.tv = AsyncMock(return_value={"poster_path": None, "backdrop_path": None})

    scraper = AsyncMock()
    scraper.fetch_metadata = AsyncMock(
        return_value=type(
            "Meta",
            (),
            {
                "poster": "https://img.downet.net/uploads/poster.jpg",
                "fanart": "https://img.downet.net/uploads/fanart.jpg",
            },
        )()
    )

    poster, fanart = await art.resolve_show_artwork_urls(
        poster_url=None,
        fanart_url=None,
        tmdb_id=1,
        akwam_url="https://akwam.it/series/example",
        tmdb=tmdb,
        scraper=scraper,
    )

    assert poster == "https://img.downet.net/uploads/poster.jpg"
    assert fanart == "https://img.downet.net/uploads/fanart.jpg"


@pytest.mark.asyncio
async def test_ensure_image_skips_invalid_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = False

    async def fake_download(url: str, dest: Path) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(art.meta, "download_image", fake_download)

    assert not await art.ensure_image("https://akwam.it/style/assets/images/logo-white.svg", tmp_path / "x.jpg")
    assert not called


@pytest.mark.asyncio
async def test_ensure_image_rejects_episode_thumb_poster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = tmp_path / "Show"
    season = series / "Season 01"
    season.mkdir(parents=True)
    thumb_bytes = _jpeg_bytes()
    (season / "Show - S01E01-thumb.jpg").write_bytes(thumb_bytes)

    async def fake_download(url: str, dest: Path) -> bool:
        dest.write_bytes(thumb_bytes)
        return True

    monkeypatch.setattr(art.meta, "download_image", fake_download)

    ok = await art.ensure_image(
        "https://cdn.example/poster.jpg",
        series / "poster.jpg",
        series_folder=series,
    )
    assert not ok
    assert not (series / "poster.jpg").exists()


@pytest.mark.asyncio
async def test_ensure_image_replaces_invalid_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "poster.jpg"
    dest.write_bytes(b"tiny")

    good = _jpeg_bytes()

    async def fake_download(url: str, dest: Path) -> bool:
        dest.write_bytes(good)
        return True

    monkeypatch.setattr(art.meta, "download_image", fake_download)

    assert await art.ensure_image("https://cdn.example/poster.jpg", dest)
    assert dest.read_bytes() == good
