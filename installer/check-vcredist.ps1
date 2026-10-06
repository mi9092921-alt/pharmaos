# installer/check-vcredist.ps1 - M2 gate (decision 16).
#
# Empirically determines the VC++ Redistributable requirement of the BUNDLED
# PostgreSQL binaries by scanning their PE import tables for CRT DLL names.
# The plan deliberately does NOT trust any recorded version ("2013", "2015-
# 2022") - it trusts what the actual artifacts import:
#   - no CRT imports        -> no redist needed (zonky: statically-linked CRT)
#   - msvcr120/msvcp120     -> VC++ 2013 redist
#   - msvcp140/vcruntime140 -> VC++ 2015-2022 redist
#
# Writes the verdict into installer/runtime-manifest.json (vcredist key).
# Usage: powershell -ExecutionPolicy Bypass -File installer\check-vcredist.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PgBin = Join-Path $RepoRoot "installer\vendor\pg\bin"
if (-not (Test-Path $PgBin)) { throw "run fetch-binaries.ps1 first" }

$Targets = @("msvcp140", "vcruntime140", "vcruntime140_1", "msvcr120", "msvcp120", "concrt140")
$Found = @{}
Get-ChildItem $PgBin -Include *.exe, *.dll -Recurse | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    foreach ($t in $Targets) {
        if ($text.Contains("$t.dll")) {
            if (-not $Found[$t]) { $Found[$t] = 0 }
            $Found[$t]++
        }
    }
}

$Verdict = "none"
if ($Found.Keys.Count -gt 0) {
    $names = $Found.Keys -join ", "
    if ($names -match "120") { $Verdict = "vc_redist_2013" }
    if ($names -match "140") { $Verdict = "vc_redist_2015_2022" }
    Write-Host "CRT imports FOUND: $names -> $Verdict"
}
else {
    Write-Host "CRT imports: NONE - the bundled PostgreSQL binaries need no VC++ redistributable (statically-linked CRT)."
}

$ManifestPath = Join-Path $PSScriptRoot "runtime-manifest.json"
if (Test-Path $ManifestPath) {
    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $Manifest | Add-Member -NotePropertyName vcredist -NotePropertyValue ([ordered]@{
        required = ($Verdict -ne "none"); verdict = $Verdict; scanned_bin_dir = $PgBin
    }) -Force
    $Manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $ManifestPath -Encoding UTF8
    Write-Host "manifest updated: vcredist.verdict = $Verdict"
}
