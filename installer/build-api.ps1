# installer/build-api.ps1 - M2: freeze the FastAPI API with PyInstaller (onedir).
#
# Builds with the repo .venv (Python 3.12, per docs/versions.md) when present,
# else creates a throwaway venv. PyInstaller bundles ONLY what the entry point
# imports - celery/redis/pytest are never imported by the API, so they never
# enter the bundle (decision 2); the spec also excludes them defensively.
#
# Output: installer\dist\pharmaos-api\  (becomes <install>\resources\api)
#
# Usage:  powershell -ExecutionPolicy Bypass -File installer\build-api.ps1

# Native stderr (pip/uv chatter) must not terminate the script under PS 5.1;
# every critical step is checked explicitly via $LASTEXITCODE / Test-Path.
$ErrorActionPreference = "Continue"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path

# Stale processes hold dist files (Access denied) and the data dir.
Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process postgres -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$RepoRoot*" } | Stop-Process -Force

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "repo .venv missing - creating build venv..."
    $Venv = Join-Path $RepoRoot "installer\.venv-build"
    py -3.12 -m venv $Venv
    $Python = Join-Path $Venv "Scripts\python.exe"
    & $Python -m pip install --quiet --upgrade pip
    & $Python -m pip install --quiet -e (Join-Path $RepoRoot "apps\api")
}
# Root .venv may be uv-managed (no pip module) - fall back to uv.
& $Python -m pip install --quiet "pyinstaller==6.16.0" 2>$null
if ($LASTEXITCODE -ne 0) {
    uv pip install -p $Python "pyinstaller==6.16.0"
}
& $Python -c "import sys; assert sys.version_info[:2] == (3, 12), 'Python 3.12 required'"

$Spec = Join-Path $PSScriptRoot "pharmaos-api.spec"
$Dist = Join-Path $PSScriptRoot "dist"
$Work = Join-Path $PSScriptRoot ".pyinstaller-work"
# CRITICAL: only the API outputs are replaced - installer/dist also holds the
# web bundle and the NSIS output; wiping the whole dist (old behavior) made
# every later electron-builder run package an installer WITHOUT the API.
$OutApiDir = Join-Path $Dist "api"
if (Test-Path $OutApiDir) { Remove-Item -Recurse -Force $OutApiDir }
if (Test-Path (Join-Path $Dist "pharmaos-api")) { Remove-Item -Recurse -Force (Join-Path $Dist "pharmaos-api") }
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }

# PyInstaller emits dist\pharmaos-api; electron-builder expects dist\api.
Move-Item (Join-Path $Dist "pharmaos-api") $OutApiDir

$OutApi = Join-Path $OutApiDir "pharmaos-api.exe"
if (-not (Test-Path $OutApi)) { throw "expected exe missing: $OutApi" }
Write-Host "API bundle ready: $OutApi"

# Record the PyInstaller version into the runtime manifest (decision 16).
$ManifestPath = Join-Path $PSScriptRoot "runtime-manifest.json"
if (Test-Path $ManifestPath) {
    $PyiVersion = (& $Python -m PyInstaller --version) -join ""
    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $Manifest | Add-Member -NotePropertyName pyinstaller -NotePropertyValue ([ordered]@{ version = $PyiVersion }) -Force
    $Manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $ManifestPath -Encoding UTF8
    Write-Host "manifest updated with pyinstaller $PyiVersion"
}
