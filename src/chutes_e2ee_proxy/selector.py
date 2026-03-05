from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.model_catalog import ModelCatalog, ModelEntry

_ROUTING_SUFFIXES = (":latency", ":throughput")


def _parse_model_parameter(model_str: str) -> tuple[str, str | None]:
    model_str = model_str.strip()
    lowered = model_str.lower()
    for suffix in _ROUTING_SUFFIXES:
        if lowered.endswith(suffix):
            return model_str[: -len(suffix)], suffix[1:]
    return model_str, None


def _dedupe_keep_order(items: list[ModelEntry]) -> list[ModelEntry]:
    seen: set[str] = set()
    deduped: list[ModelEntry] = []
    for item in items:
        if item.chute_id in seen:
            continue
        seen.add(item.chute_id)
        deduped.append(item)
    return deduped


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


class StatsCatalog:
    def __init__(self, api_base: str, ttl: float = 300.0):
        self._api_base = api_base.rstrip("/")
        self._ttl = ttl
        self._loaded_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self._stats_by_chute_id: dict[str, tuple[float | None, float | None]] = {}

    def _needs_refresh(self) -> bool:
        return time.time() - self._loaded_at >= self._ttl

    async def _fetch_async(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{self._api_base}/invocations/stats/llm",
            timeout=15,
        )
        response.raise_for_status()
        stats_by_chute_id: dict[str, tuple[float | None, float | None]] = {}
        for item in response.json():
            chute_id = item.get("chute_id")
            if not chute_id:
                continue
            average_tps = item.get("average_tps")
            average_ttft = item.get("average_ttft")
            stats_by_chute_id[chute_id] = (
                float(average_tps) if average_tps is not None else None,
                float(average_ttft) if average_ttft is not None else None,
            )
        self._stats_by_chute_id = stats_by_chute_id
        self._loaded_at = time.time()

    async def maybe_refresh_async(self, client: httpx.AsyncClient) -> None:
        if not self._needs_refresh():
            return
        async with self._refresh_lock:
            if not self._needs_refresh():
                return
            await self._fetch_async(client)

    async def get_async(
        self, chute_id: str, client: httpx.AsyncClient
    ) -> tuple[float | None, float | None]:
        try:
            await self.maybe_refresh_async(client)
        except httpx.HTTPError:
            return (None, None)
        return self._stats_by_chute_id.get(chute_id, (None, None))


class ModelSelector:
    def __init__(
        self,
        model_api_base: str,
        api_base: str,
        api_key: str,
        *,
        model_ttl: float = 300.0,
        alias_ttl: float = 120.0,
        stats_ttl: float = 300.0,
    ):
        self._models = ModelCatalog(model_api_base, api_key, ttl=model_ttl)
        self._aliases = AliasCatalog(api_base, api_key, ttl=alias_ttl)
        self._stats = StatsCatalog(api_base, ttl=stats_ttl)

    async def resolve_async(
        self,
        model: str,
        client: httpx.AsyncClient,
    ) -> list[ModelSelection]:
        requested = model.strip()
        if not requested:
            raise self._model_not_found(model)

        await self._models.maybe_refresh_async(client)

        exact = self._lookup_direct(requested)
        if exact is not None:
            return [ModelSelection(model_id=exact.model_id, chute_id=exact.chute_id)]

        raw_model, routing_mode = _parse_model_parameter(requested)
        if not raw_model:
            raise self._model_not_found(model)

        if "," in raw_model:
            tokens = [token.strip() for token in raw_model.split(",") if token.strip()]
            entries: list[ModelEntry] = []
            for token in tokens:
                entries.extend(await self._expand_token_async(token, client))
        else:
            entries = await self._expand_token_async(raw_model, client)

        deduped = _dedupe_keep_order(entries)
        if not deduped:
            raise self._model_not_found(model)

        ranked = await self._rank_async(deduped, routing_mode, client)
        return [ModelSelection(model_id=entry.model_id, chute_id=entry.chute_id) for entry in ranked]

    def _lookup_direct(self, token: str) -> ModelEntry | None:
        entry = self._models.get_by_id(token)
        if entry is not None:
            return entry
        entry = self._models.get_by_chute_id(token)
        if entry is not None:
            return entry
        return self._models.get_by_root(token)

    async def _expand_token_async(
        self,
        token: str,
        client: httpx.AsyncClient,
    ) -> list[ModelEntry]:
        direct = self._lookup_direct(token)
        if direct is not None:
            return [direct]

        alias_chute_ids = await self._aliases.get_async(token, client)
        if not alias_chute_ids:
            return []

        resolved: list[ModelEntry] = []
        for chute_id in alias_chute_ids:
            entry = self._models.get_by_chute_id(chute_id)
            if entry is not None:
                resolved.append(entry)
        return resolved

    async def _rank_async(
        self,
        entries: list[ModelEntry],
        routing_mode: str | None,
        client: httpx.AsyncClient,
    ) -> list[ModelEntry]:
        if routing_mode is None or len(entries) < 2:
            return entries

        indexed = list(enumerate(entries))
        scored: list[tuple[int, ModelEntry, float | None]] = []
        for index, entry in indexed:
            average_tps, average_ttft = await self._stats.get_async(entry.chute_id, client)
            score = average_tps if routing_mode == "throughput" else average_ttft
            scored.append((index, entry, score))

        if routing_mode == "throughput":
            scored.sort(
                key=lambda item: (
                    item[2] is None,
                    0.0 if item[2] is None else -item[2],
                    item[0],
                )
            )
        else:
            scored.sort(
                key=lambda item: (
                    item[2] is None,
                    float("inf") if item[2] is None else item[2],
                    item[0],
                )
            )

        return [entry for _, entry, _ in scored]

    @staticmethod
    def _model_not_found(model: str) -> ProxyRequestError:
        return ProxyRequestError(404, "model_not_found", f"model not found: {model}")
