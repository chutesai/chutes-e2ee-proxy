from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading
import time

import httpx


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    chute_id: str
    root: str
    created: int


class ModelCatalog:
    def __init__(self, api_base: str, api_key: str, ttl: float = 300.0):
        self._api_base = api_base.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}
        self._ttl = ttl
        self._loaded_at = 0.0
        self._refresh_lock = threading.Lock()
        self._async_refresh_lock = asyncio.Lock()
        self._entries_by_id: dict[str, ModelEntry] = {}
        self._entries_by_chute_id: dict[str, ModelEntry] = {}
        self._entries_by_root: dict[str, ModelEntry] = {}

    @property
    def loaded_at(self) -> float:
        return self._loaded_at

    @property
    def exact_model_map(self) -> dict[str, str]:
        return {entry.model_id: entry.chute_id for entry in self._entries_by_id.values()}

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def _needs_refresh(self) -> bool:
        return time.time() - self._loaded_at >= self._ttl

    def _update_map(self, payload: dict) -> None:
        entries_by_id: dict[str, ModelEntry] = {}
        entries_by_chute_id: dict[str, ModelEntry] = {}
        entries_by_root: dict[str, ModelEntry] = {}
        for item in payload.get("data", []):
            model_id = item.get("id")
            chute_id = item.get("chute_id")
            if not model_id or not chute_id:
                continue
            root = item.get("root") or model_id
            try:
                created = int(item.get("created") or 0)
            except (TypeError, ValueError):
                created = 0

            entry = ModelEntry(
                model_id=model_id,
                chute_id=chute_id,
                root=root,
                created=created,
            )
            entries_by_id[model_id] = entry
            entries_by_chute_id[chute_id] = entry

            current_root = entries_by_root.get(root)
            if current_root is None or entry.created >= current_root.created:
                entries_by_root[root] = entry

        self._entries_by_id = entries_by_id
        self._entries_by_chute_id = entries_by_chute_id
        self._entries_by_root = entries_by_root
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

    def get_by_id(self, model_id: str) -> ModelEntry | None:
        return self._entries_by_id.get(model_id)

    def get_by_root(self, root: str) -> ModelEntry | None:
        return self._entries_by_root.get(root)

    def get_by_chute_id(self, chute_id: str) -> ModelEntry | None:
        return self._entries_by_chute_id.get(chute_id)
