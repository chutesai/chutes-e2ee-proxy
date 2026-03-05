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

_LOGGER = logging.getLogger("chutes_e2ee_proxy.pool")


@dataclass
class _Entry:
    transport: Any
    last_used: float


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

        if upstream == e2e_upstream:
            return AsyncChutesE2EETransport(api_key=api_key, api_base=e2e_upstream)

        required_discovery_methods = (
            "_maybe_refresh_model_map",
            "_maybe_refresh_model_map_async",
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

        class _SplitModelDiscoveryManager(DiscoveryManager):
            """Compatibility shim for split upstream topology.

            Why this exists:
            - /v1/models currently resolves on the OpenAI-compatible upstream (for example llm.chutes.ai)
            - /e2e/* discovery+invoke resolves on the API upstream (for example api.chutes.ai)

            The upstream/e2e split requires overriding only model-map refresh while leaving
            nonce + instance discovery on DiscoveryManager's configured api_base.
            """

            def __init__(self, model_api_base: str, e2e_api_base: str, key: str):
                super().__init__(api_base=e2e_api_base, api_key=key)
                self._model_api_base = model_api_base.rstrip("/")

            def _maybe_refresh_model_map(self, client: httpx.Client) -> None:
                now = time.time()
                if now - self._model_map_loaded_at < self._MODEL_MAP_TTL:
                    return
                with self._model_map_lock:
                    if now - self._model_map_loaded_at < self._MODEL_MAP_TTL:
                        return
                    resp = client.get(
                        f"{self._model_api_base}/v1/models",
                        headers=self._auth_headers,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json().get("data", [])
                    new_map: dict[str, str] = {}
                    for entry in data:
                        model_id = entry.get("id")
                        chute_id = entry.get("chute_id")
                        if model_id and chute_id:
                            new_map[model_id] = chute_id
                    self._model_map = new_map
                    self._model_map_loaded_at = time.time()

            async def _maybe_refresh_model_map_async(self, client: httpx.AsyncClient) -> None:
                now = time.time()
                if now - self._model_map_loaded_at < self._MODEL_MAP_TTL:
                    return
                resp = await client.get(
                    f"{self._model_api_base}/v1/models",
                    headers=self._auth_headers,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                new_map: dict[str, str] = {}
                for entry in data:
                    model_id = entry.get("id")
                    chute_id = entry.get("chute_id")
                    if model_id and chute_id:
                        new_map[model_id] = chute_id
                self._model_map = new_map
                self._model_map_loaded_at = time.time()

        transport = AsyncChutesE2EETransport(api_key=api_key, api_base=e2e_upstream)
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
            transport._discovery = discovery
        except Exception as exc:  # pragma: no cover - defensive branch
            raise RuntimeError(
                "Split discovery compatibility shim cannot be applied because "
                "AsyncChutesE2EETransport._discovery is not writable."
            ) from exc

        split_discovery = _SplitModelDiscoveryManager(
            model_api_base=upstream,
            e2e_api_base=e2e_upstream,
            key=api_key,
        )
        transport._discovery = split_discovery
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
