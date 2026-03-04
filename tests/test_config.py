import pytest

from chutes_e2ee_proxy.config import Settings, TunnelMode


def test_env_resolution(monkeypatch) -> None:
    monkeypatch.setenv("CHUTES_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("CHUTES_PROXY_PORT", "9999")
    monkeypatch.setenv("CHUTES_UPSTREAM", "https://example.com")
    monkeypatch.setenv("CHUTES_PROXY_TUNNEL", "off")
    monkeypatch.setenv("CHUTES_CLOUDFLARED_BIN", "/tmp/cloudflared")
    monkeypatch.setenv("CHUTES_LOG_LEVEL", "debug")

    settings = Settings.from_cli(None, None, None, None, None, None)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9999
    assert settings.upstream == "https://example.com"
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
        tunnel="required",
        cloudflared_bin=None,
        log_level="info",
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787
    assert settings.tunnel is TunnelMode.REQUIRED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": None, "port": -1, "upstream": None, "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": "not-a-url", "tunnel": None, "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "tunnel": "bad", "cloudflared_bin": None, "log_level": None},
        {"host": None, "port": None, "upstream": None, "tunnel": None, "cloudflared_bin": None, "log_level": "bad"},
    ],
)
def test_invalid_values_raise(kwargs) -> None:
    with pytest.raises(ValueError):
        Settings.from_cli(**kwargs)
