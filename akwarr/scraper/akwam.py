"""Akwam.it scraper — search, metadata, download links."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from akwarr.config import Settings
from akwarr.scraper.flaresolverr import FlareSolverrClient

RGX_QUALITY_LINK = re.compile(
    r'tab-content quality.*?a href="(https?://[^"]+/(?:link|download)/\d+[^"]*)"',
    re.DOTALL | re.IGNORECASE,
)
RGX_TAB_BUTTON = re.compile(r"#(tab-\d+)$")
RGX_EPISODE_NUM = re.compile(r"(?:الحلقة|حلقة)\s*([\d٠-٩]+)")
RGX_SEASON_NUM = re.compile(r"(?:الموسم|موسم|الجزء|جزء)\s*([\d٠-٩]+|[\w\s]+?)\s+(?:الحلقة|حلقة)")
ARABIC_ORDINALS = {
    "الأول": 1,
    "الاول": 1,
    "الثاني": 2,
    "الثانى": 2,
    "الثالث": 3,
    "الرابع": 4,
    "الخامس": 5,
    "السادس": 6,
    "السابع": 7,
    "الثامن": 8,
    "التاسع": 9,
    "العاشر": 10,
}
DIRECT_MEDIA_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
INVALID_ARTWORK_MARKERS = (
    "logo-white",
    "/style/assets/",
    "/assets/images/logo",
    "favicon",
)


def is_valid_artwork_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".svg"):
        return False
    return not any(marker in lower for marker in INVALID_ARTWORK_MARKERS)


@dataclass
class AkwamSearchResult:
    title: str
    url: str
    kind: str  # movie | series
    poster: str | None = None
    year: str | None = None


@dataclass
class AkwamDownload:
    quality: str
    size: str | None
    link_url: str


@dataclass
class AkwamEpisode:
    number: int
    title: str
    url: str
    season: int | None = None


@dataclass
class AkwamMetadata:
    title: str
    url: str
    kind: str
    poster: str | None
    fanart: str | None
    overview: str | None
    year: str | None
    downloads: list[AkwamDownload]
    episodes: list[AkwamEpisode]


class AkwamScraper:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.akwam_base.rstrip("/")
        self.fetcher = FlareSolverrClient(settings)
        self.qualities = settings.quality_list

    async def search(self, query: str, *, section: str = "movie") -> list[AkwamSearchResult]:
        url = f"{self.base}/search?q={quote(query)}&section={section}"
        html = (await self.fetcher.get(url)).text
        soup = BeautifulSoup(html, "lxml")
        results_by_url: dict[str, AkwamSearchResult] = {}

        def add_result(item: AkwamSearchResult) -> None:
            existing = results_by_url.get(item.url)
            if existing is None or _result_title_score(item.title) > _result_title_score(existing.title):
                results_by_url[item.url] = item

        for card in soup.select("div.media-card, div.media, article.media-card, div.col-lg-2"):
            link = card.find("a", href=True)
            if not link:
                continue
            href = link["href"]
            if "/movie/" not in href and "/series/" not in href:
                continue
            kind = "movie" if "/movie/" in href else "series"
            title = _clean_result_title(link.get("title") or link.get_text(strip=True), href)
            if not title:
                continue
            poster = None
            img = card.find("img")
            if img and img.get("src"):
                poster = urljoin(self.base, img["src"])
            full_url = urljoin(self.base, href)
            add_result(AkwamSearchResult(title=title, url=full_url, kind=kind, poster=poster))

        if not results_by_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if section == "movie" and "/movie/" in href:
                    kind = "movie"
                elif section == "series" and "/series/" in href:
                    kind = "series"
                else:
                    continue
                title = _clean_result_title(a.get_text(strip=True), href)
                if len(title) < 2:
                    continue
                full_url = urljoin(self.base, href)
                add_result(AkwamSearchResult(title=title, url=full_url, kind=kind))

        return list(results_by_url.values())

    async def best_match(
        self,
        query: str,
        *,
        section: str,
        alt_queries: list[str] | None = None,
    ) -> AkwamSearchResult | None:
        queries: list[str] = []
        for item in [query, *(alt_queries or [])]:
            if item and item not in queries:
                queries.append(item)
        seen_urls: set[str] = set()
        best: AkwamSearchResult | None = None
        best_score = 0.0

        for q in queries:
            if not q:
                continue
            for item in await self.search(q, section=section):
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                score = _similarity(q, item.title)
                if score > best_score:
                    best_score = score
                    best = item

        return best if best_score >= 0.45 else None

    async def fetch_metadata(self, page_url: str, *, kind: str | None = None) -> AkwamMetadata:
        html = (await self.fetcher.get(page_url)).text
        soup = BeautifulSoup(html, "lxml")

        if kind is None:
            kind = "series" if "/series/" in page_url else "movie"

        title_el = soup.find("h1") or soup.find("h2")
        title = title_el.get_text(strip=True) if title_el else "Unknown"

        poster = _extract_poster_url(soup, self.base)

        fanart = None
        gallery = soup.find("a", attrs={"data-fancybox": re.compile("gallery")})
        if gallery and gallery.get("href"):
            fanart = urljoin(self.base, gallery["href"])
        if not is_valid_artwork_url(fanart) and poster:
            fanart = poster
        if not is_valid_artwork_url(poster) and is_valid_artwork_url(fanart):
            poster = fanart

        overview = None
        story = soup.find("p", class_=re.compile("story|overview|description"))
        if story:
            overview = story.get_text(strip=True)

        year = None
        year_match = re.search(r"(19|20)\d{2}", title)
        if year_match:
            year = year_match.group(0)

        downloads = await self._extract_downloads(html, page_url)
        episodes: list[AkwamEpisode] = []
        if kind == "series":
            seen_episodes: set[tuple[int | None, int]] = set()
            for div in soup.find_all("div", class_="bg-primary2"):
                h2 = div.find("h2")
                if not h2:
                    continue
                link = h2.find("a", href=True)
                if not link:
                    continue
                ep_title = link.get_text(strip=True)
                episode_number = _parse_episode_number(ep_title)
                if episode_number is None:
                    continue
                season = _parse_season_number(ep_title)
                episode_key = (season, episode_number)
                if episode_key in seen_episodes:
                    trailing_number = _parse_trailing_episode_number(ep_title)
                    trailing_key = (season, trailing_number or episode_number)
                    if (
                        trailing_number
                        and trailing_number != episode_number
                        and trailing_key not in seen_episodes
                    ):
                        episode_number = trailing_number
                        episode_key = trailing_key
                    else:
                        continue
                seen_episodes.add(episode_key)
                episodes.append(
                    AkwamEpisode(
                        number=episode_number,
                        title=ep_title,
                        url=urljoin(self.base, link["href"]),
                        season=season,
                    )
                )
            episodes.sort(key=lambda e: e.number)

        return AkwamMetadata(
            title=title,
            url=page_url,
            kind=kind,
            poster=poster,
            fanart=fanart,
            overview=overview,
            year=year,
            downloads=downloads,
            episodes=episodes,
        )

    async def resolve_direct_url(self, link_page_url: str) -> str:
        """Follow Akwam redirect chain to the final HTTP download URL."""
        current = link_page_url
        for _ in range(5):
            if _is_direct_media_url(current):
                return current
            response = await self.fetcher.get(current)
            if response.url != current and _is_direct_media_url(response.url):
                return response.url
            html = response.text
            if html.strip().startswith("http"):
                direct = html.strip().split()[0]
                if _is_direct_media_url(direct):
                    return direct
                current = direct
                continue
            soup = BeautifulSoup(html, "lxml")
            meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
            if meta and meta.get("content"):
                m = re.search(r"url=(.+)", meta["content"], re.I)
                if m:
                    current = urljoin(current, m.group(1).strip("'\""))
                    continue
            iframe = soup.find("iframe", src=True)
            if iframe:
                current = urljoin(current, iframe["src"])
                continue
            a = _find_download_redirect(soup)
            if a and a["href"].startswith("http"):
                current = a["href"]
                continue
            if a:
                current = urljoin(current, a["href"])
                continue
            break
        raise RuntimeError(f"Could not resolve direct download URL from {link_page_url}")

    async def pick_download(
        self,
        metadata: AkwamMetadata,
        *,
        qualities: list[str] | None = None,
    ) -> tuple[str, str]:
        """Return (quality_label, link_page_url) for preferred quality."""
        if not metadata.downloads:
            page = await self.fetch_metadata(metadata.url, kind=metadata.kind)
            metadata.downloads = page.downloads
        if not metadata.downloads:
            raise RuntimeError(f"No download links on {metadata.url}")

        by_quality = {d.quality.lower(): d for d in metadata.downloads}
        for q in qualities or self.qualities:
            key = q.lower()
            if key in by_quality:
                return by_quality[key].quality, by_quality[key].link_url
        first = metadata.downloads[0]
        return first.quality, first.link_url

    async def episode_download_url(
        self,
        episode_url: str,
        *,
        qualities: list[str] | None = None,
    ) -> tuple[str, str]:
        meta = await self.fetch_metadata(episode_url, kind="series")
        quality, link = await self.pick_download(meta, qualities=qualities)
        direct = await self.resolve_direct_url(link)
        return quality, direct

    async def _extract_downloads(self, html: str, page_url: str) -> list[AkwamDownload]:
        downloads: list[AkwamDownload] = []
        soup = BeautifulSoup(html, "lxml")

        # Map tab ids to quality labels from tab buttons: <a href="#tab-N">720p</a>
        quality_labels: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            m = RGX_TAB_BUTTON.match(a["href"])
            if m:
                label = a.get_text(strip=True)
                if label:
                    quality_labels[m.group(1)] = label

        for block in soup.select("div.tab-content.quality, div.quality-tab"):
            block_id = block.get("id", "")
            default_quality = quality_labels.get(block_id, "")

            # New structure: rows with data-quality, containing a.link-download
            rows = block.select("div[data-quality]")
            for row in rows:
                dl_link = row.select_one("a.link-download")
                if not dl_link or not dl_link.get("href"):
                    continue
                href = dl_link["href"]
                if "/download/" not in href and "/link/" not in href:
                    continue
                label = default_quality or row.get("data-quality", "") or "unknown"
                size_text = ""
                for span in dl_link.find_all("span"):
                    t = span.get_text(strip=True)
                    if re.search(r"GB|MB", t, re.I):
                        size_text = t
                        break
                downloads.append(
                    AkwamDownload(
                        quality=_normalize_quality(label),
                        size=size_text or None,
                        link_url=urljoin(page_url, href),
                    )
                )

            # Old structure: direct <a> with /link/ in href (backward compat)
            if not rows:
                for a in block.find_all("a", href=True):
                    href = a["href"]
                    if "/link/" not in href:
                        continue
                    label = a.get_text(strip=True) or default_quality or "unknown"
                    size_el = a.find_next(string=re.compile(r"GB|MB"))
                    downloads.append(
                        AkwamDownload(
                            quality=_normalize_quality(label),
                            size=size_el.strip() if size_el else None,
                            link_url=urljoin(page_url, href),
                        )
                    )

        if not downloads:
            for match in RGX_QUALITY_LINK.finditer(html):
                downloads.append(
                    AkwamDownload(quality="720p", size=None, link_url=match.group(1))
                )

        return downloads


def _normalize_quality(label: str) -> str:
    lower = label.lower()
    for q in ("1080p", "720p", "480p", "360p", "2160p", "4k"):
        if q.replace("p", "") in lower or q in lower:
            return q
    return label.strip() or "720p"


def _find_download_redirect(soup: BeautifulSoup):
    selectors = (
        "a.download-link[href]",
        'a[href*="/download/"]',
    )
    for selector in selectors:
        link = soup.select_one(selector)
        if link:
            return link

    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True).lower()
        if "go for your link" in text or "click here" in text:
            return link

    for link in soup.find_all("a", href=True):
        if str(link["href"]).startswith("http"):
            return link
    return None


def _is_direct_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(suffix) for suffix in DIRECT_MEDIA_SUFFIXES)


def _extract_poster_url(soup: BeautifulSoup, base: str) -> str | None:
    candidates: list[str] = []
    for img in soup.find_all("img", src=True):
        src = urljoin(base, img["src"]).replace("thumb/260x380/", "")
        if not is_valid_artwork_url(src):
            continue
        classes = " ".join(img.get("class") or [])
        if "poster" in classes.lower():
            return src
        if "img-fluid" in classes.lower() or "downet" in src.lower():
            candidates.append(src)
    return candidates[0] if candidates else None


def _parse_episode_number(title: str) -> int | None:
    match = RGX_EPISODE_NUM.search(title)
    if not match:
        return None
    return _parse_int(match.group(1))


def _parse_trailing_episode_number(title: str) -> int | None:
    trailing = re.search(r"([\d٠-٩]+)\s*$", title)
    if not trailing:
        return None
    number = _parse_int(trailing.group(1))
    if number and number <= 200:
        return number
    return None


def _parse_season_number(title: str) -> int | None:
    match = RGX_SEASON_NUM.search(title)
    if not match:
        return None
    return _parse_int(match.group(1).strip())


def _parse_int(value: str) -> int | None:
    normalized = "".join(str(unicodedata.digit(ch)) if ch.isdigit() else ch for ch in value)
    if normalized.isdigit():
        return int(normalized)
    return ARABIC_ORDINALS.get(normalized.strip())


def _similarity(a: str, b: str) -> float:
    a_norm = _normalize_title(a)
    b_norm = _normalize_title(b)
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _clean_result_title(title: str, href: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if title and title not in {"مشاهدة", "تحميل"}:
        return title
    slug = unquote(href.rstrip("/").split("/")[-1])
    slug = re.sub(r"-\d+$", "", slug)
    slug = slug.replace("-", " ")
    return re.sub(r"\s+", " ", slug).strip()


def _result_title_score(title: str) -> tuple[int, int]:
    if title in {"مشاهدة", "تحميل"}:
        return (0, len(title))
    return (1, len(title))
