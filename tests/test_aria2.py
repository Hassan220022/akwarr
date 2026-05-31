import httpx
import pytest

from akwarr.config import Settings
from akwarr.download.aria2 import Aria2Client


class FakeAsyncClient:
    def __init__(self, *, response: httpx.Response, **kwargs):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, json: dict):
        return self.response


@pytest.mark.asyncio
async def test_aria2_http_400_surfaces_json_rpc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        400,
        json={"id": "1", "jsonrpc": "2.0", "error": {"code": 1, "message": "Unauthorized"}},
        request=httpx.Request("POST", "http://aria2/jsonrpc"),
    )

    def client_factory(**kwargs):
        return FakeAsyncClient(response=response, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = Aria2Client(Settings(aria2_rpc_url="http://aria2/jsonrpc", aria2_secret="bad"))

    with pytest.raises(RuntimeError, match="Unauthorized"):
        await client._call("aria2.getVersion")


@pytest.mark.asyncio
async def test_aria2_tell_status_requests_progress_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        200,
        json={
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "gid": "abc",
                "status": "active",
                "totalLength": "100",
                "completedLength": "25",
                "downloadSpeed": "5",
            },
        },
        request=httpx.Request("POST", "http://aria2/jsonrpc"),
    )

    class CapturingAsyncClient(FakeAsyncClient):
        async def post(self, url: str, json: dict):
            calls.append(json)
            return self.response

    def client_factory(**kwargs):
        return CapturingAsyncClient(response=response, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = Aria2Client(Settings(aria2_rpc_url="http://aria2/jsonrpc", aria2_secret="secret"))
    status = await client.tell_status("abc")

    assert status["downloadSpeed"] == "5"
    assert "downloadSpeed" in calls[0]["params"][2]


@pytest.mark.asyncio
async def test_aria2_add_uri_sends_single_uri_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        200,
        json={"id": "1", "jsonrpc": "2.0", "result": "gid123"},
        request=httpx.Request("POST", "http://aria2/jsonrpc"),
    )

    class CapturingAsyncClient(FakeAsyncClient):
        async def post(self, url: str, json: dict):
            calls.append(json)
            return self.response

    def client_factory(**kwargs):
        return CapturingAsyncClient(response=response, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = Aria2Client(Settings(aria2_rpc_url="http://aria2/jsonrpc", aria2_secret="secret"))
    gid = await client.add_uri("https://cdn.example/movie.mp4", "/downloads", "movie.mp4")

    assert gid == "gid123"
    assert calls[0]["params"] == [
        "token:secret",
        ["https://cdn.example/movie.mp4"],
        {"dir": "/downloads", "out": "movie.mp4"},
    ]


@pytest.mark.asyncio
async def test_aria2_remove_sends_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        200,
        json={"id": "1", "jsonrpc": "2.0", "result": "gid123"},
        request=httpx.Request("POST", "http://aria2/jsonrpc"),
    )

    class CapturingAsyncClient(FakeAsyncClient):
        async def post(self, url: str, json: dict):
            calls.append(json)
            return self.response

    def client_factory(**kwargs):
        return CapturingAsyncClient(response=response, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = Aria2Client(Settings(aria2_rpc_url="http://aria2/jsonrpc", aria2_secret="secret"))
    await client.remove("gid123")

    assert calls[0]["method"] == "aria2.remove"
    assert calls[0]["params"] == ["token:secret", "gid123"]


@pytest.mark.asyncio
async def test_aria2_pause_resume_and_force_remove_send_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        200,
        json={"id": "1", "jsonrpc": "2.0", "result": "OK"},
        request=httpx.Request("POST", "http://aria2/jsonrpc"),
    )

    class CapturingAsyncClient(FakeAsyncClient):
        async def post(self, url: str, json: dict):
            calls.append(json)
            return self.response

    def client_factory(**kwargs):
        return CapturingAsyncClient(response=response, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = Aria2Client(Settings(aria2_rpc_url="http://aria2/jsonrpc", aria2_secret="secret"))
    await client.pause("gid123")
    await client.unpause("gid123")
    await client.force_remove("gid123")

    assert [call["method"] for call in calls] == ["aria2.pause", "aria2.unpause", "aria2.forceRemove"]
    assert all(call["params"] == ["token:secret", "gid123"] for call in calls)
