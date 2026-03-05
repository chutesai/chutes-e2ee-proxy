from __future__ import annotations

import asyncio
import threading
import time
import httpx


class ModelCatalog:
    def __init__(self, api_base: str, api_key: str, ttl: float = 300.0):
        self._api_base = api_base.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}
        self._ttl = ttl
        self._loaded_at = 0.0
        self._refresh_lock = threading.Lock()
        self._async_refresh_lock = asyncio.Lock()
        self._model_map: dict[str, str] = {}

    @property
    def loaded_at(self) -> float:
        return self._loaded_at

    @property
    def exact_model_map(self) -> dict[str, str]:
        return dict(self._model_map)

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def _needs_refresh(self) -> bool:
        return time.time() - self._loaded_at >= self._ttl

    def _update_map(self, payload: dict) -> None:
        model_map: dict[str, str] = {}
        for item in payload.get("data", []):
            model_id = item.get("id")
            chute_id = item.get("chute_id")
            if not model_id or not chute_id:
                continue
            model_map[model_id] = chute_id
        self._model_map = model_map
        self._loaded_at = time.time()

    def _fetch(self, client: httpx.Client) -> None:
        response = client.get(
            f"{self._api_base}/v1/models",
            headers=self._auth_headers,
            timeout=15,
        )
        response.raise_for_status()
        self._update_map(response.json())

    async def _fetch_async(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{self._api_base}/v1/models",
            headers=self._auth_headers,
            timeout=15,
        )
        response.raise_for_status()
        self._update_map(response.json())

    def maybe_refresh(self, client: httpx.Client | None = None) -> None:
        if not self._needs_refresh():
            return
        with self._refresh_lock:
            if not self._needs_refresh():
                return
            if client is not None:
                self._fetch(client)
                return
            with httpx.Client() as owned_client:
                self._fetch(owned_client)

    async def maybe_refresh_async(self, client: httpx.AsyncClient | None = None) -> None:
        if not self._needs_refresh():
            return
        async with self._async_refresh_lock:
            if not self._needs_refresh():
                return
            if client is not None:
                await self._fetch_async(client)
                return
            async with httpx.AsyncClient() as owned_client:
                await self._fetch_async(owned_client)

    def resolve(self, model: str, client: httpx.Client | None = None) -> str | None:
        self.maybe_refresh(client)
        chute_id = self._model_map.get(model)
        if chute_id is not None:
            return chute_id
        self.invalidate()
        self.maybe_refresh(client)
        return self._model_map.get(model)

    async def resolve_async(
        self,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> str | None:
        await self.maybe_refresh_async(client)
        chute_id = self._model_map.get(model)
        if chute_id is not None:
            return chute_id
        self.invalidate()
        await self.maybe_refresh_async(client)
        return self._model_map.get(model)
