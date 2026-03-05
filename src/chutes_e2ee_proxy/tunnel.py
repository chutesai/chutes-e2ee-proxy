from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import re
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable

from chutes_e2ee_proxy.config import TunnelMode

_TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


@dataclass
class TunnelSnapshot:
    mode: str
    status: str
    public_url: str | None
    last_error: str | None


class TunnelManager:
    def __init__(
        self,
        mode: TunnelMode,
        host: str,
        port: int,
        cloudflared_bin: str | None,
        logger: logging.Logger,
        local_tls_enabled: bool = False,
        cloudflared_origin_ca_pool: str | None = None,
        on_required_exit: Callable[[], None | Awaitable[None]] | None = None,
    ):
        self._mode = mode
        self._host = host
        self._port = port
        self._cloudflared_bin = cloudflared_bin
        self._logger = logger
        self._local_tls_enabled = local_tls_enabled
        self._cloudflared_origin_ca_pool = cloudflared_origin_ca_pool
        self._on_required_exit = on_required_exit

        self._status = "off" if mode is TunnelMode.OFF else "disconnected"
        self._public_url: str | None = None
        self._last_error: str | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._ready = asyncio.Event()
        self._stopping = False

    @staticmethod
    def parse_tunnel_url(text: str) -> str | None:
        match = _TUNNEL_URL_RE.search(text)
        return match.group(0) if match else None

    @staticmethod
    def resolve_cloudflared(explicit: str | None = None) -> str | None:
        if explicit:
            return explicit if os.path.exists(explicit) else None

        candidates: list[str] = []
        from_path = shutil.which("cloudflared")
        if from_path:
            candidates.append(from_path)

        system = platform.system().lower()
        if "windows" in system:
            candidates.extend(
                [
                    "C:\\Program Files\\cloudflared\\cloudflared.exe",
                    "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe",
                    "C:\\cloudflared\\cloudflared.exe",
                ]
            )
        else:
            candidates.extend([
                "/usr/local/bin/cloudflared",
                "/opt/homebrew/bin/cloudflared",
                "/usr/bin/cloudflared",
            ])

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _build_cloudflared_command(self, binary: str) -> list[str]:
        origin_scheme = "https" if self._local_tls_enabled else "http"
        command = [
            binary,
            "tunnel",
            "--url",
            f"{origin_scheme}://{self._host}:{self._port}",
        ]
        if self._local_tls_enabled:
            ca_pool = (self._cloudflared_origin_ca_pool or "").strip()
            if ca_pool and os.path.isfile(ca_pool):
                command.extend(["--origin-ca-pool", ca_pool])
            else:
                # Local TLS certs may be self-signed or privately issued.
                # Use strict verification when a trust bundle is provided; otherwise degrade gracefully.
                command.append("--no-tls-verify")
        return command

    async def start(self) -> None:
        if self._mode is TunnelMode.OFF:
            self._status = "off"
            self._public_url = None
            return

        self._stopping = False
        self._ready = asyncio.Event()
        self._public_url = None
        self._last_error = None

        binary = self.resolve_cloudflared(self._cloudflared_bin)
        if not binary:
            self._status = "disconnected"
            self._last_error = "cloudflared binary not found"
            self._logger.warning(
                "cloudflared unavailable; tunnel disabled",
                extra={"fields": {"tunnel_mode": self._mode.value}},
            )
            if self._mode is TunnelMode.REQUIRED:
                raise RuntimeError("tunnel mode is required but cloudflared was not found")
            return

        if self._local_tls_enabled:
            ca_pool = (self._cloudflared_origin_ca_pool or "").strip()
            if not (ca_pool and os.path.isfile(ca_pool)):
                self._logger.warning(
                    "cloudflared origin TLS verification disabled; set CHUTES_CLOUDFLARED_ORIGIN_CA_POOL to a trusted CA bundle path",
                    extra={"fields": {"tunnel_mode": self._mode.value}},
                )

        self._status = "starting"
        self._process = await asyncio.create_subprocess_exec(
            *self._build_cloudflared_command(binary),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if self._process.stdout is not None:
            self._tasks.append(asyncio.create_task(self._read_stream(self._process.stdout, "stdout")))
        if self._process.stderr is not None:
            self._tasks.append(asyncio.create_task(self._read_stream(self._process.stderr, "stderr")))

        self._tasks.append(asyncio.create_task(self._watch_exit()))

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            self._status = "disconnected"
            self._last_error = "timed out waiting for cloudflared tunnel URL"
            self._logger.warning(
                "cloudflared started but tunnel URL was not observed within timeout",
                extra={"fields": {"tunnel_mode": self._mode.value}},
            )
            if self._mode is TunnelMode.REQUIRED:
                await self.stop()
                raise RuntimeError(self._last_error)

    async def _read_stream(self, stream: asyncio.StreamReader, stream_name: str) -> None:
        try:
            while not stream.at_eof():
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                url = self.parse_tunnel_url(text)
                if url and not self._public_url:
                    self._public_url = url
                    self._status = "connected"
                    self._last_error = None
                    self._ready.set()
                    self._logger.info(
                        "cloudflared tunnel connected",
                        extra={"fields": {"tunnel_url": url}},
                    )

                self._logger.debug(
                    "cloudflared output",
                    extra={"fields": {"stream": stream_name, "line": text}},
                )
        except asyncio.CancelledError:
            return

    async def _watch_exit(self) -> None:
        if self._process is None:
            return

        return_code = await self._process.wait()
        if self._stopping:
            return

        self._status = "disconnected"
        self._public_url = None
        self._last_error = f"cloudflared exited with code {return_code}"

        self._logger.warning(
            "cloudflared tunnel exited",
            extra={"fields": {"return_code": return_code, "tunnel_mode": self._mode.value}},
        )

        if self._mode is TunnelMode.REQUIRED and self._on_required_exit is not None:
            result = self._on_required_exit()
            if asyncio.iscoroutine(result):
                await result

    async def stop(self) -> None:
        self._stopping = True

        process = self._process
        self._process = None

        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        if self._mode is TunnelMode.OFF:
            self._status = "off"
        else:
            self._status = "disconnected"
        self._public_url = None

    def snapshot(self) -> TunnelSnapshot:
        return TunnelSnapshot(
            mode=self._mode.value,
            status=self._status,
            public_url=self._public_url,
            last_error=self._last_error,
        )
