from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _json_default(value: Any) -> str | Any:
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _normalize_logger_name(record: logging.LogRecord) -> str:
    # Uvicorn emits normal lifecycle INFO logs on the "uvicorn.error" channel.
    # Normalize those for readability while preserving the original channel separately.
    if record.name == "uvicorn.error" and record.levelno < logging.ERROR:
        return "uvicorn.lifecycle"
    return record.name


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        normalized_logger = _normalize_logger_name(record)
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": normalized_logger,
            "component": normalized_logger.split(".", 1)[0],
            "message": record.getMessage(),
        }
        if normalized_logger != record.name:
            payload["source_logger"] = record.name

        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=_json_default)


def configure_logging(level: str = "info") -> None:
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
