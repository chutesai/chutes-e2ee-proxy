from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from chutes_e2ee_proxy.auth import AuthError, extract_bearer_token, key_prefix
from chutes_e2ee_proxy.config import Settings
from chutes_e2ee_proxy.errors import ProxyRequestError
from chutes_e2ee_proxy.pool import TransportPool
from chutes_e2ee_proxy.tunnel import TunnelManager

REQUEST_HOP_BY_HOP = {
    "host",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
}

RESPONSE_HOP_BY_HOP = {
    "connection",
    "transfer-encoding",
    "keep-alive",
    "content-length",
}


@dataclass
class AppState:
    in_flight: int = 0


def _json_proxy_error(status_code: int, message: str) -> JSONResponse:
    return _json_error(status_code, "proxy_error", message)


def _json_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": error_type, "message": message}}, status_code=status_code)


def _filter_request_headers(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in request.headers.items():
        lname = name.lower()
        if lname in REQUEST_HOP_BY_HOP:
            continue
        result[name] = value
    return result


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in RESPONSE_HOP_BY_HOP:
            continue
        result[name] = value
    return result


def _build_upstream_url(settings: Settings, request: Request) -> str:
    base = settings.upstream.rstrip("/")
    path = request.url.path
    query = request.url.query
    if query:
        return f"{base}{path}?{query}"
    return f"{base}{path}"


def _error_preview(body: bytes, max_chars: int = 240) -> str:
    text = body.decode("utf-8", errors="replace")
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars] + "..."


async def _stream_response(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


def _is_public_models_request(request: Request) -> bool:
    return request.method.upper() == "GET" and request.url.path == "/v1/models"


async def _send_plain_upstream_request(
    request: Request,
    upstream_url: str,
    headers: dict[str, str],
    body: bytes,
) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=response.request,
        )


def create_app(
    settings: Settings,
    pool: TransportPool,
    tunnel: TunnelManager,
    shutdown_callback,
) -> Starlette:
    logger = logging.getLogger("chutes_e2ee_proxy.app")
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        pool.start_cleanup_task()
        try:
            await tunnel.start()
        except BaseException:
            with suppress(Exception):
                await tunnel.stop()
            with suppress(Exception):
                await pool.close_all()
            raise
        try:
            yield
        finally:
            wait_until = time.monotonic() + settings.shutdown_grace_seconds
            while state.in_flight > 0 and time.monotonic() < wait_until:
                await asyncio.sleep(0.05)

            await pool.close_all()
            await tunnel.stop()

    async def health(_request: Request) -> Response:
        snapshot = tunnel.snapshot()
        return JSONResponse(
            {
                "status": "ok",
                "upstream": settings.upstream,
                "e2e_upstream": settings.e2e_upstream,
                "tunnel": {
                    "mode": snapshot.mode,
                    "status": snapshot.status,
                    "public_url": snapshot.public_url,
                    "last_error": snapshot.last_error,
                },
                "pool": pool.stats(),
                "in_flight": state.in_flight,
            }
        )

    async def proxy_handler(request: Request) -> Response:
        started = time.monotonic()
        state.in_flight += 1

        token = ""
        allow_unauthenticated = _is_public_models_request(request)
        try:
            token = extract_bearer_token(request.headers)
        except AuthError as exc:
            if not allow_unauthenticated:
                state.in_flight -= 1
                return _json_error(401, "unauthorized", exc.message)
        log_ctx = {
            "method": request.method,
            "path": request.url.path,
            "key_prefix": key_prefix(token) if token else "anonymous",
        }

        try:
            body = await request.body()
            if token:
                transport = await pool.get(token)
            headers = _filter_request_headers(request)
            if allow_unauthenticated and not token:
                headers.pop("Authorization", None)
                headers.pop("authorization", None)
            upstream_url = _build_upstream_url(settings, request)

            if token:
                upstream_request = httpx.Request(
                    method=request.method,
                    url=upstream_url,
                    headers=headers,
                    content=body,
                )
                upstream_response = await transport.handle_async_request(upstream_request)
            else:
                upstream_response = await _send_plain_upstream_request(
                    request,
                    upstream_url,
                    headers,
                    body,
                )

            latency_ms = int((time.monotonic() - started) * 1000)
            if upstream_response.status_code >= 400:
                body = await upstream_response.aread()
                fields = {
                    **log_ctx,
                    "status_code": upstream_response.status_code,
                    "latency_ms": latency_ms,
                }
                if body:
                    fields["upstream_detail"] = _error_preview(body)

                logger.warning(
                    "upstream error passthrough",
                    extra={"fields": fields},
                )
                return Response(
                    content=body,
                    status_code=upstream_response.status_code,
                    headers=_filter_response_headers(upstream_response.headers),
                )

            logger.info(
                "request proxied",
                extra={
                    "fields": {
                        **log_ctx,
                        "status_code": upstream_response.status_code,
                        "latency_ms": latency_ms,
                    }
                },
            )

            return StreamingResponse(
                _stream_response(upstream_response),
                status_code=upstream_response.status_code,
                headers=_filter_response_headers(upstream_response.headers),
            )

        except httpx.ConnectError:
            logger.warning(
                "upstream connect error",
                extra={
                    "fields": {
                        **log_ctx,
                        "error_type": "connect_error",
                    }
                },
            )
            return _json_proxy_error(502, "upstream unreachable")
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException):
            logger.warning(
                "upstream timeout",
                extra={
                    "fields": {
                        **log_ctx,
                        "error_type": "timeout",
                    }
                },
            )
            return _json_proxy_error(504, "upstream timeout")
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            log_fields = {
                **log_ctx,
                "status_code": status_code,
            }
            if exc.response.content:
                log_fields["upstream_detail"] = _error_preview(exc.response.content)
            logger.warning(
                "upstream status error passthrough",
                extra={"fields": log_fields},
            )
            return Response(
                content=exc.response.content,
                status_code=status_code,
                headers=_filter_response_headers(exc.response.headers),
            )
        except ProxyRequestError as exc:
            logger.warning(
                "proxy contract error",
                extra={
                    "fields": {
                        **log_ctx,
                        "status_code": exc.status_code,
                        "error_type": exc.error_type,
                    }
                },
            )
            return _json_error(exc.status_code, exc.error_type, exc.message)
        except Exception as exc:
            logger.exception(
                "proxy request failed",
                extra={
                    "fields": {
                        **log_ctx,
                        "error_type": "transport_error",
                    }
                },
            )
            message = str(exc) or "transport failure"
            return _json_proxy_error(502, message)
        finally:
            state.in_flight -= 1

    routes = [
        Route("/_chutes_proxy/health", endpoint=health, methods=["GET"]),
        Route(
            "/{path:path}",
            endpoint=proxy_handler,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"],
        ),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.request_shutdown = shutdown_callback
    return app
