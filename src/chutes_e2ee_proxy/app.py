from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from chutes_e2ee_proxy.auth import AuthError, extract_bearer_token, key_prefix
from chutes_e2ee_proxy.config import Settings
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
    return JSONResponse(
        {"error": {"type": "proxy_error", "message": message}},
        status_code=status_code,
    )


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


async def _stream_response(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


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
        await tunnel.start()
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

        mode = "transport"
        token = ""
        try:
            token = extract_bearer_token(request.headers)
        except AuthError as exc:
            state.in_flight -= 1
            return JSONResponse({"error": {"type": "unauthorized", "message": exc.message}}, status_code=401)

        try:
            body = await request.body()
            headers = _filter_request_headers(request)
            upstream_url = _build_upstream_url(settings, request)

            transport = await pool.get(token)
            upstream_request = httpx.Request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body,
            )

            upstream_response = await transport.handle_async_request(upstream_request)

            latency_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "request proxied",
                extra={
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "has_auth": True,
                        "key_prefix": key_prefix(token),
                        "status_code": upstream_response.status_code,
                        "latency_ms": latency_ms,
                        "mode": mode,
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
                        "method": request.method,
                        "path": request.url.path,
                        "has_auth": True,
                        "key_prefix": key_prefix(token),
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
                        "method": request.method,
                        "path": request.url.path,
                        "has_auth": True,
                        "key_prefix": key_prefix(token),
                        "error_type": "timeout",
                    }
                },
            )
            return _json_proxy_error(504, "upstream timeout")
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            logger.info(
                "upstream status error passthrough",
                extra={
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "has_auth": True,
                        "key_prefix": key_prefix(token),
                        "status_code": status_code,
                        "mode": mode,
                    }
                },
            )
            return Response(
                content=exc.response.content,
                status_code=status_code,
                headers=_filter_response_headers(exc.response.headers),
            )
        except Exception as exc:
            logger.exception(
                "proxy request failed",
                extra={
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "has_auth": True,
                        "key_prefix": key_prefix(token),
                        "error_type": "transport_error",
                        "mode": mode,
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
