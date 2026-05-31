"""NFO and sidecar artwork for Jellyfin."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import httpx


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
    overview: str | None = None,
    language: str = "ar",
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
    overview: str | None = None,
    language: str = "ar",
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
    _indent(root)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


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
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            if "image" not in content_type and len(r.content) < 500:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
    except httpx.HTTPError:
        return False
