from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest
from starlette.requests import Request

from chutes_e2ee_proxy.app import create_app
from chutes_e2ee_proxy.config import Settings, TunnelMode


class FakeStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response: httpx.Response | None = None
        self.exc: Exception | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


class FakePool:
    def __init__(self) -> None:
        self.by_key: dict[str, FakeTransport] = {}

    async def get(self, api_key: str) -> FakeTransport:
        if api_key not in self.by_key:
            self.by_key[api_key] = FakeTransport()
        return self.by_key[api_key]

    def start_cleanup_task(self) -> None:
        return

    async def close_all(self) -> None:
        return

    def stats(self) -> dict[str, int]:
        return {"size": len(self.by_key)}


class FakeTunnel:
    def __init__(self, status: str = "off", mode: str = "off", public_url: str | None = None):
        self._status = status
        self._mode = mode
        self._public_url = public_url

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    def snapshot(self):
        class Snapshot:
            mode = self._mode
            status = self._status
            public_url = self._public_url
            last_error = None

        return Snapshot()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8787,
        upstream="https://llm.chutes.ai",
        e2e_upstream="https://api.chutes.ai",
        tunnel=TunnelMode.OFF,
    )


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def app(settings: Settings, fake_pool: FakePool):
    return create_app(settings, fake_pool, FakeTunnel(), lambda: None)


@pytest.mark.asyncio
async def test_health_endpoint_reports_status(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/_chutes_proxy/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["upstream"] == "https://llm.chutes.ai"
    assert payload["e2e_upstream"] == "https://api.chutes.ai"
    assert payload["tunnel"]["status"] == "off"


@pytest.mark.asyncio
async def test_missing_auth_returns_401(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json={"model": "x"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_stream_body_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-1")
    transport.response = httpx.Response(200, json={"ok": True})

    body = b'{"model":"abc","messages":[{"role":"user","content":"hi"}]}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions?x=1",
            content=body,
            headers={"Authorization": "Bearer token-1", "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.url == httpx.URL("https://llm.chutes.ai/v1/chat/completions?x=1")
    assert sent.content == body


@pytest.mark.asyncio
async def test_sse_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-2")
    transport.response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=FakeStream([b"data: hello\n\n", b"data: [DONE]\n\n"]),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer token-2"},
        )

    assert response.status_code == 200
    assert response.text == "data: hello\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_timeout_maps_504(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-3")
    transport.exc = httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer token-3"})

    assert response.status_code == 504


@pytest.mark.asyncio
async def test_connect_error_maps_502(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-4")
    req = httpx.Request("GET", "https://llm.chutes.ai/v1/models")
    transport.exc = httpx.ConnectError("unreachable", request=req)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer token-4"})

    assert response.status_code == 502


@pytest.mark.asyncio
async def test_upstream_500_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-5")
    transport.response = httpx.Response(500, content=b"boom")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer token-5"})

    assert response.status_code == 500
    assert response.text == "boom"


@pytest.mark.asyncio
async def test_http_status_error_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-status")
    req = httpx.Request("GET", "https://llm.chutes.ai/v1/models")
    resp = httpx.Response(401, request=req, content=b'{"error":"unauthorized"}')
    transport.exc = httpx.HTTPStatusError("401", request=req, response=resp)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer token-status"})

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_different_keys_use_separate_transports(app, fake_pool: FakePool) -> None:
    t1 = await fake_pool.get("token-a")
    t2 = await fake_pool.get("token-b")
    t1.response = httpx.Response(200, json={"key": "a"})
    t2.response = httpx.Response(200, json={"key": "b"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.get("/v1/models", headers={"Authorization": "Bearer token-a"})
        r2 = await client.get("/v1/models", headers={"Authorization": "Bearer token-b"})

    assert r1.json() == {"key": "a"}
    assert r2.json() == {"key": "b"}


@pytest.mark.asyncio
async def test_health_reports_connected_tunnel_state(settings: Settings, fake_pool: FakePool) -> None:
    app = create_app(
        settings,
        fake_pool,
        FakeTunnel(status="connected", mode="auto", public_url="https://abc.trycloudflare.com"),
        lambda: None,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/_chutes_proxy/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tunnel"]["mode"] == "auto"
    assert payload["tunnel"]["status"] == "connected"
    assert payload["tunnel"]["public_url"] == "https://abc.trycloudflare.com"
