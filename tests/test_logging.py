import json
import logging

from chutes_e2ee_proxy.config import TunnelMode
from chutes_e2ee_proxy.logging import JsonFormatter


def test_json_formatter_serializes_enum_fields() -> None:
    formatter = JsonFormatter()

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.fields = {"tunnel": TunnelMode.AUTO, "value": 1}

    parsed = json.loads(formatter.format(record))
    assert parsed["message"] == "hello"
    assert parsed["logger"] == "test"
    assert parsed["component"] == "test"
    assert parsed["tunnel"] == "auto"
    assert parsed["value"] == 1


def test_json_formatter_normalizes_uvicorn_error_channel_for_info() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=(),
        exc_info=None,
    )

    parsed = json.loads(formatter.format(record))
    assert parsed["logger"] == "uvicorn.lifecycle"
    assert parsed["source_logger"] == "uvicorn.error"
    assert parsed["component"] == "uvicorn"


def test_json_formatter_keeps_uvicorn_error_logger_for_error_severity() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Unhandled server error",
        args=(),
        exc_info=None,
    )

    parsed = json.loads(formatter.format(record))
    assert parsed["logger"] == "uvicorn.error"
    assert parsed["component"] == "uvicorn"
    assert "source_logger" not in parsed
