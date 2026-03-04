#!/usr/bin/env bash
set -euo pipefail

REPO_FALLBACK="git+https://github.com/chutesai/chutes-e2ee-proxy.git"

log() {
  printf '[install] %s\n' "$1"
}

require_python() {
  local py_bin=""
  if command -v python3 >/dev/null 2>&1; then
    py_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    py_bin="python"
  else
    echo "Python 3.10+ is required." >&2
    exit 1
  fi

  "$py_bin" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required")
PY

  echo "$py_bin"
}

ensure_pipx() {
  if command -v pipx >/dev/null 2>&1; then
    return
  fi

  log "pipx not found, installing..."
  if command -v brew >/dev/null 2>&1; then
    brew install pipx
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y pipx
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y pipx
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y pipx
  else
    "$PY_BIN" -m pip install --user pipx
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    "$PY_BIN" -m pipx ensurepath || true
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    echo "Failed to install pipx." >&2
    exit 1
  fi
}

install_proxy() {
  if pipx list 2>/dev/null | grep -q 'package chutes-e2ee-proxy'; then
    log "Upgrading chutes-e2ee-proxy..."
    pipx upgrade chutes-e2ee-proxy || pipx install "$REPO_FALLBACK"
  else
    log "Installing chutes-e2ee-proxy via pipx..."
    pipx install chutes-e2ee-proxy || pipx install "$REPO_FALLBACK"
  fi
}

ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return
  fi

  log "cloudflared not found, attempting installation..."
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflare/cloudflare/cloudflared
  elif command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y cloudflared || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y cloudflared || true
  fi

  if ! command -v cloudflared >/dev/null 2>&1; then
    log "cloudflared is still missing; proxy will run with --tunnel auto fallback behavior."
  fi
}

PY_BIN="$(require_python)"
ensure_pipx
install_proxy
ensure_cloudflared

log "Starting chutes-e2ee-proxy..."
exec chutes-e2ee-proxy serve "$@"
