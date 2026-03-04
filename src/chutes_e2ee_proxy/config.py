from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class TunnelMode(str, Enum):
    AUTO = "auto"
    REQUIRED = "required"
    OFF = "off"


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    upstream: str = "https://llm.chutes.ai"
    tunnel: TunnelMode = TunnelMode.AUTO
    cloudflared_bin: str | None = None
    log_level: str = "info"

    pool_max_size: int = 64
    pool_idle_ttl: float = 300.0
    pool_cleanup_interval: float = 60.0
    shutdown_grace_seconds: float = 5.0

    @staticmethod
    def _coalesce(cli_value: str | None, env_name: str, default: str) -> str:
        if cli_value is not None:
            return cli_value
        env_value = os.getenv(env_name)
        if env_value is not None and env_value != "":
            return env_value
        return default

    @classmethod
    def from_cli(
        cls,
        host: str | None,
        port: int | None,
        upstream: str | None,
        tunnel: str | None,
        cloudflared_bin: str | None,
        log_level: str | None,
    ) -> "Settings":
        resolved_host = cls._coalesce(host, "CHUTES_PROXY_HOST", "127.0.0.1")
        resolved_port = int(cls._coalesce(str(port) if port is not None else None, "CHUTES_PROXY_PORT", "8787"))
        resolved_upstream = cls._coalesce(upstream, "CHUTES_UPSTREAM", "https://llm.chutes.ai").rstrip("/")
        resolved_tunnel = cls._coalesce(tunnel, "CHUTES_PROXY_TUNNEL", "auto").lower()
        resolved_cloudflared = cls._coalesce(
            cloudflared_bin,
            "CHUTES_CLOUDFLARED_BIN",
            "",
        )
        resolved_log_level = cls._coalesce(log_level, "CHUTES_LOG_LEVEL", "info").lower()

        if resolved_tunnel not in {m.value for m in TunnelMode}:
            raise ValueError(f"Invalid tunnel mode: {resolved_tunnel}")

        if resolved_port <= 0 or resolved_port > 65535:
            raise ValueError(f"Invalid port: {resolved_port}")

        if not resolved_upstream.startswith(("http://", "https://")):
            raise ValueError("upstream must start with http:// or https://")

        if resolved_log_level not in {"debug", "info", "warning", "error", "critical"}:
            raise ValueError(f"Invalid log level: {resolved_log_level}")

        return cls(
            host=resolved_host,
            port=resolved_port,
            upstream=resolved_upstream,
            tunnel=TunnelMode(resolved_tunnel),
            cloudflared_bin=resolved_cloudflared or None,
            log_level=resolved_log_level,
        )
