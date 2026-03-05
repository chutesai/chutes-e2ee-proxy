from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest

import chutes_e2ee_proxy.app as app_module
from chutes_e2ee_proxy.app import create_app
from chutes_e2ee_proxy.config import Settings, TunnelMode
from chutes_e2ee_proxy.errors import ProxyRequestError


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


class FailingStartTunnel(FakeTunnel):
    def __init__(self):
        super().__init__(status="disconnected", mode="required")
        self.stopped = False

    async def start(self) -> None:
        raise RuntimeError("tunnel start failed")

    async def stop(self) -> None:
        self.stopped = True


class TrackingPool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = False
        self.closed = False

    def start_cleanup_task(self) -> None:
        self.cleanup_started = True

    async def close_all(self) -> None:
        self.closed = True


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
async def test_public_models_allows_no_auth(settings: Settings, fake_pool: FakePool, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_send_plain(request, upstream_url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        captured["method"] = request.method
        captured["upstream_url"] = upstream_url
        captured["headers"] = headers
        captured["body"] = body
        return httpx.Response(200, json={"data": [{"id": "model-1"}]})

    monkeypatch.setattr(app_module, "_send_plain_upstream_request", fake_send_plain)
    app = create_app(settings, fake_pool, FakeTunnel(), lambda: None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "model-1"
    assert captured["method"] == "GET"
    assert captured["upstream_url"] == "https://llm.chutes.ai/v1/models"
    assert fake_pool.by_key == {}


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
async def test_json_request_body_is_forwarded_unchanged(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-unchanged")
    transport.response = httpx.Response(200, json={"ok": True})
    body = (
        b'{"model":"zai-org/GLM-4.7:THINKING","messages":[{"role":"user","content":"hi"}],'
        b'"continue_final_message":true,"tools":[{"type":"function","function":{"name":"lookup","parameters":{}}}]}'
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            content=body,
            headers={
                "Authorization": "Bearer token-unchanged",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    sent = transport.requests[0]
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
async def test_upstream_403_passthrough_preserves_body(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-403")
    transport.response = httpx.Response(
        403,
        headers={"content-type": "application/json"},
        content=b'{"detail":"Invalid, expired, or already-used nonce"}',
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer token-403"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid, expired, or already-used nonce"


@pytest.mark.asyncio
async def test_upstream_429_passthrough_preserves_body(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-429")
    transport.response = httpx.Response(
        429,
        headers={"content-type": "application/json"},
        content=b'{"detail":"Instance is at maximum capacity, try again later"}',
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer token-429"})

    assert response.status_code == 429
    assert response.json()["detail"] == "Instance is at maximum capacity, try again later"


@pytest.mark.asyncio
async def test_upstream_error_html_body_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-html")
    html = b"<html><body><h1>403 Forbidden</h1></body></html>"
    transport.response = httpx.Response(
        403,
        headers={"content-type": "text/html"},
        content=html,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer token-html"})

    assert response.status_code == 403
    assert response.content == html
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_upstream_error_empty_body_passthrough(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-empty")
    transport.response = httpx.Response(
        403,
        headers={"content-type": "text/plain"},
        content=b"",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer token-empty"})

    assert response.status_code == 403
    assert response.content == b""


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
async def test_proxy_request_errors_return_explicit_status(app, fake_pool: FakePool) -> None:
    transport = await fake_pool.get("token-contract")
    transport.exc = ProxyRequestError(404, "model_not_found", "model not found: alias")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "alias"},
            headers={"Authorization": "Bearer token-contract"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"type": "model_not_found", "message": "model not found: alias"}
    }


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


@pytest.mark.asyncio
async def test_lifespan_closes_pool_if_tunnel_start_fails(settings: Settings) -> None:
    pool = TrackingPool()
    tunnel = FailingStartTunnel()
    app = create_app(settings, pool, tunnel, lambda: None)

    with pytest.raises(RuntimeError, match="tunnel start failed"):
        async with app.router.lifespan_context(app):
            pass

    assert pool.cleanup_started is True
    assert pool.closed is True
    assert tunnel.stopped is True
