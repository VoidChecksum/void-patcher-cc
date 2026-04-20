# vpcc 1-liner installer — Windows (PowerShell 5.1+ or 7+)
#
#   irm https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/install.ps1 | iex
#
# Chains on top of Anthropic's official native installer
# (https://claude.ai/install.ps1) → then deploys vpcc patches + preload hook.
# No hardcoded absolute paths — all paths derived from $env:USERPROFILE /
# $env:LOCALAPPDATA / $env:APPDATA.
$ErrorActionPreference = 'Stop'

function Step($m) { Write-Host "[vpcc] $m" -ForegroundColor Cyan }
function OK($m)   { Write-Host "[ ok ] $m" -ForegroundColor Green }
function Warn2($m){ Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }

$Repo              = 'VoidChecksum/void-patcher-cc'
$AnthropicInstall  = 'https://claude.ai/install.ps1'

$BinDir     = Join-Path $env:USERPROFILE '.local\bin'
$DataDir    = Join-Path $env:USERPROFILE '.local\share\void-patcher-cc'
$PreloadDir = Join-Path $env:LOCALAPPDATA 'void-patcher'

$env:PATH = "$BinDir;$env:PATH"

# 1. Claude Code — install only if genuinely missing
function Test-RealClaude {
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $out = & $cmd.Source --version 2>$null
            if ($out -match 'Claude Code|claude-code') { return $true }
        } catch {}
    }
    $probes = @(
        "$env:APPDATA\npm\node_modules\@anthropic-ai\claude-code\node_modules\@anthropic-ai\claude-code-win32-x64\claude.exe",
        "$env:APPDATA\npm\node_modules\@anthropic-ai\claude-code\node_modules\@anthropic-ai\claude-code-win32-arm64\claude.exe",
        "$env:LOCALAPPDATA\Programs\claude-code\claude.exe",
        "$env:USERPROFILE\.claude\local\claude.exe",
        "$env:USERPROFILE\.claude\bin\claude.exe"
    )
    foreach ($p in $probes) {
        if ((Test-Path $p) -and ((Get-Item $p).Length -gt 1MB)) { return $true }
    }
    return $false
}
if (Test-RealClaude) {
    OK "claude already installed — skipping Anthropic installer"
} else {
    Step "installing Claude Code via $AnthropicInstall"
    Invoke-RestMethod $AnthropicInstall | Invoke-Expression
}

# 2. Python + pipx (skip if present)
$py = $null
foreach ($cand in 'python','python3') {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) { Die "Python missing — install from https://python.org" }

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    OK "pipx already installed"
} else {
    Step "installing pipx"
    & $py -m pip install --user pipx | Out-Null
    & $py -m pipx ensurepath        | Out-Null
}

# 3. vpcc — install or upgrade only if needed
$vpccInstalled = $false
try { if ((pipx list --short 2>$null | Select-String '^vpcc\s')) { $vpccInstalled = $true } } catch {}
if ($vpccInstalled) {
    $remote = (git ls-remote "https://github.com/$Repo" HEAD 2>$null).Split()[0]
    $already = $false
    try { if ((pipx runpip vpcc show vpcc 2>$null | Select-String "$remote")) { $already = $true } } catch {}
    if ($already) {
        OK "vpcc already at upstream HEAD — skipping reinstall"
    } else {
        Step "upgrading vpcc to latest upstream"
        pipx install --force "git+https://github.com/$Repo" | Out-Null
    }
} else {
    Step "installing vpcc from git+$Repo"
    pipx install "git+https://github.com/$Repo" | Out-Null
}

# 4. Clone repo locally (for contrib/ assets)
New-Item -ItemType Directory -Force -Path $DataDir    | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir     | Out-Null
New-Item -ItemType Directory -Force -Path $PreloadDir | Out-Null

if (-not (Test-Path (Join-Path $DataDir '.git'))) {
    Step "cloning $Repo to $DataDir"
    git clone --depth=1 "https://github.com/$Repo" $DataDir 2>$null
} else {
    Push-Location $DataDir; git pull --ff-only 2>$null | Out-Null; Pop-Location
}

# 5. Apply patches + deploy preload hook
Step "applying signature patches"
vpcc patch

$srcPreload = Join-Path $DataDir 'contrib\preload\claude-preload.js'
if (Test-Path $srcPreload) {
    Copy-Item $srcPreload (Join-Path $PreloadDir 'claude-preload.js') -Force
    OK "preload hook → $PreloadDir\claude-preload.js"
}

# 6. Drop Windows wrappers on user PATH
foreach ($f in 'claude.cmd','claude.ps1') {
    $src = Join-Path $DataDir "contrib\wrappers\$f"
    if (Test-Path $src) { Copy-Item $src (Join-Path $BinDir $f) -Force }
}
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable('PATH', "$BinDir;$userPath", 'User')
    Warn2 "$BinDir added to User PATH — reopen your shell"
}

# 7. Verify
Write-Host ""
vpcc doctor
Write-Host ""
OK "install complete"
OK "usage:  vpcc patch · vpcc scan · vpcc watch · vpcc doctor"
