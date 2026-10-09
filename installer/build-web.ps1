# installer/build-web.ps1 - M3: the UI without Node installed on the device.
#
# Builds apps/web (Next standalone output) and assembles
# installer\dist\web\: the standalone server.js tree + .next/static + public
# + the BUNDLED node.exe. The device runtime starts it with
# PORT=3000 HOSTNAME=127.0.0.1 PHARMAOS_API_INTERNAL_URL=http://127.0.0.1:8000.
#
# Usage:  powershell -ExecutionPolicy Bypass -File installer\build-web.ps1

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$NodeExe = Join-Path $RepoRoot "installer\vendor\node\node.exe"
if (-not (Test-Path $NodeExe)) { throw "fetch-binaries.ps1 first (node.exe missing)" }

Write-Host "building apps/web (Next standalone)..."
$env:NEXT_TELEMETRY_DISABLED = "1"
Push-Location $RepoRoot
pnpm --filter @pharmaos/web build
$BuildRc = $LASTEXITCODE
Pop-Location
if ($BuildRc -ne 0) { throw "next build failed with $BuildRc" }

$Web = Join-Path $RepoRoot "apps\web"
$Standalone = Join-Path $Web ".next\standalone"
# In a pnpm workspace Next nests the server at apps/web/server.js.
$ServerJs = Join-Path $Standalone "apps\web\server.js"
if (-not (Test-Path $ServerJs)) { $ServerJs = Join-Path $Standalone "server.js" }
if (-not (Test-Path $ServerJs)) { throw "standalone server.js missing - is output:'standalone' in next.config.ts?" }
$ServerRel = $ServerJs.Substring($Standalone.Length + 1)

$Out = Join-Path $PSScriptRoot "dist\web"
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force -Path $Out | Out-Null

# 1. the standalone server tree (server.js + node_modules + .next/server ...)
# robocopy, not Copy-Item: pnpm's node_modules entries are junctions, and
# Copy-Item breaks them (styled-jsx MODULE_NOT_FOUND). robocopy FOLLOWS the
# junctions and materializes a self-contained tree; exit codes 0-7 = success.
robocopy $Standalone $Out /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }

# pnpm virtual-store flattening: the standalone tree's node_modules entries
# are JUNCTIONS into node_modules/.pnpm/<pkg>@<ver>/node_modules/. Once
# materialized as real dirs (robocopy follows them), a package's own deps
# (styled-jsx, @swc/helpers...) no longer resolve from its siblings - so for
# every direct dep of the web app we ALSO merge its .pnpm node_modules
# content (the package + everything it requires) into apps/web/node_modules.
$appNodeModules = Join-Path $Out "apps\web\node_modules"
Get-ChildItem (Join-Path $Web "node_modules") -Directory | ForEach-Object {
    if ($_.LinkType -ne "Junction" -and $_.LinkType -ne "SymbolicLink") { return }
    $real = $_.Target
    if ($real -is [array]) { $real = $real[0] }
    # .pnpm/<pkg>@<ver>/node_modules/<name> -> .pnpm/<pkg>@<ver>/node_modules
    $pkgNodeModules = Split-Path $real -Parent
    if (Test-Path $pkgNodeModules) {
        robocopy $pkgNodeModules $appNodeModules /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy dep merge failed ($($name)): $LASTEXITCODE" }
    }
}

# 2. .next/static: the standalone output does NOT include the client chunks -
#    without them every /_next/static/* URL 404s, React never hydrates, and
#    the device UI sits on "loading" forever (the M3 gate only checked HTML +
#    the API proxy, so this shipped broken). MUST come from the SAME build.
$StaticSrc = Join-Path $Web ".next\static"
$StaticDst = Join-Path $Out "apps\web\.next\static"
if (-not (Test-Path $StaticSrc)) { throw ".next/static missing - did the Next build succeed?" }
robocopy $StaticSrc $StaticDst /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy static failed with $LASTEXITCODE" }

# 3. public/: the standalone server serves it from its own directory in the
#    BUNDLE (skip when the app has no public dir).
$PublicSrc = Join-Path $Web "public"
if (Test-Path $PublicSrc) {
    $OutServerDir = Join-Path $Out (Split-Path $ServerRel -Parent)
    robocopy $PublicSrc (Join-Path $OutServerDir "public") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy public failed with $LASTEXITCODE" }
}

# 4. the bundled Node runtime (decision: no Node on the device)
Copy-Item $NodeExe (Join-Path $Out "node.exe") -Force

if (-not (Test-Path (Join-Path $Out "node.exe"))) { throw "node.exe missing after assembly" }
Write-Host "web bundle ready: $Out (server: $ServerRel)"
