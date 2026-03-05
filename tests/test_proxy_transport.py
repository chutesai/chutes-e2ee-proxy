import json
import threading
import types

import httpx
import pytest

import chutes_e2ee_proxy.proxy_transport as proxy_transport
from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.proxy_transport import ProxyAsyncChutesE2EETransport


def _mock_response(url: str, payload: dict | list, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", url), json=payload)


def _build_transport(*, models: list[dict], aliases: list[dict] | None = None):
    aliases = aliases or []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/v1/models"):
            return _mock_response(url, {"data": models})
        if url.endswith("/model_aliases/"):
            return _mock_response(url, aliases)
        if "/e2e/instances/" in url:
            chute_id = url.rsplit("/", 1)[-1]
            return _mock_response(
                url,
                {
                    "instances": [
                        {
                            "instance_id": f"inst-{chute_id}",
                            "e2e_pubkey": "pubkey",
                            "nonces": [f"nonce-{chute_id}"],
                        }
                    ],
                    "nonce_expires_in": 55,
                },
            )
        raise AssertionError(url)

    return ProxyAsyncChutesE2EETransport(
        api_key="cpk_test",
        model_api_base="https://llm.example",
        api_base="https://api.example",
        inner=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_transport_normalizes_single_root_inside_e2ee_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_transport(
        models=[
            {"id": "model-a-tee", "root": "model-a", "created": 1, "chute_id": "chute-a"},
        ]
    )
    captured: list[dict] = []

    monkeypatch.setattr(
        proxy_transport,
        "build_e2ee_request",
        lambda _pubkey, payload: captured.append(dict(payload))
        or types.SimpleNamespace(blob=b"blob", response_sk=b"sk"),
    )

    async def fake_handle_non_stream(self, url, headers, blob, response_sk, original_request):
        _ = url, blob, response_sk
        return httpx.Response(
            200,
            request=original_request,
            json={"ok": True, "chute_id": headers["X-Chute-Id"]},
        )

    transport._handle_non_stream = types.MethodType(fake_handle_non_stream, transport)

    original_body = json.dumps(
        {"model": "model-a", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    request = httpx.Request(
        "POST",
        "https://llm.example/v1/chat/completions",
        content=original_body,
        headers={"Content-Type": "application/json"},
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert response.json()["chute_id"] == "chute-a"
    assert request.content == original_body
    assert captured == [
        {"model": "model-a-tee", "messages": [{"role": "user", "content": "hi"}]}
    ]

    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_rejects_multi_model_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_transport(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
            {"id": "model-b", "root": "model-b", "created": 1, "chute_id": "chute-b"},
        ]
    )

    build_calls = 0

    def fake_build(_pubkey, payload):
        nonlocal build_calls
        _ = payload
        build_calls += 1
        return types.SimpleNamespace(blob=b"blob", response_sk=b"sk")

    monkeypatch.setattr(proxy_transport, "build_e2ee_request", fake_build)

    request = httpx.Request(
        "POST",
        "https://llm.example/v1/chat/completions",
        json={"model": "model-a,model-b:throughput", "messages": [{"role": "user", "content": "hi"}]},
    )

    with pytest.raises(ProxyRequestError, match="single resolved model target"):
        await transport.handle_async_request(request)

    assert build_calls == 0
    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_nonce_retry_works_without_invalidate_nonce_cache_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_transport(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
        ]
    )

    class LegacyDiscovery:
        def __init__(self) -> None:
            self._nonce_cache = {"chute-a": object()}
            self._cache_lock = threading.Lock()
            self.calls = 0

        async def get_nonce_async(self, chute_id: str, client: httpx.AsyncClient):
            _ = chute_id, client
            self.calls += 1
            return (
                types.SimpleNamespace(instance_id=f"inst-{self.calls}", e2e_pubkey="pubkey"),
                f"nonce-{self.calls}",
            )

    legacy_discovery = LegacyDiscovery()
    transport._discovery = legacy_discovery

    monkeypatch.setattr(
        proxy_transport,
        "build_e2ee_request",
        lambda _pubkey, payload: types.SimpleNamespace(blob=b"blob", response_sk=b"sk"),
    )

    async def fake_handle_non_stream(self, url, headers, blob, response_sk, original_request):
        _ = url, blob, response_sk
        if headers["X-E2E-Nonce"] == "nonce-1":
            return httpx.Response(
                403,
                request=original_request,
                content=b"Invalid, expired, or already-used nonce",
            )
        return httpx.Response(200, request=original_request, json={"ok": True})

    transport._handle_non_stream = types.MethodType(fake_handle_non_stream, transport)

    request = httpx.Request(
        "POST",
        "https://llm.example/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert legacy_discovery.calls == 2
    assert legacy_discovery._nonce_cache == {}

    await transport.aclose()
