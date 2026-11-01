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

# Data layout (decision 3): the data root stays admin-owned/read-only for
# normal users, while the runtime-owned subdirectories are explicitly
# writable. pgdata must exist before first-run: initdb cannot create a child
# directory below the protected ProgramData root as the daily user.
$data = Join-Path $env:PROGRAMDATA 'PharmaOS'
$pgData = Join-Path $data 'pgdata'
$installerUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
New-Item -ItemType Directory -Force -Path (Join-Path $data 'backups') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $data 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path $pgData | Out-Null
# The licensing reconciler keeps one MAC-authenticated clock record directly
# under the data root (the second copy is in HKCU).  The root is intentionally
# read-only for ordinary users, so create the file while elevated and grant
# Modify only to the installing/daily account below.  Without this explicit
# ACL activation can appear to work in the current process, but the next API
# boot fails when it tries to reseed the file store and reports E-LIC-007.
$licenseStore = Join-Path $data 'license-store.json'
if (-not (Test-Path $licenseStore)) {
    New-Item -ItemType File -Force -Path $licenseStore | Out-Null
}
# Users (S-1-5-32-545): Modify on the data dirs the app writes into; the
# PharmaOS root stays admin-owned (Users: read via RX inheritance).
icacls (Join-Path $data 'backups') /grant '*S-1-5-32-545:(OI)(CI)M' /T | Out-Null
icacls (Join-Path $data 'logs') /grant '*S-1-5-32-545:(OI)(CI)M' /T | Out-Null
# PostgreSQL is more sensitive than logs/backups: only the installing/daily
# user's SID receives Modify, so another local Windows user cannot alter the
# database files. SYSTEM and Administrators retain recovery/maintenance access.
$installerUserModify = "*$installerUserSid`:(OI)(CI)M"
icacls $pgData /inheritance:r /grant '*S-1-5-18:F' '*S-1-5-32-544:F' $installerUserModify /T | Out-Null
icacls $data /inheritance:r /grant '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:RX' | Out-Null
# The file itself needs no inheritance flags: the daily account may update
# it, while SYSTEM and Administrators retain recovery access. Grant the Users
# GROUP (not only the installing account) Modify: an administrator installs
# but a different daily account runs the app, and a missing/stale per-SID
# grant made the API fail every licensing write (E-LIC-007 / PermissionError
# -> status "error" -> middleware 503s all business routes; observed live in
# the 2026-10-30 VM round). Tampering risk is covered by the MAC over the
# record (LOCK-2) — a forged file degrades licensing, it cannot extend it.
$installerUserFileModify = "*$installerUserSid`:(M)"
icacls $licenseStore /inheritance:r /grant '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:(M)' $installerUserFileModify | Out-Null

# VC++ redistributable: NOT required for the pinned binaries
# (check-vcredist.ps1 verdict: none - statically-linked CRT). Re-run that
# script if the PG pin changes; ship vc_redist only when the verdict flips.

# Security: do not change Windows Defender policy during installation. A broad
# Program Files/ProgramData exclusion would make the application directories a
# blind spot for malware. Signed binaries and a clean VM test are preferred;
# any site-specific Defender policy belongs to the administrator.

# Tasks are registered by FIRST-RUN (daily user, decision 2).
exit 0
