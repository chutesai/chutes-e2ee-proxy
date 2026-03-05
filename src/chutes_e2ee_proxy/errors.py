from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProxyRequestError(Exception):
    status_code: int
    error_type: str
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)
