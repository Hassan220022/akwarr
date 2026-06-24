"""FlareSolverr HTTP client with optional direct fetch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from akwarr.config import Settings


@dataclass
class FetchResponse:
    text: str
    status_code: int
    url: str


class FlareSolverrClient:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.flaresolverr_url.rstrip("/")
        self.enabled = settings.flaresolverr_enable
        self.auto = settings.flaresolverr_auto
        self._session_id: str | None = None
        self._session_last_used: datetime | None = None
        self._session_timeout = 600
        self._cache: dict[str, tuple[datetime, FetchResponse]] = {}
        self._cache_ttl = 3600

    async def get(self, target_url: str) -> FetchResponse:
        cache_key = hashlib.md5(target_url.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached and datetime.now() < cached[0]:
            return cached[1]

        if not self.enabled:
            result = await self._direct_get(target_url)
            self._cache[cache_key] = (datetime.now() + timedelta(seconds=self._cache_ttl), result)
            return result

        if self.auto:
            try:
                direct = await self._direct_get(target_url)
            except httpx.HTTPError:
                direct = None
            if direct is not None and not self._is_cloudflare_challenge(direct.text, direct.status_code):
                self._cache[cache_key] = (datetime.now() + timedelta(seconds=self._cache_ttl), direct)
                return direct

        result = await self._flaresolverr_get(target_url)
        self._cache[cache_key] = (datetime.now() + timedelta(seconds=self._cache_ttl), result)
        return result

    async def _direct_get(self, target_url: str) -> FetchResponse:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(target_url)
            return FetchResponse(text=r.text, status_code=r.status_code, url=str(r.url))

    async def _flaresolverr_get(self, target_url: str) -> FetchResponse:
        session = await self._get_or_create_session()
        payload: dict = {"cmd": "request.get", "url": target_url, "maxTimeout": 60000}
        if session:
            payload["session"] = session
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(self.url, json=payload)
            data = r.json()
            if data.get("status") != "ok":
                msg = data.get("message", "FlareSolverr error")
                if "session" in msg.lower():
                    await self._destroy_session()
                raise RuntimeError(msg)
            solution = data.get("solution", {})
            return FetchResponse(
                text=solution.get("response", ""),
                status_code=solution.get("status", 200),
                url=target_url,
            )

    async def _get_or_create_session(self) -> str | None:
        now = datetime.now()
        if self._session_id and self._session_last_used:
            if (now - self._session_last_used).total_seconds() < self._session_timeout:
                self._session_last_used = now
                return self._session_id
            await self._destroy_session()

        session_name = f"akwarr_{int(now.timestamp())}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                self.url,
                json={"cmd": "sessions.create", "session": session_name},
            )
            data = r.json()
            if data.get("status") == "ok":
                self._session_id = data.get("session")
                self._session_last_used = now
                return self._session_id
        return None

    async def _destroy_session(self) -> None:
        if not self._session_id:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    self.url,
                    json={"cmd": "sessions.destroy", "session": self._session_id},
                )
        finally:
            self._session_id = None
            self._session_last_used = None

    @staticmethod
    def _is_cloudflare_challenge(content: str, status_code: int) -> bool:
        if status_code in (403, 503, 429):
            return True
        lower = content.lower()
        signatures = (
            "challenge-platform",
            "checking your browser",
            "just a moment",
            "cf_chl_opt",
            "__cf_chl_jschl_tk__",
        )
        return any(sig in lower for sig in signatures)
