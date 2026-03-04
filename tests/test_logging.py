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
    assert parsed["tunnel"] == "auto"
    assert parsed["value"] == 1
