# Hermes Agent (Mongo) curl installer — Windows
#
#   irm https://raw.githubusercontent.com/<user>/<repo>/main/install/install-agent.ps1 | iex
#
# Set GH_TOKEN only if you use a private fork.

$ErrorActionPreference = "Stop"
$Repo = if ($env:HERMES_MONGO_REPO) { $env:HERMES_MONGO_REPO } else { "KiberSlesar/hermes-agent-MongoDB-private" }
$Ref = if ($env:HERMES_MONGO_REF) { $env:HERMES_MONGO_REF } else { "main" }
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }

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

Write-Host ""
Write-Host "Hermes Agent installer (Mongo / Windows)"
Write-Host "  HERMES_HOME=$HermesHome"
Write-Host ""

New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "certs") | Out-Null
$env:HERMES_HOME = $HermesHome

$tmp = Join-Path $env:TEMP ("hermes-agent-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$archive = Join-Path $tmp "src.tgz"
Write-Host "Downloading Mongo fork $Repo@$Ref …"
Invoke-WebRequest -Uri "https://api.github.com/repos/$Repo/tarball/$Ref" -Headers (Get-AuthHeaders) -OutFile $archive
tar -xzf $archive -C $tmp
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
$agentDir = Join-Path $HermesHome "hermes-agent"
if (Test-Path $agentDir) { Remove-Item -Recurse -Force $agentDir }
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
$ans = Read-Host "Connect this PC to Hermes DB now? [Y/n]"
if (-not $ans) { $ans = "Y" }
if ($ans -match '^[Yy]') {
    & $launcher db connect
} else {
    Write-Host "Later: $launcher db connect"
}
