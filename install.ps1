$ErrorActionPreference = "Stop"

function Write-Log($msg) {
    Write-Host "[install] $msg"
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

function Ensure-Pipx($pyExec) {
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

    if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
        throw "Failed to install pipx"
    }
}

function Install-Proxy {
    $fallback = "git+https://github.com/chutesai/chutes-e2ee-proxy.git"
    $pipxList = pipx list 2>$null
    if ($pipxList -match "package chutes-e2ee-proxy") {
        Write-Log "Upgrading chutes-e2ee-proxy..."
        pipx upgrade chutes-e2ee-proxy 2>$null
        if ($LASTEXITCODE -ne 0) {
            pipx install $fallback
        }
    } else {
        Write-Log "Installing chutes-e2ee-proxy via pipx..."
        pipx install chutes-e2ee-proxy 2>$null
        if ($LASTEXITCODE -ne 0) {
            pipx install $fallback
        }
    }
}

function Ensure-Cloudflared {
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) { return }

    Write-Log "cloudflared not found, attempting installation..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements 2>$null
    }

    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Log "cloudflared is still missing; proxy will run with --tunnel auto fallback behavior."
    }
}

$pyExec = Require-Python
Ensure-Pipx $pyExec
Install-Proxy
Ensure-Cloudflared

Write-Log "Starting chutes-e2ee-proxy..."
chutes-e2ee-proxy serve @args
