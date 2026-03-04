from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass
class AuthError(Exception):
    message: str


def extract_bearer_token(headers: Mapping[str, str]) -> str:
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        raise AuthError("missing Authorization header")

    parts = raw.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be Bearer <token>")

    token = parts[1].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def key_prefix(token: str, width: int = 8) -> str:
    if len(token) <= width:
        return token
    return token[:width]
