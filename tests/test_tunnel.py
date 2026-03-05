import asyncio
import logging

import pytest

from chutes_e2ee_proxy.config import TunnelMode
from chutes_e2ee_proxy.tunnel import TunnelManager


class _FakeProcess:
    def __init__(self, return_code: int = 1) -> None:
        self._return_code = return_code
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self.returncode = self._return_code

    def kill(self) -> None:
        self.returncode = self._return_code


def test_parse_tunnel_url() -> None:
    line = "INF +------------------------------------------------------------+ https://abc-123.trycloudflare.com"
    assert TunnelManager.parse_tunnel_url(line) == "https://abc-123.trycloudflare.com"
    assert TunnelManager.parse_tunnel_url("no url") is None


def test_resolve_cloudflared_prefers_explicit(tmp_path) -> None:
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\n")
    assert TunnelManager.resolve_cloudflared(str(fake)) == str(fake)


def test_build_cloudflared_command_http_origin() -> None:
    manager = TunnelManager(
        mode=TunnelMode.AUTO,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logging.getLogger("test"),
        local_tls_enabled=False,
    )
    command = manager._build_cloudflared_command("/usr/bin/cloudflared")
    assert command == [
        "/usr/bin/cloudflared",
        "tunnel",
        "--url",
        "http://127.0.0.1:8787",
    ]


def test_build_cloudflared_command_https_origin_uses_ca_pool_when_available(tmp_path) -> None:
    ca_pool = tmp_path / "rootCA.pem"
    ca_pool.write_text("ca")
    manager = TunnelManager(
        mode=TunnelMode.AUTO,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logging.getLogger("test"),
        local_tls_enabled=True,
        cloudflared_origin_ca_pool=str(ca_pool),
    )
    command = manager._build_cloudflared_command("/usr/bin/cloudflared")
    assert command == [
        "/usr/bin/cloudflared",
        "tunnel",
        "--url",
        "https://127.0.0.1:8787",
        "--origin-ca-pool",
        str(ca_pool),
    ]


def test_build_cloudflared_command_https_origin_falls_back_to_no_tls_verify() -> None:
    manager = TunnelManager(
        mode=TunnelMode.AUTO,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logging.getLogger("test"),
        local_tls_enabled=True,
        cloudflared_origin_ca_pool="/missing/rootCA.pem",
    )
    command = manager._build_cloudflared_command("/usr/bin/cloudflared")
    assert command == [
        "/usr/bin/cloudflared",
        "tunnel",
        "--url",
        "https://127.0.0.1:8787",
        "--no-tls-verify",
    ]


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
    assert manager.snapshot().public_url is None


@pytest.mark.asyncio
async def test_required_mode_start_fails_quickly_when_cloudflared_exits_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("test")
    manager = TunnelManager(
        mode=TunnelMode.REQUIRED,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logger,
    )

    monkeypatch.setattr(manager, "resolve_cloudflared", lambda explicit=None: "/usr/bin/cloudflared")

    async def fake_create_subprocess_exec(*args, **kwargs):
        _ = args, kwargs
        return _FakeProcess(return_code=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="cloudflared exited with code 2"):
        await asyncio.wait_for(manager.start(), timeout=0.5)


@pytest.mark.asyncio
async def test_stop_clears_public_url() -> None:
    logger = logging.getLogger("test")
    manager = TunnelManager(
        mode=TunnelMode.AUTO,
        host="127.0.0.1",
        port=8787,
        cloudflared_bin=None,
        logger=logger,
    )

    manager._status = "connected"
    manager._public_url = "https://abc.trycloudflare.com"

    await manager.stop()

    snapshot = manager.snapshot()
    assert snapshot.status == "disconnected"
    assert snapshot.public_url is None
