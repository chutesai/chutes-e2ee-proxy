from __future__ import annotations

from chutes_e2ee_proxy.cli import _local_base_urls, _print_startup_hint
from chutes_e2ee_proxy.config import Settings, TunnelMode


def _settings(*, host: str = "127.0.0.1", tls: bool = True, tunnel: TunnelMode = TunnelMode.OFF) -> Settings:
    return Settings(
        host=host,
        port=8787,
        upstream="https://llm.chutes.ai",
        e2e_upstream="https://api.chutes.ai",
        tls_cert_file="/tmp/cert.pem" if tls else None,
        tls_key_file="/tmp/key.pem" if tls else None,
        tunnel=tunnel,
    )


def test_local_base_urls_prefer_localhost_for_loopback() -> None:
    local_base_url, bind_base_url = _local_base_urls(_settings(host="127.0.0.1"))
    assert local_base_url == "https://localhost:8787/v1"
    assert bind_base_url == "https://127.0.0.1:8787/v1"


def test_local_base_urls_keep_custom_host() -> None:
    local_base_url, bind_base_url = _local_base_urls(_settings(host="my-proxy.local"))
    assert local_base_url == "https://my-proxy.local:8787/v1"
    assert bind_base_url == "https://my-proxy.local:8787/v1"


def test_startup_hint_prints_local_tls_trust_guidance(capsys) -> None:
    settings = _settings(tunnel=TunnelMode.OFF, tls=True)
    _print_startup_hint(
        settings,
        "https://localhost:8787/v1",
        "https://127.0.0.1:8787/v1",
        True,
        "ok",
    )

    output = capsys.readouterr().out
    assert "Local endpoint:" in output
    assert "base_url: https://localhost:8787/v1" in output
    assert "health:    ok" in output
    assert "Recommended endpoint now: https://localhost:8787/v1" in output
    assert "NODE_EXTRA_CA_CERTS" in output
    assert "waiting for cloudflared URL" not in output


def test_startup_hint_prints_tunnel_wait_message(capsys) -> None:
    settings = _settings(tunnel=TunnelMode.AUTO, tls=False)
    _print_startup_hint(
        settings,
        "http://localhost:8787/v1",
        "http://127.0.0.1:8787/v1",
        True,
        "ok",
    )

    output = capsys.readouterr().out
    assert "Tunnel endpoint (recommended compatibility):" in output
    assert "waiting for cloudflared URL..." in output
