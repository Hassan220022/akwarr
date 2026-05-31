"""API key authentication for Radarr/Sonarr-compatible endpoints."""

from fastapi import Header, HTTPException, Query

from akwarr.config import get_settings


async def verify_api_key(
    x_api_key: str | None = Header(default=None),
    apikey: str | None = Query(default=None),
    api_key: str | None = Query(default=None, alias="apiKey"),
) -> None:
    settings = get_settings()
    if not settings.api_key or settings.api_key == "change-me":
        return
    provided_key = x_api_key or apikey or api_key
    if provided_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
