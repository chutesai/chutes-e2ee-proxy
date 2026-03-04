#!/usr/bin/env bash
set -euo pipefail

REPO_FALLBACK="git+https://github.com/chutesai/chutes-e2ee-proxy.git"
STATE_DIR="${HOME}/.chutes-e2ee-proxy"
CERT_DIR="${STATE_DIR}/certs"
CERT_FILE="${CERT_DIR}/localhost.pem"
KEY_FILE="${CERT_DIR}/localhost-key.pem"

log() {
  printf '[install] %s\n' "$1"
}

add_local_bins_to_path() {
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

has_flag() {
  local flag="$1"
  shift
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "$flag" ]; then
      return 0
    fi
    case "$arg" in
      "${flag}"|"${flag}="*)
        return 0
        ;;
    esac
    prev="$arg"
  done
  return 1
}

resolve_tunnel_mode() {
  local mode="${CHUTES_PROXY_TUNNEL:-}"
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "--tunnel" ]; then
      mode="$arg"
      prev=""
      continue
    fi
    case "$arg" in
      --tunnel=*)
        mode="${arg#--tunnel=}"
        ;;
      --tunnel)
        prev="--tunnel"
        ;;
    esac
  done

  if [ -z "$mode" ]; then
    mode="off"
  fi
  printf '%s' "$mode"
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

ensure_uv() {
  add_local_bins_to_path
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  log "uv not found, installing..."
  if command -v brew >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install uv </dev/null || true
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh || true
  fi

  add_local_bins_to_path
  command -v uv >/dev/null 2>&1
}

ensure_pipx() {
  add_local_bins_to_path
  if command -v pipx >/dev/null 2>&1; then
    return
  fi

  log "pipx not found, installing..."
  if command -v brew >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install pipx </dev/null
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
    add_local_bins_to_path
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    echo "Failed to install pipx." >&2
    exit 1
  fi
}

ensure_mkcert() {
  add_local_bins_to_path
  if command -v mkcert >/dev/null 2>&1; then
    return 0
  fi

  log "mkcert not found, attempting installation for local TLS..."
  if command -v brew >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install mkcert </dev/null || true
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update || true
    sudo apt-get install -y mkcert libnss3-tools || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y mkcert nss-tools || true
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y mkcert nss-tools || true
  fi

  command -v mkcert >/dev/null 2>&1
}

ensure_local_tls_cert() {
  mkdir -p "$CERT_DIR"
  if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    return 0
  fi

  if ensure_mkcert; then
    mkcert -install >/dev/null 2>&1 || true
    if mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" localhost 127.0.0.1 ::1 </dev/null; then
      log "Generated trusted local TLS cert with mkcert."
      return 0
    fi
  fi

  if command -v openssl >/dev/null 2>&1; then
    local openssl_cfg="${CERT_DIR}/openssl.cnf"
    cat >"$openssl_cfg" <<'EOF'
[req]
prompt = no
distinguished_name = req_distinguished_name
x509_extensions = v3_req
[req_distinguished_name]
CN = localhost
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
EOF
    openssl req \
      -x509 \
      -nodes \
      -newkey rsa:2048 \
      -days 365 \
      -keyout "$KEY_FILE" \
      -out "$CERT_FILE" \
      -config "$openssl_cfg" \
      -extensions v3_req </dev/null >/dev/null 2>&1
    rm -f "$openssl_cfg"
    if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
      log "Generated self-signed local TLS cert with openssl."
      log "If your client rejects TLS, install mkcert and re-run install."
      return 0
    fi
  fi

  echo "Failed to generate local TLS cert/key." >&2
  return 1
}

install_proxy() {
  add_local_bins_to_path
  if command -v uv >/dev/null 2>&1; then
    log "Installing chutes-e2ee-proxy via uv..."
    local uv_err
    uv_err="$(mktemp)"
    if uv tool install --upgrade chutes-e2ee-proxy </dev/null 2>"$uv_err"; then
      rm -f "$uv_err"
      return 0
    fi

    if grep -qi "Executable already exists: chutes-e2ee-proxy" "$uv_err"; then
      rm -f "$uv_err"
      log "Existing chutes-e2ee-proxy executable detected; using pipx upgrade path."
    else
      rm -f "$uv_err"
      if uv tool install --upgrade --force chutes-e2ee-proxy </dev/null; then
        return 0
      fi
      if uv tool install --upgrade "$REPO_FALLBACK" </dev/null; then
        return 0
      fi
      if uv tool install --upgrade --force "$REPO_FALLBACK" </dev/null; then
        return 0
      fi
      log "uv installation failed; falling back to pipx..."
    fi
  fi

  ensure_pipx
  if pipx list 2>/dev/null | grep -q 'package chutes-e2ee-proxy'; then
    log "Upgrading chutes-e2ee-proxy..."
    pipx upgrade chutes-e2ee-proxy </dev/null || pipx install "$REPO_FALLBACK" </dev/null
  else
    log "Installing chutes-e2ee-proxy via pipx..."
    pipx install chutes-e2ee-proxy </dev/null || pipx install "$REPO_FALLBACK" </dev/null
  fi
}

ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return
  fi

  log "cloudflared not found, attempting installation..."
  if command -v brew >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install cloudflare/cloudflare/cloudflared </dev/null
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
ensure_uv || true
install_proxy

TUNNEL_MODE="$(resolve_tunnel_mode "$@")"
if [ "$TUNNEL_MODE" = "off" ] && [ -z "${CHUTES_PROXY_TUNNEL:-}" ] && ! has_flag --tunnel "$@"; then
  export CHUTES_PROXY_TUNNEL="off"
fi

HAS_TLS_CERT=false
HAS_TLS_KEY=false
if [ -n "${CHUTES_TLS_CERT_FILE:-}" ] || has_flag --tls-cert-file "$@"; then
  HAS_TLS_CERT=true
fi
if [ -n "${CHUTES_TLS_KEY_FILE:-}" ] || has_flag --tls-key-file "$@"; then
  HAS_TLS_KEY=true
fi

if [ "$TUNNEL_MODE" = "off" ]; then
  if [ "$HAS_TLS_CERT" = false ] && [ "$HAS_TLS_KEY" = false ]; then
    ensure_local_tls_cert
    export CHUTES_TLS_CERT_FILE="$CERT_FILE"
    export CHUTES_TLS_KEY_FILE="$KEY_FILE"
  fi
else
  ensure_cloudflared
fi

log "Starting chutes-e2ee-proxy..."
exec chutes-e2ee-proxy serve "$@"
