# Hermes Agent (Mongo) curl installer — Windows
#
#   irm https://raw.githubusercontent.com/<user>/<repo>/main/install/install-agent.ps1 | iex
#
# Private: $env:GH_TOKEN = "..."

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

Write-Host ""
Write-Host "Hermes Agent installer (Mongo / Windows)"
Write-Host "  HERMES_HOME=$HermesHome"
Write-Host ""

New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "certs") | Out-Null
$env:HERMES_HOME = $HermesHome

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Hermes base runtime…"
    try {
        irm https://hermes-agent.nousresearch.com/install.ps1 | iex
    } catch {
        Write-Host "WARNING: base install failed: $_"
    }
}

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
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv pip install -e .
    } else {
        python -m pip install -e .
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Agent installed."
$ans = Read-Host "Connect this PC to Hermes DB now? [Y/n]"
if (-not $ans) { $ans = "Y" }
if ($ans -match '^[Yy]') {
    if (Get-Command hermes -ErrorAction SilentlyContinue) {
        hermes db connect
    } else {
        Write-Host "Run after PATH reload: hermes db connect"
    }
} else {
    Write-Host "Later: hermes db connect"
}
