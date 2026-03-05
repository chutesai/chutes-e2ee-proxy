$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$proxyRef = if ([string]::IsNullOrWhiteSpace($env:CHUTES_PROXY_GIT_REF)) { "main" } else { $env:CHUTES_PROXY_GIT_REF.Trim() }
$uvRequired = @("1", "true", "yes", "on") -contains ($env:CHUTES_PROXY_UV_REQUIRED + "").Trim().ToLowerInvariant()
$RepoFallback = "git+https://github.com/chutesai/chutes-e2ee-proxy.git@$proxyRef"
$StateDir = Join-Path $HOME ".chutes-e2ee-proxy"
$CertDir = Join-Path $StateDir "certs"
$CertFile = Join-Path $CertDir "localhost.pem"
$KeyFile = Join-Path $CertDir "localhost-key.pem"

function Write-Log($msg) {
    Write-Host "[install] $msg"
}

function Invoke-NativeQuiet([string]$command, [string[]]$arguments) {
    # Set SilentlyContinue locally so NativeCommandErrors from stderr output are
    # discarded rather than promoted to terminating errors by the outer Stop preference.
    # Stdout is left unredir'd: redirecting it (e.g. *>$null) gives uv a non-console
    # handle and causes it to exit -1.  Stderr is suppressed with 2>$null so the
    # terminal stays clean.  $ErrorActionPreference change is scoped to this function.
    $ErrorActionPreference = "SilentlyContinue"
    & $command @arguments 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Add-LocalBinsToPath {
    $localBin = Join-Path $HOME ".local\bin"
    $cargoBin = Join-Path $HOME ".cargo\bin"
    if ($env:Path -notlike "*$localBin*") {
        $env:Path = "$localBin;$env:Path"
    }
    if ($env:Path -notlike "*$cargoBin*") {
        $env:Path = "$cargoBin;$env:Path"
    }
}

function Refresh-PathFromRegistry {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($machinePath -or $userPath) {
        $env:Path = "$machinePath;$userPath"
    }
    Add-LocalBinsToPath
}

function Resolve-UVCommand {
    $uvExe = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvExe) { return $uvExe.Source }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return $uv.Source }
    return $null
}

function Ensure-UVExecutionPolicy {
    try {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force *> $null
    } catch {
        return
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
    Ensure-UVExecutionPolicy
    if (Resolve-UVCommand) { return $true }

    Write-Log "uv not found, attempting installation..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (-not (Invoke-NativeQuiet "winget" @("install", "--id", "astral-sh.uv", "--accept-package-agreements", "--accept-source-agreements"))) {
            Write-Log "winget could not install uv; trying fallback."
        }
    }

    Refresh-PathFromRegistry
    if (-not (Resolve-UVCommand)) {
        try {
            irm https://astral.sh/uv/install.ps1 | iex
        } catch {
            Write-Log "uv install script failed; will try pipx fallback."
        }
    }

    Refresh-PathFromRegistry
    if (Resolve-UVCommand) { return $true }
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

    Refresh-PathFromRegistry

    if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
        throw "Failed to install pipx"
    }
}

function Ensure-Mkcert {
    Add-LocalBinsToPath
    if (Get-Command mkcert -ErrorAction SilentlyContinue) { return $true }

    Write-Log "mkcert not found, attempting installation for local TLS..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Invoke-NativeQuiet "winget" @("install", "--id", "FiloSottile.mkcert", "--accept-package-agreements", "--accept-source-agreements") | Out-Null
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        Invoke-NativeQuiet "choco" @("install", "mkcert", "-y") | Out-Null
    }

    Refresh-PathFromRegistry
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

        try {
            mkcert -cert-file $CertFile -key-file $KeyFile localhost 127.0.0.1 ::1 *> $null
        } catch {}
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

        try {
            & openssl req -x509 -nodes -newkey rsa:2048 -days 365 -keyout $KeyFile -out $CertFile -config $opensslCfg -extensions v3_req *> $null
        } catch {}
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
    $uvCmd = Resolve-UVCommand
    if ($uvCmd) {
        Ensure-UVExecutionPolicy
        Write-Log "Installing chutes-e2ee-proxy from GitHub ref '$proxyRef' via uv..."
        if (Invoke-NativeQuiet $uvCmd @("tool", "install", "--upgrade", "--force", $RepoFallback)) { return }
        if (Invoke-NativeQuiet $uvCmd @("tool", "install", "--upgrade", $RepoFallback)) { return }

        # Corrupt or stale tool environment — uninstall and purge any leftover dirs, then retry.
        Invoke-NativeQuiet $uvCmd @("tool", "uninstall", "chutes-e2ee-proxy") | Out-Null
        $uvToolDir = Join-Path $env:APPDATA "uv\tools\chutes-e2ee-proxy"
        if (Test-Path $uvToolDir) { Remove-Item -Path $uvToolDir -Recurse -Force -ErrorAction SilentlyContinue }
        $uvTrampoline = Join-Path $HOME ".local\bin\chutes-e2ee-proxy.exe"
        if (Test-Path $uvTrampoline) { Remove-Item -Path $uvTrampoline -Force -ErrorAction SilentlyContinue }
        if (Invoke-NativeQuiet $uvCmd @("tool", "install", "--force", $RepoFallback)) { return }
        if (Invoke-NativeQuiet $uvCmd @("tool", "install", $RepoFallback)) { return }

        if (Invoke-NativeQuiet $uvCmd @("tool", "install", "--upgrade", "--force", "chutes-e2ee-proxy")) { return }
        if (Invoke-NativeQuiet $uvCmd @("tool", "install", "--upgrade", "chutes-e2ee-proxy")) { return }

        if ($uvRequired) {
            throw "uv installation is required but failed. Set CHUTES_PROXY_UV_REQUIRED=0 to allow pipx fallback."
        }
        Write-Log "uv installation failed; falling back to pipx..."
    } elseif ($uvRequired) {
        throw "uv installation is required but uv is unavailable. Ensure uv is installed and runnable."
    }

    Ensure-Pipx $pyExec
    Write-Log "Installing chutes-e2ee-proxy from GitHub ref '$proxyRef' via pipx..."
    if (Invoke-NativeQuiet "pipx" @("install", "--force", $RepoFallback)) { return }

    $pipxList = $null
    try { $pipxList = & pipx list *>&1 } catch {}
    if ($pipxList -match "package chutes-e2ee-proxy") {
        if (-not (Invoke-NativeQuiet "pipx" @("upgrade", "chutes-e2ee-proxy"))) {
            Invoke-NativeQuiet "pipx" @("install", $RepoFallback) | Out-Null
        }
    } else {
        if (-not (Invoke-NativeQuiet "pipx" @("install", "chutes-e2ee-proxy"))) {
            Invoke-NativeQuiet "pipx" @("install", $RepoFallback) | Out-Null
        }
    }
}

function Ensure-Cloudflared {
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) { return $true }

    Write-Log "cloudflared not found, attempting installation..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Invoke-NativeQuiet "winget" @("install", "--id", "Cloudflare.cloudflared", "--accept-package-agreements", "--accept-source-agreements") | Out-Null
    }

    Refresh-PathFromRegistry
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Log "cloudflared is still missing; proxy will run with --tunnel auto fallback behavior."
        return $false
    }
    return $true
}

$pyExec = Require-Python
$uvAvailable = Ensure-UV
if ($uvRequired -and -not $uvAvailable) {
    throw "uv installation is required but uv is unavailable. Ensure uv is installed and runnable."
}
Install-Proxy $pyExec
Refresh-PathFromRegistry

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
