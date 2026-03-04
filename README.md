# chutes-e2ee-proxy

Native, container-free E2EE proxy for Chutes. It accepts OpenAI-compatible HTTP requests locally, forwards them through `chutes-e2ee-transport`, and returns upstream responses transparently.

## Install

### Primary (pipx)

```bash
pipx install chutes-e2ee-proxy
```

If the package is not on PyPI yet:

```bash
pipx install git+https://github.com/chutesai/chutes-e2ee-proxy.git
```

### One-line bootstrap

macOS/Linux/Git Bash:

```bash
curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

PowerShell:

```powershell
irm https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install.ps1 | iex
```

## Run

```bash
chutes-e2ee-proxy serve
```

Default local URL: `http://127.0.0.1:8787`

Point your client `base_url` to that local URL (or the tunnel URL if enabled) and keep using your normal `Authorization: Bearer ...` key.

## CLI

### `serve`

| Flag | Env | Default |
|---|---|---|
| `--host` | `CHUTES_PROXY_HOST` | `127.0.0.1` |
| `--port` | `CHUTES_PROXY_PORT` | `8787` |
| `--upstream` | `CHUTES_UPSTREAM` | `https://llm.chutes.ai` |
| `--tunnel` | `CHUTES_PROXY_TUNNEL` | `auto` |
| `--cloudflared-bin` | `CHUTES_CLOUDFLARED_BIN` | auto-detect |
| `--log-level` | `CHUTES_LOG_LEVEL` | `info` |

Tunnel modes:
- `auto`: try cloudflared, continue without tunnel if unavailable
- `required`: fail startup (or request shutdown) if tunnel unavailable/exits
- `off`: disable tunnel

### `doctor`

```bash
chutes-e2ee-proxy doctor
```

Runs local checks for Python version, `chutes-e2ee` importability, cloudflared availability, and upstream connectivity.

## Endpoints

- `ANY /{path:path}`: transparent proxy
- `GET /_chutes_proxy/health`: health + tunnel + pool stats

## Behavior

1. Requires `Authorization: Bearer <token>` per request.
2. No request schema validation/body interpretation in the proxy.
3. Uses per-key pooled `AsyncChutesE2EETransport` instances.
4. Streams upstream response bytes back to caller.
5. Upstream 4xx/5xx pass through unchanged.

## Development

```bash
cd chutes-e2ee-proxy
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
