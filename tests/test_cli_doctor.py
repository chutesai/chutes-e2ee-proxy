from __future__ import annotations

import pytest

from chutes_e2ee_proxy.cli import _socket_target_for_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com", ("example.com", 443)),
        ("http://example.com", ("example.com", 80)),
        ("https://example.com:8443/v1/models", ("example.com", 8443)),
        ("https://[::1]:9443/v1/models", ("::1", 9443)),
    ],
)
def test_socket_target_for_url(url: str, expected: tuple[str, int]) -> None:
    assert _socket_target_for_url(url) == expected


def test_socket_target_for_url_rejects_invalid_host() -> None:
    with pytest.raises(ValueError, match="Invalid URL host"):
        _socket_target_for_url("https:///v1/models")
