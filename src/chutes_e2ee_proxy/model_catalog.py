from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ModelCatalogEntry:
    model_id: str
    root: str | None
    chute_id: str
    confidential_compute: bool
    supported_features: tuple[str, ...]


class ModelCatalog:
    def __init__(self, api_base: str, api_key: str, ttl: float = 300.0):
        self._api_base = api_base.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}
        self._ttl = ttl
        self._loaded_at = 0.0
        self._refresh_lock = threading.Lock()
        self._async_refresh_lock = asyncio.Lock()
        self._models_by_id: dict[str, ModelCatalogEntry] = {}
        self._models_by_root: dict[str, list[ModelCatalogEntry]] = {}

    @property
    def loaded_at(self) -> float:
        return self._loaded_at

    @property
    def exact_model_map(self) -> dict[str, str]:
        return {entry.model_id: entry.chute_id for entry in self._models_by_id.values()}

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def _needs_refresh(self) -> bool:
        return time.time() - self._loaded_at >= self._ttl

    def _update_indexes(self, payload: dict) -> None:
        models_by_id: dict[str, ModelCatalogEntry] = {}
        models_by_root: dict[str, list[ModelCatalogEntry]] = {}
        for item in payload.get("data", []):
            model_id = item.get("id")
            chute_id = item.get("chute_id")
            if not model_id or not chute_id:
                continue
            root = item.get("root")
            supported_features = item.get("supported_features")
            if not isinstance(supported_features, list):
                supported_features = []
            entry = ModelCatalogEntry(
                model_id=model_id,
                root=root if isinstance(root, str) else None,
                chute_id=chute_id,
                confidential_compute=bool(item.get("confidential_compute")),
                supported_features=tuple(
                    feature for feature in supported_features if isinstance(feature, str)
                ),
            )
            models_by_id[entry.model_id] = entry
            if entry.root:
                models_by_root.setdefault(entry.root, []).append(entry)
        self._models_by_id = models_by_id
        self._models_by_root = models_by_root
        self._loaded_at = time.time()

    def _fetch(self, client: httpx.Client) -> None:
        response = client.get(
            f"{self._api_base}/v1/models",
            headers=self._auth_headers,
            timeout=15,
        )
        response.raise_for_status()
        self._update_indexes(response.json())

    async def _fetch_async(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{self._api_base}/v1/models",
            headers=self._auth_headers,
            timeout=15,
        )
        response.raise_for_status()
        self._update_indexes(response.json())

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

    def _root_match(self, model: str) -> ModelCatalogEntry | None:
        candidates = self._models_by_root.get(model, [])
        if len(candidates) == 1:
            return candidates[0]
        confidential_candidates = [item for item in candidates if item.confidential_compute]
        if len(confidential_candidates) == 1:
            return confidential_candidates[0]
        return None

    def resolve(self, model: str, client: httpx.Client | None = None) -> ModelCatalogEntry | None:
        self.maybe_refresh(client)
        entry = self._models_by_id.get(model) or self._root_match(model)
        if entry is not None:
            return entry
        self.invalidate()
        self.maybe_refresh(client)
        return self._models_by_id.get(model) or self._root_match(model)

    async def resolve_async(
        self,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> ModelCatalogEntry | None:
        await self.maybe_refresh_async(client)
        entry = self._models_by_id.get(model) or self._root_match(model)
        if entry is not None:
            return entry
        self.invalidate()
        await self.maybe_refresh_async(client)
        return self._models_by_id.get(model) or self._root_match(model)
