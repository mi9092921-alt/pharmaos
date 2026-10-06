# installer/fetch-binaries.ps1 - M2 (installer decision 16).
#
# Downloads the PINNED runtime binaries into installer/vendor/ (gitignored)
# and writes installer/runtime-manifest.json (exact versions + SHA256 of every
# downloaded artifact + of each FINAL artifact placed in vendor/).
#
# Sources (pinned here - bump deliberately, record in docs/versions.md):
#   PostgreSQL 17.11.0 - the official EDB Windows binaries ZIP (FULL toolset:
#                        initdb/pg_ctl/postgres AND psql/pg_dump/pg_restore,
#                        which the backup/restore subsystem requires - the
#                        zonky embedded binaries ship the server only).
#   Node.js 22.23.3    - official win-x64 zip (we ship node.exe only); its
#                        SHA256 is verified against Node's SHASUMS256.txt.
#
# The VC++ runtime requirement of the PG binaries is determined empirically
# by check-vcredist.ps1 (never assumed).
#
# Usage:  powershell -ExecutionPolicy Bypass -File installer\fetch-binaries.ps1

$ErrorActionPreference = "Stop"

$PgVersion = "17.11"
$PgZipUrl = "https://get.enterprisedb.com/postgresql/postgresql-$PgVersion-1-windows-x64-binaries.zip"
$NodeVersion = "22.23.3"
$NodeZipUrl = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip"
$NodeShasumsUrl = "https://nodejs.org/dist/v$NodeVersion/SHASUMS256.txt"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$Vendor = Join-Path $RepoRoot "installer\vendor"
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

function Get-FileWithSha([string]$Url, [string]$OutFile) {
    if (Test-Path $OutFile) {
        Write-Host "cached: $OutFile"
    }
    else {
        Write-Host "downloading: $Url"
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
    return (Get-FileHash -Path $OutFile -Algorithm SHA256).Hash.ToLower()
}

# ----------------------------------------------------------------- PG (EDB)
$PgZip = Join-Path $Vendor "postgresql-$PgVersion-1-windows-x64-binaries.zip"
$pgZipSha = Get-FileWithSha $PgZipUrl $PgZip

$PgHome = Join-Path $Vendor "pg"
if (-not (Test-Path (Join-Path $PgHome "bin\pg_dump.exe"))) {
    Write-Host "extracting postgres binaries (zip ~340MB, takes a minute)..."
    $TmpDir = Join-Path $Vendor "_pg-extract"
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
    Expand-Archive -Path $PgZip -DestinationPath $TmpDir -Force
    if (Test-Path $PgHome) { Remove-Item -Recurse -Force $PgHome }
    # The zip carries a top-level pgsql/ directory.
    Move-Item (Join-Path $TmpDir "pgsql") $PgHome
    Remove-Item -Recurse -Force $TmpDir
}
foreach ($tool in @("postgres.exe", "initdb.exe", "pg_ctl.exe", "pg_dump.exe", "pg_restore.exe", "psql.exe", "pg_isready.exe")) {
    if (-not (Test-Path (Join-Path $PgHome "bin\$tool"))) { throw "$tool missing from the EDB binaries" }
}
$PgPostgresSha = (Get-FileHash (Join-Path $PgHome "bin\postgres.exe") -Algorithm SHA256).Hash.ToLower()
$PgVersionOut = & (Join-Path $PgHome "bin\postgres.exe") --version
Write-Host "postgres: $PgVersionOut"

# ------------------------------------------------------------------- Node
$NodeZip = Join-Path $Vendor "node-v$NodeVersion-win-x64.zip"
$nodeZipSha = Get-FileWithSha $NodeZipUrl $NodeZip

# Verify against Node's official SHASUMS256.txt (the zip sha is published).
$Shasums = Join-Path $Vendor "SHASUMS256-$NodeVersion.txt"
Get-FileWithSha $NodeShasumsUrl $Shasums | Out-Null
$ExpectedZipSha = (Select-String -Path $Shasums -Pattern ([regex]::Escape("node-v$NodeVersion-win-x64.zip"))).Line.Split(" ")[0]
if ($nodeZipSha -ne $ExpectedZipSha) { throw "node zip SHA256 mismatch: $nodeZipSha != $ExpectedZipSha" }
Write-Host "node zip SHA256 verified against SHASUMS256.txt"

$NodeHome = Join-Path $Vendor "node"
$NodeExe = Join-Path $NodeHome "node.exe"
if (-not (Test-Path $NodeExe)) {
    Write-Host "extracting node.exe..."
    $TmpNode = Join-Path $Vendor "_node-extract"
    if (Test-Path $TmpNode) { Remove-Item -Recurse -Force $TmpNode }
    Expand-Archive -Path $NodeZip -DestinationPath $TmpNode -Force
    New-Item -ItemType Directory -Force -Path $NodeHome | Out-Null
    Copy-Item (Join-Path $TmpNode "node-v$NodeVersion-win-x64\node.exe") $NodeExe
    Remove-Item -Recurse -Force $TmpNode
}
$NodeExeSha = (Get-FileHash $NodeExe -Algorithm SHA256).Hash.ToLower()
$NodeVersionOut = (& $NodeExe --version) -join ""

# ------------------------------------------------------------- Manifest
$Manifest = [ordered]@{
    generated_at    = (Get-Date).ToUniversalTime().ToString("o")
    postgresql      = [ordered]@{ version = $PgVersion; source = $PgZipUrl; source_sha256 = $pgZipSha; postgres_exe_sha256 = $PgPostgresSha; version_output = $PgVersionOut }
    node            = [ordered]@{ version = "v$NodeVersion"; source = $NodeZipUrl; source_sha256 = $nodeZipSha; node_exe_sha256 = $NodeExeSha; version_output = $NodeVersionOut }
}
$ManifestPath = Join-Path $PSScriptRoot "runtime-manifest.json"
$Manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $ManifestPath -Encoding UTF8
Write-Host "manifest written: $ManifestPath"
Write-Host "M2 binaries ready under installer\vendor\"
