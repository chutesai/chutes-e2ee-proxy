import pytest

from chutes_e2ee_proxy.config import Settings, TunnelMode


def test_env_resolution(monkeypatch) -> None:
    monkeypatch.setenv("CHUTES_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("CHUTES_PROXY_PORT", "9999")
    monkeypatch.setenv("CHUTES_UPSTREAM", "https://example.com")
    monkeypatch.setenv("CHUTES_E2E_UPSTREAM", "https://example-e2e.com")
    monkeypatch.setenv("CHUTES_PROXY_TUNNEL", "off")
    monkeypatch.setenv("CHUTES_CLOUDFLARED_BIN", "/tmp/cloudflared")
    monkeypatch.setenv("CHUTES_LOG_LEVEL", "debug")

    settings = Settings.from_cli(None, None, None, None, None, None, None, None, None)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9999
    assert settings.upstream == "https://example.com"
    assert settings.e2e_upstream == "https://example-e2e.com"
    assert settings.tunnel is TunnelMode.OFF
    assert settings.cloudflared_bin == "/tmp/cloudflared"
    assert settings.log_level == "debug"


def test_cli_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("CHUTES_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("CHUTES_PROXY_PORT", "9999")

    settings = Settings.from_cli(
        host="127.0.0.1",
        port=8787,
        upstream="https://llm.chutes.ai",
        e2e_upstream=None,
        tls_cert_file=None,
        tls_key_file=None,
        tunnel="required",
        cloudflared_bin=None,
        log_level="info",
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787
    assert settings.tunnel is TunnelMode.REQUIRED


def test_default_e2e_upstream_is_derived_from_llm_host() -> None:
    settings = Settings.from_cli(
        host=None,
        port=None,
        upstream="https://llm.chutes.ai",
        e2e_upstream=None,
        tls_cert_file=None,
        tls_key_file=None,
        tunnel=None,
        cloudflared_bin=None,
        log_level=None,
    )
    assert settings.e2e_upstream == "https://api.chutes.ai"


def test_tls_files_can_be_provided_together(tmp_path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert")
    key.write_text("key")

    settings = Settings.from_cli(
        host=None,
        port=None,
        upstream=None,
        e2e_upstream=None,
        tls_cert_file=str(cert),
        tls_key_file=str(key),
        tunnel=None,
        cloudflared_bin=None,
        log_level=None,
    )
    assert settings.tls_cert_file == str(cert)
    assert settings.tls_key_file == str(key)


@pytest.mark.parametrize(
    "tls_cert_file,tls_key_file",
    [
        ("cert-only.pem", None),
        (None, "key-only.pem"),
    ],
)
def test_tls_requires_both_files(tls_cert_file, tls_key_file) -> None:
    with pytest.raises(ValueError, match="Both tls_cert_file and tls_key_file must be provided together"):
        Settings.from_cli(
            host=None,
            port=None,
            upstream=None,
            e2e_upstream=None,
            tls_cert_file=tls_cert_file,
            tls_key_file=tls_key_file,
            tunnel=None,
            cloudflared_bin=None,
            log_level=None,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": None, "port": -1, "upstream": None, "e2e_upstream": None, "tls_cert_file": None, "tls_key_file": None, "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": "not-a-url", "e2e_upstream": None, "tls_cert_file": None, "tls_key_file": None, "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "e2e_upstream": "not-a-url", "tls_cert_file": None, "tls_key_file": None, "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "e2e_upstream": None, "tls_cert_file": "/definitely/missing/cert.pem", "tls_key_file": "/definitely/missing/key.pem", "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "e2e_upstream": None, "tls_cert_file": None, "tls_key_file": None, "tunnel": "bad", "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "e2e_upstream": None, "tls_cert_file": None, "tls_key_file": None, "tunnel": None, "cloudflared_bin": None, "log_level": "bad"},
    ],
)
def test_invalid_values_raise(kwargs) -> None:
    with pytest.raises(ValueError):
        Settings.from_cli(**kwargs)
