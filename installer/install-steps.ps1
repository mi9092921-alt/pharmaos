# installer/install-steps.ps1 - admin-time install steps (M5), called from NSIS.
# First-run secrets/initdb/migrate/task-registration happen in the DAILY
# USER's session (Electron first-run) - never here (decisions 2/3/4).
$ErrorActionPreference = 'SilentlyContinue'

# Stop anything from a previous version before files are replaced.
Stop-Process -Name pharmaos-api -Force
Stop-Process -Name node -Force

$pgData = Join-Path $env:PROGRAMDATA 'PharmaOS\pgdata'
$pgCtl = Join-Path $env:PROGRAMFILES 'PharmaOS\resources\pg\bin\pg_ctl.exe'
if ((Test-Path $pgCtl) -and (Test-Path (Join-Path $pgData 'postmaster.pid'))) {
    & $pgCtl -D $pgData stop -m fast | Out-Null
}

# Data layout (decision 3): backups/logs writable by the daily user;
# pgdata is NOT created here - initdb (first run) makes the daily user its
# owner with restrictive ACLs; .env is created by first-run too.
$data = Join-Path $env:PROGRAMDATA 'PharmaOS'
New-Item -ItemType Directory -Force -Path (Join-Path $data 'backups') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $data 'logs') | Out-Null
# Users (S-1-5-32-545): Modify on the data dirs the app writes into; the
# PharmaOS root stays admin-owned (Users: read via RX inheritance).
icacls (Join-Path $data 'backups') /grant '*S-1-5-32-545:(OI)(CI)M' /T | Out-Null
icacls (Join-Path $data 'logs') /grant '*S-1-5-32-545:(OI)(CI)M' /T | Out-Null
icacls $data /inheritance:r /grant '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:RX' | Out-Null

# VC++ redistributable: NOT required for the pinned binaries
# (check-vcredist.ps1 verdict: none - statically-linked CRT). Re-run that
# script if the PG pin changes; ship vc_redist only when the verdict flips.

# Tasks are registered by FIRST-RUN (daily user, decision 2).
exit 0
