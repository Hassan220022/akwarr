"""Map Jellyseerr/Radarr quality profiles to Akwam download preferences."""

from __future__ import annotations

from typing import Any

from akwarr.config import Settings


def _settings_qualities(settings: Settings) -> list[str]:
    listed = getattr(settings, "quality_list", None)
    if listed:
        return list(listed)
    preferred = getattr(settings, "preferred_qualities", None)
    if preferred:
        return [q.strip() for q in str(preferred).split(",") if q.strip()]
    return ["720p"]


def quality_for_profile_id(profile_id: int, settings: Settings) -> str:
    qualities = _settings_qualities(settings)
    idx = int(profile_id) - 1
    if 0 <= idx < len(qualities):
        return qualities[idx]
    return qualities[0]


def pick_order_for_quality(requested: str, settings: Settings) -> list[str]:
    """Prefer requested quality, then fall back through configured order."""
    base = _settings_qualities(settings)
    ordered = [requested, *[q for q in base if q.lower() != requested.lower()]]
    seen: set[str] = set()
    unique: list[str] = []
    for q in ordered:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)
    return unique


def quality_profiles_payload(settings: Settings) -> list[dict[str, Any]]:
    qualities = _settings_qualities(settings)
    return [
        {
            "id": i,
            "name": f"Arabic {q}",
            "upgradeAllowed": False,
            "cutoff": i,
            "items": [{"quality": {"id": i, "name": q}, "allowed": True}],
        }
        for i, q in enumerate(qualities, start=1)
    ]
