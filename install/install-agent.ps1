# Hermes Agent (Mongo) curl installer — Windows
#
#   irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex

$ErrorActionPreference = "Stop"
$Repo = if ($env:HERMES_MONGO_REPO) { $env:HERMES_MONGO_REPO } else { "KiberSlesar/hermes-agent-MongoDB" }
if ($Repo -eq "KiberSlesar/hermes-agent-MongoDB-private") {
    $Repo = "KiberSlesar/hermes-agent-MongoDB"
}
$Ref = if ($env:HERMES_MONGO_REF) { $env:HERMES_MONGO_REF } else { "main" }
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }
$Yes = ($env:HERMES_YES -eq "1")
$SkipConnect = ($env:HERMES_SKIP_CONNECT -eq "1")

# Promote Telegram/messaging proxy → HTTPS_PROXY for GitHub downloads.
$envFile = Join-Path $HermesHome ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $k = $Matches[1]; $v = $Matches[2].Trim().Trim('"').Trim("'")
            if (-not [string]::IsNullOrWhiteSpace($v) -and -not [Environment]::GetEnvironmentVariable($k)) {
                [Environment]::SetEnvironmentVariable($k, $v, "Process")
            }
        }
    }
}
if (-not ($env:HTTPS_PROXY -or $env:HTTP_PROXY -or $env:https_proxy -or $env:http_proxy)) {
    $msgProxy = $env:TELEGRAM_PROXY
    if (-not $msgProxy) { $msgProxy = $env:DISCORD_PROXY }
    if (-not $msgProxy) { $msgProxy = $env:GATEWAY_PROXY_URL }
    if ($msgProxy) {
        $env:HTTPS_PROXY = $msgProxy
        $env:HTTP_PROXY = $msgProxy
        $env:https_proxy = $msgProxy
        $env:http_proxy = $msgProxy
    }
}
if (($env:HTTPS_PROXY -or $env:HTTP_PROXY) -and -not ($env:NO_PROXY -or $env:no_proxy)) {
    $noProxy = "127.0.0.1,localhost,::1,.local"
    if ($env:HERMES_NO_PROXY) {
        $noProxy = "$noProxy,$($env:HERMES_NO_PROXY)"
    }
    $env:NO_PROXY = $noProxy
    $env:no_proxy = $env:NO_PROXY
}

function Get-AuthHeaders {
    $h = @{}
    $tok = $env:GH_TOKEN; if (-not $tok) { $tok = $env:GITHUB_TOKEN }
    if ($tok) { $h["Authorization"] = "Bearer $tok" }
    return $h
}

function Download-RepoTarball {
    param(
        [string]$RepoName,
        [string]$RefName,
        [string]$OutFile,
        [int]$Attempts = 5
    )

    $uri = "https://api.github.com/repos/$RepoName/tarball/$RefName"
    $codeload = "https://codeload.github.com/$RepoName/tar.gz/$RefName"
    try {
        [Net.ServicePointManager]::SecurityProtocol = (
            [Net.ServicePointManager]::SecurityProtocol -bor
            [Net.SecurityProtocolType]::Tls12
        )
    } catch {}

    $headers = Get-AuthHeaders
    $lastError = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            if (Test-Path $OutFile) { Remove-Item -Force $OutFile -ErrorAction SilentlyContinue }

            $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
            if ($curl) {
                $args = @(
                    "-fsSL", "--retry", "3", "--retry-all-errors",
                    "-A", "hermes-agent-installer",
                    "-o", $OutFile
                )
                if ($headers["Authorization"]) {
                    $args += @("-H", "Authorization: $($headers['Authorization'])")
                }
                # Prefer codeload (more stable than api.github.com tarball redirects).
                & $curl.Source @args $codeload
                if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutFile) -or ((Get-Item $OutFile).Length -lt 1024)) {
                    & $curl.Source @args $uri
                }
            } else {
                $ProgressPreference = "SilentlyContinue"
                try {
                    Invoke-WebRequest -Uri $codeload -Headers $headers -OutFile $OutFile -UseBasicParsing
                } catch {
                    Invoke-WebRequest -Uri $uri -Headers $headers -OutFile $OutFile -UseBasicParsing
                }
            }

            if ((Test-Path $OutFile) -and ((Get-Item $OutFile).Length -gt 1024)) {
                return
            }
            throw "Downloaded archive is empty or too small"
        } catch {
            $lastError = $_
            Write-Host ("Download retry {0}/{1}: {2}" -f $i, $Attempts, $_.Exception.Message)
            Start-Sleep -Seconds (2 * $i)
        }
    }
    throw "Failed to download $RepoName@$RefName after $Attempts attempts. Last error: $lastError"
}

function Get-UvPath {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    foreach ($candidate in @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Ensure-Uv {
    $uv = Get-UvPath
    if ($uv) { return $uv }

    Write-Host "Installing uv bootstrap…"
    irm https://astral.sh/uv/install.ps1 | iex
    $uv = Get-UvPath
    if (-not $uv) {
        throw "uv installation failed. Install uv from https://docs.astral.sh/uv/ and rerun this installer."
    }
    return $uv
}

function Set-UvNetworkDefaults {
    # Slow / flaky PyPI links (common on Windows) exceed uv's 30s default.
    if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = "180" }
    if (-not $env:UV_HTTP_RETRIES) { $env:UV_HTTP_RETRIES = "10" }
    if (-not $env:UV_REQUEST_TIMEOUT) { $env:UV_REQUEST_TIMEOUT = $env:UV_HTTP_TIMEOUT }
}

function Install-MongoEditable {
    param(
        [string]$Uv,
        [string]$Python,
        [string]$AgentDir,
        [int]$Attempts = 5
    )

    Set-UvNetworkDefaults
    Write-Host ("Installing Mongo packages (uv HTTP timeout={0}s, retries={1})…" -f `
        $env:UV_HTTP_TIMEOUT, $env:UV_HTTP_RETRIES)

    $lastError = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            & $Uv pip install --python $Python -e $AgentDir
            if ($LASTEXITCODE -ne 0) {
                throw "uv pip install exited with code $LASTEXITCODE"
            }
            & $Python -c "import yaml, hermes_cli, hermes_storage"
            if ($LASTEXITCODE -ne 0) {
                throw "post-install import check failed (yaml/hermes_cli/hermes_storage)"
            }
            return
        } catch {
            $lastError = $_
            Write-Host ("Package install retry {0}/{1}: {2}" -f $i, $Attempts, $_.Exception.Message)
            if ($i -lt $Attempts) {
                Start-Sleep -Seconds (5 * $i)
            }
        }
    }

    throw @"
Failed to install Mongo packages after $Attempts attempts (PyPI timeout/network).
Last error: $lastError

Retry with a longer timeout, or set a mirror:
  `$env:UV_HTTP_TIMEOUT = '300'
  `$env:UV_HTTP_RETRIES = '15'
  # optional: `$env:UV_INDEX_URL = 'https://pypi.org/simple'
  `$env:HERMES_YES = '1'; `$env:HERMES_SKIP_CONNECT = '1'
  irm https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.ps1 | iex

Or repair the existing checkout without re-download:
  cd `$env:LOCALAPPDATA\hermes\hermes-agent
  `$env:UV_HTTP_TIMEOUT = '300'; `$env:UV_HTTP_RETRIES = '15'
  uv pip install --python .venv\Scripts\python.exe -e .
"@
}

function Stop-HermesRuntimeLocks {
    param(
        [string]$AgentDir
    )

    # Gateway / CLI keep .venv\Scripts\python.exe locked on Windows.
    # Force-kill checkout processes first — `hermes gateway stop` needs a healthy
    # venv, can block 30s+ on drain, and often hangs mid-broken-upgrade.
    $agentFull = [System.IO.Path]::GetFullPath($AgentDir)
    $killed = @()
    foreach ($proc in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        $cmd = [string]$proc.CommandLine
        $exe = [string]$proc.ExecutablePath
        if (-not $cmd -and -not $exe) { continue }
        $hay = "$exe $cmd"
        if ($hay -like "*$agentFull*") {
            try {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                $killed += $proc.ProcessId
            } catch {}
        }
    }
    if ($killed.Count -gt 0) {
        Write-Host ("Stopped locked Hermes process(es): {0}" -f ($killed -join ", "))
        Start-Sleep -Seconds 2
    }

    $launcher = Get-Command hermes -ErrorAction SilentlyContinue
    if (-not $launcher) { return }

    try {
        Write-Host "Stopping hermes gateway (if still running)…"
        $job = Start-Job -ScriptBlock {
            param($LauncherPath)
            & $LauncherPath gateway stop 2>&1 | Out-Null
        } -ArgumentList $launcher.Source
        $completed = Wait-Job -Job $job -Timeout 10
        if (-not $completed) {
            Stop-Job -Job $job -Force -ErrorAction SilentlyContinue
            Write-Host "gateway stop timed out after 10s; continuing install…"
        }
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Host "gateway stop skipped (runtime may be broken); continuing install…"
    }
}

function Remove-TreeWithRetry {
    param(
        [string]$Path,
        [int]$Attempts = 5
    )

    if (-not (Test-Path $Path)) { return }
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($i -eq $Attempts) { throw }
            Write-Host ("Retry delete ({0}/{1}): {2}" -f $i, $Attempts, $_.Exception.Message)
            Start-Sleep -Seconds (1 + $i)
        }
    }
}

function Confirm-ReplaceExistingInstallation {
    param(
        [string]$AgentDir
    )

    $existingCommand = Get-Command hermes -ErrorAction SilentlyContinue
    $hasExistingCheckout = Test-Path $AgentDir
    if (-not $existingCommand -and -not $hasExistingCheckout) {
        return
    }

    Write-Host "An existing Hermes installation was found:"
    if ($existingCommand) {
        Write-Host "  command: $($existingCommand.Source)"
    }
    if ($hasExistingCheckout) {
        Write-Host "  checkout: $AgentDir"
    }
    Write-Host "Replacing it removes its launcher/runtime only."
    Write-Host "HERMES_HOME data and the existing DB connection (bootstrap.yaml + certs) are kept."
    if (-not $Yes) {
        $answer = Read-Host "Update / replace existing Hermes runtime? [y/N]"
        if ($answer -notmatch '^[Yy]') {
            Write-Host "Installation cancelled; existing Hermes was left unchanged."
            exit 0
        }
    } else {
        Write-Host "HERMES_YES=1: replacing existing install"
    }

    # The old checkout is the runtime managed by this installer. Do not delete
    # arbitrary directories or user profile data discovered through PATH.
    if ($hasExistingCheckout) {
        Stop-HermesRuntimeLocks -AgentDir $AgentDir
        $backup = Join-Path (Split-Path $AgentDir -Parent) (
            "hermes-agent.bak." + (Get-Date -Format 'yyyyMMddHHmmss')
        )
        try {
            Move-Item -LiteralPath $AgentDir -Destination $backup -Force -ErrorAction Stop
        } catch {
            Remove-TreeWithRetry -Path $AgentDir
        }
    }
    if ($existingCommand -and
        $existingCommand.CommandType -eq "Application" -and
        $existingCommand.Source -and
        (Test-Path $existingCommand.Source)) {
        Remove-Item -Force $existingCommand.Source -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Hermes Agent installer (Mongo / Windows)"
Write-Host "  HERMES_HOME=$HermesHome"
Write-Host ""

New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "certs") | Out-Null
$env:HERMES_HOME = $HermesHome

$tmp = Join-Path $env:TEMP ("hermes-agent-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$archive = Join-Path $tmp "src.tgz"
$agentDir = Join-Path $HermesHome "hermes-agent"
Write-Host "Downloading Mongo fork $Repo@$Ref …"
Download-RepoTarball -RepoName $Repo -RefName $Ref -OutFile $archive
tar -xzf $archive -C $tmp
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
if (-not $src) { throw "Empty archive from $Repo@$Ref" }
Confirm-ReplaceExistingInstallation -AgentDir $agentDir
Move-Item $src.FullName $agentDir

Push-Location $agentDir
try {
    # This fork owns its entire runtime. Do not invoke the upstream Hermes
    # installer: it installs a second CLI/config path and can overwrite the
    # Mongo-aware command surface.
    $uv = Ensure-Uv
    Set-UvNetworkDefaults
    Write-Host "Installing Python 3.12 for the Mongo fork…"
    & $uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv python install 3.12 failed (exit $LASTEXITCODE)" }
    & $uv venv .venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit $LASTEXITCODE)" }
    $python = Join-Path $agentDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { throw "venv python missing: $python" }
    Install-MongoEditable -Uv $uv -Python $python -AgentDir $agentDir
} finally {
    Pop-Location
}

$binDir = Join-Path $HermesHome "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$launcher = Join-Path $binDir "hermes.cmd"
@"
@echo off
set "HERMES_HOME=$HermesHome"
cd /d "$agentDir"
"$python" -m hermes_cli.main %*
"@ | Set-Content -Path $launcher -Encoding Ascii

# Make the Mongo launcher available in this shell and future user shells.
$env:Path = "$binDir;$env:Path"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (-not (($userPath -split ';') | Where-Object {
    $_.TrimEnd('\') -ieq $binDir.TrimEnd('\')
})) {
    [Environment]::SetEnvironmentVariable(
        "Path",
        (($userPath.TrimEnd(';') + ";$binDir").TrimStart(';')),
        "User"
    )
}

# Stamp version/ref for fleet auto-update compare.
$ver = ""
try {
    $ver = (& $python -c "from hermes_cli import __version__; print(__version__)" 2>$null | Out-String).Trim()
} catch { }
$stamp = @{
    version = $ver
    ref     = $Ref
    repo    = $Repo
    applied_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
} | ConvertTo-Json -Compress
Set-Content -Path (Join-Path $HermesHome ".fleet_install_stamp") -Value $stamp -Encoding utf8

Write-Host ""
Write-Host "Mongo fork installed. No upstream Hermes runtime was installed."
$bootstrap = Join-Path $HermesHome "bootstrap.yaml"
$agentPem = Join-Path $HermesHome "certs\agent.pem"
$alreadyConnected = Test-Path $bootstrap
if ($SkipConnect) {
    if ($alreadyConnected) {
        Write-Host "HERMES_SKIP_CONNECT=1: keeping existing DB connection."
    } else {
        Write-Host "HERMES_SKIP_CONNECT=1: later run $launcher db connect"
    }
} else {
    if ($alreadyConnected) {
        Write-Host ""
        Write-Host "Existing DB connection found:"
        Write-Host "  $bootstrap"
        if (Test-Path $agentPem) { Write-Host "  $agentPem" }
        Write-Host "It was not removed by the update. Choose n unless you want a new one-time code."
        $ans = Read-Host "Connect again with a new code? [y/N]"
        if (-not $ans) { $ans = "N" }
    } else {
        $ans = Read-Host "Connect this PC to Hermes DB now? [Y/n]"
        if (-not $ans) { $ans = "Y" }
    }
    if ($ans -match '^[Yy]') {
        & $launcher db connect
    } elseif ($alreadyConnected) {
        Write-Host "Keeping existing DB connection."
    } else {
        Write-Host "Later: $launcher db connect"
    }
}
