@echo off
chcp 65001 >nul
title تفعيل نظام PharmaOS
color 0b

echo ======================================================
echo             تفعيل نظام إدارة الصيدليات PharmaOS
echo ======================================================
echo.

set SERVER_URL=http://localhost:8000/api/v1/license

:: البحث عن أي ملف ترخيص موجود في نفس المجلد
set "LICENSE_FILE="
for %%f in (*.license) do (
    set "LICENSE_FILE=%%f"
    goto :found
)

:found
if "%LICENSE_FILE%"=="" (
    echo [!] لم يتم العثور على ملف ترخيص تلقائياً.
    echo.
    echo يرجى سحب ملف الترخيص (.license) وإفلاته هنا داخل هذه النافذة ثم اضغط Enter:
    set /p LICENSE_FILE="مسار الملف: "
)

if "%LICENSE_FILE%"=="" (
    color 0c
    echo [خطأ] لم يتم تحديد ملف الترخيص!
    goto :end
)

:: تنظيف علامات التنصيص إن وجدت
set LICENSE_FILE=%LICENSE_FILE:"=%

if not exist "%LICENSE_FILE%" (
    color 0c
    echo [خطأ] الملف غير موجود: %LICENSE_FILE%
    goto :end
)

echo.
echo جاري تفعيل الترخيص من الملف: %LICENSE_FILE% ...
echo.

:: إرسال الملف إلى الـ API
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { " ^
    "  $resp = Invoke-RestMethod -Uri '%SERVER_URL%/activate' -Method Post -InFile '%LICENSE_FILE%' -ContentType 'application/octet-stream'; " ^
    "  if ($resp.data.status -eq 'active') { " ^
    "    Write-Host '======================================================' -ForegroundColor Green; " ^
    "    Write-Host '  ✓ تم تفعيل البرنامج بنجاح!' -ForegroundColor Green; " ^
    "    Write-Host ('  صالح حتى: ' + $resp.data.valid_until) -ForegroundColor Cyan; " ^
    "    Write-Host ('  الأيام المتبقية: ' + $resp.data.days_left) -ForegroundColor Cyan; " ^
    "    Write-Host '======================================================' -ForegroundColor Green; " ^
    "  } else { " ^
    "    Write-Host ('حالة الترخيص: ' + $resp.data.status) -ForegroundColor Yellow; " ^
    "  } " ^
    "} catch { " ^
    "  Write-Host '✗ فشل التفعيل:' -ForegroundColor Red; " ^
    "  Write-Host $_.Exception.Message -ForegroundColor Red; " ^
    "  exit 1; " ^
    "}"

if %ERRORLEVEL% equ 0 (
    echo.
    echo يمكنك الآن فتح واستخدام البرنامج بشكل طبيعي.
) else (
    color 0c
    echo.
    echo برجاء التأكد من أن البرنامج يعمل، أو تواصل مع الدعم الفني.
)

:end
echo.
pause
