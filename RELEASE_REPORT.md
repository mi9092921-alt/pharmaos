# تقرير تجهيز إطلاق PharmaOS

التاريخ: 2026-10-19
الفرع: `release-prep`

## القرار

**جاهز بشروط، وليس جاهزاً لإطلاقه في صيدلية حقيقية بعد.**
اختبارات البرمجيات، الذرّية، الاسترجاع، الحجم، الوظائف، الصلاحيات، الأمان،
والبناء نجحت. الشرطان المتبقيان قبل التشغيل الفعلي هما اختبار المثبت والاسترجاع
على جهاز Windows نظيف، ثم اجتياز تجربة العتاد والصيدلية التجريبية.

## ملخص المرحلة 0

- اللغة الخلفية: Python؛ FastAPI + SQLAlchemy async + asyncpg.
- الواجهة: Next.js 16 / React 19 / TypeScript، داخل Electron على الجهاز المحلي.
- قاعدة البيانات: PostgreSQL 17؛ الاختبارات المحلية استخدمت PostgreSQL 17.11.
  Redis موجود للخدمات السحابية الاختيارية، وليس شرطاً لمسار الجهاز المحلي.
- التشغيل: Electron يشغّل PostgreSQL المضمّن على `127.0.0.1:5433`، ثم API على
  `127.0.0.1:8000` والواجهة على `127.0.0.1:3000`.
- بيانات الإنتاج: `%ProgramData%\PharmaOS\pgdata`، والنسخ في
  `%ProgramData%\PharmaOS\backups`، والمفاتيح الحساسة في مخزن نظام التشغيل/DPAPI.
- النسخ الحالية: `pg_dump` بصيغة custom، ثم تشفير AES-256-GCM مع حزمة مفاتيح
  طوارئ، checksum، احتفاظ افتراضي بآخر 14 نسخة، ونسخة ثانية اختيارية عبر
  `BACKUP_SECONDARY_PATH`. الجدولة اليومية 02:00 عبر Windows Task Scheduler.
- شغّل الأمر `python -m pytest apps/api/tests -q`: جُمعت 456 حالة، وانتهت
  بنسبة 100% دون أخطاء. استخدمت البيئة Python 3.14.3؛ الإصدار المستهدف في
  المشروع هو Python 3.12، ولذلك يجب إعادة البوابة على 3.12 قبل الإنتاج.
- Docker لم يكن متاحاً (`docker compose ps` فشل لعدم تشغيل Docker Desktop)،
  لذلك لم أدّع اختبار مسار Docker؛ استُخدم PostgreSQL المضمّن في الاختبارات.

## نتائج المراحل

| المرحلة | ما اختُبر                                                                                 | النتيجة | الدليل                                                                                                                                                                                                                                                          |
| ------- | ----------------------------------------------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0       | الاستكشاف واختبارات المشروع                                                               | نجح     | `python -m pytest apps/api/tests -q` → 456 حالة، 100%                                                                                                                                                                                                           |
| 1       | بيع ذري، قتل عملية العميل، قتل خادم PostgreSQL، WAL، سلامة الجداول                        | نجح     | `python tools/release-prep/crash_sale_test.py run --port 5434 --iterations 50 --pgdata D:\pharmaos-release-prep\pgcrash` → `iterations=50 failures=0`, و`amcheck=Y` في 50/50، و`pg_crash_recoveries=25`                                                         |
| 1       | إعدادات الاستمرارية                                                                       | نجح     | PostgreSQL 17.11: `wal_level=replica`, `synchronous_commit=on`, `fsync=on`, `full_page_writes=on`                                                                                                                                                               |
| 2       | نسخة مشفرة، checksum، retention، نسخة ثانية، استرجاع إلى قاعدة مؤقتة، استرجاع cluster آمن | نجح     | `python -m pytest apps/api/tests/test_backup.py apps/api/tests/test_backup_restore_cluster.py -q` → 100%؛ ثم البوابة الكاملة نجحت                                                                                                                               |
| 3       | 10,000 صنف و50,000 فاتورة وبيع وتقارير                                                    | نجح     | `python tools/release-prep/volume_benchmark.py`؛ النتائج في `tools/release-prep/results/volume_results_20261019T155150.json`                                                                                                                                    |
| 4       | البيع، المرتجعات، الجرد، القيم غير الصالحة، الصلاحيات، سجل التدقيق                        | نجح     | `python -m pytest apps/api/tests/test_pos_api.py apps/api/tests/test_returns_m7.py apps/api/tests/test_users_admin.py apps/api/tests/test_rbac.py apps/api/tests/test_audit.py apps/api/tests/test_inventory.py apps/api/tests/test_inventory_api.py -q` → 100% |
| 5       | استيراد الكتالوج CSV/XLSX، الدفعات، expiry alerts/sweep، منع بيع الدفعة المنتهية          | نجح     | `python -m pytest apps/api/tests/test_seed.py apps/api/tests/test_batch_tracking_m4.py apps/api/tests/test_alerts_m6.py apps/api/tests/test_hardening_m11.py -q` → 100%                                                                                         |
| 6       | Argon2، تشفير الحقول، رسائل الأخطاء، fail-closed، فحص الأسرار                             | نجح     | اختبارات الأمان المستهدفة → 100%؛ فحص الشجرة لم يجد مفاتيح خاصة أو أسرار تشغيلية، والنتائج الموجودة لكلمات المرور محصورة في fixtures اختبارية واضحة                                                                                                             |
| 7       | lint/typecheck/build للواجهة، إعدادات المثبت والمهام المجدولة                             | جزئي    | `pnpm lint` و`pnpm typecheck` و`pnpm build` نجحت؛ لم يُشغّل `installer/smoke-test.ps1` على جهاز نظيف ولم يُختبر USB/الطابعة الحرارية فعلياً                                                                                                                     |

## قياسات الحجم

البيانات مولّدة وموسومة `GEN-PERF` داخل قاعدة مؤقتة حُذفت بعد القياس. الأرقام
بالميلي ثانية على جهاز التطوير، بعد warm-up:

| العملية               | الوسيط | أسوأ عينة |       الهدف |
| --------------------- | -----: | --------: | ----------: |
| البحث عن صنف          | 100.49 |    119.70 | أقل من 1000 |
| إتمام بيع فعلي        |  26.15 |    137.42 | أقل من 1000 |
| تقرير المبيعات اليومي | 210.60 |    276.98 | أقل من 1000 |
| تقرير المخزون         |  53.34 |     82.59 | أقل من 1000 |

لم أضف فهارس جديدة بعد القياس؛ الاستعلامات الحالية ضمن الهدف، واختبارات خطط
الاستعلام الموجودة تغطي استخدام الفهارس الأساسية.

## ما تم إصلاحه

1. `e7d41ee` — جعل harness الانقطاع يعيد إنشاء قاعدة اختبار نظيفة، يزرع المخزون
   عبر primitive الاستلام الحقيقي، ويثبت `amcheck`؛ النتيجة 50/50 سليمة.
2. `8387556` — إضافة checksum وretention، وجهة محلية ثانية ذرية عبر
   `BACKUP_SECONDARY_PATH`، وعدّادات الأصناف/الفواتير/المستخدمين/التدقيق في
   اختبار الاسترجاع، ودليل الاسترجاع العربي.
3. `85dade7` — إضافة أداة benchmark قابلة لإعادة التشغيل على حجم 10k/50k مع
   نتيجة محفوظة.

## المخاطر المتبقية قبل التشغيل الفعلي

1. **عالية:** لم يُختبر المثبت والاسترجاع الكامل على جهاز Windows نظيف؛ يجب
   تشغيل `installer/smoke-test.ps1`، والتأكد من بقاء `pgdata` بعد الترقية، وتجربة
   `backup import-key` ثم `backup restore` على جهاز آخر.
2. **عالية:** لم يُختبر انقطاع كهرباء فعلي أو وسيط تخزين تالف. اختبار القتل
   البرمجي قوي لكنه ليس بديلاً عن تجربة UPS/قرص حقيقي.
3. **عالية:** لم تُختبر الطابعة الحرارية وقارئ الباركود ودرج النقد فعلياً.
4. **متوسطة:** الاختبارات نفذت على Python 3.14.3، بينما مصفوفة الإصدار المستهدفة
   Python 3.12؛ يجب اجتياز نفس البوابات على 3.12.
5. **متوسطة:** النسخ السحابي غير مهيأ؛ الاعتماد الحالي يكون على النسخة المحلية
   والوجهة الثانية. يجب ضبط وفحص `BACKUP_SECONDARY_PATH` في كل صيدلية.
6. **متوسطة:** مصدر قاعدة الأدوية الخارجي غير مهيأ؛ الاستيراد الحالي من CSV/XLSX
   مع تحقق من الملف، ويجب اعتماد ملف مصدر رسمي قبل إدخال بيانات حقيقية.
7. **منخفضة/خارج النطاق:** النظام لا يقرأ الوصفات عبر OCR ولا يفحص التداخلات
   الدوائية، ويجب إبلاغ المستخدم بذلك بوضوح.

## خطة التراجع

- لا يُرقّى الجهاز قبل إنشاء نسخة مشفّرة والتحقق من checksum ووجود نسخة ثانية.
- عند فشل إصدار جديد، أوقف التطبيق، أعد نشر الإصدار السابق، واترك `%ProgramData%\PharmaOS`
  دون حذف.
- لا تُجرى هجرة مدمّرة. إذا احتاجت النسخة السابقة إلى بيانات أقدم، استخدم مسار
  `backup restore` الآمن الذي يفحص staging أولاً ويحتفظ بنقطة `pgdata.previous`
  أثناء التبديل.
- وسم المرشح الحالي: `v1.1.0-rc3` بعد اعتماد هذا التقرير؛ لا يُعامل كإصدار
  إنتاج نهائي قبل إغلاق المخاطر عالية الخطورة.

## التشغيل الموازي لمدة أسبوعين

### الأسبوع الأول

- اختر صيدلية واحدة وابدأ بنسخة تجريبية لا تؤثر على النظام الحالي.
- أدخل كل عملية بيع ومرتجع واستلام وجرد في النظامين في نفس اللحظة.
- قارِن يومياً: عدد الفواتير، إجمالي المبيعات، المرتجعات، النقد، وكميات خمسة
  أصناف سريعة الحركة على الأقل.
- نفّذ نسخة احتياطية يدوية واختبار استرجاع في نهاية اليوم الثالث والسابع.

### الأسبوع الثاني

- وسّع المقارنة إلى كامل الأصناف والدفعات والصلاحيات وسجل التعديلات.
- نفّذ انقطاعاً مخططاً بعد إغلاق البيع، ثم تحقق من الإقلاع وسلامة المخزون.
- راقب زمن البحث والبيع وتقارير نهاية اليوم، وأي فروق في الفواتير أو المخزون.
- لا تعتمد الإطلاق إلا بعد صفر فروق غير مفسرة، نجاح استرجاع نسخة على جهاز نظيف،
  نجاح العتاد، وموافقة مالك الصيدلية على سجل المقارنة.

## مستجدات rc2

- أُعيدت بوابة API الكاملة على Python `3.12.14`: `458` اختباراً، وانتهت دون فشل.
- أصبح إصدار Python في بناء PyInstaller مثبتاً ومتحققاً منه؛ البناء المجمد نجح،
  وفحص `--check-imports` نجح، وسجل `runtime-manifest.json` Python 3.12.14 وPostgreSQL 17.11.
- أصبح `initdb` يثبت صراحةً `wal_level=replica` و`fsync=on` و`full_page_writes=on`
  و`synchronous_commit=on`، ويثبت `smoke-test.ps1` القيم بعد التهيئة وبعد الاسترجاع.
- أصبح مفتاح النسخ يُعلَّم كمُصدّر عند تصديره، ويُمنع إكمال الإعداد بدونه، مع
  تحذير دائم داخل الواجهة، واختبار استرجاع كامل على جهاز نظيف بعد استيراد المفتاح فقط.
- أصبحت مهمة النسخ تستخدم `StartWhenAvailable`، وتسجل نجاحها أو فشلها، ويظهر
  تنبيه الواجهة بعد أكثر من 36 ساعة دون نسخة ناجحة.
- أضيفت صفحة “حول” ودليل مستخدم يذكران صراحةً أن النظام لا يفحص التداخلات
  الدوائية ولا يقرأ الوصفات عبر OCR.
- الفحوص البرمجية لا تغلق اختبارات VM النظيفة، وانقطاع الكهرباء الحقيقي، والعتاد،
  والتجربة الموازية؛ هذه ما زالت شروط الاعتماد الميداني.

## مستجدات rc3

- commit الإصلاح: `e1655841eb1c7c68f54f1cea15e3460f7d32c4eb`، والوسم annotated:
  `v1.1.0-rc3`.
- بُني المثبّت من نسخة نظيفة عند هذا commit، من دون تثبيته على جهاز البناء:
  `installer/dist/installer/PharmaOS-Setup-1.1.0.exe`، الحجم `320998366` بايت.
- SHA256 للمثبّت: `EF77DE1679078B13F3BF5F4A63D94258AE98FD92883C235B8E4B7B286BD5572F`.
- نجح `installer/smoke-test.ps1` على مخرجات البناء الفعلية بالمنافذ المؤقتة
  PostgreSQL `55433` وstaging `55434` وAPI `18000`، مع `M2 FULL-CHAIN GATE: PASS`.
  أُرفقت المخرجات الكاملة في `docs/smoke-test-rc3.log`.
- أُعيد تشغيل المجموعة الكاملة: `458 passed` على Python `3.12.14` وPostgreSQL
  `17.11` بإعداد builtin `C.UTF-8`.

## RC4 release evidence (2026-10-20)

- Release commit: `a85674fc03e1714fdd7b0dd75b8fade47677d5bb`.
- Annotated tag: `v1.1.0-rc4`.
- The installer was built from a clean clone at `D:\projects\pharmaos-rc4-clean-a85674f` checked out at the tag. The canonical artifact is `D:\projects\pharmaos-rc4-clean-a85674f\installer\dist\installer\PharmaOS-Setup-1.1.0.exe`.
- Installer size: `321031014` bytes.
- Installer SHA256: `89FBECFB507BA55FE431584174DAF063727E1DFC24C5A4943B819CD062702A74`.
- The bundled PostgreSQL package was inspected under `installer\dist\installer\win-unpacked\resources\pg` and contains:
  - `lib\fuzzystrmatch.dll` (56832 bytes)
  - `share\extension\fuzzystrmatch.control` (190 bytes)
  - `share\extension\fuzzystrmatch--1.1.sql` (1576 bytes)
  - `share\extension\fuzzystrmatch--1.0--1.1.sql` (788 bytes)
  - `share\extension\fuzzystrmatch--1.1--1.2.sql` (351 bytes)
- A fresh PostgreSQL 17.11 cluster was initialized with the bundled binaries. The migration runner applied all migrations, including `20261020000100_fuzzystrmatch_arabic_search`; the initdb-created role `Mosta` was verified as `rolsuper = true`. The fresh database contains both `fuzzystrmatch` and `pg_trgm`, and the migration version is recorded in `_pharmaos_migrations`.
- Full API suite: `459 passed` on Python `3.12.14`, PostgreSQL `17.11`, builtin `C.UTF-8`.
- Full backup restore lifecycle passed on a fresh temporary cluster after the schema-version gate was updated for the new migration. The backup service now accepts schema version `20261020000100`.
- Volume benchmark: `tools/release-prep/results/volume_results_20261020T020713.json`; 10000 medications and 50000 invoices. Arabic exact-search median: `154.00 ms`; Arabic typo-search median: `154.34 ms`; both stayed below one second. The recorded EXPLAIN ANALYZE plans show sequential scans for the trigram and Levenshtein fallback paths, as expected; exact FTS execution was `4.304 ms`, trigram `117.834 ms`, and Levenshtein `21.027 ms` in the benchmark database.
- Arabic regression tests cover hamza/alef, taa marbuta/haa, yaa/alef maqsura, missing letters, and unrelated-name nonmatches.
- Production `installer/smoke-test.ps1 -Clean`: **NOT TESTED / blocked in this environment**. The production device-init path reached Windows Credential Manager and failed with `WinError 1312` (`A specified logon session does not exist`) under the noninteractive execution context. Do not treat this as a pass; rerun it from an ordinary interactive PowerShell session or on the clean VM before transferring or approving the installer.
