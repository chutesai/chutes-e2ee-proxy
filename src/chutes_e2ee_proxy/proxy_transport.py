from __future__ import annotations

import json
from contextlib import suppress

import httpx
from chutes_e2ee import AsyncChutesE2EETransport
from chutes_e2ee.crypto import build_e2ee_request

from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.selector import ModelSelection, ModelSelector

_NONCE_RETRY_MARKERS = (
    b"invalid, expired, or already-used nonce",
    b"already-used nonce",
    b"expired nonce",
    b"invalid nonce",
)


def _extract_json_body(request: httpx.Request) -> dict | None:
    body = request.content
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _original_path(request: httpx.Request) -> str:
    return request.url.raw_path.decode("ascii").split("?")[0]


def _build_invoke_headers(
    api_key: str,
    chute_id: str,
    instance_id: str,
    nonce: str,
    stream: bool,
    e2e_path: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Chute-Id": chute_id,
        "X-Instance-Id": instance_id,
        "X-E2E-Nonce": nonce,
        "X-E2E-Stream": str(stream).lower(),
        "X-E2E-Path": e2e_path,
        "Content-Type": "application/octet-stream",
    }


async def _should_retry_nonce_rejection_async(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    body = await response.aread()
    lowered = body.lower()
    return any(marker in lowered for marker in _NONCE_RETRY_MARKERS)


def _is_streaming(payload: dict) -> bool:
    return bool(payload.get("stream", False))


class ProxyAsyncChutesE2EETransport(AsyncChutesE2EETransport):
    def __init__(
        self,
        api_key: str,
        *,
        model_api_base: str,
        api_base: str,
        inner: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(api_key=api_key, api_base=api_base, inner=inner)
        self._selector = ModelSelector(
            model_api_base=model_api_base,
            api_base=api_base,
            api_key=api_key,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = _extract_json_body(request)
        if payload is None:
            return await self._inner.handle_async_request(request)

        model = payload.get("model")
        if model is None:
            return await self._inner.handle_async_request(request)
        if not isinstance(model, str):
            raise ProxyRequestError(400, "invalid_request", "request field 'model' must be a string")

        stream = _is_streaming(payload)
        e2e_path = _original_path(request)
        http = await self._get_http()
        selections = await self._selector.resolve_async(model, http)

        last_overload_response: httpx.Response | None = None
        last_overload_error: ProxyRequestError | None = None
        invoke_url = f"{self._api_base}/e2e/invoke"

        for selection in selections:
            candidate_payload = self._canonicalize_payload(payload, selection)
            try:
                response = await self._invoke_candidate(
                    request=request,
                    payload=candidate_payload,
                    selection=selection,
                    stream=stream,
                    e2e_path=e2e_path,
                    invoke_url=invoke_url,
                    http=http,
                )
            except ProxyRequestError as exc:
                if exc.status_code in (429, 503) and len(selections) > 1:
                    last_overload_error = exc
                    continue
                raise

            if response.status_code in (429, 503) and len(selections) > 1:
                body = await response.aread()
                last_overload_response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=body,
                    request=request,
                )
                await response.aclose()
                continue

            return response

        if last_overload_response is not None:
            return last_overload_response
        if last_overload_error is not None:
            raise last_overload_error
        raise ProxyRequestError(503, "model_unavailable", f"no E2EE-capable instances available for {model}")

    def _canonicalize_payload(self, payload: dict, selection: ModelSelection) -> dict:
        if payload.get("model") == selection.model_id:
            return payload
        return {**payload, "model": selection.model_id}

    async def _invoke_candidate(
        self,
        *,
        request: httpx.Request,
        payload: dict,
        selection: ModelSelection,
        stream: bool,
        e2e_path: str,
        invoke_url: str,
        http: httpx.AsyncClient,
    ) -> httpx.Response:
        for attempt in range(2):
            try:
                instance, nonce = await self._discovery.get_nonce_async(selection.chute_id, http)
            except httpx.HTTPStatusError as exc:
                raise self._normalize_instances_error(selection.chute_id, exc) from exc
            except RuntimeError as exc:
                raise self._normalize_nonce_error(selection.model_id, exc) from exc

            result = build_e2ee_request(instance.e2e_pubkey, payload)
            headers = _build_invoke_headers(
                self._api_key,
                selection.chute_id,
                instance.instance_id,
                nonce,
                stream,
                e2e_path,
            )

            if stream:
                response = await self._handle_stream(
                    invoke_url,
                    headers,
                    result.blob,
                    result.response_sk,
                    request,
                )
            else:
                response = await self._handle_non_stream(
                    invoke_url,
                    headers,
                    result.blob,
                    result.response_sk,
                    request,
                )

            if attempt == 0 and await _should_retry_nonce_rejection_async(response):
                self._discovery.invalidate_nonce_cache(selection.chute_id)
                await response.aclose()
                continue
            return response

        raise ProxyRequestError(502, "proxy_error", "E2EE request failed after nonce refresh retry")

    @staticmethod
    def _normalize_nonce_error(model_id: str, exc: RuntimeError) -> ProxyRequestError:
        message = str(exc)
        if "No nonces available for chute" in message:
            return ProxyRequestError(503, "model_unavailable", f"no E2EE-capable instances available for {model_id}")
        return ProxyRequestError(502, "proxy_error", message or "E2EE transport failure")

    @staticmethod
    def _normalize_instances_error(chute_id: str, exc: httpx.HTTPStatusError) -> ProxyRequestError:
        detail = None
        with suppress(Exception):
            payload = exc.response.json()
            if isinstance(payload, dict):
                detail = payload.get("detail")
        if exc.response.status_code == 401:
            return ProxyRequestError(401, "unauthorized", detail or "Authentication required.")
        if detail == "Chute not found":
            return ProxyRequestError(404, "model_not_found", f"model not found: {chute_id}")
        if detail in {"No active instances found for this chute", "No E2E-capable instances available"}:
            return ProxyRequestError(503, "model_unavailable", detail)
        return ProxyRequestError(
            502,
            "proxy_error",
            detail or f"Failed to resolve E2EE-capable instances for chute {chute_id}",
        )
