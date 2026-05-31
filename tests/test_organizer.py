from pathlib import Path

import pytest

from akwarr.config import Settings
from akwarr.library.organizer import MediaOrganizer


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="radarr",
        movies_path=tmp_path / "movies",
        series_path=tmp_path / "series",
        staging_path=tmp_path / "staging",
        data_path=tmp_path / "config",
    )


def test_movie_folder_naming(settings: Settings) -> None:
    org = MediaOrganizer(settings)
    plan = org.movie_plan(title="الست", year=2026, quality="720p")
    assert plan.folder.name == "الست (2026)"
    assert plan.video.name == "الست (2026) 720p.mkv"
    assert plan.nfo.name == "movie.nfo"
    assert plan.poster.name == "poster.jpg"


def test_episode_folder_naming(settings: Settings) -> None:
    org = MediaOrganizer(settings)
    plan = org.episode_plan(
        series_title="اللعبة",
        year=2025,
        season=1,
        episode=3,
        episode_title="حلقة 3",
        quality="720p",
    )
    assert "Season 01" in str(plan.folder)
    assert plan.video.name.startswith("اللعبة - S01E03")


def test_sanitize_invalid_chars(settings: Settings) -> None:
    org = MediaOrganizer(settings)
    plan = org.movie_plan(title='Bad:Title/Name', year=2020, quality="720p")
    assert ":" not in plan.folder.name
    assert "/" not in plan.folder.name


def test_move_file_keeps_import_when_mount_rejects_chmod(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = MediaOrganizer(settings)
    src = settings.staging_path / "movie.mp4"
    dest = settings.movies_path / "Movie" / "Movie.mp4"
    src.parent.mkdir(parents=True)
    src.write_text("video")

    original_chmod = Path.chmod

    def chmod(self: Path, mode: int) -> None:
        if self == dest:
            raise PermissionError("chmod blocked by mount")
        original_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", chmod)

    org._move_file(src, dest)

    assert dest.read_text() == "video"
    assert not src.exists()
