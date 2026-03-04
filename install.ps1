$ErrorActionPreference = "Stop"

function Write-Log($msg) {
    Write-Host "[install] $msg"
}

function Add-LocalBinsToPath {
    $localBin = Join-Path $HOME ".local\bin"
    if ($env:Path -notlike "*$localBin*") {
        $env:Path = "$localBin;$env:Path"
    }
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
        winget install --id astral-sh.uv --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
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

function Install-Proxy($pyExec) {
    $fallback = "git+https://github.com/chutesai/chutes-e2ee-proxy.git"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Log "Installing chutes-e2ee-proxy via uv..."
        uv tool install --upgrade chutes-e2ee-proxy 2>$null
        if ($LASTEXITCODE -eq 0) { return }

        uv tool install --upgrade $fallback 2>$null
        if ($LASTEXITCODE -eq 0) { return }

        Write-Log "uv installation failed; falling back to pipx..."
    }

    Ensure-Pipx $pyExec
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
Ensure-UV | Out-Null
Install-Proxy $pyExec
Ensure-Cloudflared

Write-Log "Starting chutes-e2ee-proxy..."
chutes-e2ee-proxy serve @args
