import httpx

from chutes_e2ee_proxy.app import _filter_response_headers


def test_filter_response_headers_strips_content_length() -> None:
    headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-length": "123",
            "connection": "keep-alive",
            "x-request-id": "abc",
        }
    )

    filtered = _filter_response_headers(headers)

    assert "content-length" not in {k.lower(): v for k, v in filtered.items()}
    assert "connection" not in {k.lower(): v for k, v in filtered.items()}
    assert filtered["content-type"] == "application/json"
    assert filtered["x-request-id"] == "abc"
