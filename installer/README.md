# PharmaOS Offline Installer (Windows)

مثبّت واحد `PharmaOS-Setup-x.x.exe` (~522MB) يعمل على جهاز ويندوز **نظيف تماماً** —
بدون Docker ولا Python ولا Node.js ولا PostgreSQL ولا Redis — وبدون إنترنت.
التشغيل اليومي بحساب Windows **عادي** (غير مرتفع الصلاحيات).

## بنية الجهاز بعد التثبيت

```
C:\Program Files\PharmaOS\            PharmaOS.exe (Electron orchestrator) + resources:
  api\pharmaos-api.exe                  FastAPI مجمّد بـ PyInstaller (onedir)
  pg\                                   PostgreSQL 17.11 (EDB binaries كاملة: initdb, pg_ctl, psql, pg_dump, pg_restore)
  web\node.exe + apps\web\server.js     Next.js standalone + Node 22.23.3 المجمع
  watchdog.ps1, setup.html, install-steps.ps1, uninstall-steps.ps1

C:\ProgramData\PharmaOS\              البيانات (مالكها المستخدم اليومي):
  pgdata\                               بيانات PostgreSQL (initdb أول تشغيل: SCRAM + builtin C.UTF-8)
  backups\  logs\                       النسخ المشفرة + السجلات
  .env                                  غير الأسرار فقط — كلمة مرور DB في Windows Credential Manager (DPAPI)
```

## دورة التشغيل (Electron orchestrator)

منفذ free-check برسائل عربية → PostgreSQL (initdb أول مرة) → `pg_isready` →
`pharmaos-api.exe` (انتظار `/api/v1/health`) → `migrate` (idempotent) →
`node.exe server.js` → نافذة على `127.0.0.1:3000`. الإغلاق يوقف بالعكس، و
**watchdog** منفصل يقتل الشجرة كاملة لو مات Electron (لا عمليات يتيمة تحتجز المنافذ).
كل العمليات تعمل بنفس حساب المستخدم اليومي — مولّد مفاتيح DPAPI وقارئها واحد.

## أول تشغيل (wizard عربي)

`device-init` (كلمة مرور DB في الذاكرة → initdb → keystore → حذف آمن) →
`migrate` (30 ترحيل + RBAC seeds من SQL المجمّد) → wizard: مالك النظام + الفرع +
**كلمة مرور ويندوز مرة واحدة** (لتسجيل المهام "تعمل حتى بدون تسجيل دخول" — لا تُخزَّن) →
عرض **مفتاح استعادة النسخ الاحتياطي مرة واحدة** → `setup-complete`.

## المهام المجدولة (تسجلها أول تشغيل بصوت المستخدم اليومي)

| المهمة                    | الجدول                                                                                 |
| ------------------------- | -------------------------------------------------------------------------------------- |
| PharmaOS-Backup           | يومياً 02:00 (`backup create`)                                                         |
| PharmaOS-Compliance-Drain | كل 15 دقيقة (`compliance-drain` — fail-closed في production بلا بيانات ETA/EDA حقيقية) |
| PharmaOS-Expiry-Sweep     | يومياً 03:30                                                                           |
| PharmaOS-Alerts-Evaluate  | يومياً 03:45                                                                           |

## البناء (من جذر المستودع، جهاز التطوير)

```powershell
powershell -ExecutionPolicy Bypass -File installer\build-installer.ps1
```

الخطوات: تنزيل الثنائيات المثبتة (+SHA256 → `runtime-manifest.json`) → فحص VC++
empirically (**النتيجة الحالية: لا حاجة لأي VC++ Redistributable — CRT ثابت في
ثنائيات EDB**) → PyInstaller onedir → Next standalone + node.exe → NSIS
per-machine. انظر `runtime-manifest.json` للمطابقة بين المُختبر والموزَّع.

## قائمة تحقق النشر (قبل أول صيدلية حقيقية)

- [ ] **(أ)** تثبيت نظيف على VM ويندوز (Defender الافتراضي نشط بلا استثناءات) — والتثبيت يعمل من أول فتح
- [ ] **(ب)** ترقية فوق نسخة قائمة: `pgdata` يبقى، لا إعادة initdb
- [ ] **(ج)** تثبيت بحساب admin → أول تشغيل بمستخدم عادي مختلف: المهام تحت حسابه، وعميل بـ`national_id` يُقرأ بعد إعادة تشغيل (roundtrip DPAPI)
- [ ] **(د)** انقطاع قسري منتصف wizard/استيراد كتالوج → الاستئناف يكمل من أول خطوة ناقصة بلا تكرار
- [ ] **(هـ)** تعافٍ عبر الأجهزة: نسخة احتياطية → VM جديدة → `backup import-key` (stdin) + `backup restore` → `national_id` يُقرأ
- [ ] **(ز)** logout → تشغيل مهمة النسخ يدوياً → تنجح ويُقرأ DPAPI
- [ ] **(و)** قتل قسري لـElectron (Task Manager) → 5433/8000/3000 حرة → إعادة فتح نظيفة

## حدود معروفة

- المثبّت **غير موقّع** → SmartScreen: "More info → Run anyway" (التوقيع بند لاحق).
- النسخ الاحتياطي المجدول مسجّل "run whether user is logged on or not" بكلمة مرور
  الويندوز التي أدخلها المستخدم في الـwizard لحظياً — تغيير كلمة مرور الويندوز
  يتطلب إعادة تسجيل المهام.
- auto-update غير منفّذ: الترقية = تشغيل المثبّت الأحدث فوق القديم (البيانات تبقى).
