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

function Get-AuthHeaders {
    $h = @{}
    $tok = $env:GH_TOKEN; if (-not $tok) { $tok = $env:GITHUB_TOKEN }
    if ($tok) { $h["Authorization"] = "Bearer $tok" }
    return $h
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

function Stop-HermesRuntimeLocks {
    param(
        [string]$AgentDir
    )

    # Gateway / CLI keep .venv\Scripts\python.exe locked on Windows.
    $launcher = Get-Command hermes -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            Write-Host "Stopping hermes gateway (if running)…"
            & $launcher.Source gateway stop 2>$null | Out-Null
        } catch {}
    }

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
        Remove-TreeWithRetry -Path $AgentDir
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
Invoke-WebRequest -Uri "https://api.github.com/repos/$Repo/tarball/$Ref" -Headers (Get-AuthHeaders) -OutFile $archive
tar -xzf $archive -C $tmp
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
Confirm-ReplaceExistingInstallation -AgentDir $agentDir
Move-Item $src.FullName $agentDir

Push-Location $agentDir
try {
    # This fork owns its entire runtime. Do not invoke the upstream Hermes
    # installer: it installs a second CLI/config path and can overwrite the
    # Mongo-aware command surface.
    $uv = Ensure-Uv
    Write-Host "Installing Python 3.12 for the Mongo fork…"
    & $uv python install 3.12
    & $uv venv .venv --python 3.12
    $python = Join-Path $agentDir ".venv\Scripts\python.exe"
    & $uv pip install --python $python -e .
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
