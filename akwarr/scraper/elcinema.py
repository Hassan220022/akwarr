"""ElCinema search bridge for Arabic title candidates."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from akwarr.config import Settings
from akwarr.scraper.flaresolverr import FlareSolverrClient

RGX_ARABIC = re.compile(r"[\u0600-\u06ff]")
RGX_YEAR = re.compile(r"(19|20)\d{2}")


@dataclass
class ElCinemaSearchResult:
    title: str
    url: str
    kind: str
    year: str | None = None
    english_title: str | None = None
    poster: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class ElCinemaScraper:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.elcinema_base.rstrip("/")
        self.fetcher = FlareSolverrClient(settings)

    async def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 8,
    ) -> list[ElCinemaSearchResult]:
        if not query.strip():
            return []

        url = f"{self.base}/ajaxable/search_all?q={quote(query)}&type=autocomplete&per_page={limit}"
        html = (await self.fetcher.get(url)).text
        try:
            fragments = json.loads(html)
        except json.JSONDecodeError:
            return []

        results: list[ElCinemaSearchResult] = []
        seen: set[tuple[str, str | None]] = set()
        for fragment in fragments:
            if not isinstance(fragment, str):
                continue
            result = self._parse_result(fragment)
            if result is None:
                continue
            if kind and result.kind != kind:
                continue
            key = (result.title, result.year)
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

        return results

    async def arabic_candidates(
        self,
        *queries: str | None,
        year: int | str | None,
        kind: str,
    ) -> list[str]:
        return [result.title for result in await self.matched_candidates(*queries, year=year, kind=kind)]

    async def matched_candidates(
        self,
        *queries: str | None,
        year: int | str | None,
        kind: str,
    ) -> list[ElCinemaSearchResult]:
        all_results: list[ElCinemaSearchResult] = []
        for query in _unique_queries(*queries):
            all_results.extend(await self.search(query, kind=kind))

        target_year = str(year) if year else None
        all_results.sort(key=lambda item: 0 if target_year and item.year == target_year else 1)

        candidates: list[ElCinemaSearchResult] = []
        seen: set[str] = set()
        for result in all_results:
            if "..." in result.title:
                continue
            if result.title not in seen and RGX_ARABIC.search(result.title):
                candidates.append(result)
                seen.add(result.title)
        return candidates

    def _parse_result(self, fragment: str) -> ElCinemaSearchResult | None:
        soup = BeautifulSoup(fragment, "lxml")
        root = soup.find(attrs={"data-entity": re.compile("^work$", re.I), "data-url": True})
        if root is None:
            root = soup.find(attrs={"data-url": re.compile(r"^/work/")})
        if root is None:
            return None

        path = root.get("data-url", "")
        if not path.startswith("/work/"):
            return None

        data_text = _clean_text(root.get("data-text"))
        rtl_title = _node_text(root.select_one('[dir="rtl"]'))
        title = data_text if RGX_ARABIC.search(data_text) and "..." not in data_text else rtl_title or data_text
        english_title = _node_text(root.select_one('[dir="ltr"]'))
        if not title:
            return None

        meta_items = [_clean_text(li.get_text(" ", strip=True)) for li in root.select("li")]
        result_kind = _kind_from_meta(meta_items)
        if result_kind is None:
            return None

        year = None
        for item in meta_items:
            match = RGX_YEAR.search(item)
            if match:
                year = match.group(0)
                break

        poster = None
        img = root.find("img")
        if img and img.get("src"):
            poster = urljoin(self.base, img["src"])

        return ElCinemaSearchResult(
            title=_clean_text(title),
            url=urljoin(self.base, path),
            kind=result_kind,
            year=year,
            english_title=_clean_text(english_title) or None,
            poster=poster,
        )


def _kind_from_meta(meta_items: list[str]) -> str | None:
    category = meta_items[0].lower() if meta_items else ""
    if "فيلم" in category or "movie" in category:
        return "movie"
    if "مسلسل" in category or "series" in category or category == "tv":
        return "series"
    return None


def _node_text(node) -> str:
    return _clean_text(node.get_text(" ", strip=True)) if node else ""


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _unique_queries(*values: str | None) -> list[str]:
    queries: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    return queries
