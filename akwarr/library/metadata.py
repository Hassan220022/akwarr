"""NFO and sidecar artwork for Jellyfin."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import httpx

from akwarr.scraper.akwam import is_valid_artwork_url


def _indent(elem: ET.Element, level: int = 0) -> None:
    pad = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = pad
    elif level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def write_movie_nfo(
    path: Path,
    *,
    title: str,
    original_title: str | None,
    year: int | None,
    tmdb_id: int,
    imdb_id: str | None = None,
    elcinema_id: str | None = None,
    elcinema_url: str | None = None,
    elcinema_title: str | None = None,
    overview: str | None = None,
    language: str = "ar",
    poster_file: str | None = None,
    fanart_file: str | None = None,
) -> None:
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = title
    if original_title:
        ET.SubElement(root, "originaltitle").text = original_title
    if year:
        ET.SubElement(root, "year").text = str(year)
    if overview:
        ET.SubElement(root, "plot").text = overview
    ET.SubElement(root, "language").text = language
    uid = ET.SubElement(root, "uniqueid", attrib={"type": "tmdb", "default": "true"})
    uid.text = str(tmdb_id)
    if imdb_id:
        imdb = ET.SubElement(root, "uniqueid", attrib={"type": "imdb"})
        imdb.text = imdb_id
    _append_elcinema(root, elcinema_id=elcinema_id, elcinema_url=elcinema_url, elcinema_title=elcinema_title)
    _append_local_art(root, poster_file=poster_file, fanart_file=fanart_file)
    _indent(root)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_tvshow_nfo(
    path: Path,
    *,
    title: str,
    original_title: str | None,
    year: int | None,
    tmdb_id: int,
    imdb_id: str | None = None,
    tvdb_id: int | None = None,
    elcinema_id: str | None = None,
    elcinema_url: str | None = None,
    elcinema_title: str | None = None,
    overview: str | None = None,
    language: str = "ar",
    poster_file: str | None = None,
    fanart_file: str | None = None,
) -> None:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = title
    if original_title:
        ET.SubElement(root, "originaltitle").text = original_title
    if year:
        ET.SubElement(root, "year").text = str(year)
    if overview:
        ET.SubElement(root, "plot").text = overview
    ET.SubElement(root, "language").text = language
    uid = ET.SubElement(root, "uniqueid", attrib={"type": "tmdb", "default": "true"})
    uid.text = str(tmdb_id)
    if imdb_id:
        imdb = ET.SubElement(root, "uniqueid", attrib={"type": "imdb"})
        imdb.text = imdb_id
    if tvdb_id:
        tvdb = ET.SubElement(root, "uniqueid", attrib={"type": "tvdb"})
        tvdb.text = str(tvdb_id)
    _append_elcinema(root, elcinema_id=elcinema_id, elcinema_url=elcinema_url, elcinema_title=elcinema_title)
    _append_local_art(root, poster_file=poster_file, fanart_file=fanart_file)
    _indent(root)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def patch_nfo_art(path: Path, *, poster_file: str | None, fanart_file: str | None) -> None:
    if not path.exists() or (not poster_file and not fanart_file):
        return
    root = ET.parse(path).getroot()
    _append_local_art(root, poster_file=poster_file, fanart_file=fanart_file, replace=True)
    _indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_episode_nfo(
    path: Path,
    *,
    title: str,
    season: int,
    episode: int,
    tmdb_episode_id: int | None = None,
) -> None:
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "season").text = str(season)
    ET.SubElement(root, "episode").text = str(episode)
    if tmdb_episode_id:
        uid = ET.SubElement(root, "uniqueid", attrib={"type": "tmdb", "default": "true"})
        uid.text = str(tmdb_episode_id)
    _indent(root)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


async def download_image(url: str, dest: Path) -> bool:
    if not is_valid_artwork_url(url):
        return False
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            if "svg" in content_type.lower():
                return False
            if "image" not in content_type and len(r.content) < 500:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
    except httpx.HTTPError:
        return False


def _append_local_art(
    root: ET.Element,
    *,
    poster_file: str | None,
    fanart_file: str | None,
    replace: bool = False,
) -> None:
    if not poster_file and not fanart_file:
        return
    art = root.find("art")
    if art is not None and replace:
        root.remove(art)
        art = None
    if art is None:
        art = ET.SubElement(root, "art")
    if poster_file:
        poster = art.find("poster")
        if poster is None:
            poster = ET.SubElement(art, "poster")
        poster.text = poster_file
    if fanart_file:
        fanart = art.find("fanart")
        if fanart is None:
            fanart = ET.SubElement(art, "fanart")
        fanart.text = fanart_file


def _append_elcinema(
    root: ET.Element,
    *,
    elcinema_id: str | None,
    elcinema_url: str | None,
    elcinema_title: str | None,
) -> None:
    identifier = elcinema_id or _elcinema_id_from_url(elcinema_url)
    if identifier:
        uid = ET.SubElement(root, "uniqueid", attrib={"type": "elcinema"})
        uid.text = identifier
    if elcinema_url:
        ET.SubElement(root, "elcinemaurl").text = elcinema_url
    if elcinema_title:
        ET.SubElement(root, "elcinematitle").text = elcinema_title


def _elcinema_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "work" and parts[1].isdigit():
        return parts[1]
    return None
