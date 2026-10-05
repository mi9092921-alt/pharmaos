# PharmaOS — خطة تنفيذ Phase 4: حماية الترخيص (Licensing & Protection)

> **الحالة:** معتمدة 2026-10-05 بعد مراجعة مستقلة متعددة الأدوار (R1 → R17) على `main` @ `b90762b`.
> **النطاق:** الترخيص والحماية فقط — **خارج النطاق صراحةً:** Offline Installer / تغليف PyInstaller / bytenode /
> electron-builder NSIS / التحديثات (موجة مستقلة لاحقة، ومدمجة لاحقاً مع P5-M5).
> **لا كود إنتاجي قبل commit مصالحة الترقيم الموثقي** (انظر §9).

## 0) Threat Model

P4 يحمي من: تزوير ملف الترخيص، نقله لجهاز آخر، ترجيع الساعة، تسميم high-water أماميًا، بتر ذيل السلسلة، مسح/تزوير سجلات المصادر الخارجية (MAC — الحد = الوصول للـ keystore)، حذف مدخل الـ keystore (قاعدة §3)، وإدخال صفوف زائفة بالـ INSERT — تُكتشف بالتحقق (§1) ولا تصنع سلسلة تتحدث دون مفتاح الـ keystore.

**خارج حدود الحماية (يُنص عليه):** local admin بخلفية Python يعدّل كود التحقق (يُقفل في موجة التغليف) · PostgreSQL superuser/مالك الـ DB المحلية · من يصل لمفاتيح OS keystore للقراءة. **مسار رسمي معلن ضمن هذا الحد:** الـ bundle يحمل مفتاح الساعة، ومفتاح الـ backup قابل للطباعة بالأمر الرسمي `backup export-key` — من يشغّل CLI ويقرأ أي ملف backup يصل لمفتاح الساعة؛ في موجة التغليف **لا يُشحن `export-key` للعميل**. **حد متبقٍ معلن:** استعادة **لقطة متسقة كاملة** (DB + سجلا المصادر الموقّعان من نفس اللحظة) لا تُكشف أوفلاين — تعزيز اختياري مؤجل (رأس السلسلة كمرساة رابعة في الـ keystore) يرفع الحد لمستوى "وصول Python" فقط. ملف جديد صادر لم يُفعّل = reset واحد محدود بـ `issued_at` الموقّع، وكل إصدار في ledger المالك.

## 1) سلسلة `license_clock_events`

- **إلحاق مزدوج القفل:** Python يمسك `pg_advisory_xact_lock(740029001)` قبل قراءة آخر seq/hash وقبل الحساب؛ **والـ trigger يبدأ بـ `PERFORM pg_advisory_xact_lock(740029001)`** (تسلسل غير قابل للتجاوز حتى بـ SQL يدوي). العزل **READ COMMITTED صراحة** (REPEATABLE READ يأخذ snapshot قبل القفل). الترتيب: BEGIN → lock → SELECT → build event → HMAC → INSERT → trigger يتحقق فقط (`NEW.seq == last+1 ∧ prev_hash == last_hash ∧ seq > 0`) → COMMIT. قبول: N=50 تزامن حقيقي ⇒ 50 صفًا، seq 1..50 بلا فجوات، ربط وهاش كاملان؛ rollback معاملة بلا فجوة.
- **🔒 LOCK-1 — صيغة `entry_hash` الحرفية (قابلة للاختبار بـ test vector):**
  `message = b"pharmaos.clock.chain.v1\n" || u64_be(seq) || prev_hash_bytes || canonical_event_bytes` ثم `entry_hash = HMAC-SHA256(LICENSE_CLOCK_HMAC_KEY, message)`.
  حيث: `u64_be(seq)` = 8 بايت big-endian · `prev_hash_bytes` = 32 بايت خام (`b""` للـ genesis) · `canonical_event_bytes` = canonical JSON لـ `LicenseClockEventV1` بقواعد §2 · يُخزن **64 حرف hex lowercase**.
- **🔓 LOCK-1b — حدود أصالة الهاش (ادعاء مضبوط):** الـ trigger يضمن **البنية فقط** (الترتيب والربط والحصانة) — لا يمكنه التحقق من HMAC (المفتاح خارج DB بالتصميم). **الأصالة تُفرض بإعادة الحساب عند boot/activation:** صف مزوّر بـ entry_hash وهمي (بربط صحيح) **يُكتشف حتمًا** ⇒ tamper ⇒ مسار ملف جديد — ولا يمكن صنع سلسلة **تتحدث** إلا بمفتاح الـ keystore. **حدود الـ INSERT صريحة:** `REVOKE INSERT ON license_clock_events FROM app_user` (بجانب UPDATE/DELETE/TRUNCATE — `app_user` NOLOGIN في `000600`)؛ INSERT مسموح فقط لدور اتصال التطبيق عبر خدمة الترخيص (صاحبة القفل والمفتاح). من يملك INSERT فقط يستطيع **حقن تزوير يُكتشف** (DoS حتى ملف جديد) — داخل حدود DB-owner المعلنة.
- **سياسة النمو:** `boot_seen` يُلحق فقط إذا تقدم high_water **≥ 1 ساعة** (~≤8,760 صف/سنة)؛ الحوادث والتفعيلات وتغييرات الحالة دائمًا.
- قيود: `seq BIGINT PRIMARY KEY CHECK (seq > 0)`؛ trigger يمنع UPDATE/DELETE؛ **`BEFORE TRUNCATE … FOR EACH STATEMENT`** + `REVOKE UPDATE, DELETE, INSERT, TRUNCATE ON license_clock_events FROM app_user` (نمط `audit_log.sql`؛ `app_user` يُنشأ idempotent في `000600`). توثيق: الـ triggers تحمي البنية والـ REVOKE defense-in-depth (الاتصال الافتراضي `pharmaos`).
- **الرؤوس — قاعدة اتجاهية:** الرؤوس الخارجية لكشف **بتر الذيل فقط**: `external_head.seq > db_head.seq` ⇒ tamper؛ `external ≤ db` مع سلسلة داخلية سليمة من `verified_from_seq` ⇒ **stale حميد** (crash بين كتابة المصادر) ⇒ إعادة مزامنة بلا عقوبة. اختبار crash بين المصادر.
- **Re-seal:** تفعيل ملف جديد يسجل `verified_from_seq = seq الرأس الحالي` في المصادر الخارجية؛ التحقق الكامل عند boot يبدأ منه (استمرارية seq + ربط prev_hash + HMAC للصفوف ≥ منه)؛ الصفوف الأقدم تبقى للتدقيق. كسر بعد نقطة الختم = E-LIC-006 يُحل بختم جديد.
- تمثيل الهاش: 64 حرف ASCII hex lowercase بالضبط؛ genesis: `prev_hash IS NULL` و`prev_hash_bytes = b""`؛ غير ذلك `bytes.fromhex` — تحويل واحد ثابت.
- `LicenseClockEventV1` strict مغلق: `{event_type ∈ [boot_seen|rollback_detected|source_regression|activation|state_changed], observed_at (ISO-8601 UTC بلاحقة Z), high_water_utc (ISO-8601 UTC), origin ∈ [boot|activation|periodic|reconciliation], ref (string ≤64 — الحالة الجديدة أو license_id عند activation أو المصدر عند source_regression، وإلا ""), anomaly_count (int ≥ 0)}` — `seq`/`prev_hash`/`entry_hash` خارج الـ event؛ أي حقل إضافي = reject.
- المفتاح `LICENSE_CLOCK_HMAC_KEY` من OS keystore فقط عبر `ensure_clock_hmac_key()` (نمط `ensure_*` رابع في `security/keystore.py`) — **بسيمانتيك §3: توليد في الحالة العذراء فقط**. PostgreSQL لا يرى المفتاح أبدًا. تحقق كامل للسلسلة عند boot/activation. **Test vector ملتزم للمصفوفة كلها بمفاتيح TEST-ONLY.**

## 2) License Container + Issuer Key + External MAC

**الملف (UTF-8 JSON، تنسيق نقلي حر وترتيب حقول بلا معنى):**
`{"format":"pharmaos-license-v1","kid":"…","payload":{…},"signature":"<std base64 مع padding>"}`

- Top-level strict `{format, kid, payload, signature}` فقط · duplicate JSON keys = reject (`object_pairs_hook` في المُصدر والـ API) · `format == "pharmaos-license-v1"` حرفيًا case-sensitive بلا aliases · `schema_version: 1` داخل الـ payload · signature = 64-byte Ed25519 → standard Base64 مع padding · **مدخل التوقيع = canonical payload bytes فقط** (`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`) — الـ top-level لا يُcanonicalize · `MAX_LICENSE_FILE_BYTES = 64 KiB` قبل أي parsing.
- `LicensePayloadV1` (Pydantic strict): `license_id ^LIC-\d{4}-\d{4,6}$` · `hwid ^PHAR-[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$` · `customer` · `issued_at`/`valid_until` ISO-8601 UTC مع `valid_until > issued_at` · `kind ∈ {trial,subscription,emergency}` · `features` = المعرفات المعرفة في الـ schema فقط، تُخزن ولا يُبنى عليها gating في v1. أي حقل إضافي = reject.
- ملف مفتاح الإصدار — مواصفة واحدة بلا خيارات: `magic "PLKEY1" | version u8=1 | kid (≤16 ASCII) | scrypt(N=32768, r=8, p=1, dkLen=32) | salt 16B | nonce 12B | AES-256-GCM(ct+16B tag) فوق 32B private seed` — خارج الـ repo دائمًا + `export/import`. **ممنوع وجود مفتاح الإصدار على جهاز عميل أو في أي backup للجهاز.**
- **🔒 LOCK-2 — خوارزمية MAC الخارجية النهائية (حرفيًا HMAC-SHA256):**
  `ext_key = HKDF-SHA256(ikm=LICENSE_CLOCK_HMAC_KEY, salt=b"", info=b"pharmaos.clock.ext.v1", L=32)` ثم `ext_mac = HMAC-SHA256(ext_key, canonical_json_bytes(record))` — يُخزن 64 hex lowercase بجوار السجل. **فشل الـ MAC أو غيابه ⇒ tamper.** Test vector ملتزم.
- فصل تشغيلي: `license-cli export/import` = owner-side issuer key فقط؛ استرجاع أسرار الجهاز = backup keys bundle فقط. **CI: كل fixtures بمفاتيح TEST-ONLY + passphrase TEST-ONLY — يُمنع production key أو أي نسخة منه في المستودع.**

## 3) التفعيل والاسترجاع

**سجلات المصادر MAC'd** (المحتوى أعلاه). **المصادر ثلاثة: DB + مصدران خارجيان** (ProgramData، Registry).

- **قاعدة المصدر الواحد المفقود:** مصدر خارجي غائب والآخر MAC سليم ومتسق مع الـ DB ⇒ resync الغائب بلا عقوبة؛ خلاف ذلك ⇒ tamper.
- **قاعدة التهيئة المضمّقة:** مصادر خارجية فارغة = initialization **فقط إذا الـ DB أيضًا بلا سلسلة** (لا صفوف ولا verified_from_seq)؛ فارغة + سلسلة موجودة ⇒ **tamper يُحل بملف جديد** — لا كلفة على مستخدم شرعي لأن جهازًا جديدًا يحتاج ملفًا جديدًا أصلًا (HWID).
- **مقارنة التفعيل:** على **أقصى قيمة tuple بين المصادر الثلاثة** (لا مصدر واحد — وإلا crash بين الكتابات يقبل ملفًا أقدم كجديد)؛ **tuple الـ DB = آخر حدث `activation`** — **invariant مكتوب ومختبر:** حدث `activation` يضبط دائمًا `high_water_utc = issued_at` و`ref = license_id`. المقارنة **عددية `(year, int(number))`** لا نصية (`LIC-2026-99999` vs `LIC-2026-100000`). **المقارنات الخارجية تُتخطى في `key_lost`** (غير قابلة للتحقق) وتُستبدل بقاعدة آخر صف أدناه.
- **ملف جديد** (`>` على الـ tuple): تفعيل حقيقي — `high_water := issued_at` (قد تُخفض — علاج مقصود لتسميم المستقبل)، `verified_from_seq := الرأس الحالي`، **مسح tamper_flag/anomaly_count**، تحديث الـ payload، حدث `activation` بـ `ref = license_id`.
- **ملف قديم/مطابق** (`≤`): يُقبل idempotent — بلا أي مسح flags وبلا تغيير high_water/payload. إعادة تفعيل نفس الملف بعد تلاعب **لا تفرغ الكشف**.
- **رفض المؤرخ مستقبلًا:** `issued_at > effective_now + 24h` حيث **`effective_now = max(now_utc, high_water)`** — يتحمل ساعة جهاز متأخرة حين يكون high_water سليمًا (CMOS)؛ **الجهاز الجديد بلا سلسلة** يستخدم now_utc الخام، والرفض برسالة **"صحّح تاريخ الجهاز"**. الكود E-LIC-003 مع **`details.reason = "issued_at_in_future"`** (العميل لا يظنه ملفًا تالف التوقيع).
- **`key_lost`:** مفتاح غائب + سلسلة/مصادر موجودة ⇒ **لا توليد مفتاح جديد**؛ حالة `key_lost`. **إعادة التفعيل فيها تُقبل فقط لملف `issued_at ≥ high_water_utc` لآخر صف في السلسلة** (مرجع مقروء محمي بالـ triggers حتى لو تعذر التحقق من HMAC) — ملف المالك الصادر اليوم يمر، والقديم يُرفض. **مسار (أ):** أمر CLI مستقل **`backup import-keys` — يستورد `license_clock_hmac_key` وحده** (لا JWT ولا encryption_key — استيرادها على جهاز ببيانات مشفرة بمفتاح آخر يكسر قراءة الحقول) — **بلا DB ولا لمس مصادر — ويُرفض صراحة إذا كان مفتاح الساعة موجودًا في الـ keystore** (لا استبدال مفتاح سليم بمفتاح أقدم) — ثم تحقق السلسلة؛ فشل التحقق (مفتاح لا يطابق) يُبقي `key_lost`. **مسار (ب):** ملف جديد من المالك ⇒ **تدوير مفتاح + re-seal** (الصفوف الأقدم للتدقيق تحت المفتاح القديم). **الحالة النادرة** (high_water مسموم + مفتاح مفقود) = قرار مالك — runbook.
- **clock_error بسببين ورسالة واحدة:** ساعة متأخرة فعلًا (تصحيح مجاني وفوري ثم boot يرجع active) أو high_water مسموم بقفزة أمامية لحظية (ملف جديد يخفضها لـ `issued_at`). نص الواجهة في M3: **"صحّح تاريخ الجهاز — وإن كان صحيحًا فالمطلوب ملف ترخيص جديد"**.
- **Restore لا يلمس المصادر الخارجية أبدًا** ⇒ استعادة backup أقدم على نفس الجهاز ⇒ تعارض رؤوس ⇒ tamper ⇒ يُحل بملف جديد — restore نفسه ليس طريق التفاف. جهاز جديد = مصادر فارغة + بلا سلسلة = initialization.
- الحالات: `unlicensed | active (تحذير آخر 14 يوم) | grace (valid_until+30d تاريخ مطلق من الملف الموقع — إعادة التنصيب لا تؤثر) | read_only | clock_error | key_lost | error | tamper` — كل ما بعد active/grace يمنع mutations؛ الفرق في الرسالة ومسار الإصلاح.

## 4) Runtime — عمياء عن env بلا استثناء واحد

- **صفر فروع على `pharmaos_env` في كود الترخيص** — شملًا المهمة الدورية (الاستثناء محذوف؛ وغير لازم أصلًا: الاختبارات القائمة تستخدم `httpx.ASGITransport` التي لا تشغّل lifespan فلا المهمة ولا حساب boot يعملان فيها). لاختبار lifespan مستقبلًا: **معامل في الـ factory** `create_app(license_scheduler: bool = True)` — لا env.
- **الحقن بنمط `_isolated_keystore` الموجود:** fixture autouse session-scoped يعمل monkeypatch لمزوّد الحالة لترجع active — لا يلمس أي اختبار ولا يدخل env (الاعتماد على `app.state` وحده لا يكفي: اختبارات تبني `create_app()` بنفسها مثل `test_users_admin.py:192/:251`). **قيد تنفيذي ملزم:** الـ middleware يستدعي المزوّد **عبر attribute على الـ module (`runtime.get_state()`) في كل طلب** — ممنوع `from … import` أو التقاط الدالة عند بناء الـ middleware، وإلا لا يصلها الـ patch وتفشل الاختبارات كلها بـ E-LIC-001. **+ اختبار وحدة صريح يقلب ناتج `runtime.get_state()` بين active وunlicensed ويتأكد أن رد الـ gate يتبعه** — حارس patchability لا يعتمد على صدفة ترتيب الاختبارات.
- **الدورية:** asyncio task داخل `_lifespan` كل 15 دقيقة (لا Redis ولا celery — الـ beat الحالي للـ backup فقط) — **إلغاء نظيف عند shutdown + عزل استثناء كل دورة**؛ فشل الدورة → حالة `error` (لا صمت كالـ boot jobs الحالية).
- **Boot fail-closed بتمييز:** فشل بنية مؤقت (DB/keystore) → `error` = read_only عمليًا + إعادة محاولة تلقائية (backoff 60s) — **ليست E-LIC-006** (لا تهمة تلاعب)؛ فشل تحقق/سلسلة/MAC → tamper؛ مفتاح غائب + سلسلة → `key_lost`. أي حالة شاذة → مقفول، لا يفتح أبدًا.
- **Middleware:** `add_middleware` يعمل LIFO — ترتيب الإضافة: `LoginRateLimitMiddleware` ثم `LicenseGateMiddleware` ثم **`SecurityHeadersMiddleware` أخيرًا = الخارجية** ⇒ التنفيذ: SecurityHeaders → LicenseGate → LoginRateLimit → app، وردود الـ gate تأخذ الـ headers تلقائيًا بلا تكرار. الـ gate **pure ASGI** (لا `BaseHTTPMiddleware`) يبني الـ envelope بنفسه (يتجاوز exception handlers)، و64 KiB يُفرض بلفّ `receive` (Content-Length + عدّ chunked أثناء القراءة). **bucket rate-limit خاص بـ `/license/activate`:** توسيع `LoginRateLimitMiddleware` (الحالي مقصور على login) بمسار ثانٍ — يقع بعد الـ gate فلا يُستهلك بالمحجوبين.
- `LicenseRuntimeState` immutable (status, valid_until, grace_until, high_water, days_left, hwid, license_id — **بلا مفاتيح إطلاقًا**)؛ بناء عند boot، استبدال بمرجع واحد عند التفعيل/الدورية (verify → persist → recalculate → atomic swap). مسار الطلب memory-only (لا DB/fs/registry/crypto) مع **`effective_now = max(now_utc, state.high_water)`** — يغلق الترجيع بين الدورات.
- **حقن الـ session factory:** دوال `licensing/` تأخذ **engine/session factory كمعامل صريح** — لا اعتماد على `DATABASE_URL` العام (`conftest.py` يثبته عند الاستيراد و`get_settings` مُخزنة `lru_cache` فلا يمكن تحويل الاتصال لاحقًا) — شرط scratch DB في M1 وreal restore في M2.

## 5) Backup/Restore + استرجاع المفاتيح — بدلالات تنفيذية محددة

keys bundle في `backup_service.py` يُضاف إليه `license_clock_hmac_key` (M2). مفتاح الإصدار لا يدخل backup أبدًا. **backups سابقة لـ P4 بلا هذا الحقل ⇒ تحقق keys.json في real restore يعامله اختياريًا** (غيابه مع سلسلة ⇒ `key_lost` بمساره). `restore-drill` الحالي = scratch DB بلا استيراد مفاتيح.

**real restore (جديد M2) — مساره الكامل:**

1. **الحصول على Backup Encryption Key أولًا** — على جهاز نظيف الـ keystore فارغ ولا يمكن فك الـ bundle أصلًا: المالك يصدّر المفتاح من أي جهاز سليم بـ `backup export-key` (الموجود، برسالته الرسمية "Store this backup key OFFLINE") ويُدخله عند الاستعادة عبر **`--backup-key-file PATH` أو إدخال stdin محمي (prompt بلا echo) — لا argv** (ظهور في process list/shell history). **بدون المفتاح: الاستعادة تنقطع نظيفة قبل أي تغيير** (لا keystore ولا DB).
2. decrypt → validate keys.json كاملًا (قبل أي كتابة) → **import secrets بدلالات transactional حقيقية** → restore DB (بلا لمس المصادر — §3) → verify chain من `verified_from_seq` → calculate state → start API.

**دلالات الـ atomic import (تعريف تنفيذي — الـ keyring لا يملك transactions):** (أ) التحقق الكامل من الـ bundle **قبل أي لمس للـ keystore**؛ (ب) التقاط snapshot لقيم الـ keystore الحالية للمفاتيح التي ستُستبدل؛ (ج) كتابة المفاتيح واحدًا واحدًا (3–4 أسرار فقط — نافذة ضيقة)؛ (د) عند أي فشل: **استعادة الـ snapshot كاملًا**؛ (هـ) إن فشلت الاستعادة نفسها: حالة ظاهرة fail-closed (`key_lost`-family) + runbook — لا نصف استعادة صامتة.

**تمييز دلالي موثق:** **real restore يستبدل الأسرار الموجودة في الـ keystore** — مسار استرجاع جهاز كامل ينفذه المالك عبر CLI — بينما `import-keys` يُرفض بمفتاح موجود لأنه أداة إصلاح نقطة. **وإن كانت الاستعادة أقدم من آخر تدوير مفتاح (مسار ب §3) فنتيجتها المتوقعة tamper ثم ملف جديد** — موافق لقاعدة الاستعادة في §3.

**النتيجة المتوقعة على جهاز نظيف:** البيانات مستعادة، الحالة tamper (مصادر خارجية فارغة + سلسلة موجودة في الـ DB المستعادة)، والتفعيل بملف جديد للبصمة الجديدة — **متسق مع §3: الجهاز الجديد يحتاج ملفًا جديدًا أصلًا لتغيّر HWID**، والاستعادة أنقذت البيانات (قراءة/تصدير/backup شغالة فورًا).

read_only: backup create/verify/export عبر CLI/Celery (خارج HTTP) مسموحة دائمًا وتُختبر؛ restore وimport-keys = عملية تشغيلية صريحة CLI-only؛ `restore-drill` يُختبر في read_only.

## 6) جرد النهايات + Allowlist + Schemas

**117 endpoint (63 GET / 54 mutation = 39 POST + 2 PUT + 9 PATCH + 4 DELETE) في 16 router + health = 118 route في `app.routes`** (مطابقة لتدقيق المراجعة على `main`). الجرد **يُولَّد برمجيًا من `app.routes`** (المولّد هو المرجع) → registry تصنيف يدوي `licensing/mutations.py` (`enforce_csrf` استدعاء inline داخل الـ handlers — مثل `users.py:93` — لا يُستنتج من الـ dependencies) + **اختبار يمشي على `app.routes` ويؤكد أن كل زوج مصنف، لا زوج غير مصنف** + الملحق في نهاية هذه الوثيقة (يُلحق في بداية M2).

فوق المنهج: `POST /pos/invoices/{id}/print` → READ_OPERATION (مسموح في read_only)؛ `GET /reports/*/export` → قراءة. **قرارات مجمدة:** `/compliance/*/drain` و`/tt-events/import` → COMPLIANCE_OUTBOUND مسموحة في read_only (حجبها يكدّس طوابير ETA/EDA)؛ `/alerts/evaluate` → صيانة داخلية مسموحة؛ `/notifications/{id}/read` و`/notifications/read-all` → حالة UI مسموحة.

**Allowlist بمسارات وأساليب صريحة:** دائمًا: `GET /api/v1/health` + `OPTIONS/HEAD *`. في الحالات المرخصة فقط (active/grace/read_only): `POST /api/v1/auth/login`، `POST /api/v1/auth/refresh`، `POST /api/v1/auth/logout` (الخروج لا يُحجب بترخيص منتهي). **unlicensed = health + OPTIONS/HEAD + license/status + license/activate فقط** (login أُزيل من unlicensed — بلا `/auth/me` هي بلا فائدة، تقليل surface)؛ ما عداه DENY (E-LIC-001). read_only: الـ mutations المقفولة DENY (E-LIC-002). قرار مقصود وموثق: login مسموح بدون ترخيص (سيناريو الدعم)، والـ UI يوجه لـ /activation قبل الدخول. **اختبار صريح: unlicensed → `GET /api/v1/auth/me` = E-LIC-001** (ليست كل auth مسموحة).

**CSRF (موثق):** `enforce_csrf` اليوم استدعاء inline لكل endpoint — **activate عام قبل أي جلسة/كوكي فلا يُطبق عليه enforce_csrf ولا يحجبه الـ gate** (له bucket الخاص)؛ اختبارات CSRF تخص الـ mutations المعتمدة.

**Schemas النهايات العامة:** `GET /api/v1/license/status` (عام) — **nullability مثبتة:** unlicensed → `{"status":"unlicensed","hwid":"PHAR-…","valid_until":null,"days_left":null,"needs_activation":true}`؛ مرخص → تُملأ القيم. بلا signature/customer/license_id — التفاصيل الكاملة في endpoint authenticated داخل settings بخاضع لـ `licensing.view`. `POST /api/v1/license/activate` (عام + bucket خاص، حد 64 KiB): نجاح → نفس الاستجابة المحدودة؛ فشل → E-LIC-003/004/005/006 حسب السبب (مع `details.reason` عند الحاجة).

## 7) أكواد الأخطاء — mapping ملزم بالحالة HTTP

`E-LIC-001 LICENSE_REQUIRED → 403` · `E-LIC-002 LICENSE_READ_ONLY → 403` · `E-LIC-003 LICENSE_INVALID_SIGNATURE → 400` (**`details.reason`: bad_signature | duplicate_keys | unknown_fields | schema_mismatch | issued_at_in_future**) · `E-LIC-004 LICENSE_DEVICE_MISMATCH → 409` · `E-LIC-005 LICENSE_EXPIRED → 409` · `E-LIC-006 LICENSE_TAMPER_DETECTED → 423` · `E-LIC-007 LICENSE_STATE_ERROR → 503` (فشل بنية مؤقت — retry منطقي) · `E-LIC-008 LICENSE_KEY_LOST → 423` (يحتاج إجراء مستخدم لا retry).

تُضاف للسجلين معًا من اليوم الأول (`apps/api/.../errors.py` + `packages/shared/src/errors.ts`)، **وفي نفس الـ commit يُضاف `E-SYS-001` لسجل Python** (مستخدم literal في main.py ومعرف في TS فقط — إغلاق السابقة). **عقد العميل:** الـ web client يتفرع على `error.code` (+ `details.reason` عند الحاجة) **لا على HTTP status** — E-LIC-001/002 كلاهما 403 مثل E-AUTH-002 لكنهما ليسا "لا صلاحية" — يظهر في اختبارات M3.

## 8) الميلستونات

### P4-M1 — نواة الترخيص

Migration `20260711002900_license_state.sql` (+ down مقترن + أمان idempotency لبوابة double-apply) بقيود §1 كاملة + `ensure_clock_hmac_key()` (توليد عذراء فقط) + موديول `pharmaos_api/licensing/` (payload/canonical/event/hwid/state/chain/runtime/activation/external-stores) — **كل الدوال تأخذ session factory صريحًا (§4)** — + `ExternalStoreProvider`: واجهة قابلة للاستبدال + تنفيذ Windows (ملف `%PROGRAMDATA%\PharmaOS\` + **HKCU**\Software\PharmaOS\Clock) + **تنفيذ POSIX fallback** يُشغّل الاختبارات كاملة على ubuntu + حراسة `sys.platform` لـ `winreg` تحت mypy strict + حزمة `tools/license-cli` (init/issue/list --expiring-soon/verify/export/import؛ presets: trial 14d, monthly, annual, emergency 7d, `--months N`) + أكواد E-LIC-00x بالسجلين (+E-SYS-001) + i18n.
**عزل اختبارات الترخيص:** **scratch DB مخصصة لوحدة اختبارات الترخيص** (تطبَّق عليها الـ migrations — الجدول يمنع DELETE/TRUNCATE حتى لـ postgres والاختبارات القائمة لا تنظف، فالتأكيدات المطلقة صحيحة: genesis، N=50 ⇒ 50 صفًا بالضبط) + **keystore معزول لكل اختبار (function-scoped) لوحدة الترخيص** بدل مشاركة `_isolated_keystore` الـ session — مصفوفة key_lost وحالة العذراء تحتاج مخازن مستقلة.
**الاختبارات:** تزامن N=50 + rollback معاملة بلا فجوة + genesis + تعديل/حذف/TRUNCATE مرفوضة + **test vectors لـ LOCK-1 (entry_hash) وLOCK-2 (ext_mac) بمفاتيح TEST-ONLY** + **حقن صف بربط صحيح وهاش مزوّر ⇒ يُكتشف عند التحقق (tamper) ولا يُقبل صامتًا** + اتجاهية الرؤوس + crash بين المصادر + MAC (تزوير/مسح/عبث سجل خارجي ⇒ tamper) + فارغ+سلسلة ⇒ tamper + مصدر واحد مفقود بالاتجاهين + مصفوفة key_lost (حذف المدخل + ملف قديم ⇒ رفض؛ ملف `issued_at ≥ high_water` آخر صف ⇒ re-seal؛ import-keys بمفتاح موجود ⇒ رفض؛ import-keys خاطئ يبقي key_lost) + رفض `issued_at > effective_now+24h` + **اختبار قلب المزوّد (active↔unlicensed يتبعه رد الـ gate)** + مصفوفة التفعيل (tuple عددي على أقصى المصادر، قديم idempotent، invariant حدث activation) + سياسة النمو + cross-vectors TEST-ONLY + مصفوفة HWID: مكوّن فارغ→MachineGuid، فشل PowerShell→MachineGuid وحدها، الكاش تحسين لا مصداق؛ الثبات عبر إعادة الويندوز توقع له مسار إعادة تفعيل لا ضمان.

### P4-M2 — فرض الترخيص في الـ API

توليد الـ inventory من `app.routes` (ملحق هذه الوثيقة) → registry يدوي + ترتيب الـ middleware §4 (**استدعاء المزوّد عبر module attribute في كل طلب**) + boot fail-closed عمياء عن env + دورية asyncio (إلغاء نظيف، عزل أخطاء → error، بلا فرع env) + معامل `license_scheduler` في الـ factory + `routers/license.py` بالـ schemas §6 + CLI `license status/activate` + **`backup import-keys` (مفتاح الساعة وحده، يُرفض بمفتاح موجود)** + audit actions `license.activated/state_changed` في السجل المغلق + صلاحية `licensing.view` (44→45، seed regen + count asserts) + **real restore §5 كاملًا: مسار backup-key للجهاز النظيف + دلالات الـ import transactional (snapshot → كتابة → rollback عند الفشل → fail-closed ظاهر) + اختبار أن restore يستبدل الأسرار، وأن استعادة أقدم من آخر تدوير ⇒ tamper ثم ملف جديد + اختبار الجهاز النظيف (فارغ خارجي + سلسلة ⇒ tamper، البيانات قابلة للقراءة/التصدير)** + مفتاح السلسلة في keys bundle.
**الاختبارات:** المصفوفة الكاملة (شاملة unlicensed→`/auth/me` = E-LIC-001 واختبار بلا حقن بإلغاء الـ patch محليًا ⇒ E-LIC-001)، CSRF على الـ mutations المعتمدة، backup/restore-drill في read_only، 64 KiB بلفّ receive، `effective_now`، invariant بلا مفاتيح، autouse fixture.
**Runbook (docs):** keystore/HKCU لكل مستخدم Windows — حساب ثانٍ ⇒ `key_lost` (التطبيق أصلًا أحادي المستخدم بمفتاح تشفير الحقول في الـ keyring — يوثق كافتراض) + الحالة النادرة (مسموم + مفتاح مفقود = قرار مالك) + `backup import-keys` خطوة بخطوة + تنبيه `backup export-key` (§0) + **فرق real restore (يستبدل الأسرار) عن import-keys (يُرفض) + إجراء الاستعادة على جهاز نظيف بالكامل**.

### P4-M3 — واجهة الويب

صفحة `/activation` خارج مجموعة `(app)` بلا جلسة (تستهلك الـ schemas المحدودة؛ رسائل clock_error ثنائية السبب وkey_lost بمساريه و`details.reason` للمؤرخ مستقبلًا) + **بوابة license في `(app)/layout.tsx` بجانب بوابة me — تغيير مقصود في session boot flow يوثق لئلا يُعامل regression** (unlicensed → /activation، grace → شريط تحذير بعدّاد الأيام، read_only → Modal حاجز غير قابل للإغلاق برسالة التجديد) + كارت "ترخيص البرنامج" في settings — **مفاتيح i18n تحت namespace `settings.software_license.*`** بلا تصادم مع `settings.license_number` (رخصة الصيدلية النظامية) — **+ M3 يبني فاحص توازن ar/en** (سكربت يفحص `dictionaries` في `i18n.ts` — بوابة CI دائمة، يستهلكه لاحقًا P5-M7) + i18n متزامنة (الحالات الثمانية).

### P4-M4 — بوابة الـ Electron (التحقق المزدوج)

`main.ts` اليوم scaffold فقط — M4 ميلستون حقيقي: `requestSingleInstanceLock` + splash → poll على **`/api/v1/health`** → جلب `/license/status` **في الـ main process نفسه** (renderer لا يُثق به أبدًا في قرار الترخيص): active/grace → الواجهة، غير ذلك → `/activation` (نفس الأصل فقفل التنقل قائم) + IPC ضيق واحد بنمط "explicit, narrow channels": `pickLicenseFile()` عبر `dialog.showOpenDialog`. `electron-builder 25.1.8` موجود كـ dependency غير مستخدم — **لا يُستخدم في P4** (التغليف خارج النطاق).

**CI إضافة مقترحة (غير مانعة):** job على `windows-latest` لاختبارات مزودات Windows (Registry/ProgramData/PowerShell) — المنطق الأساسي مغطى كاملًا على ubuntu عبر الـ POSIX fallback.

## 9) العملية

1. **مصالحة الترقيم — أول خطوة قبل أي كود أو migration (قرار المالك):** **P4 = الحماية والترخيص (P4-M1..M4)** · مسودة offline تُلحق بالـ git باسمها الجديد `docs/phase5-execution-plan-offline.md` (ميلستوناتها P5-M1..M8، هجراتها من **…003000**) · **CLAUDE.md + pharmaos.md: Phase 4 = الحماية والترخيص، Phase 5 = Offline (النطاق المعدل)، Phase 6 = Enterprise** · تحديث `docs/progress-and-roadmap.md`. كل التوثيقات في commit واحد، **يعرض على المراجعة المستقلة قبل M1**.
2. لكل ميلستون: مراجعة مستقلة **بندًا بندًا (موجود/غير موجود/تعارض/يحتاج قرار) مقابل البنية قبل أي patch** → `feat(licensing): P4-M# ...` → `docs(progress): P4-M# shipped & CI-green ...` → البوابات المحلية + CI (pytest 309+الجديد، migrations up/down/re-up + double-apply، seed freshness، i18n parity من M3، ruff آخرًا، black، mypy strict، eslint/tsc/build). **معيار القبول: كل invariant وthreat-model ومسار استرجاع في هذه الخطة له implementation واختبار يثبته — لا يكفي نجاح analyzer/tests.** اختبارات `tools/license-cli` في خطوة python بـ CI.
3. `versions.md`: لا اعتماديات جديدة — يوثق قرار إعادة استخدام `cryptography==49.0.0` و`keyring==25.7.0`.
4. المخرج: تفعيل أوفلاين كامل — أنت تولّد الترخيص وترسله واتساب، العميل يفعّل بثلاث نقرات، البيانات آمنة دائمًا، والحدود الأمنية موثقة بصدق (§0).

## الملحق (يُلحق في بداية M2)

- **ملحق أ — جرد الـ endpoints:** الجدول الكامل المولد برمجيًا من `app.routes` (`route+method | Read/Mutation | allowed in unlicensed/grace/read_only`) لكل الـ 117 endpoint + health.
- **ملحق ب — مصفوفة HWID:** جدول حالات المصادر (BaseBoard/Processor/Disk/PowerShell/MachineGuid/الكاش) والسلوك المحدد لكل حالة.
