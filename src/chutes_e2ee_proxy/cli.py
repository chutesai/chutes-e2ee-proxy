from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import sys
from dataclasses import asdict
from importlib import metadata
from urllib.parse import urlparse

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


def _runtime_build_info() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        info["proxy_version"] = metadata.version("chutes-e2ee-proxy")
    except metadata.PackageNotFoundError:
        pass
    try:
        info["transport_version"] = metadata.version("chutes-e2ee")
    except metadata.PackageNotFoundError:
        pass

    try:
        dist = metadata.distribution("chutes-e2ee-proxy")
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            parsed = json.loads(direct_url)
            vcs_info = parsed.get("vcs_info") or {}
            commit_id = vcs_info.get("commit_id")
            requested_revision = vcs_info.get("requested_revision")
            if commit_id:
                info["proxy_commit"] = commit_id
            if requested_revision:
                info["proxy_requested_revision"] = requested_revision
    except Exception:
        pass

    return info


@main.command("serve")
@click.option("--host", type=str, default=None)
@click.option("--port", type=int, default=None)
@click.option("--upstream", type=str, default=None)
@click.option("--e2e-upstream", type=str, default=None)
@click.option("--tls-cert-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--tls-key-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--tunnel", type=click.Choice([m.value for m in TunnelMode]), default=None)
@click.option("--cloudflared-bin", type=str, default=None)
@click.option("--log-level", type=click.Choice(["debug", "info", "warning", "error", "critical"]), default=None)
def serve_command(
    host: str | None,
    port: int | None,
    upstream: str | None,
    e2e_upstream: str | None,
    tls_cert_file: str | None,
    tls_key_file: str | None,
    tunnel: str | None,
    cloudflared_bin: str | None,
    log_level: str | None,
) -> None:
    """Run proxy server."""
    settings = Settings.from_cli(
        host=host,
        port=port,
        upstream=upstream,
        e2e_upstream=e2e_upstream,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tunnel=tunnel,
        cloudflared_bin=cloudflared_bin,
        log_level=log_level,
    )

    configure_logging(settings.log_level)
    logger = logging.getLogger("chutes_e2ee_proxy.cli")

    build_info = _runtime_build_info()
    if build_info:
        logger.info("runtime build info", extra={"fields": build_info})

    logger.info("starting chutes-e2ee-proxy", extra={"fields": asdict(settings)})

    asyncio.run(_serve(settings))


def _local_base_urls(settings: Settings) -> tuple[str, str]:
    scheme = "https" if settings.tls_cert_file else "http"
    bind_base_url = f"{scheme}://{settings.host}:{settings.port}/v1"
    display_host = settings.host
    if settings.host in {"127.0.0.1", "0.0.0.0", "::1"}:
        display_host = "localhost"
    display_base_url = f"{scheme}://{display_host}:{settings.port}/v1"
    return display_base_url, bind_base_url


def _socket_target_for_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL host: {url}")
    if parsed.port is not None:
        return hostname, parsed.port
    if parsed.scheme == "https":
        return hostname, 443
    if parsed.scheme == "http":
        return hostname, 80
    raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")


def _print_node_tls_hint() -> None:
    click.echo("Node/Cursor local TLS trust (only needed if your app reports TLS errors):")
    click.echo("  macOS/Linux:")
    click.echo('    export NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem"')
    click.echo("    <your-app-command>")
    click.echo("  PowerShell:")
    click.echo('    $env:NODE_EXTRA_CA_CERTS = "$(mkcert -CAROOT)\\rootCA.pem"')
    click.echo("    <your-app-command>")


def _print_startup_hint(
    settings: Settings,
    local_base_url: str,
    bind_base_url: str,
) -> None:
    click.echo("")
    click.echo("chutes-e2ee-proxy is running.")
    click.echo("")
    click.echo("Local endpoint:")
    click.echo(f"  base_url: {local_base_url}")
    if bind_base_url != local_base_url:
        click.echo(f"  bind_url:  {bind_base_url}")

    if settings.tls_cert_file:
        click.echo("")
        _print_node_tls_hint()

    if settings.tunnel is not TunnelMode.OFF:
        click.echo("")
        click.echo("Tunnel endpoint (recommended compatibility):")
        click.echo("  waiting for cloudflared URL...")

    click.echo("")


async def _watch_tunnel_hint(
    settings: Settings,
    tunnel_manager: TunnelManager,
    local_base_url: str,
) -> None:
    if settings.tunnel is TunnelMode.OFF:
        return

    click.echo("Waiting for tunnel URL...")
    printed_unavailable = False
    while True:
        snapshot = tunnel_manager.snapshot()
        if snapshot.public_url:
            click.echo("")
            click.echo("Tunnel endpoint (recommended compatibility):")
            click.echo(f"  base_url: {snapshot.public_url}/v1")
            click.echo("Local fallback endpoint:")
            click.echo(f"  base_url: {local_base_url}")
            click.echo("")
            return

        if (
            snapshot.status == "disconnected"
            and snapshot.last_error
            and not printed_unavailable
        ):
            click.echo("")
            click.echo(
                f"Tunnel unavailable ({snapshot.last_error}). "
                f"Use local endpoint: {local_base_url}"
            )
            if settings.tls_cert_file:
                _print_node_tls_hint()
            click.echo("")
            printed_unavailable = True
            if settings.tunnel is not TunnelMode.REQUIRED:
                return

        await asyncio.sleep(0.5)


async def _serve(settings: Settings) -> None:
    logger = logging.getLogger("chutes_e2ee_proxy.cli")

    pool = TransportPool(
        upstream=settings.upstream,
        e2e_upstream=settings.e2e_upstream,
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
        local_tls_enabled=bool(settings.tls_cert_file),
        on_required_exit=request_shutdown,
    )

    app = create_app(settings, pool, tunnel_manager, request_shutdown)

    uvicorn_config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.tls_cert_file,
        ssl_keyfile=settings.tls_key_file,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(uvicorn_config)
    server_ref["server"] = server

    logger.info(
        "proxy listening",
        extra={
            "fields": {
                "local_url": (
                    f"{'https' if settings.tls_cert_file else 'http'}://{settings.host}:{settings.port}"
                )
            }
        },
    )

    local_base_url, bind_base_url = _local_base_urls(settings)
    _print_startup_hint(settings, local_base_url, bind_base_url)
    tunnel_hint_task = asyncio.create_task(_watch_tunnel_hint(settings, tunnel_manager, local_base_url))

    try:
        await server.serve()
    finally:
        tunnel_hint_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tunnel_hint_task


@main.command("doctor")
@click.option("--upstream", type=str, default=None)
@click.option("--e2e-upstream", type=str, default=None)
@click.option("--cloudflared-bin", type=str, default=None)
def doctor_command(upstream: str | None, e2e_upstream: str | None, cloudflared_bin: str | None) -> None:
    """Run local environment diagnostics."""
    settings = Settings.from_cli(
        host=None,
        port=None,
        upstream=upstream,
        e2e_upstream=e2e_upstream,
        tls_cert_file=None,
        tls_key_file=None,
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

    try:
        upstream_host, upstream_port = _socket_target_for_url(settings.upstream)
        with socket.create_connection((upstream_host, upstream_port), timeout=5):
            checks.append(("PASS", "upstream_tcp", f"{upstream_host}:{upstream_port} reachable"))
    except (ValueError, OSError) as exc:
        checks.append(("FAIL", "upstream_tcp", str(exc)))

    try:
        response = httpx.get(f"{settings.upstream}/v1/models", timeout=10)
        if response.status_code < 500:
            checks.append(("PASS", "upstream_http", f"status={response.status_code}"))
        else:
            checks.append(("WARN", "upstream_http", f"status={response.status_code}"))
    except Exception as exc:
        checks.append(("FAIL", "upstream_http", str(exc)))

    try:
        e2e_host, e2e_port = _socket_target_for_url(settings.e2e_upstream)
        with socket.create_connection((e2e_host, e2e_port), timeout=5):
            checks.append(("PASS", "e2e_tcp", f"{e2e_host}:{e2e_port} reachable"))
    except (ValueError, OSError) as exc:
        checks.append(("FAIL", "e2e_tcp", str(exc)))

    try:
        # No headers are intentional; 422 means endpoint exists and is routable.
        response = httpx.post(f"{settings.e2e_upstream}/e2e/invoke", timeout=10, content=b"")
        if response.status_code in {401, 403, 422}:
            checks.append(("PASS", "e2e_http", f"status={response.status_code}"))
        elif response.status_code < 500:
            checks.append(("WARN", "e2e_http", f"status={response.status_code}"))
        else:
            checks.append(("WARN", "e2e_http", f"status={response.status_code}"))
    except Exception as exc:
        checks.append(("FAIL", "e2e_http", str(exc)))

    width = max(len(name) for _, name, _ in checks)
    for level, name, detail in checks:
        click.echo(f"[{level}] {name.ljust(width)}  {detail}")

    fail_count = sum(1 for level, _, _ in checks if level == "FAIL")
    if fail_count > 0:
        raise SystemExit(1)
