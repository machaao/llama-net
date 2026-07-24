#Requires -Version 5.1
<#
.SYNOPSIS
    LlamaNet Windows Installer
.DESCRIPTION
    Installs LlamaNet with Python venv, creates shortcuts, adds to PATH.
    By default joins the public LlamaNet network at llamanet.app.
.NOTES
    Usage: irm https://llamanet.app/install.ps1 | iex
#>

[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\LlamaNet",
    [string]$BootstrapPeers = "https://llamanet.app",
    [switch]$SkipShortcuts,
    [switch]$Developer
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$VenvDir       = Join-Path $InstallDir "venv"
$BinDir        = Join-Path $InstallDir "bin"
$LaunchScript  = Join-Path $BinDir "llamanet.cmd"
$DesktopDir    = [Environment]::GetFolderPath("Desktop")
$StartMenuDir  = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\LlamaNet"
$MinPythonMinor = 9

function Write-Step  { param($msg) Write-Host "`n  $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "  $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  LlamaNet Installer for Windows" -ForegroundColor White
Write-Host "  ─────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# ── Step 1: Detect OS ──
Write-Step "Detecting system..."
$osVersion = [System.Environment]::OSVersion.Version
$arch = $env:PROCESSOR_ARCHITECTURE
Write-Ok "Windows $($osVersion.Major).$($osVersion.Minor) ($arch)"

$localAppDataQualifier = Split-Path $env:LOCALAPPDATA -Qualifier -ErrorAction SilentlyContinue
$driveLetter = if ($localAppDataQualifier) { $localAppDataQualifier.TrimEnd(':') } else { $env:SystemDrive.TrimEnd(':') }
try {
    $freeGB = [math]::Round((Get-PSDrive -Name $driveLetter -ErrorAction Stop).Free / 1GB, 1)
} catch {
    try {
        $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='${driveLetter}:'" -ErrorAction Stop
        $freeGB = [math]::Round($disk.FreeSpace / 1GB, 1)
    } catch {
        $freeGB = 100
        Write-Warn "Could not detect free disk space — assuming sufficient"
    }
}
if ($freeGB -lt 2) {
    Write-Fail "Insufficient disk space: ${freeGB}GB free (need 2GB+)"
}
Write-Ok "Disk space: ${freeGB} GB available"

# ── Step 2: Find or Install Python ──
Write-Step "Checking Python..."

function Find-Python {
    $candidates = @("python3.11", "python3.12", "python3.10", "python3.9", "python3", "python", "py")
    foreach ($cmd in $candidates) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                $parts = $ver.Split(".")
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if ($major -ge 3 -and $minor -ge $MinPythonMinor) {
                    return @{ Command = $cmd; Path = $found.Source; Version = $ver }
                }
            } catch { continue }
        }
    }
    return $null
}

$python = Find-Python

if (-not $python) {
    Write-Warn "Python 3.${MinPythonMinor}+ not found"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Step "Installing Python via winget..."
        & winget install --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $python = Find-Python
        if ($python) {
            Write-Ok "Python $($python.Version) installed"
        } else {
            Write-Host ""
            Write-Host "  Python installed but not in PATH yet." -ForegroundColor Yellow
            Write-Host "  Close this window, open a NEW PowerShell, and re-run:" -ForegroundColor Yellow
            Write-Host "  irm https://llamanet.app/install.ps1 | iex" -ForegroundColor Cyan
            Write-Host ""
            exit 0
        }
    } else {
        Write-Host ""
        Write-Host "  Python not found and winget not available." -ForegroundColor Red
        Write-Host "  Install Python manually:" -ForegroundColor Yellow
        Write-Host "    winget install Python.Python.3.11" -ForegroundColor Cyan
        Write-Host "  Or download from https://www.python.org/downloads/" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  IMPORTANT: Check 'Add Python to PATH' during install!" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
} else {
    Write-Ok "Python $($python.Version) at $($python.Path)"
}

# ── Step 3: Check Build Tools ──
Write-Step "Checking build tools..."
$vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$hasBuildTools = $false

if (Test-Path $vsWhere) {
    $installPath = & $vsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    if ($installPath) { $hasBuildTools = $true }
}

if (-not $hasBuildTools) {
    $vcBuildTools = Get-ChildItem "${env:ProgramFiles(x86)}\Microsoft Visual Studio\*\BuildTools" -ErrorAction SilentlyContinue
    if ($vcBuildTools) { $hasBuildTools = $true }
}

$gcc = Get-Command gcc -ErrorAction SilentlyContinue

if ($hasBuildTools) {
    Write-Ok "Visual Studio Build Tools detected"
} elseif ($gcc) {
    Write-Ok "GCC compiler found"
} else {
    Write-Warn "No C++ build tools detected"
    Write-Host ""
    Write-Host "  llama-cpp-python needs a C++ compiler." -ForegroundColor Yellow
    Write-Host "  Install Build Tools:" -ForegroundColor White
    Write-Host "    winget install Microsoft.VisualStudio.2022.BuildTools" -ForegroundColor Cyan
    Write-Host ""
    $continue = Read-Host "  Continue anyway? (y/N)"
    if ($continue -ne "y" -and $continue -ne "Y") {
        Write-Host "  Install build tools and re-run this installer."
        exit 1
    }
}

# ── Step 4: Create Directories ──
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
Write-Step "Directories created at $InstallDir"

# ── Step 5: Create Virtual Environment ──
Write-Step "Setting up virtual environment..."
$upgradeMode = $false

if (Test-Path (Join-Path $VenvDir "Scripts\activate.ps1")) {
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (Test-Path $venvPython) {
        try {
            & $venvPython -c "import fastapi" 2>$null
            Write-Ok "Existing venv found — will upgrade"
            $upgradeMode = $true
        } catch {
            Write-Warn "Existing venv broken — recreating"
            Remove-Item -Recurse -Force $VenvDir
        }
    }
}

if (-not (Test-Path $VenvDir)) {
    & $python.Command -m venv $VenvDir
    Write-Ok "Virtual environment created"
}

$activateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
. $activateScript

$pip = Join-Path $VenvDir "Scripts\pip.exe"
& $pip install --upgrade pip setuptools wheel 2>$null | Out-Null

# ── Step 6: Install LlamaNet ──
Write-Step "Installing LlamaNet (this may take a few minutes)..."

if ($Developer -and (Test-Path ".\pyproject.toml") -and (Test-Path ".\inference_node")) {
    & $pip install -e "."
    & $pip install -r requirements-inference.txt
} else {
    Write-Host "  Installing from GitHub..." -ForegroundColor DarkGray
    try {
        & $pip install "git+https://github.com/machaao/llama-net.git"
    } catch {
        Write-Fail "Failed to install LlamaNet from GitHub. Check your internet connection."
    }
    Write-Ok "LlamaNet package installed"

    Write-Host "  Installing inference engine..." -ForegroundColor DarkGray
    try {
        & $pip install llama-cpp-python psutil pynvml tqdm huggingface_hub
    } catch {
        Write-Warn "Pre-built wheel not available, building from source..."
        & $pip install llama-cpp-python --verbose
    }
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
try {
    & $venvPython -c "import inference_node" 2>$null
    Write-Ok "LlamaNet verified"
} catch {
    Write-Warn "Verification failed — attempting repair"
    & $pip install -e . 2>$null
}

# ── Step 7: Set Default Bootstrap Peers ──
Write-Step "Configuring network: joining public LlamaNet network..."
$existingBootstrap = [System.Environment]::GetEnvironmentVariable("BOOTSTRAP_PEERS", "User")
if (-not $existingBootstrap) {
    [System.Environment]::SetEnvironmentVariable("BOOTSTRAP_PEERS", $BootstrapPeers, "User")
    $env:BOOTSTRAP_PEERS = $BootstrapPeers
    Write-Ok "Bootstrap peers set: $BootstrapPeers"
} else {
    Write-Ok "Bootstrap peers already configured: $existingBootstrap"
}

# ── Step 8: Create CLI Launcher ──
Write-Step "Creating launcher..."

$launcherContent = @"
@echo off
setlocal enabledelayedexpansion

set "LLAMANET_HOME=%LOCALAPPDATA%\LlamaNet"
set "VENV_DIR=%LLAMANET_HOME%\venv"

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo LlamaNet venv not found. Re-run installer:
    echo   irm https://llamanet.app/install.ps1 ^| iex
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"

REM Default to public LlamaNet network
if not defined BOOTSTRAP_PEERS set "BOOTSTRAP_PEERS=$BootstrapPeers"

REM Daily update check
set "UPDATE_CHECK=%LLAMANET_HOME%\.last_update_check"
for /f "tokens=2 delims==" %%i in ('wmic os get localdatetime /value') do set DT=%%i
set "TODAY=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%"
if exist "%UPDATE_CHECK%" (
    set /p LAST_CHECK=<"%UPDATE_CHECK%"
) else (
    set "LAST_CHECK="
)
if not "%TODAY%"=="%LAST_CHECK%" (
    echo Checking for updates...
    pip install --upgrade "git+https://github.com/machaao/llama-net.git" 2>nul
    echo %TODAY%>"%UPDATE_CHECK%"
)

if "%~1"=="" goto :LAUNCH
python -m inference_node.server %*
goto :EOF

:LAUNCH
echo.
echo  Starting LlamaNet...
echo.

start /b python -m inference_node.server --host 0.0.0.0 --port 8000 --bootstrap-peers "%BOOTSTRAP_PEERS%" > "%LLAMANET_HOME%\server.log" 2>&1

echo  Waiting for server...
set READY=0
for /L %%i in (1,1,30) do (
    if !READY!==0 (
        curl -s http://localhost:8000/health >nul 2>&1
        if !errorlevel!==0 (
            set READY=1
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)

if %READY%==1 (
    echo.
    echo   Web UI:    http://localhost:8000
    echo   Network:   Connected to %BOOTSTRAP_PEERS%
    echo   API:       http://localhost:8000/v1/chat/completions
    echo.
    echo   Press Ctrl+C to stop
    echo.
    start http://localhost:8000
    powershell -Command "Get-Content '%LLAMANET_HOME%\server.log' -Wait"
) else (
    echo  Server failed to start. Check %LLAMANET_HOME%\server.log
    type "%LLAMANET_HOME%\server.log"
    pause
)

endlocal
"@

Set-Content -Path $LaunchScript -Value $launcherContent -Encoding ASCII
Write-Ok "CLI launcher created"

# ── Step 9: Create Shortcuts ──
if (-not $SkipShortcuts) {
    Write-Step "Creating shortcuts..."
    $WshShell = New-Object -ComObject WScript.Shell
    $pythonExe = Join-Path $VenvDir "Scripts\python.exe"

    # Desktop
    $desktopShortcut = $WshShell.CreateShortcut((Join-Path $DesktopDir "LlamaNet.lnk"))
    $desktopShortcut.TargetPath = "cmd.exe"
    $desktopShortcut.Arguments = "/c `"$LaunchScript`""
    $desktopShortcut.WorkingDirectory = $env:USERPROFILE
    $desktopShortcut.Description = "LlamaNet - Local AI Inference"
    $desktopShortcut.WindowStyle = 1
    $desktopShortcut.IconLocation = if (Test-Path $pythonExe) { "$pythonExe,0" } else { "shell32.dll,13" }
    $desktopShortcut.Save()
    Write-Ok "Desktop shortcut created"

    # Start Menu
    $startShortcut = $WshShell.CreateShortcut((Join-Path $StartMenuDir "LlamaNet.lnk"))
    $startShortcut.TargetPath = "cmd.exe"
    $startShortcut.Arguments = "/c `"$LaunchScript`""
    $startShortcut.WorkingDirectory = $env:USERPROFILE
    $startShortcut.Description = "LlamaNet - Local AI Inference"
    $startShortcut.WindowStyle = 1
    $startShortcut.IconLocation = if (Test-Path $pythonExe) { "$pythonExe,0" } else { "shell32.dll,13" }
    $startShortcut.Save()
    Write-Ok "Start Menu shortcut created"
}

# ── Step 10: Update PATH ──
Write-Step "Updating PATH..."
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [System.Environment]::SetEnvironmentVariable("Path", "$BinDir;$userPath", "User")
    $env:Path = "$BinDir;$env:Path"
    Write-Ok "Added to user PATH"
    Write-Host "  Open a new terminal to use 'llamanet' command" -ForegroundColor DarkGray
} else {
    Write-Ok "PATH already configured"
}

# ── Done ──
Write-Host ""
Write-Host "  ─────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  LlamaNet installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  Start now:  llamanet" -ForegroundColor White
Write-Host "  Or double-click:  Desktop\LlamaNet" -ForegroundColor White
Write-Host ""
Write-Host "  The Web UI will open at http://localhost:8000" -ForegroundColor White
Write-Host "  Your node will join the public network at llamanet.app" -ForegroundColor White
Write-Host "  Use the Model Manager to download a GGUF model." -ForegroundColor White
Write-Host ""
Write-Host "  Install path:  $InstallDir" -ForegroundColor DarkGray
Write-Host "  Python:        $($python.Version)" -ForegroundColor DarkGray
Write-Host "  Network:       $BootstrapPeers" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ─────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

$response = Read-Host "  Start LlamaNet now? (Y/n)"
if ($response -ne "n" -and $response -ne "N") {
    & $LaunchScript
}
