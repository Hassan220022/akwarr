from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from akwarr.library import artwork as art
from akwarr.scraper.akwam import is_valid_artwork_url


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


@pytest.mark.asyncio
async def test_ensure_series_artwork_downloads_missing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_folder = tmp_path / "Show (2026)"
    series_folder.mkdir()
    poster_bytes = b"poster-image"
    fanart_bytes = b"fanart-image"

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
async def test_resolve_show_artwork_urls_falls_back_to_tmdb(
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
        akwam_url=None,
        tmdb=tmdb,
        scraper=None,
    )

    assert poster == "https://image.tmdb.org/t/p/original/abc.jpg"
    assert fanart == "https://img.downet.net/uploads/BH0T9.jpg"


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
