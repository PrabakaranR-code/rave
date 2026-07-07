# RAVE installer for Windows.
# Run in PowerShell:  irm https://raw.githubusercontent.com/PrabakaranR-code/rave/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:RAVE_REPO_URL) { $env:RAVE_REPO_URL } else { "https://github.com/PrabakaranR-code/rave" }
$Dir = if ($env:RAVE_HOME) { $env:RAVE_HOME } else { Join-Path $env:USERPROFILE "rave" }
$BinDir = Join-Path $env:USERPROFILE ".local\bin"

function Say($msg) { Write-Host $msg }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function WaitEnter { Read-Host "(press Enter when done)" | Out-Null }

function Find-Python {
    foreach ($c in @("python", "python3", "py")) {
        if (Have $c) {
            try {
                & $c -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return $c }
            } catch {}
        }
    }
    return $null
}

Say "→ Installing RAVE on Windows"

# --- Python 3.11+ (the language RAVE runs on) --------------------------------
$Py = Find-Python
while (-not $Py) {
    if (Have "winget") {
        Say "→ Installing Python (the language RAVE runs on)…"
        winget install --silent --accept-package-agreements --accept-source-agreements Python.Python.3.12 | Out-Null
    } else {
        Say "RAVE needs Python 3.11 or newer (the language RAVE runs on)."
        Say "I've opened the download page. Install it (tick 'Add to PATH'), then come back."
        Start-Process "https://www.python.org/downloads/"
        WaitEnter
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $Py = Find-Python
}
Say "✓ Python found: $Py"

# --- Git (a program that downloads code) -------------------------------------
while (-not (Have "git")) {
    if (Have "winget") {
        Say "→ Installing Git (a program that downloads code)…"
        winget install --silent --accept-package-agreements --accept-source-agreements Git.Git | Out-Null
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        if (Have "git") { break }
    }
    Say "RAVE needs Git (a program that downloads code)."
    Say "I've opened the download page. Install it, then come back."
    Start-Process "https://git-scm.com/downloads"
    WaitEnter
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
Say "✓ Git found"

# --- Get the code, build the sandbox, install dependencies -------------------
if (Test-Path (Join-Path $Dir ".git")) {
    Say "→ Updating RAVE in $Dir"
    git -C $Dir pull --ff-only
} else {
    Say "→ Downloading RAVE to $Dir"
    git clone $RepoUrl $Dir
}
Set-Location $Dir
& $Py -m venv .venv
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r requirements.txt
Say "✓ RAVE installed"

# --- The `rave` command -------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Shim = Join-Path $BinDir "rave.cmd"
"@echo off`r`n`"$VenvPy`" `"$Dir\main.py`" %*" | Set-Content -Path $Shim -Encoding ascii
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$BinDir", "User")
    $env:Path = "$env:Path;$BinDir"
    Say "✓ Command installed: rave (new terminals will know it)"
} else {
    Say "✓ Command installed: rave"
}

# --- Hand over to the setup wizard --------------------------------------------
& $VenvPy (Join-Path $Dir "main.py") setup
