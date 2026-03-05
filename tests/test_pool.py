import asyncio
import sys
import threading
import types

import httpx
import pytest

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.pool import TransportPool


class FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FailingCloseTransport:
    async def aclose(self) -> None:
        raise RuntimeError("close failed")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return self._payload


class _FakeSyncClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, headers: dict | None = None, timeout: int | None = None) -> _FakeResponse:
        _ = headers, timeout
        self.urls.append(url)
        if url.endswith("/v1/models"):
            return _FakeResponse({"data": [{"id": "model-1", "chute_id": "chute-1"}]})
        return _FakeResponse({"instances": [], "nonce_expires_in": 55})


class _FakeAsyncClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(
        self, url: str, headers: dict | None = None, timeout: int | None = None
    ) -> _FakeResponse:
        _ = headers, timeout
        self.urls.append(url)
        if url.endswith("/v1/models"):
            return _FakeResponse({"data": [{"id": "model-1", "chute_id": "chute-1"}]})
        return _FakeResponse({"instances": [], "nonce_expires_in": 55})


def _install_fake_transport_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing_method: bool = False,
    missing_required_attr: bool = False,
) -> None:
    fake_chutes_e2ee = types.ModuleType("chutes_e2ee")
    fake_discovery_module = types.ModuleType("chutes_e2ee.discovery")

    class FakeDiscoveryManager:
        _MODEL_MAP_TTL = 300

        def __init__(self, api_base: str, api_key: str):
            self._api_base = api_base.rstrip("/")
            self._api_key = api_key
            self._auth_headers = {"Authorization": f"Bearer {api_key}"}
            self._model_map: dict[str, str] = {}
            self._model_map_loaded_at = 0.0
            if not missing_required_attr:
                self._model_map_lock = threading.Lock()

        def _maybe_refresh_model_map(self, client) -> None:
            response = client.get(f"{self._api_base}/v1/models", headers=self._auth_headers, timeout=15)
            response.raise_for_status()
            self._model_map_loaded_at = 1.0

        def _fetch_instances(self, chute_id: str, client):
            response = client.get(
                f"{self._api_base}/e2e/instances/{chute_id}",
                headers=self._auth_headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return types.SimpleNamespace(
                instances=list(data.get("instances", [])),
                nonce_expires_at=float(data.get("nonce_expires_in", 55)),
            )

        async def _fetch_instances_async(self, chute_id: str, client):
            response = await client.get(
                f"{self._api_base}/e2e/instances/{chute_id}",
                headers=self._auth_headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return types.SimpleNamespace(
                instances=list(data.get("instances", [])),
                nonce_expires_at=float(data.get("nonce_expires_in", 55)),
            )

    if not missing_method:

        async def _maybe_refresh_model_map_async(self, client) -> None:
            response = await client.get(
                f"{self._api_base}/v1/models",
                headers=self._auth_headers,
                timeout=15,
            )
            response.raise_for_status()
            self._model_map_loaded_at = 1.0

        FakeDiscoveryManager._maybe_refresh_model_map_async = _maybe_refresh_model_map_async

    class FakeTransportImpl:
        def __init__(self, api_key: str, api_base: str):
            self._api_key = api_key
            self._api_base = api_base.rstrip("/")
            self._discovery = FakeDiscoveryManager(api_base=self._api_base, api_key=api_key)

        async def aclose(self) -> None:
            return

    fake_chutes_e2ee.AsyncChutesE2EETransport = FakeTransportImpl
    fake_discovery_module.DiscoveryManager = FakeDiscoveryManager

    monkeypatch.setitem(sys.modules, "chutes_e2ee", fake_chutes_e2ee)
    monkeypatch.setitem(sys.modules, "chutes_e2ee.discovery", fake_discovery_module)


@pytest.mark.asyncio
async def test_pool_reuses_transport_for_same_key() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str, _e2e_upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", "https://api.chutes.ai", transport_factory=factory)
    one = await pool.get("k1")
    two = await pool.get("k1")

    assert one is two
    assert len(created) == 1

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_evicts_oldest_when_max_size_reached() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str, _e2e_upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool(
        "https://llm.chutes.ai",
        "https://api.chutes.ai",
        max_size=2,
        transport_factory=factory,
    )

    t1 = await pool.get("k1")
    await asyncio.sleep(0)
    _t2 = await pool.get("k2")
    _t3 = await pool.get("k3")

    assert t1.closed is True

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_cleanup_evicts_idle_entries() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str, _e2e_upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool(
        "https://llm.chutes.ai",
        "https://api.chutes.ai",
        idle_ttl=0.0,
        transport_factory=factory,
    )
    _t1 = await pool.get("k1")
    await pool.cleanup()

    assert created[0].closed is True

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_close_all_closes_every_transport() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str, _e2e_upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", "https://api.chutes.ai", transport_factory=factory)
    await pool.get("k1")
    await pool.get("k2")

    await pool.close_all()

    assert all(t.closed for t in created)


@pytest.mark.asyncio
async def test_default_factory_uses_split_model_discovery_when_upstreams_differ() -> None:
    pytest.importorskip("chutes_e2ee")

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.chutes.ai",
        "https://api.chutes.ai",
    )
    try:
        assert transport._api_base == "https://api.chutes.ai"
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_default_factory_uses_transport_default_when_upstreams_match() -> None:
    pytest.importorskip("chutes_e2ee")

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.chutes.ai",
        "https://llm.chutes.ai",
    )
    try:
        assert transport._api_base == "https://llm.chutes.ai"
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_default_factory_split_manager_targets_model_and_e2e_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery
    sync_client = _FakeSyncClient()
    async_client = _FakeAsyncClient()

    discovery._model_map_loaded_at = 0.0
    discovery._maybe_refresh_model_map(sync_client)
    assert sync_client.urls == ["https://llm.example/v1/models"]

    discovery._model_map_loaded_at = 0.0
    await discovery._maybe_refresh_model_map_async(async_client)
    assert async_client.urls == ["https://llm.example/v1/models"]

    sync_client.urls.clear()
    discovery._fetch_instances("chute-123", sync_client)
    assert sync_client.urls == ["https://api.example/e2e/instances/chute-123"]


@pytest.mark.asyncio
async def test_default_factory_resolves_exact_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    class ModelAsyncClient:
        async def get(self, url: str, headers: dict | None = None, timeout: int | None = None):
            _ = headers, timeout
            request = httpx.Request("GET", url)
            if url.endswith("/v1/models"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"data": [{"id": "model-1", "chute_id": "chute-1"}]},
                )
            raise AssertionError(url)

    assert await discovery.resolve_chute_id_async("model-1", ModelAsyncClient()) == "chute-1"


@pytest.mark.asyncio
async def test_default_factory_rejects_uuid_selector_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    with pytest.raises(ProxyRequestError, match="use an exact model id"):
        await discovery.resolve_chute_id_async(
            "08a7a60f-6956-5a9e-9983-5603c3ac5a38",
            _FakeAsyncClient(),
        )


@pytest.mark.asyncio
async def test_default_factory_rejects_noncanonical_public_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    class RootAsyncClient:
        async def get(self, url: str, headers: dict | None = None, timeout: int | None = None):
            _ = headers, timeout
            request = httpx.Request("GET", url)
            if url.endswith("/v1/models"):
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "data": [
                            {
                                "id": "zai-org/GLM-4.7-TEE",
                                "root": "zai-org/GLM-4.7",
                                "chute_id": "chute-tee",
                            }
                        ]
                    },
                )
            raise AssertionError(url)

    with pytest.raises(ProxyRequestError, match="model not found: zai-org/GLM-4.7"):
        await discovery.resolve_chute_id_async("zai-org/GLM-4.7", RootAsyncClient())


@pytest.mark.asyncio
async def test_default_factory_rejects_ordered_failover_lists_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    with pytest.raises(ProxyRequestError, match="use an exact model id"):
        await discovery.resolve_chute_id_async("model-a, model-b", _FakeAsyncClient())


@pytest.mark.asyncio
async def test_default_factory_rejects_metric_routing_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    with pytest.raises(ProxyRequestError, match="use an exact model id"):
        await discovery.resolve_chute_id_async("model-1:latency", _FakeAsyncClient())


@pytest.mark.asyncio
async def test_default_factory_maps_instance_discovery_errors_to_contract_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch)

    transport = TransportPool._default_factory(
        "cpk_test",
        "https://llm.example",
        "https://api.example",
    )
    discovery = transport._discovery

    class UnavailableAsyncClient:
        async def get(self, url: str, headers: dict | None = None, timeout: int | None = None):
            _ = headers, timeout
            request = httpx.Request("GET", url)
            if url.endswith("/e2e/instances/chute-1"):
                return httpx.Response(
                    404,
                    request=request,
                    json={"detail": "No E2E-capable instances available"},
                )
            raise AssertionError(url)

    client = UnavailableAsyncClient()
    with pytest.raises(ProxyRequestError, match="No E2E-capable instances available"):
        await discovery._fetch_instances_async("chute-1", client)


def test_default_factory_split_manager_contract_check_for_missing_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch, missing_method=True)

    with pytest.raises(RuntimeError, match="missing required methods"):
        TransportPool._default_factory(
            "cpk_test",
            "https://llm.example",
            "https://api.example",
        )


def test_default_factory_split_manager_contract_check_for_missing_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport_modules(monkeypatch, missing_required_attr=True)

    with pytest.raises(RuntimeError, match="missing required fields"):
        TransportPool._default_factory(
            "cpk_test",
            "https://llm.example",
            "https://api.example",
        )


@pytest.mark.asyncio
async def test_pool_close_all_continues_when_one_transport_close_fails() -> None:
    created: dict[str, object] = {}

    def factory(api_key: str, _upstream: str, _e2e_upstream: str) -> object:
        if api_key == "bad":
            transport = FailingCloseTransport()
        else:
            transport = FakeTransport()
        created[api_key] = transport
        return transport

    pool = TransportPool("https://llm.chutes.ai", "https://api.chutes.ai", transport_factory=factory)
    await pool.get("bad")
    await pool.get("good")

    await pool.close_all()

    assert created["good"].closed is True


@pytest.mark.asyncio
async def test_pool_cleanup_continues_when_one_transport_close_fails() -> None:
    created: dict[str, object] = {}

    def factory(api_key: str, _upstream: str, _e2e_upstream: str) -> object:
        if api_key == "bad":
            transport = FailingCloseTransport()
        else:
            transport = FakeTransport()
        created[api_key] = transport
        return transport

    pool = TransportPool(
        "https://llm.chutes.ai",
        "https://api.chutes.ai",
        idle_ttl=0.0,
        transport_factory=factory,
    )
    await pool.get("bad")
    await pool.get("good")

    await pool.cleanup()

    assert created["good"].closed is True
    await pool.close_all()
