#Requires -Version 5.1
<#
.SYNOPSIS
    LlamaNet Windows Uninstaller
.NOTES
    Usage: irm https://llamanet.app/uninstall.ps1 | iex
#>

$ErrorActionPreference = "SilentlyContinue"
$InstallDir   = "$env:LOCALAPPDATA\LlamaNet"
$BinDir       = Join-Path $InstallDir "bin"
$DesktopDir   = [Environment]::GetFolderPath("Desktop")
$StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\LlamaNet"

Write-Host ""
Write-Host "  LlamaNet Uninstaller" -ForegroundColor Yellow
Write-Host "  ─────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

$confirm = Read-Host "  This will remove all LlamaNet files. Continue? (y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") {
    Write-Host "  Cancelled."
    exit 0
}

if (Test-Path $InstallDir) {
    Write-Host "  Removing $InstallDir..." -ForegroundColor DarkGray
    Remove-Item -Recurse -Force $InstallDir
    Write-Host "  Removed install directory" -ForegroundColor Green
}

$desktopLnk = Join-Path $DesktopDir "LlamaNet.lnk"
if (Test-Path $desktopLnk) {
    Remove-Item $desktopLnk
    Write-Host "  Removed Desktop shortcut" -ForegroundColor Green
}

if (Test-Path $StartMenuDir) {
    Remove-Item -Recurse -Force $StartMenuDir
    Write-Host "  Removed Start Menu shortcuts" -ForegroundColor Green
}

$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$BinDir*") {
    $newPath = ($userPath -split ";" | Where-Object { $_ -ne $BinDir }) -join ";"
    [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  Removed from PATH" -ForegroundColor Green
}

# Remove BOOTSTRAP_PEERS env var
$bootstrapEnv = [System.Environment]::GetEnvironmentVariable("BOOTSTRAP_PEERS", "User")
if ($bootstrapEnv) {
    [System.Environment]::SetEnvironmentVariable("BOOTSTRAP_PEERS", $null, "User")
    Write-Host "  Removed BOOTSTRAP_PEERS env var" -ForegroundColor Green
}

Write-Host ""
Write-Host "  ─────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  LlamaNet uninstalled successfully." -ForegroundColor Green
Write-Host ""
Write-Host "  Note: HuggingFace cache not removed. Delete manually:" -ForegroundColor DarkGray
Write-Host "    Remove-Item -Recurse ~\.cache\huggingface" -ForegroundColor DarkGray
Write-Host ""
