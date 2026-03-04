import pytest

from chutes_e2ee_proxy.auth import AuthError, extract_bearer_token


def test_extract_bearer_token_success() -> None:
    token = extract_bearer_token({"Authorization": "Bearer abc123"})
    assert token == "abc123"


def test_extract_bearer_token_case_insensitive_scheme() -> None:
    token = extract_bearer_token({"authorization": "bearer abc123"})
    assert token == "abc123"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
    ],
)
def test_extract_bearer_token_errors(headers: dict[str, str]) -> None:
    with pytest.raises(AuthError):
        extract_bearer_token(headers)
