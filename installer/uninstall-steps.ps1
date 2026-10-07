# installer/uninstall-steps.ps1 - stop components, drop scheduled tasks.
# DATA (pgdata/backups) is preserved - a data wipe is a separate manual action.
$ErrorActionPreference = 'SilentlyContinue'
Stop-Process -Name pharmaos-api -Force
Stop-Process -Name node -Force
$pgData = Join-Path $env:PROGRAMDATA 'PharmaOS\pgdata'
$pgCtl = Join-Path $env:PROGRAMFILES 'PharmaOS\resources\pg\bin\pg_ctl.exe'
if ((Test-Path $pgCtl) -and (Test-Path (Join-Path $pgData 'postmaster.pid'))) {
    & $pgCtl -D $pgData stop -m fast | Out-Null
}
foreach ($task in @('PharmaOS-Backup', 'PharmaOS-Compliance-Drain', 'PharmaOS-Expiry-Sweep', 'PharmaOS-Alerts-Evaluate')) {
    schtasks /Delete /F /TN $task | Out-Null
}
exit 0
