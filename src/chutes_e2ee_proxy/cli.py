from __future__ import annotations

import asyncio
import logging
import socket
import sys
from dataclasses import asdict

import click
import httpx
import uvicorn

from chutes_e2ee_proxy.app import create_app
from chutes_e2ee_proxy.config import Settings, TunnelMode
from chutes_e2ee_proxy.logging import configure_logging
from chutes_e2ee_proxy.pool import TransportPool
from chutes_e2ee_proxy.tunnel import TunnelManager


@click.group()
def main() -> None:
    """chutes-e2ee-proxy CLI."""


@main.command("serve")
@click.option("--host", type=str, default=None)
@click.option("--port", type=int, default=None)
@click.option("--upstream", type=str, default=None)
@click.option("--tunnel", type=click.Choice([m.value for m in TunnelMode]), default=None)
@click.option("--cloudflared-bin", type=str, default=None)
@click.option("--log-level", type=click.Choice(["debug", "info", "warning", "error", "critical"]), default=None)
def serve_command(
    host: str | None,
    port: int | None,
    upstream: str | None,
    tunnel: str | None,
    cloudflared_bin: str | None,
    log_level: str | None,
) -> None:
    """Run proxy server."""
    settings = Settings.from_cli(
        host=host,
        port=port,
        upstream=upstream,
        tunnel=tunnel,
        cloudflared_bin=cloudflared_bin,
        log_level=log_level,
    )

    configure_logging(settings.log_level)
    logger = logging.getLogger("chutes_e2ee_proxy.cli")

    logger.info("starting chutes-e2ee-proxy", extra={"fields": asdict(settings)})

    asyncio.run(_serve(settings))


async def _serve(settings: Settings) -> None:
    logger = logging.getLogger("chutes_e2ee_proxy.cli")

    pool = TransportPool(
        upstream=settings.upstream,
        max_size=settings.pool_max_size,
        idle_ttl=settings.pool_idle_ttl,
        cleanup_interval=settings.pool_cleanup_interval,
    )

    server_ref: dict[str, uvicorn.Server] = {}

    def request_shutdown() -> None:
        server = server_ref.get("server")
        if server is not None:
            server.should_exit = True
            logger.warning("shutdown requested by tunnel manager")

    tunnel_manager = TunnelManager(
        mode=settings.tunnel,
        host=settings.host,
        port=settings.port,
        cloudflared_bin=settings.cloudflared_bin,
        logger=logger,
        on_required_exit=request_shutdown,
    )

    app = create_app(settings, pool, tunnel_manager, request_shutdown)

    uvicorn_config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(uvicorn_config)
    server_ref["server"] = server

    logger.info(
        "proxy listening",
        extra={"fields": {"local_url": f"http://{settings.host}:{settings.port}"}},
    )

    await server.serve()


@main.command("doctor")
@click.option("--upstream", type=str, default=None)
@click.option("--cloudflared-bin", type=str, default=None)
def doctor_command(upstream: str | None, cloudflared_bin: str | None) -> None:
    """Run local environment diagnostics."""
    settings = Settings.from_cli(
        host=None,
        port=None,
        upstream=upstream,
        tunnel=None,
        cloudflared_bin=cloudflared_bin,
        log_level=None,
    )

    checks: list[tuple[str, str, str]] = []

    if sys.version_info >= (3, 10):
        checks.append(("PASS", "python", f"{sys.version.split()[0]}"))
    else:
        checks.append(("FAIL", "python", f"{sys.version.split()[0]} (<3.10)"))

    try:
        import chutes_e2ee  # noqa: F401

        checks.append(("PASS", "chutes-e2ee", "import ok"))
    except Exception as exc:  # pragma: no cover - exercised in environments missing dep
        checks.append(("FAIL", "chutes-e2ee", f"import failed: {exc}"))

    cloudflared_path = TunnelManager.resolve_cloudflared(settings.cloudflared_bin)
    if cloudflared_path:
        checks.append(("PASS", "cloudflared", cloudflared_path))
    else:
        checks.append(("WARN", "cloudflared", "not found (tunnel auto mode will continue without it)"))

    upstream_host = settings.upstream.split("://", 1)[-1].split("/", 1)[0]
    try:
        with socket.create_connection((upstream_host, 443), timeout=5):
            checks.append(("PASS", "upstream_tcp", f"{upstream_host}:443 reachable"))
    except OSError as exc:
        checks.append(("FAIL", "upstream_tcp", str(exc)))

    try:
        response = httpx.get(f"{settings.upstream}/v1/models", timeout=10)
        if response.status_code < 500:
            checks.append(("PASS", "upstream_http", f"status={response.status_code}"))
        else:
            checks.append(("WARN", "upstream_http", f"status={response.status_code}"))
    except Exception as exc:
        checks.append(("FAIL", "upstream_http", str(exc)))

    width = max(len(name) for _, name, _ in checks)
    for level, name, detail in checks:
        click.echo(f"[{level}] {name.ljust(width)}  {detail}")

    fail_count = sum(1 for level, _, _ in checks if level == "FAIL")
    if fail_count > 0:
        raise SystemExit(1)
