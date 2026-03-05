$ErrorActionPreference = "Stop"

$proxyRef = if ([string]::IsNullOrWhiteSpace($env:CHUTES_PROXY_GIT_REF)) { "main" } else { $env:CHUTES_PROXY_GIT_REF.Trim() }
$RepoFallback = "git+https://github.com/chutesai/chutes-e2ee-proxy.git@$proxyRef"
$StateDir = Join-Path $HOME ".chutes-e2ee-proxy"
$CertDir = Join-Path $StateDir "certs"
$CertFile = Join-Path $CertDir "localhost.pem"
$KeyFile = Join-Path $CertDir "localhost-key.pem"

function Write-Log($msg) {
    Write-Host "[install] $msg"
}

function Add-LocalBinsToPath {
    $localBin = Join-Path $HOME ".local\bin"
    if ($env:Path -notlike "*$localBin*") {
        $env:Path = "$localBin;$env:Path"
    }
}

function Has-Flag($flag, [string[]]$arguments) {
    $prev = ""
    foreach ($arg in $arguments) {
        if ($prev -eq $flag) {
            return $true
        }
        if ($arg -eq $flag -or $arg.StartsWith("$flag=")) {
            return $true
        }
        $prev = $arg
    }
    return $false
}

function Resolve-TunnelMode([string[]]$arguments) {
    $mode = $env:CHUTES_PROXY_TUNNEL
    $prev = ""
    foreach ($arg in $arguments) {
        if ($prev -eq "--tunnel") {
            $mode = $arg
            $prev = ""
            continue
        }

        if ($arg.StartsWith("--tunnel=")) {
            $mode = $arg.Substring(9)
            continue
        }

        if ($arg -eq "--tunnel") {
            $prev = "--tunnel"
        }
    }

    if ([string]::IsNullOrWhiteSpace($mode)) {
        return "auto"
    }

    return $mode.Trim().ToLowerInvariant()
}

function Test-NonEmptyFile([string]$path) {
    if (-not (Test-Path -Path $path -PathType Leaf)) {
        return $false
    }
    return (Get-Item -Path $path).Length -gt 0
}

function Require-Python {
    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($python) {
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        if ($LASTEXITCODE -eq 0) { return "py" }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        if ($LASTEXITCODE -eq 0) { return "python" }
    }

    throw "Python 3.10+ is required."
}

function Ensure-UV {
    Add-LocalBinsToPath
    if (Get-Command uv -ErrorAction SilentlyContinue) { return $true }

    Write-Log "uv not found, attempting installation..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id astral-sh.uv --accept-package-agreements --accept-source-agreements *> $null
    }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        try {
            irm https://astral.sh/uv/install.ps1 | iex
        } catch {
            Write-Log "uv install script failed; will try pipx fallback."
        }
    }

    Add-LocalBinsToPath
    if (Get-Command uv -ErrorAction SilentlyContinue) { return $true }
    return $false
}

function Ensure-Pipx($pyExec) {
    Add-LocalBinsToPath
    if (Get-Command pipx -ErrorAction SilentlyContinue) { return }

    Write-Log "pipx not found, installing..."
    if ($pyExec -eq "py") {
        py -3 -m pip install --user pipx
        py -3 -m pipx ensurepath
    } else {
        & $pyExec -m pip install --user pipx
        & $pyExec -m pipx ensurepath
    }

    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    Add-LocalBinsToPath

    if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
        throw "Failed to install pipx"
    }
}

function Ensure-Mkcert {
    Add-LocalBinsToPath
    if (Get-Command mkcert -ErrorAction SilentlyContinue) { return $true }

    Write-Log "mkcert not found, attempting installation for local TLS..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id FiloSottile.mkcert --accept-package-agreements --accept-source-agreements *> $null
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        choco install mkcert -y *> $null
    }

    Add-LocalBinsToPath
    if (Get-Command mkcert -ErrorAction SilentlyContinue) { return $true }
    return $false
}

function Ensure-LocalTlsCert {
    New-Item -ItemType Directory -Path $CertDir -Force | Out-Null

    if ((Test-NonEmptyFile $CertFile) -and (Test-NonEmptyFile $KeyFile)) {
        return
    }

    if (Ensure-Mkcert) {
        try {
            mkcert -install *> $null
        } catch {
            Write-Log "mkcert root CA install failed; continuing."
        }

        mkcert -cert-file $CertFile -key-file $KeyFile localhost 127.0.0.1 ::1 *> $null
        if ((Test-NonEmptyFile $CertFile) -and (Test-NonEmptyFile $KeyFile)) {
            Write-Log "Generated trusted local TLS cert with mkcert."
            return
        }
    }

    if (Get-Command openssl -ErrorAction SilentlyContinue) {
        $opensslCfg = Join-Path $CertDir "openssl.cnf"
@"
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
"@ | Set-Content -Path $opensslCfg -Encoding ascii

        & openssl req -x509 -nodes -newkey rsa:2048 -days 365 -keyout $KeyFile -out $CertFile -config $opensslCfg -extensions v3_req *> $null
        Remove-Item -Path $opensslCfg -ErrorAction SilentlyContinue

        if ((Test-NonEmptyFile $CertFile) -and (Test-NonEmptyFile $KeyFile)) {
            Write-Log "Generated self-signed local TLS cert with openssl."
            Write-Log "If your client rejects TLS, install mkcert and re-run install."
            return
        }
    }

    throw "Failed to generate local TLS cert/key."
}

function Install-Proxy($pyExec) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Log "Installing chutes-e2ee-proxy from GitHub ref '$proxyRef' via uv..."
        uv tool install --upgrade --force $RepoFallback *> $null
        if ($LASTEXITCODE -eq 0) { return }
        uv tool install --upgrade $RepoFallback *> $null
        if ($LASTEXITCODE -eq 0) { return }

        uv tool install --upgrade --force chutes-e2ee-proxy *> $null
        if ($LASTEXITCODE -eq 0) { return }
        uv tool install --upgrade chutes-e2ee-proxy *> $null
        if ($LASTEXITCODE -eq 0) { return }

        Write-Log "uv installation failed; falling back to pipx..."
    }

    Ensure-Pipx $pyExec
    Write-Log "Installing chutes-e2ee-proxy from GitHub ref '$proxyRef' via pipx..."
    pipx install --force $RepoFallback 2>$null
    if ($LASTEXITCODE -eq 0) { return }

    $pipxList = pipx list 2>$null
    if ($pipxList -match "package chutes-e2ee-proxy") {
        pipx upgrade chutes-e2ee-proxy 2>$null
        if ($LASTEXITCODE -ne 0) {
            pipx install $RepoFallback
        }
    } else {
        pipx install chutes-e2ee-proxy 2>$null
        if ($LASTEXITCODE -ne 0) {
            pipx install $RepoFallback
        }
    }
}

function Ensure-Cloudflared {
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) { return $true }

    Write-Log "cloudflared not found, attempting installation..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements *> $null
    }

    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Log "cloudflared is still missing; proxy will run with --tunnel auto fallback behavior."
        return $false
    }
    return $true
}

$pyExec = Require-Python
Ensure-UV | Out-Null
Install-Proxy $pyExec

$tunnelMode = Resolve-TunnelMode $args
if ([string]::IsNullOrWhiteSpace($env:CHUTES_PROXY_TUNNEL) -and -not (Has-Flag "--tunnel" $args)) {
    $env:CHUTES_PROXY_TUNNEL = $tunnelMode
}

$hasTlsCert = (-not [string]::IsNullOrWhiteSpace($env:CHUTES_TLS_CERT_FILE)) -or (Has-Flag "--tls-cert-file" $args)
$hasTlsKey = (-not [string]::IsNullOrWhiteSpace($env:CHUTES_TLS_KEY_FILE)) -or (Has-Flag "--tls-key-file" $args)

if (-not $hasTlsCert -and -not $hasTlsKey) {
    Ensure-LocalTlsCert
    $env:CHUTES_TLS_CERT_FILE = $CertFile
    $env:CHUTES_TLS_KEY_FILE = $KeyFile
}

if ($tunnelMode -ne "off") {
    if (-not (Ensure-Cloudflared)) {
        if ($tunnelMode -eq "required") {
            throw "cloudflared is required but unavailable."
        }
        Write-Log "Falling back to local HTTPS because tunnel is unavailable."
        $env:CHUTES_PROXY_TUNNEL = "off"
        $tunnelMode = "off"
    }
}

Write-Log "Starting chutes-e2ee-proxy..."
chutes-e2ee-proxy serve @args
