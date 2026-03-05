from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class TunnelMode(str, Enum):
    AUTO = "auto"
    REQUIRED = "required"
    OFF = "off"


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    upstream: str = "https://llm.chutes.ai"
    e2e_upstream: str = "https://api.chutes.ai"
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    tunnel: TunnelMode = TunnelMode.AUTO
    cloudflared_bin: str | None = None
    cloudflared_origin_ca_pool: str | None = None
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
        e2e_upstream: str | None,
        tls_cert_file: str | None,
        tls_key_file: str | None,
        tunnel: str | None,
        cloudflared_bin: str | None,
        log_level: str | None = None,
        cloudflared_origin_ca_pool: str | None = None,
    ) -> "Settings":
        resolved_host = cls._coalesce(host, "CHUTES_PROXY_HOST", "127.0.0.1")
        resolved_port = int(cls._coalesce(str(port) if port is not None else None, "CHUTES_PROXY_PORT", "8787"))
        resolved_upstream = cls._coalesce(upstream, "CHUTES_UPSTREAM", "https://llm.chutes.ai").rstrip("/")
        resolved_e2e_upstream = cls._coalesce(
            e2e_upstream,
            "CHUTES_E2E_UPSTREAM",
            cls._default_e2e_upstream_for(resolved_upstream),
        ).rstrip("/")
        resolved_tls_cert_file = cls._coalesce(tls_cert_file, "CHUTES_TLS_CERT_FILE", "").strip()
        resolved_tls_key_file = cls._coalesce(tls_key_file, "CHUTES_TLS_KEY_FILE", "").strip()
        resolved_tunnel = cls._coalesce(tunnel, "CHUTES_PROXY_TUNNEL", "auto").lower()
        resolved_cloudflared = cls._coalesce(
            cloudflared_bin,
            "CHUTES_CLOUDFLARED_BIN",
            "",
        )
        resolved_cloudflared_origin_ca_pool = cls._coalesce(
            cloudflared_origin_ca_pool,
            "CHUTES_CLOUDFLARED_ORIGIN_CA_POOL",
            "",
        ).strip()
        resolved_log_level = cls._coalesce(log_level, "CHUTES_LOG_LEVEL", "info").lower()

        if resolved_tunnel not in {m.value for m in TunnelMode}:
            raise ValueError(f"Invalid tunnel mode: {resolved_tunnel}")

        if resolved_port <= 0 or resolved_port > 65535:
            raise ValueError(f"Invalid port: {resolved_port}")

        cls._validate_base_url("upstream", resolved_upstream)
        cls._validate_base_url("e2e_upstream", resolved_e2e_upstream)

        if resolved_log_level not in {"debug", "info", "warning", "error", "critical"}:
            raise ValueError(f"Invalid log level: {resolved_log_level}")

        has_cert = bool(resolved_tls_cert_file)
        has_key = bool(resolved_tls_key_file)
        if has_cert != has_key:
            raise ValueError("Both tls_cert_file and tls_key_file must be provided together")
        if has_cert and not os.path.isfile(resolved_tls_cert_file):
            raise ValueError(f"TLS cert file not found: {resolved_tls_cert_file}")
        if has_key and not os.path.isfile(resolved_tls_key_file):
            raise ValueError(f"TLS key file not found: {resolved_tls_key_file}")

        return cls(
            host=resolved_host,
            port=resolved_port,
            upstream=resolved_upstream,
            e2e_upstream=resolved_e2e_upstream,
            tls_cert_file=resolved_tls_cert_file or None,
            tls_key_file=resolved_tls_key_file or None,
            tunnel=TunnelMode(resolved_tunnel),
            cloudflared_bin=resolved_cloudflared or None,
            cloudflared_origin_ca_pool=resolved_cloudflared_origin_ca_pool or None,
            log_level=resolved_log_level,
        )

    @staticmethod
    def _validate_base_url(name: str, value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"{name} must start with http:// or https://")
        if not parsed.hostname:
            raise ValueError(f"{name} must include a hostname")
        if parsed.path not in {"", "/"}:
            raise ValueError(f"{name} must not include a path; use the host root only")
        if parsed.params or parsed.query or parsed.fragment:
            raise ValueError(f"{name} must not include params, query, or fragment")

    @staticmethod
    def _default_e2e_upstream_for(upstream: str) -> str:
        parsed = urlparse(upstream)
        host = parsed.hostname or ""
        if host.startswith("llm."):
            derived_host = f"api.{host[4:]}"
            if parsed.port is not None:
                derived_host = f"{derived_host}:{parsed.port}"
            return f"{parsed.scheme}://{derived_host}"
        return upstream
