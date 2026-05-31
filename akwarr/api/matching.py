"""Akwam title matching helpers."""

from __future__ import annotations

import re
from typing import Any

RGX_ARABIC = re.compile(r"[\u0600-\u06ff]")


async def best_arabic_akwam_match(
    scraper: Any,
    *,
    fallback_title: str,
    section: str,
    arabic_queries: list[str],
    base_queries: list[str],
) -> Any | None:
    preferred = _unique_queries(*arabic_queries, *[query for query in base_queries if has_arabic(query)])
    if preferred:
        return await scraper.best_match(preferred[0], section=section, alt_queries=preferred[1:])

    fallback = _unique_queries(fallback_title, *base_queries)
    if not fallback:
        return None
    return await scraper.best_match(fallback[0], section=section, alt_queries=fallback[1:])


def has_arabic(value: str | None) -> bool:
    return bool(value and RGX_ARABIC.search(value))


def _unique_queries(*values: Any) -> list[str]:
    queries: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    return queries
