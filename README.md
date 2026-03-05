# chutes-e2ee-proxy

Native, container-free E2EE proxy for Chutes. It accepts OpenAI-compatible HTTP requests locally, forwards them through `chutes-e2ee-transport`, and returns upstream responses as-is.

**TLDR:** the app is a thin local HTTPS proxy. It forwards client requests into an E2EE transport, that transport resolves the model using live Chutes metadata, encrypts the payload, sends it to the E2EE invoke path, then decrypts the response back into normal OpenAI-compatible output.

## Quick Start (One-line bootstrap)

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install.ps1 | iex
```

Windows Git Bash:

```bash
curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

Windows WSL:

```bash
curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

The bootstrap scripts attempt `uv` first, then fall back to `pipx`, and prefer installing from GitHub `main` so bootstrap behavior matches the latest repository updates.

Bootstrap defaults:
- auto-generate/reuse local HTTPS certs under `~/.chutes-e2ee-proxy/certs`
- run with tunnel mode `auto`
- expose both local HTTPS and (when available) a cloudflared HTTPS endpoint
- when mkcert is available, configure cloudflared origin verification using `CHUTES_CLOUDFLARED_ORIGIN_CA_POOL`

If cloudflared is unavailable, bootstrap automatically falls back to local HTTPS only.
Pass `--tunnel off` to force local-only HTTPS.

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

## Run

```bash
chutes-e2ee-proxy serve
```

Direct `serve` defaults:
- local URL is `http://127.0.0.1:8787`
- TLS is disabled unless `--tls-cert-file` and `--tls-key-file` (or env equivalents) are provided
- tunnel mode defaults to `auto`

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
| `--cloudflared-origin-ca-pool` | `CHUTES_CLOUDFLARED_ORIGIN_CA_POOL` | unset |
| `--log-level` | `CHUTES_LOG_LEVEL` | `info` |

`--upstream` and `--e2e-upstream` must be host-root bases only.
Do not include `/v1`, `/e2e`, query params, or fragments.

Tunnel modes:
- `auto`: try cloudflared, continue without tunnel if unavailable
- `required`: fail startup (or request shutdown) if tunnel unavailable/exits
- `off`: disable tunnel

When local TLS is enabled, cloudflared uses strict origin verification if `CHUTES_CLOUDFLARED_ORIGIN_CA_POOL` points to a CA bundle. If unset/unreadable, it falls back to `--no-tls-verify` for compatibility.

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

1. Requires `Authorization: Bearer <token>` for model invocations; health and `GET /v1/models` may be called without auth.
2. The proxy forwards request bodies unchanged. It does not rewrite or normalize JSON payloads before E2EE transport resolution.
3. Uses per-key pooled `AsyncChutesE2EETransport` instances.
4. Streams upstream response bytes back to caller.
5. Upstream 4xx/5xx pass through unchanged (status, body bytes, and safe headers).
6. The proxy app forwards request bytes unchanged; model normalization happens only inside the E2EE transport boundary that already parses payloads for encryption.
7. The E2EE transport caches live `/v1/models`, `/model_aliases/`, and LLM stats in memory so clients can keep using normal Chutes model selectors.
8. Exact model ids, public roots, chute ids, aliases, ordered failover lists, and metric-ranked selectors such as `:latency` and `:throughput` are resolved dynamically with no hardcoded model tables.

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

## Bootstrap Advanced

Set `CHUTES_PROXY_GIT_REF` to pin bootstrap installs to a specific tag or commit:

```bash
CHUTES_PROXY_GIT_REF=v0.1.0 curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

PowerShell equivalent:

```powershell
$env:CHUTES_PROXY_GIT_REF="v0.1.0"; irm https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install.ps1 | iex
```

Require uv-only installs (disable pipx fallback):

```bash
CHUTES_PROXY_UV_REQUIRED=1 curl -fsSL https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install | bash
```

PowerShell equivalent:

```powershell
$env:CHUTES_PROXY_UV_REQUIRED="1"; irm https://raw.githubusercontent.com/chutesai/chutes-e2ee-proxy/main/install.ps1 | iex
```

## Canary CI (Live Key)

A nightly canary workflow (`.github/workflows/canary.yml`) can run a real `/v1/models`, non-stream completion, and streaming completion through the proxy.

Set repository secrets in GitHub:
1. `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`
2. Add `CHUTES_API_KEY` (required)
3. Optionally add `CHUTES_CANARY_MODEL` (specific model id to force)
