import xml.etree.ElementTree as ET
from pathlib import Path

from akwarr.library import metadata as meta


def test_write_movie_nfo(tmp_path: Path) -> None:
    nfo = tmp_path / "movie.nfo"
    meta.write_movie_nfo(
        nfo,
        title="الست",
        original_title="Al Set",
        year=2026,
        tmdb_id=12345,
        language="ar",
    )
    text = nfo.read_text(encoding="utf-8")
    assert "الست" in text
    assert "12345" in text
    assert "tmdb" in text
    root = ET.parse(nfo).getroot()
    assert root.tag == "movie"
    assert root.findtext("title") == "الست"
    assert root.findtext("language") == "ar"
    assert root.find("uniqueid[@type='tmdb'][@default='true']").text == "12345"


def test_write_tvshow_nfo(tmp_path: Path) -> None:
    nfo = tmp_path / "tvshow.nfo"
    meta.write_tvshow_nfo(
        nfo,
        title="اللعبة",
        original_title=None,
        year=2025,
        tmdb_id=999,
    )
    assert nfo.exists()
    assert "999" in nfo.read_text(encoding="utf-8")
    root = ET.parse(nfo).getroot()
    assert root.tag == "tvshow"
    assert root.find("uniqueid[@type='tmdb'][@default='true']").text == "999"
