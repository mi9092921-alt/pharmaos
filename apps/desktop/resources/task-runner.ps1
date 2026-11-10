param(
    [Parameter(Mandatory = $true)]
    [string]$ApiExe,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ApiArgs
)

# Scheduled tasks must not flash a console window. Keep the API executable as
# a console build for Electron diagnostics, but launch it through this hidden
# PowerShell host for unattended maintenance commands.
$ErrorActionPreference = 'Stop'
$process = Start-Process -FilePath $ApiExe -ArgumentList $ApiArgs -WindowStyle Hidden -Wait -PassThru
exit $process.ExitCode
