# installer decision 14 - process-tree watchdog.
# Runs DETACHED from Electron. Polls the launcher PID; when it disappears
# (crash / Task Manager End Task), force-kills the whole device tree so no
# orphaned postgres/node/api process holds ports 5433/8000/3000.
param(
    [Parameter(Mandatory = $true)][int]$LauncherPid
)
$ErrorActionPreference = 'SilentlyContinue'
$deadline = (Get-Date).AddHours(24)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 2
}
# Launcher is gone (or 24h liveness cap) - kill every device process.
Get-Process pharmaos-api -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process node -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like '*PharmaOS*' -or $_.Path -like '*installer\dist\web*' } |
    Stop-Process -Force
$pgDataDir = Join-Path $env:PROGRAMDATA 'PharmaOS\pgdata'
$pidFile = Join-Path $pgDataDir 'postmaster.pid'
if (Test-Path $pidFile) {
    $postmaster = (Get-Content $pidFile -First 1).Trim()
    if ($postmaster -match '^\d+$') {
        $proc = Get-Process -Id ([int]$postmaster) -ErrorAction SilentlyContinue
        if ($proc -and $proc.Path -like '*PharmaOS*') { Stop-Process -Id $proc.Id -Force }
    }
}
