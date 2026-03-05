"""Transport pooling with compatibility shims for split upstream topology.

This proxy may need different upstream hosts for:
- model listing (`/v1/models`) on the OpenAI-compatible host
- E2EE discovery/invoke (`/e2e/*`) on the API host

When those hosts differ, a DiscoveryManager shim routes model-map refreshes to the
OpenAI-compatible host while keeping nonce discovery on the API host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.model_catalog import ModelCatalog

_LOGGER = logging.getLogger("chutes_e2ee_proxy.pool")


@dataclass
class _Entry:
    transport: Any
    last_used: float


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 5:
        return False
    try:
        int(value.replace("-", ""), 16)
        return len(value) == 36
    except ValueError:
        return False


def _build_proxy_discovery_manager(base_manager_cls):
    class ProxyDiscoveryManager(base_manager_cls):
        def __init__(self, model_api_base: str, e2e_api_base: str, api_key: str):
            super().__init__(api_base=e2e_api_base, api_key=api_key)
            self._model_catalog = ModelCatalog(model_api_base, api_key, ttl=self._MODEL_MAP_TTL)

        def _maybe_refresh_model_map(self, client: httpx.Client) -> None:
            if self._model_map_loaded_at == 0.0:
                self._model_catalog.invalidate()
            self._model_catalog.maybe_refresh(client)
            self._model_map = self._model_catalog.exact_model_map
            self._model_map_loaded_at = self._model_catalog.loaded_at

        async def _maybe_refresh_model_map_async(self, client: httpx.AsyncClient) -> None:
            if self._model_map_loaded_at == 0.0:
                self._model_catalog.invalidate()
            await self._model_catalog.maybe_refresh_async(client)
            self._model_map = self._model_catalog.exact_model_map
            self._model_map_loaded_at = self._model_catalog.loaded_at

        def _model_not_found(self, model: str) -> ProxyRequestError:
            return ProxyRequestError(
                404,
                "model_not_found",
                f"model not found: {model}. Use an exact model id returned by /v1/models.",
            )

        def _unsupported_selector(self, model: str) -> ProxyRequestError:
            return ProxyRequestError(
                400,
                "unsupported_model_selector",
                f"E2EE proxy request bodies are passed through unchanged; use an exact model id from "
                f"/v1/models instead of {model!r}.",
            )

        def _normalize_instances_error(self, chute_id: str, exc: httpx.HTTPStatusError) -> ProxyRequestError:
            detail = None
            with contextlib.suppress(Exception):
                payload = exc.response.json()
                if isinstance(payload, dict):
                    detail = payload.get("detail")
            if exc.response.status_code == 401:
                return ProxyRequestError(401, "unauthorized", detail or "Authentication required.")
            if detail == "Chute not found":
                return self._model_not_found(chute_id)
            if detail in {"No active instances found for this chute", "No E2E-capable instances available"}:
                return ProxyRequestError(503, "model_unavailable", detail)
            return ProxyRequestError(
                502,
                "proxy_error",
                detail or f"Failed to resolve E2EE-capable instances for chute {chute_id}",
            )

        def _validate_requested_model(self, model: str) -> str:
            requested = model.strip()
            lowered = requested.lower()
            if (
                not requested
                or _looks_like_uuid(requested)
                or "," in requested
                or lowered.endswith(":latency")
                or lowered.endswith(":throughput")
            ):
                raise self._unsupported_selector(model)
            return requested

        def resolve_chute_id(self, model: str, client: httpx.Client) -> str:
            requested = self._validate_requested_model(model)
            chute_id = self._model_catalog.resolve(requested, client)
            if chute_id is None:
                raise self._model_not_found(requested)
            self._model_map = self._model_catalog.exact_model_map
            self._model_map_loaded_at = self._model_catalog.loaded_at
            return chute_id

        async def resolve_chute_id_async(self, model: str, client: httpx.AsyncClient) -> str:
            requested = self._validate_requested_model(model)
            chute_id = await self._model_catalog.resolve_async(requested, client)
            if chute_id is None:
                raise self._model_not_found(requested)
            self._model_map = self._model_catalog.exact_model_map
            self._model_map_loaded_at = self._model_catalog.loaded_at
            return chute_id

        def _fetch_instances(self, chute_id: str, client: httpx.Client):
            try:
                return super()._fetch_instances(chute_id, client)
            except httpx.HTTPStatusError as exc:
                raise self._normalize_instances_error(chute_id, exc) from exc

        async def _fetch_instances_async(self, chute_id: str, client: httpx.AsyncClient):
            try:
                return await super()._fetch_instances_async(chute_id, client)
            except httpx.HTTPStatusError as exc:
                raise self._normalize_instances_error(chute_id, exc) from exc

    return ProxyDiscoveryManager


class TransportPool:
    def __init__(
        self,
        upstream: str,
        e2e_upstream: str,
        max_size: int = 64,
        idle_ttl: float = 300.0,
        cleanup_interval: float = 60.0,
        transport_factory: Callable[[str, str, str], Any] | None = None,
    ):
        self._upstream = upstream
        self._e2e_upstream = e2e_upstream
        self._max_size = max_size
        self._idle_ttl = idle_ttl
        self._cleanup_interval = cleanup_interval
        self._transport_factory = transport_factory or self._default_factory

        self._pool: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None

    @staticmethod
    def _default_factory(api_key: str, upstream: str, e2e_upstream: str) -> Any:
        from chutes_e2ee import AsyncChutesE2EETransport
        from chutes_e2ee.discovery import DiscoveryManager

        required_discovery_methods = (
            "_maybe_refresh_model_map",
            "_maybe_refresh_model_map_async",
            "_fetch_instances",
            "_fetch_instances_async",
        )
        missing_methods = [
            method for method in required_discovery_methods if not hasattr(DiscoveryManager, method)
        ]
        if missing_methods:
            joined = ", ".join(sorted(missing_methods))
            raise RuntimeError(
                "Split discovery compatibility shim is incompatible with installed chutes-e2ee: "
                f"DiscoveryManager is missing required methods: {joined}. "
                "Upgrade/downgrade chutes-e2ee or set --e2e-upstream equal to --upstream."
            )

        transport = AsyncChutesE2EETransport(api_key=api_key, api_base=e2e_upstream)
        proxy_discovery_cls = _build_proxy_discovery_manager(DiscoveryManager)
        discovery = getattr(transport, "_discovery", None)
        if discovery is None:
            raise RuntimeError(
                "Split discovery compatibility shim is incompatible with installed chutes-e2ee: "
                "AsyncChutesE2EETransport has no _discovery attribute."
            )

        required_discovery_attrs = (
            "_model_map",
            "_model_map_loaded_at",
            "_MODEL_MAP_TTL",
            "_auth_headers",
            "_model_map_lock",
        )
        missing_attrs = [
            attr for attr in required_discovery_attrs if not hasattr(discovery, attr)
        ]
        if missing_attrs:
            joined = ", ".join(sorted(missing_attrs))
            raise RuntimeError(
                "Split discovery compatibility shim is incompatible with installed chutes-e2ee: "
                f"discovery object is missing required fields: {joined}. "
                "Upgrade/downgrade chutes-e2ee or set --e2e-upstream equal to --upstream."
            )

        try:
            transport._discovery = proxy_discovery_cls(
                model_api_base=upstream,
                e2e_api_base=e2e_upstream,
                api_key=api_key,
            )
        except Exception as exc:  # pragma: no cover - defensive branch
            raise RuntimeError(
                "Proxy discovery compatibility shim cannot be applied because "
                "AsyncChutesE2EETransport._discovery could not be replaced."
            ) from exc
        return transport

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._cleanup_interval)
                await self.cleanup()
        except asyncio.CancelledError:
            return

    async def get(self, api_key: str) -> Any:
        to_close: list[Any] = []
        now = time.monotonic()

        async with self._lock:
            if self._closed:
                raise RuntimeError("TransportPool is closed")

            entry = self._pool.get(api_key)
            if entry is not None:
                entry.last_used = now
                return entry.transport

            if len(self._pool) >= self._max_size:
                oldest_key = min(self._pool, key=lambda k: self._pool[k].last_used)
                oldest = self._pool.pop(oldest_key)
                to_close.append(oldest.transport)

            transport = self._transport_factory(api_key, self._upstream, self._e2e_upstream)
            self._pool[api_key] = _Entry(transport=transport, last_used=now)

        for transport in to_close:
            await self._close_transport(transport)

        return transport

    async def cleanup(self) -> None:
        now = time.monotonic()
        to_close: list[Any] = []

        async with self._lock:
            expired_keys = [
                key for key, entry in self._pool.items() if now - entry.last_used > self._idle_ttl
            ]
            for key in expired_keys:
                to_close.append(self._pool.pop(key).transport)

        for transport in to_close:
            await self._close_transport(transport)

    async def close_all(self) -> None:
        async with self._lock:
            self._closed = True
            transports = [entry.transport for entry in self._pool.values()]
            self._pool.clear()
            cleanup_task = self._cleanup_task
            self._cleanup_task = None

        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task

        for transport in transports:
            await self._close_transport(transport)

    async def aclose(self) -> None:
        await self.close_all()

    async def _close_transport(self, transport: Any) -> None:
        try:
            close_fn = getattr(transport, "aclose", None)
            if close_fn is not None:
                await close_fn()
                return

            close_sync = getattr(transport, "close", None)
            if close_sync is not None:
                close_sync()
        except Exception as exc:  # pragma: no cover - defensive branch
            _LOGGER.warning(
                "transport close failed",
                extra={"fields": {"transport_type": type(transport).__name__}},
                exc_info=exc,
            )

    def stats(self) -> dict[str, int | float]:
        return {
            "size": len(self._pool),
            "max_size": self._max_size,
            "idle_ttl": self._idle_ttl,
            "e2e_upstream_split": int(self._upstream != self._e2e_upstream),
        }
