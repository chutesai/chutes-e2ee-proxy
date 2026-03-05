from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.model_catalog import ModelCatalog, ModelEntry

_ROUTING_SUFFIXES = (":latency", ":throughput")


@dataclass(frozen=True)
class ModelSelection:
    model_id: str
    chute_id: str


class AliasCatalog:
    def __init__(self, api_base: str, api_key: str, ttl: float = 120.0):
        self._api_base = api_base.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}
        self._ttl = ttl
        self._loaded_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self._alias_map: dict[str, tuple[str, ...]] = {}

    def _needs_refresh(self) -> bool:
        return time.time() - self._loaded_at >= self._ttl

    async def _fetch_async(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{self._api_base}/model_aliases/",
            headers=self._auth_headers,
            timeout=15,
        )
        if response.status_code == 404:
            self._alias_map = {}
            self._loaded_at = time.time()
            return
        response.raise_for_status()
        alias_map: dict[str, tuple[str, ...]] = {}
        for item in response.json():
            alias = item.get("alias")
            chute_ids = item.get("chute_ids")
            if not alias or not isinstance(chute_ids, list):
                continue
            normalized = tuple(
                chute_id.strip()
                for chute_id in chute_ids
                if isinstance(chute_id, str) and chute_id.strip()
            )
            if normalized:
                alias_map[alias.lower()] = normalized
        self._alias_map = alias_map
        self._loaded_at = time.time()

    async def maybe_refresh_async(self, client: httpx.AsyncClient) -> None:
        if not self._needs_refresh():
            return
        async with self._refresh_lock:
            if not self._needs_refresh():
                return
            await self._fetch_async(client)

    async def get_async(self, alias: str, client: httpx.AsyncClient) -> tuple[str, ...] | None:
        await self.maybe_refresh_async(client)
        return self._alias_map.get(alias.lower())


class ModelSelector:
    def __init__(
        self,
        model_api_base: str,
        api_base: str,
        api_key: str,
        *,
        model_ttl: float = 300.0,
        alias_ttl: float = 120.0,
    ):
        self._models = ModelCatalog(model_api_base, api_key, ttl=model_ttl)
        self._aliases = AliasCatalog(api_base, api_key, ttl=alias_ttl)

    async def resolve_async(
        self,
        model: str,
        client: httpx.AsyncClient,
    ) -> ModelSelection:
        requested = model.strip()
        if not requested:
            raise self._model_not_found(model)

        await self._models.maybe_refresh_async(client)

        direct = self._lookup_direct(requested)
        if direct is not None:
            return ModelSelection(model_id=direct.model_id, chute_id=direct.chute_id)

        if self._is_unsupported_selector(requested):
            raise self._unsupported_selector(requested)

        alias_chute_ids = await self._aliases.get_async(requested, client)
        if alias_chute_ids is None:
            raise self._model_not_found(requested)
        if len(alias_chute_ids) != 1:
            raise self._unsupported_selector(requested)

        entry = self._models.get_by_chute_id(alias_chute_ids[0])
        if entry is None:
            raise self._model_not_found(requested)

        return ModelSelection(model_id=entry.model_id, chute_id=entry.chute_id)

    def _lookup_direct(self, token: str) -> ModelEntry | None:
        entry = self._models.get_by_id(token)
        if entry is not None:
            return entry
        entry = self._models.get_by_chute_id(token)
        if entry is not None:
            return entry
        return self._models.get_by_root(token)

    @staticmethod
    def _is_unsupported_selector(model: str) -> bool:
        lowered = model.lower()
        return "," in model or any(lowered.endswith(suffix) for suffix in _ROUTING_SUFFIXES)

    @staticmethod
    def _unsupported_selector(model: str) -> ProxyRequestError:
        return ProxyRequestError(
            400,
            "unsupported_model_selector",
            "E2EE proxy supports a single resolved model target per request. "
            f"Comma-separated selectors, multi-target aliases, and metric routing are not "
            f"supported: {model}",
        )

    @staticmethod
    def _model_not_found(model: str) -> ProxyRequestError:
        return ProxyRequestError(404, "model_not_found", f"model not found: {model}")
