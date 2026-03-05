# chutes-e2ee-proxy

Native, container-free E2EE proxy for Chutes. It accepts OpenAI-compatible HTTP requests locally, forwards them through `chutes-e2ee-transport`, and returns upstream responses transparently.

## Install

### Primary (uv)

```bash
uv tool install --upgrade chutes-e2ee-proxy
```

If the package is not on PyPI yet:

```bash
uv tool install --upgrade git+https://github.com/chutesai/chutes-e2ee-proxy.git
```

### Alternative (pipx)

```bash
pipx install chutes-e2ee-proxy || pipx install git+https://github.com/chutesai/chutes-e2ee-proxy.git
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

The bootstrap scripts attempt `uv` first, then fall back to `pipx`, and prefer installing from GitHub `main` so bootstrap behavior matches the latest repository updates.
By default they run with `--tunnel auto` and also auto-generate/reuse local HTTPS certs under `~/.chutes-e2ee-proxy/certs`.
This gives both local HTTPS and (when available) a cloudflared HTTPS tunnel from the same one-liner startup.
If cloudflared is unavailable, they automatically fall back to local HTTPS only.
Pass `--tunnel off` to force local-only HTTPS.
Set `CHUTES_PROXY_GIT_REF` to pin bootstrap installs to a specific tag or commit:

```bash
CHUTES_PROXY_GIT_REF=v0.1.0 curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
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
| `--e2e-upstream` | `CHUTES_E2E_UPSTREAM` | auto-derived (`https://api.chutes.ai` for `llm.chutes.ai`) |
| `--tls-cert-file` | `CHUTES_TLS_CERT_FILE` | unset |
| `--tls-key-file` | `CHUTES_TLS_KEY_FILE` | unset |
| `--tunnel` | `CHUTES_PROXY_TUNNEL` | `auto` |
| `--cloudflared-bin` | `CHUTES_CLOUDFLARED_BIN` | auto-detect |
| `--log-level` | `CHUTES_LOG_LEVEL` | `info` |

Tunnel modes:
- `auto`: try cloudflared, continue without tunnel if unavailable
- `required`: fail startup (or request shutdown) if tunnel unavailable/exits
- `off`: disable tunnel

### Local-only HTTPS (no Cloudflare)

Use local TLS cert/key files and keep tunnel disabled:

```bash
chutes-e2ee-proxy serve \
  --tunnel off \
  --tls-cert-file /path/to/localhost.pem \
  --tls-key-file /path/to/localhost-key.pem
```

Then point your app at:

```text
https://127.0.0.1:8787/v1
```

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
6. E2EE requests must use a single concrete model name. Chutes multi-model routing/failover selectors are not supported in the E2EE path because encryption/invoke requires a preselected chute/instance.

`--upstream` is the client-facing OpenAI-compatible base (for example `https://llm.chutes.ai`), while `--e2e-upstream` is where `/e2e/*` is reached (for example `https://api.chutes.ai`).

## Development

```bash
cd chutes-e2ee-proxy
uv sync --dev
uv run pytest
```

Fallback without `uv`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Canary CI (Live Key)

A nightly canary workflow (`.github/workflows/canary.yml`) can run a real `/v1/models`, non-stream completion, and streaming completion through the proxy.

Set repository secrets in GitHub:
1. `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`
2. Add `CHUTES_API_KEY` (required)
3. Optionally add `CHUTES_CANARY_MODEL` (specific model id to force)
