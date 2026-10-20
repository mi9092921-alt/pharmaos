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
# Optional port overrides (the defaults below are deliberately temporary so a
# developer PostgreSQL on 5433/5434 is never touched):
#   $env:PHARMAOS_SMOKE_PG_PORT = 55433
#   $env:PHARMAOS_SMOKE_STAGING_PORT = 55434
#   $env:PHARMAOS_SMOKE_API_PORT = 18000
#   $env:PHARMAOS_SMOKE_PROGRAMDATA = "$env:TEMP\PharmaOS-smoke"
# Usage: powershell -ExecutionPolicy Bypass -File installer\smoke-test.ps1 [-Clean]

param([switch]$Clean)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
# Prefer the unpacked shipped runtime when it exists. This prevents the gate
# from passing against the full vendor tree while the installer accidentally
# omits a required PostgreSQL file through its packaging filters.
$ShippedResources = Join-Path $RepoRoot "installer\dist\installer\win-unpacked\resources"
if (Test-Path (Join-Path $ShippedResources "api\pharmaos-api.exe")) {
    $ApiExe = Join-Path $ShippedResources "api\pharmaos-api.exe"
    $PgBin = Join-Path $ShippedResources "pg\bin"
    Write-Host "smoke runtime: shipped unpacked resources"
}
else {
    $ApiExe = Join-Path $RepoRoot "installer\dist\api\pharmaos-api.exe"
    $PgBin = Join-Path $RepoRoot "installer\vendor\pg\bin"
    Write-Host "smoke runtime: build/vendor resources"
}
$ExpectedPgVersion = "17.11"
$PgPort = if ($env:PHARMAOS_SMOKE_PG_PORT) { [int]$env:PHARMAOS_SMOKE_PG_PORT } else { 55433 }
$StagingPort = if ($env:PHARMAOS_SMOKE_STAGING_PORT) { [int]$env:PHARMAOS_SMOKE_STAGING_PORT } else { 55434 }
$ApiPort = if ($env:PHARMAOS_SMOKE_API_PORT) { [int]$env:PHARMAOS_SMOKE_API_PORT } else { 18000 }
if (@($PgPort, $StagingPort, $ApiPort) | Where-Object { $_ -lt 1024 -or $_ -gt 65535 }) {
    throw "smoke ports must be between 1024 and 65535"
}
if ($PgPort -eq $StagingPort -or $PgPort -eq $ApiPort -or $StagingPort -eq $ApiPort) {
    throw "smoke ports must be distinct"
}
if (-not (Test-Path $ApiExe)) { throw "build-api.ps1 first (exe missing)" }
if (-not (Test-Path "$PgBin\initdb.exe")) { throw "fetch-binaries.ps1 first (pg missing)" }
$pgVersion = (& "$PgBin\postgres.exe" --version) -join " "
if ($pgVersion -notmatch "(^|\s)$([regex]::Escape($ExpectedPgVersion))(\.|\s|$)") {
    throw "bundled PostgreSQL mismatch: expected $ExpectedPgVersion, got $pgVersion"
}
Write-Host "bundled PostgreSQL: $pgVersion"
Write-Host "smoke ports: PostgreSQL=$PgPort staging=$StagingPort API=$ApiPort"

$ProgramDataRoot = if ($env:PHARMAOS_SMOKE_PROGRAMDATA) {
    $env:PHARMAOS_SMOKE_PROGRAMDATA
} else {
    $env:PROGRAMDATA
}
if (-not $ProgramDataRoot) { throw "PROGRAMDATA is not set" }
# Production intentionally resolves its data root from PROGRAMDATA. The
# override keeps this artifact-level gate runnable by a non-admin developer;
# the default remains the real device path.
$env:PROGRAMDATA = $ProgramDataRoot
$DataDir = Join-Path $ProgramDataRoot "PharmaOS"
$PgData = Join-Path $DataDir "pgdata"
$PgLog = Join-Path $DataDir "logs\pg.log"
$ApiLog = Join-Path $DataDir "logs\api.log"
$BackupDir = Join-Path $DataDir "backups"
$ApiUrl = "http://127.0.0.1:$ApiPort"
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
$env:DB_PORT = "$PgPort"
$env:API_PORT = "$ApiPort"

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
    # Win32_Process.Create uses the WMI provider's environment rather than the
    # caller's modified PowerShell environment. Set the runtime overrides in
    # the launcher so the child uses the same temporary ports and data root.
    $launcherLines = @(
        "@echo off",
        "set `"PHARMAOS_ENV=production`"",
        "set `"PROGRAMDATA=$ProgramDataRoot`"",
        "set `"PG_BIN_DIR=$PgBin`"",
        "set `"DB_PORT=$PgPort`"",
        "set `"API_PORT=$ApiPort`"",
        "`"$ApiExe`" >> `"$ApiLog`" 2>&1"
    )
    Set-Content -Path $launcher -Value ($launcherLines -join [Environment]::NewLine) -Encoding ASCII
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
& $ApiExe postgres-settings --assert
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL durability settings do not match the runtime contract" }

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
& $ApiExe backup restore --file $backupFile --live-port $PgPort --staging-port $StagingPort
if ($LASTEXITCODE -ne 0) { throw "restore failed" }
$pgdataSizeAfter = (Get-ChildItem $PgData -Recurse -File | Measure-Object Length -Sum).Sum
if ($pgdataSizeAfter -lt ($pgdataSizeBefore * 0.5)) { throw "restored pgdata suspiciously small" }
& "$PgBin\pg_isready.exe" -h 127.0.0.1 -p $PgPort | Out-Null
if ($LASTEXITCODE -ne 0) {
    & "$PgBin\pg_ctl.exe" -D $PgData start -l $PgLog
    if ($LASTEXITCODE -ne 0) { throw "pg restart after restore failed - see $PgLog" }
}
& $ApiExe postgres-settings --assert
if ($LASTEXITCODE -ne 0) { throw "restored PostgreSQL durability settings do not match the runtime contract" }

Write-Host "=== 8. API back up, health again ==="
Start-DeviceApi
$health = Invoke-Api "/api/v1/health"
Write-Host "health after restore: $health"
Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host ""
Write-Host "M2 FULL-CHAIN GATE: PASS" -ForegroundColor Green
Write-Host "pgdata before/after restore: $pgdataSizeBefore / $pgdataSizeAfter bytes"
