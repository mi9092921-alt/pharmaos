# installer/build-installer.ps1 - M6: ONE command builds the offline installer.
#
# Pipeline (every stage has its own gate; stop on first failure):
#   1. fetch-binaries.ps1   - pinned PG 17.11 (EDB) + Node 22.23.3 + manifest
#   2. check-vcredist.ps1   - empirical VC++ verdict (currently: none)
#   3. build-api.ps1        - PyInstaller onedir of the FastAPI API
#   4. build-web.ps1        - Next standalone + bundled node.exe
#   5. electron-builder     - NSIS per-machine installer (extraResources carry
#                             api/web/pg + PS helpers)
#
# Output: installer\dist\installer\PharmaOS-Setup-<version>.exe (~522MB)
#
# Usage:  powershell -ExecutionPolicy Bypass -File installer\build-installer.ps1
#         (run from an ELEVATED shell is NOT required; the first-run wizard
#          on the device handles everything user-scoped)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$steps = @(
    "fetch-binaries.ps1",
    "check-vcredist.ps1",
    "build-api.ps1",
    "build-web.ps1"
)

foreach ($step in $steps) {
    Write-Host ""
    Write-Host "==== $step ====" -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $step)
    if ($LASTEXITCODE -ne 0) { throw "$step failed with $LASTEXITCODE" }
}

Write-Host ""
Write-Host "==== electron-builder (NSIS) ====" -ForegroundColor Cyan
Push-Location (Join-Path $RepoRoot "apps\desktop")
& npx electron-builder --win nsis
$Rc = $LASTEXITCODE
Pop-Location
if ($Rc -ne 0) { throw "electron-builder failed with $Rc" }

$setup = Get-ChildItem (Join-Path $RepoRoot "installer\dist\installer") -Filter "PharmaOS-Setup-*.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { throw "installer exe missing after the build" }

# The manifest travels with the build so M6-1 can prove tested == shipped.
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "OFFLINE INSTALLER READY:" -ForegroundColor Green
Write-Host $setup.FullName
Write-Host ("size: {0:N0} bytes" -f $setup.Length)
Write-Host "================================================" -ForegroundColor Green
