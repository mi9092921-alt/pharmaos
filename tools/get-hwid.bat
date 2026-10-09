@echo off
chcp 65001 >nul
title كود جهاز الصيدلية (HWID)
color 0a

echo ======================================================
echo             استخراج كود جهاز الصيدلية (HWID)
echo ======================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { " ^
    "  $resp = Invoke-RestMethod -Uri 'http://localhost:8000/api/v1/license/status' -Method Get; " ^
    "  $hwid = $resp.data.hwid; " ^
    "  Set-Clipboard -Value $hwid; " ^
    "  Write-Host '  كود جهازك هو:' -ForegroundColor Cyan; " ^
    "  Write-Host ('  ' + $hwid) -ForegroundColor Yellow; " ^
    "  Write-Host ''; " ^
    "  Write-Host '  ✓ تم نسخ الكود تلقائياً إلى الحافظة (Clipboard)!' -ForegroundColor Green; " ^
    "  Write-Host '  يمكنك الآن لصقه (Paste / Ctrl+V) وإرساله للمطور على واتساب.' -ForegroundColor Green; " ^
    "} catch { " ^
    "  Write-Host 'تعذر الاتصال بالبرنامج. تأكد أن برنامج الصيدلية يعمل أولاً.' -ForegroundColor Red; " ^
    "}"

echo.
echo ======================================================
echo.
pause
