<#
.SYNOPSIS
    إصدار ترخيص جديد لصيدلية — للمطوّر فقط (جهاز المالك).

.DESCRIPTION
    هذا السكريبت يسألك خطوة بخطوة عن بيانات الصيدلية ثم يصدر
    ملف الترخيص (.license) الذي سترسله للصيدلية.

.EXAMPLE
    .\issue-license.ps1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── ألوان مساعدة ──────────────────────────────────────────────
function Write-Header  { param($t) Write-Host "`n══════════════════════════════════════" -ForegroundColor Cyan
                                  Write-Host "  $t" -ForegroundColor Cyan
                                  Write-Host "══════════════════════════════════════`n" -ForegroundColor Cyan }
function Write-Step    { param($n,$t) Write-Host "[$n] $t" -ForegroundColor Yellow }
function Write-Ok      { param($t) Write-Host "  ✓  $t" -ForegroundColor Green }
function Write-Err     { param($t) Write-Host "  ✗  $t" -ForegroundColor Red; exit 1 }
function Ask           { param($prompt, $default="") 
    if ($default) { $r = Read-Host "$prompt [افتراضي: $default]" }
    else          { $r = Read-Host "$prompt" }
    if (-not $r -and $default) { $r = $default }
    return $r
}

# ── التحقق من وجود Python والـ CLI ────────────────────────────
Write-Header "إصدار ترخيص PharmaOS"

$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path "$repoRoot\tools\license-cli")) {
    Write-Err "لم يُعثر على مجلد license-cli. تأكد أنك داخل مجلد المشروع."
}

# تفعيل البيئة الافتراضية إذا وُجدت
$venvPy = "$repoRoot\.venv\Scripts\python.exe"
$sysPy  = "python"
if (Test-Path $venvPy) { $py = $venvPy } else { $py = $sysPy }

# التحقق من تثبيت pharmaos-license
try {
    & $py -m pharmaos_license_cli --help 2>$null | Out-Null
} catch {
    Write-Step "!" "تثبيت أداة إصدار التراخيص..."
    Push-Location "$repoRoot\tools\license-cli"
    & $py -m pip install -e . -q
    Pop-Location
    Write-Ok "تم التثبيت"
}

# ── مسار ملف المفتاح ──────────────────────────────────────────
$defaultKey = "$env:USERPROFILE\.pharmaos-vendor\license-signing.key"
if (Test-Path "$repoRoot\.pharmaos-devkeys\issuer.plkey") {
    $defaultKey = "$repoRoot\.pharmaos-devkeys\issuer.plkey"
}

# ── جمع بيانات الصيدلية ───────────────────────────────────────
Write-Step 1 "بيانات الصيدلية"
$customer = Ask "اسم الصيدلية (مثال: صيدلية النور)"
if (-not $customer) { Write-Err "اسم الصيدلية مطلوب." }

Write-Host ""
Write-Host "  ℹ  HWID يظهر على شاشة تفعيل البرنامج في الصيدلية." -ForegroundColor DarkCyan
Write-Host "     الشكل: PHAR-XXXX-XXXX-XXXX-XXXX" -ForegroundColor DarkCyan
$hwid = Ask "كود الجهاز (HWID)"
if ($hwid -notmatch "^PHAR-[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$") {
    Write-Err "HWID غير صحيح. يجب أن يكون بالشكل: PHAR-XXXX-XXXX-XXXX-XXXX"
}

Write-Step 2 "نوع الترخيص"
Write-Host "  1) trial      - تجريبي  (14 يوم)"
Write-Host "  2) monthly    - شهري    (31 يوم)"
Write-Host "  3) annual     - سنوي    (365 يوم)"
Write-Host "  4) emergency  - طوارئ   (7 أيام)"
$choice = Ask "اختر رقم الترخيص" "2"
$preset = switch ($choice) {
    "1" { "trial" }
    "2" { "monthly" }
    "3" { "annual" }
    "4" { "emergency" }
    default { Write-Err "اختيار غير صحيح."; "" }
}

Write-Step 3 "مسار ملف المفتاح السري"
$keyFile = Ask "مسار ملف المفتاح (.plkey)" $defaultKey
if (-not (Test-Path $keyFile)) {
    Write-Err "ملف المفتاح غير موجود: $keyFile"
}

$outFile = "$PSScriptRoot\output\$($customer -replace '[^\w\u0600-\u06FF]','-').license"
New-Item -ItemType Directory -Force -Path "$PSScriptRoot\output" | Out-Null

# ── تنفيذ الإصدار ─────────────────────────────────────────────
Write-Host ""
Write-Host "─────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  الصيدلية : $customer"
Write-Host "  HWID     : $hwid"
Write-Host "  النوع    : $preset"
Write-Host "  الملف    : $outFile"
Write-Host "─────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

$confirm = Ask "هل تريد إصدار الترخيص؟ (y/n)" "y"
if ($confirm -ne "y") { Write-Host "تم الإلغاء."; exit 0 }

Write-Host ""
Write-Host "أدخل كلمة سر ملف المفتاح:" -ForegroundColor Yellow

& $py -m pharmaos_license_cli `
    --key-file $keyFile `
    issue `
    --customer $customer `
    --hwid     $hwid `
    --preset   $preset `
    --out      $outFile

if ($LASTEXITCODE -ne 0) {
    Write-Err "فشل إصدار الترخيص. راجع الخطأ أعلاه."
}

Write-Host ""
Write-Ok "تم إصدار الترخيص بنجاح!"
Write-Host ""
Write-Host "  📁 الملف جاهز للإرسال:" -ForegroundColor Green
Write-Host "     $outFile" -ForegroundColor White
Write-Host ""
Write-Host "  📤 أرسل هذا الملف للصيدلية عبر واتساب أو إيميل." -ForegroundColor DarkCyan
Write-Host "     الصيدلية ترفعه من شاشة التفعيل داخل البرنامج." -ForegroundColor DarkCyan
Write-Host ""
Read-Host "اضغط Enter للخروج"
