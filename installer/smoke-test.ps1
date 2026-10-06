# installer/smoke-test.ps1 - M2 GATE: the full device chain on a SCRUBBED PATH.
#
# Proves, with nothing but the bundled artifacts (no Python/Node/pg on PATH):
#   device-init (DB password in memory -> initdb SCRAM + builtin C.UTF-8 ->
#   keystore) -> migrate -> bootstrap-admin/branch -> API up -> /api/v1/health
#   -> backup create (no pg on PATH) -> export-key -> import-key (stdin) ->
#   restore (destroy pgdata first) -> API back up -> health again.
#
# Runs in PRODUCTION mode against the REAL Windows keystore (DPAPI) and the
# REAL data dir (C:\ProgramData\PharmaOS) - the same contract as the device.
# Cleanup: -Clean removes the data dir and the keystore secrets first.
#
# Usage: powershell -ExecutionPolicy Bypass -File installer\smoke-test.ps1 [-Clean]

param([switch]$Clean)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$ApiExe = Join-Path $RepoRoot "installer\dist\pharmaos-api\pharmaos-api.exe"
$PgBin = Join-Path $RepoRoot "installer\vendor\pg\bin"
if (-not (Test-Path $ApiExe)) { throw "build-api.ps1 first (exe missing)" }
if (-not (Test-Path "$PgBin\initdb.exe")) { throw "fetch-binaries.ps1 first (pg missing)" }

$DataDir = Join-Path $env:PROGRAMDATA "PharmaOS"
$PgData = Join-Path $DataDir "pgdata"
$PgLog = Join-Path $DataDir "logs\pg.log"
$ApiLog = Join-Path $DataDir "logs\api.log"
$BackupDir = Join-Path $DataDir "backups"
$ApiUrl = "http://127.0.0.1:8000"
$ScrubbedPath = "$env:SystemRoot\System32;$env:SystemRoot"

if ($Clean) {
    Write-Host "cleaning previous smoke state..."
    Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-Process postgres -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$RepoRoot*" } | Stop-Process -Force
    & $ApiExe device-reset --yes 2>$null | Out-Null
    if (Test-Path $DataDir) { Remove-Item -Recurse -Force $DataDir }
}
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

# Scrub PATH: prove the chain needs NOTHING installed on the device.
$env:PATH = $ScrubbedPath
$env:PHARMAOS_ENV = "production"
$env:PG_BIN_DIR = $PgBin

function Invoke-Api([string]$Path) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "$ApiUrl$Path" -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { return $resp.Content }
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    throw "API not reachable at $ApiUrl$Path"
}

function Start-DeviceApi {
    # A tiny .cmd launcher sidesteps BOTH PowerShell re-quoting AND cmd /c
    # nested-quote stripping (each mangled the command line differently).
    $launcher = Join-Path $DataDir "run-api.cmd"
    Set-Content -Path $launcher -Value "@`"$ApiExe`" >> `"$ApiLog`" 2>&1" -Encoding ASCII
    $cmdLine = "cmd.exe /c `"$launcher`""
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $cmdLine
        CurrentDirectory = $DataDir
    }
    if ($result.ReturnValue -ne 0) { throw "API launch failed (WMI rc=$($result.ReturnValue))" }
}

Write-Host "=== 1. device-init (first-run, daily-user context) ==="
& $ApiExe device-init
if ($LASTEXITCODE -ne 0) { throw "device-init failed (rc=$LASTEXITCODE) - run with -Clean to reset" }

Write-Host "=== 1.5. start PG (the orchestrator's step) ==="
& "$PgBin\pg_ctl.exe" -D $PgData start -l $PgLog
if ($LASTEXITCODE -ne 0) { throw "pg start failed - see $PgLog" }

Write-Host "=== 2. migrate (bundled SQL, asyncpg) ==="
& $ApiExe migrate
if ($LASTEXITCODE -ne 0) { throw "migrate failed (rc=$LASTEXITCODE)" }

Write-Host "=== 3. bootstrap admin + branch ==="
$env:PHARMAOS_ADMIN_PASSWORD = "Sm0ke@Test!"
& $ApiExe bootstrap-admin --username smoke-admin --full-name "Smoke Tester"
if ($LASTEXITCODE -ne 0) { throw "bootstrap-admin failed" }
& $ApiExe bootstrap-branch --name "Pharmacy-Smoke"
if ($LASTEXITCODE -ne 0) { throw "bootstrap-branch failed" }
Remove-Item Env:PHARMAOS_ADMIN_PASSWORD

Write-Host "=== 4. start API, health ==="
Start-DeviceApi
$health = Invoke-Api "/api/v1/health"
Write-Host "health: $health"

Write-Host "=== 5. backup create (no pg on PATH) ==="
& $ApiExe backup create --no-cloud
if ($LASTEXITCODE -ne 0) { throw "backup create failed" }
$backupFile = (Get-ChildItem $BackupDir -Filter "*.pharmaos-backup" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
Write-Host "backup: $backupFile"

Write-Host "=== 6. export-key -> import-key (stdin) ==="
$key = (& $ApiExe backup export-key | Select-Object -First 1)
$key | & $ApiExe backup import-key --stdin
if ($LASTEXITCODE -ne 0) { throw "import-key failed" }

Write-Host "=== 7. disaster: stop API+PG, destroy pgdata, restore ==="
Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force
& "$PgBin\pg_ctl.exe" -D $PgData stop -m fast
$pgdataSizeBefore = (Get-ChildItem $PgData -Recurse -File | Measure-Object Length -Sum).Sum
Remove-Item -Recurse -Force $PgData
New-Item -ItemType Directory -Force -Path $PgData | Out-Null
& $ApiExe backup restore --file $backupFile
if ($LASTEXITCODE -ne 0) { throw "restore failed" }
$pgdataSizeAfter = (Get-ChildItem $PgData -Recurse -File | Measure-Object Length -Sum).Sum
if ($pgdataSizeAfter -lt ($pgdataSizeBefore * 0.5)) { throw "restored pgdata suspiciously small" }

Write-Host "=== 8. API back up, health again ==="
Start-DeviceApi
$health = Invoke-Api "/api/v1/health"
Write-Host "health after restore: $health"
Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host ""
Write-Host "M2 FULL-CHAIN GATE: PASS" -ForegroundColor Green
Write-Host "pgdata before/after restore: $pgdataSizeBefore / $pgdataSizeAfter bytes"
