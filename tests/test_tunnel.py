import asyncio
import logging

import pytest

from chutes_e2ee_proxy.config import TunnelMode
from chutes_e2ee_proxy.tunnel import TunnelManager


class _FakeProcess:
    def __init__(self, return_code: int = 1) -> None:
        self._return_code = return_code
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code


def test_parse_tunnel_url() -> None:
    line = "INF +------------------------------------------------------------+ https://abc-123.trycloudflare.com"
    assert TunnelManager.parse_tunnel_url(line) == "https://abc-123.trycloudflare.com"
    assert TunnelManager.parse_tunnel_url("no url") is None


def test_resolve_cloudflared_prefers_explicit(tmp_path) -> None:
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\n")
    assert TunnelManager.resolve_cloudflared(str(fake)) == str(fake)


@pytest.mark.asyncio
async def test_required_mode_without_binary_raises() -> None:
    logger = logging.getLogger("test")
    manager = TunnelManager(
        mode=TunnelMode.REQUIRED,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin="/path/does/not/exist",
        logger=logger,
    )

    with pytest.raises(RuntimeError):
        await manager.start()


@pytest.mark.asyncio
async def test_auto_mode_without_binary_does_not_raise() -> None:
    logger = logging.getLogger("test")
    manager = TunnelManager(
        mode=TunnelMode.AUTO,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin="/path/does/not/exist",
        logger=logger,
    )

    await manager.start()
    snapshot = manager.snapshot()
    assert snapshot.status == "disconnected"
    assert snapshot.last_error == "cloudflared binary not found"


@pytest.mark.asyncio
async def test_required_mode_triggers_shutdown_callback_on_exit() -> None:
    logger = logging.getLogger("test")
    called = asyncio.Event()

    def on_required_exit() -> None:
        called.set()

    manager = TunnelManager(
        mode=TunnelMode.REQUIRED,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logger,
        on_required_exit=on_required_exit,
    )

    manager._process = _FakeProcess(return_code=2)  # test-only direct injection
    await manager._watch_exit()

    assert called.is_set() is True
    assert manager.snapshot().status == "disconnected"
