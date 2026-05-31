"""aria2 JSON-RPC client."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from akwarr.config import Settings


class Aria2Client:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.aria2_rpc_url
        self.secret = settings.aria2_secret
        self._id = 0

    def _next_id(self) -> str:
        self._id += 1
        return str(self._id)

    def _params(self, *args: Any) -> list[Any]:
        if self.secret:
            return [f"token:{self.secret}", *args]
        return list(args)

    async def _call(self, method: str, *args: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": self._params(*args),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.url, json=payload)
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(data["error"].get("message", "aria2 error"))
            if data is None:
                r.raise_for_status()
                return None
            r.raise_for_status()
            return data.get("result")

    async def add_uri(self, uri: str, dest_dir: str, filename: str) -> str:
        options = {
            "dir": dest_dir,
            "out": filename,
            "check-certificate": "false",
            "referer": "https://akwam.it/",
            "user-agent": "Mozilla/5.0",
        }
        gid = await self._call("aria2.addUri", [uri], options)
        if not isinstance(gid, str):
            raise RuntimeError("aria2 did not return a GID")
        return gid

    async def remove(self, gid: str) -> None:
        await self._call("aria2.remove", gid)

    async def force_remove(self, gid: str) -> None:
        await self._call("aria2.forceRemove", gid)

    async def pause(self, gid: str) -> None:
        await self._call("aria2.pause", gid)

    async def unpause(self, gid: str) -> None:
        await self._call("aria2.unpause", gid)

    async def tell_status(self, gid: str) -> dict[str, Any]:
        result = await self._call(
            "aria2.tellStatus",
            gid,
            [
                "gid",
                "status",
                "totalLength",
                "completedLength",
                "downloadSpeed",
                "errorMessage",
                "files",
            ],
        )
        return result if isinstance(result, dict) else {}

    async def is_complete(self, gid: str) -> bool:
        status = await self.tell_status(gid)
        return status.get("status") == "complete"

    async def completed_file_path(self, gid: str, staging_dir: str, filename: str) -> str:
        status = await self.tell_status(gid)
        files = status.get("files") or []
        if files and files[0].get("path"):
            return files[0]["path"]
        return f"{staging_dir.rstrip('/')}/{filename}"

    @staticmethod
    def safe_filename(name: str) -> str:
        cleaned = name.replace("/", "-").replace("\\", "-").strip()
        return cleaned or f"download-{uuid.uuid4().hex[:8]}.mkv"
