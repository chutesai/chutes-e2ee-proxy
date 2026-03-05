import httpx
import pytest

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.selector import ModelSelector


class FakeAsyncClient:
    def __init__(
        self,
        *,
        models: list[dict],
        aliases: list[dict] | None = None,
        stats: list[dict] | None = None,
    ) -> None:
        self.models = models
        self.aliases = aliases or []
        self.stats = stats or []
        self.urls: list[str] = []

    async def get(
        self,
        url: str,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        _ = headers, timeout
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/v1/models"):
            return httpx.Response(200, request=request, json={"data": self.models})
        if url.endswith("/model_aliases/"):
            return httpx.Response(200, request=request, json=self.aliases)
        if url.endswith("/invocations/stats/llm"):
            return httpx.Response(200, request=request, json=self.stats)
        raise AssertionError(url)


@pytest.mark.asyncio
async def test_selector_resolves_exact_ids_roots_and_chute_ids() -> None:
    client = FakeAsyncClient(
        models=[
            {
                "id": "zai-org/GLM-5-TEE",
                "root": "zai-org/GLM-5",
                "created": 2,
                "chute_id": "chute-glm5",
            }
        ]
    )
    selector = ModelSelector(
        model_api_base="https://llm.example",
        api_base="https://api.example",
        api_key="cpk_test",
    )

    by_id = await selector.resolve_async("zai-org/GLM-5-TEE", client)
    by_root = await selector.resolve_async("zai-org/GLM-5", client)
    by_chute = await selector.resolve_async("chute-glm5", client)

    assert by_id[0].model_id == "zai-org/GLM-5-TEE"
    assert by_root[0].model_id == "zai-org/GLM-5-TEE"
    assert by_chute[0].model_id == "zai-org/GLM-5-TEE"


@pytest.mark.asyncio
async def test_selector_expands_aliases_with_ordered_deduplication() -> None:
    client = FakeAsyncClient(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
            {"id": "model-b", "root": "model-b", "created": 1, "chute_id": "chute-b"},
        ],
        aliases=[{"alias": "default", "chute_ids": ["chute-a", "chute-b", "chute-a"]}],
    )
    selector = ModelSelector(
        model_api_base="https://llm.example",
        api_base="https://api.example",
        api_key="cpk_test",
    )

    resolved = await selector.resolve_async("default", client)

    assert [(item.model_id, item.chute_id) for item in resolved] == [
        ("model-a", "chute-a"),
        ("model-b", "chute-b"),
    ]


@pytest.mark.asyncio
async def test_selector_ranks_comma_lists_by_throughput() -> None:
    client = FakeAsyncClient(
        models=[
            {"id": "model-a", "root": "model-a", "created": 1, "chute_id": "chute-a"},
            {"id": "model-b", "root": "model-b", "created": 1, "chute_id": "chute-b"},
        ],
        stats=[
            {"chute_id": "chute-a", "average_tps": 12.5, "average_ttft": 1.4},
            {"chute_id": "chute-b", "average_tps": 44.0, "average_ttft": 0.8},
        ],
    )
    selector = ModelSelector(
        model_api_base="https://llm.example",
        api_base="https://api.example",
        api_key="cpk_test",
    )

    resolved = await selector.resolve_async("model-a,model-b:throughput", client)

    assert [item.model_id for item in resolved] == ["model-b", "model-a"]


@pytest.mark.asyncio
async def test_selector_raises_model_not_found_when_no_candidate_exists() -> None:
    client = FakeAsyncClient(models=[])
    selector = ModelSelector(
        model_api_base="https://llm.example",
        api_base="https://api.example",
        api_key="cpk_test",
    )

    with pytest.raises(ProxyRequestError, match="model not found: missing-model"):
        await selector.resolve_async("missing-model", client)
