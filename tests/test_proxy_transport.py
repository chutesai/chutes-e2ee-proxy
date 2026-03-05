import json
import types

import httpx
import pytest

import chutes_e2ee_proxy.proxy_transport as proxy_transport
from chutes_e2ee_proxy.proxy_transport import ProxyAsyncChutesE2EETransport


def _mock_response(url: str, payload: dict | list, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", url), json=payload)


def _build_transport(*, models: list[dict], aliases: list[dict] | None = None, stats: list[dict] | None = None):
    aliases = aliases or []
    stats = stats or []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/v1/models"):
            return _mock_response(url, {"data": models})
        if url.endswith("/model_aliases/"):
            return _mock_response(url, aliases)
        if url.endswith("/invocations/stats/llm"):
            return _mock_response(url, stats)
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
async def test_transport_normalizes_ranked_selector_inside_e2ee_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_transport(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
            {"id": "model-b", "root": "model-b", "created": 1, "chute_id": "chute-b"},
        ],
        stats=[
            {"chute_id": "chute-a", "average_tps": 12.0, "average_ttft": 0.9},
            {"chute_id": "chute-b", "average_tps": 44.0, "average_ttft": 0.8},
        ],
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
        {"model": "model-a,model-b:throughput", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    request = httpx.Request(
        "POST",
        "https://llm.example/v1/chat/completions",
        content=original_body,
        headers={"Content-Type": "application/json"},
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert response.json()["chute_id"] == "chute-b"
    assert request.content == original_body
    assert captured == [
        {"model": "model-b", "messages": [{"role": "user", "content": "hi"}]}
    ]

    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_fails_over_to_next_candidate_on_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_transport(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
            {"id": "model-b", "root": "model-b", "created": 1, "chute_id": "chute-b"},
        ]
    )
    attempted_models: list[str] = []
    attempted_chutes: list[str] = []

    monkeypatch.setattr(
        proxy_transport,
        "build_e2ee_request",
        lambda _pubkey, payload: attempted_models.append(payload["model"])
        or types.SimpleNamespace(blob=b"blob", response_sk=b"sk"),
    )

    async def fake_handle_non_stream(self, url, headers, blob, response_sk, original_request):
        _ = url, blob, response_sk
        attempted_chutes.append(headers["X-Chute-Id"])
        if headers["X-Chute-Id"] == "chute-a":
            return httpx.Response(
                503,
                request=original_request,
                json={"detail": "Instance is at maximum capacity, try again later"},
            )
        return httpx.Response(200, request=original_request, json={"ok": True})

    transport._handle_non_stream = types.MethodType(fake_handle_non_stream, transport)

    request = httpx.Request(
        "POST",
        "https://llm.example/v1/chat/completions",
        json={"model": "model-a,model-b", "messages": [{"role": "user", "content": "hi"}]},
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert attempted_models == ["model-a", "model-b"]
    assert attempted_chutes == ["chute-a", "chute-b"]

    await transport.aclose()
