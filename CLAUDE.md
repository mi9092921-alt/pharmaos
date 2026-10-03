# CLAUDE.md — Pharmacy Management System (PharmaOS)
## نظام إدارة الصيدلية — دليل المشروع الكامل

> هذا الملف هو **المرجع الأساسي** لكل جلسات Claude Code.
> يجب قراءته بالكامل قبل أي عملية تطوير. لا استثناءات.

**الإصدار:** 1.1.0 | **آخر تحديث:** يوليو 2026

---

## 📝 سجل التغييرات (Changelog)

### v1.1.0 — يوليو 2026
```
إصلاحات حرجة:
  ✅ البحث العربي: استبدال to_tsvector('arabic') غير الموجود في PostgreSQL
     بإعداد بحث مخصص + دالة تطبيع عربي (كان سيفشل عند أول migration)
  ✅ الفهارس: إزالة CONCURRENTLY من migrations الإنشاء (لا يعمل داخل transaction)
  ✅ الكتالوج: فصل كتالوج الأدوية (عالمي) عن مخزون الفروع (branch_inventory)
     وحل تعارض UNIQUE(barcode) مع multi-branch
  ✅ المخزون: medication_batches هي مصدر الحقيقة الوحيد للكمية
  ✅ المزامنة: توحيد على Outbox + Last-Write-Wins (حذف CRDT المتناقض)
  ✅ المراحل: إزالة علامات الاكتمال الوهمية — المشروع يبدأ من الصفر

إضافات:
  ➕ تسلسل الوحدات: علبة/شريط/قرص مع سعر وباركود لكل مستوى (أساسي لمصر)
  ➕ قسم الامتثال المصري: الإيصال الإلكتروني ETA + منظومة تتبع الدواء EDA
  ➕ الطباعة عبر ESC/POS مباشرة (الإيصالات + درج النقود)
  ➕ تعدد العملات ISO 4217 مع EGP افتراضياً
  ➕ سياسة الإصدارات: التوافق أولاً ثم الأحدث
  ➕ تصنيف الحقول المشفرة AES-256
  ➕ حماية سجل التدقيق append-only + سياسة احتفاظ
  ➕ حماية المفاتيح عبر مخزن نظام التشغيل
  ➕ سجل أكواد الأخطاء مع ترجمة حسب اللغة
  ➕ حالة الدفعة (status) لدعم الحجر التلقائي
  ➕ قسم Non-Goals
  ➕ Phase 0 (التأسيس) قبل المراحل الأربع

تعديلات:
  🔄 مصفوفة إصدارات محدثة: Next.js 16 / React 19 / Tailwind 4 / PostgreSQL 17
  🔄 توحيد مصدر الصلاحيات: الكود هو المصدر ويبذر القاعدة
  🔄 معايير الجودة: صحة offline وموثوقية الطباعة بدل اختبار حمل 100 مستخدم
  🔄 العملة الافتراضية: EGP (كانت SAR)
```

### v1.0.0 — يونيو 2026
```
الإصدار الأول.
```

---

## 📋 نظرة عامة على المشروع

**اسم المشروع:** PharmaOS
**النوع:** نظام إدارة صيدلية هجين (Hybrid: Local + Cloud)
**الهدف:** نظام إنتاجي بمستوى عالمي يعمل offline على جهاز الصيدلية مع مزامنة Cloud
**السوق الأساسي:** مصر (EGP) — بتصميم متعدد الدول والعملات

```
PharmaOS/
├── apps/
│   ├── desktop/          # Electron + Next.js (Local Device App)
│   ├── web/              # Next.js Web Dashboard (Cloud)
│   └── api/              # FastAPI Backend (Local + Cloud)
├── packages/
│   ├── ui/               # Shared UI Components
│   ├── db/               # Database schemas & migrations
│   └── shared/           # Shared types & utilities
├── supabase/             # ⚠️ لا تُعدّل مباشرة — استخدم migrations فقط
├── docs/
└── CLAUDE.md             # هذا الملف
```

---

## 📦 سياسة الإصدارات (Version Policy)

> **القاعدة الحاكمة: التوافق المتبادل أولاً، ثم الأحدث.**
> لا تُرقَّ حزمة بمعزل عن مصفوفة التوافق. الترقيات تتم كموجة واحدة مُختبَرة.

```
1. كل الإصدارات تُثبَّت بدقة (pinned) في lockfiles — لا نطاقات مفتوحة (^/~) للحزم الحرجة
2. عند بدء المشروع: تحقق من أحدث إصدار مستقر لكل حزمة، ثم تحقق من توافقها
   المتبادل، ثم ثبّت المصفوفة كاملة
3. الترقية بموجات: (dev branch → full test suite → E2E → staging → production)
4. لا ترقية major أثناء سباق تسليم مرحلة — الترقيات بين المراحل فقط
5. سجّل كل ترقية في docs/versions.md مع سبب القرار
```

### مصفوفة الإصدارات المعتمدة (تحقق منها عند الإنشاء — يوليو 2026)
```yaml
Runtime:
  Node.js: 22 LTS            # Next.js 16 يتطلب Node 20+
  Python: 3.12               # مستقر ومتوافق مع كامل سلسلة التبعيات

Frontend:
  Next.js: 16.x              # مستقر منذ أكتوبر 2025 — Turbopack افتراضي، App Router
  React: 19.x                # المطلوب لـ Next.js 16
  TypeScript: 5.x (strict)
  Tailwind CSS: 4.x
  shadcn/ui: أحدث إصدار متوافق مع Tailwind 4
  Zustand: 5.x
  TanStack Query (React Query): 5.x
  React Hook Form + Zod: أحدث مستقر
  Recharts: أحدث مستقر

Desktop:
  Electron: أحدث خط مستقر (≥33) — ثبّت الإصدار عند الإنشاء بعد التحقق
  electron-builder: متوافق مع خط Electron المُثبَّت

Backend:
  FastAPI: ≥0.115 (متوافق Pydantic v2)
  SQLAlchemy: 2.0 (Async)
  Pydantic: v2
  Celery: 5.x
  WeasyPrint: أحدث مستقر (تقارير A4/A5 فقط — ليست الإيصالات الحرارية)
  python-barcode + مكتبة DataMatrix/GS1 parser

Database:
  PostgreSQL: 17 (محلي — Docker) ⚠️ يجب مطابقة إصدار مشروع Supabase
  Supabase CLI: أحدث مستقر
  Redis: 7.x
  Migrations: SQL عبر Supabase CLI حصراً (supabase/migrations/) —
    مصدر وحيد للمخطط يُطبَّق محلياً وسحابياً.
    ⚠️ لا Alembic للمخطط — نماذج SQLAlchemy مرآة للمخطط لا مصدر له

ملاحظات توافق حرجة:
  - Next.js 16: طلبات cookies/headers أصبحت async-only، وTurbopack افتراضي
  - Electron يحزم Node خاصاً به — طابق ميزات Node المستخدمة مع نسخة Electron
  - PostgreSQL المحلي وSupabase يجب أن يكونا نفس الإصدار الرئيسي (migrations واحدة)
```

---

## 🏗️ المعمارية التقنية

### Stack القرار النهائي (لا تغيير بدون موافقة مسبقة)

```yaml
Frontend:
  - Framework: Next.js 16 (App Router) + TypeScript
  - Desktop: Electron (Wrapper حول Next.js)
  - UI Library: shadcn/ui + Tailwind CSS 4
  - State: Zustand (Global) + TanStack Query (Server State)
  - Forms: React Hook Form + Zod
  - Charts: Recharts
  - Print: ESC/POS مباشرة للإيصالات (via main process) + WeasyPrint للتقارير

Backend:
  - Framework: FastAPI (Python 3.12)
  - ORM: SQLAlchemy 2.0 (Async)
  - Validation: Pydantic v2
  - Task Queue: Celery + Redis
  - PDF Generation: WeasyPrint (تقارير A4/A5)
  - Barcode: python-barcode (1D) + GS1 DataMatrix parser (2D)

Database:
  - Local: PostgreSQL 17 (Docker) — Primary offline DB
  - Cloud: Supabase (PostgreSQL 17) — Sync & Backup
  - Cache: Redis 7
  - Search: PostgreSQL FTS بإعداد عربي مخصص + pg_trgm (انظر قسم البحث العربي)

Auth:
  - Local: JWT (RS256) — Local Postgres users table
  - Cloud Sync: Supabase JWT
  - Session: httpOnly cookies + CSRF tokens
  - ملاحظة Electron: الجلسة عبر localhost HTTP — انظر قسم الأمان لحماية المفاتيح

Sync Engine:
  - Strategy: Event Log + Outbox Pattern (PostgreSQL)
  - Conflict Resolution: Last-Write-Wins (LWW) + Manual Override للكيانات الحرجة
  - ⚠️ لا CRDT — قرار معماري نهائي: LWW أبسط وكافٍ لنمط استخدام الصيدلية
```

---

## 🗄️ قاعدة البيانات — Schema الأساسي

### تصنيف الجداول (إلزامي)

الجداول نوعان — لكل نوع قواعد أعمدة مختلفة:

```
1. جداول الكتالوج/المرجعية (Catalog) — عالمية، مشتركة بين الفروع:
   medications, medication_packaging, medication_barcodes, categories,
   units, countries, currencies, tax_profiles, roles, permissions
   → لا تحمل branch_id
   → تحمل: id, created_at/updated_at, created_by/updated_by, is_deleted, sync_version

2. الجداول التشغيلية (Operational) — مرتبطة بفرع:
   branch_inventory, medication_batches, pack_serials, stock_movements,
   invoices, invoice_items, returns, payments, cash_sessions, expenses,
   purchase_orders, prescriptions, customers*, suppliers*, audit_logs, sync_queue
   → branch_id NOT NULL REFERENCES branches(id)
   → + كل الأعمدة الإلزامية

   (*) customers وsuppliers قد يكونان مشتركين بين الفروع حسب إعداد النظام —
       branch_id فيهما nullable مع scope واضح في الاستعلامات
```

### الأعمدة الإلزامية (كل الجداول)
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
is_deleted      BOOLEAN DEFAULT FALSE,          -- Soft Delete فقط
sync_version    BIGINT DEFAULT 0,               -- للمزامنة مع Cloud
created_at      TIMESTAMPTZ DEFAULT NOW(),
updated_at      TIMESTAMPTZ DEFAULT NOW(),
created_by      UUID REFERENCES users(id),
updated_by      UUID REFERENCES users(id)
-- + branch_id UUID NOT NULL للجداول التشغيلية
```

### الكتالوج: الدواء + التعبئة + الباركود (تصميم v1.1)

```sql
-- ✅ كتالوج الأدوية — عالمي (بلا branch_id، بلا كمية مخزون)
CREATE TABLE medications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_name          VARCHAR(255) NOT NULL,
    trade_name_ar       VARCHAR(255),            -- الاسم العربي (للبحث والعرض)
    scientific_name     VARCHAR(255),            -- المادة الفعالة
    manufacturer        VARCHAR(255),
    category_id         UUID REFERENCES categories(id),
    drug_class          VARCHAR(100),
    route               VARCHAR(50),             -- oral/topical/injection...

    -- Pharmacy specific
    requires_prescription BOOLEAN DEFAULT FALSE,
    controlled_substance  BOOLEAN DEFAULT FALSE,
    storage_conditions    VARCHAR(100),

    -- Regulatory (مصر)
    eda_registration_no VARCHAR(50),             -- رقم تسجيل هيئة الدواء
    gtin                VARCHAR(14),             -- GS1 GTIN (لتتبع الدواء)

    -- بحث عربي (عمود مُولَّد — انظر قسم البحث العربي)
    search_vector       TSVECTOR,

    is_active       BOOLEAN DEFAULT TRUE,
    is_deleted      BOOLEAN DEFAULT FALSE,
    sync_version    BIGINT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);

-- ✅ تسلسل الوحدات: علبة → شريط → قرص (أساسي للسوق المصري)
-- الصيدلية المصرية تبيع بالشريط والقرص، لا بالعلبة فقط
CREATE TABLE medication_packaging (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    level           SMALLINT NOT NULL,           -- 1=علبة, 2=شريط, 3=قرص/وحدة
    unit_id         UUID NOT NULL REFERENCES units(id),
    name_ar         VARCHAR(50) NOT NULL,        -- "علبة" / "شريط" / "قرص"
    qty_in_parent   DECIMAL(10,3),               -- كم وحدة من هذا المستوى داخل المستوى الأعلى
                                                 -- (علبة=NULL، شريط: 3 شرائط/علبة، قرص: 10 أقراص/شريط)
    is_sellable     BOOLEAN DEFAULT TRUE,        -- هل يُباع هذا المستوى منفرداً؟
    selling_price   DECIMAL(12,2) NOT NULL,      -- سعر البيع لهذا المستوى
    is_default_sale BOOLEAN DEFAULT FALSE,       -- المستوى الافتراضي في POS
    -- + الأعمدة الإلزامية
    UNIQUE (medication_id, level)
);

-- ✅ الباركود — متعدد لكل دواء (باركود لكل مستوى تعبئة + باركودات بديلة)
CREATE TABLE medication_barcodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    packaging_id    UUID REFERENCES medication_packaging(id),
    barcode         VARCHAR(64) NOT NULL,
    barcode_type    VARCHAR(20) DEFAULT 'EAN13', -- EAN13 | GS1_DATAMATRIX | CODE128
    is_primary      BOOLEAN DEFAULT FALSE,
    -- + الأعمدة الإلزامية
    UNIQUE (barcode)          -- ✅ آمن الآن: الكتالوج عالمي واحد، لا تكرار بين فروع
);
```

### المخزون: الفرع + الدفعات + التسلسل (تصميم v1.1)

```sql
-- ✅ مصدر الحقيقة الوحيد للكمية: medication_batches
-- كل استلام مشتريات يُنشئ دفعة. كل بيع/مرتجع/تسوية يحرك دفعة.
CREATE TABLE medication_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    batch_number    VARCHAR(50) NOT NULL,        -- رقم التشغيلة
    expiry_date     DATE NOT NULL,
    quantity        DECIMAL(12,3) NOT NULL DEFAULT 0,  -- بالوحدة الأصغر (قرص/وحدة)
    purchase_price  DECIMAL(12,2) NOT NULL,      -- سعر شراء الوحدة الأصغر
    supplier_id     UUID REFERENCES suppliers(id),
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
                    -- active | quarantined | expired | recalled | depleted
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- + الأعمدة الإلزامية
    CONSTRAINT chk_batch_qty CHECK (quantity >= 0)
);

-- ✅ ملخص مخزون الفرع — cache مشتق قابل لإعادة البناء (ليس مصدر حقيقة)
-- يُحدَّث عبر service layer داخل نفس الـ transaction مع حركة الدفعة
-- Invariant: branch_inventory.cached_quantity = SUM(batches.quantity WHERE active)
-- أمر إعادة بناء دوري + عند بدء التشغيل للتحقق من التطابق
CREATE TABLE branch_inventory (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    cached_quantity DECIMAL(12,3) NOT NULL DEFAULT 0,   -- بالوحدة الأصغر
    min_stock_level DECIMAL(12,3) DEFAULT 0,
    max_stock_level DECIMAL(12,3),
    reorder_point   DECIMAL(12,3),
    shelf_location  VARCHAR(50),                 -- موقع الرف
    -- + الأعمدة الإلزامية
    UNIQUE (branch_id, medication_id)
);

-- ✅ تسلسل العبوات — منظومة تتبع الدواء المصرية (قرارا 161 و475 لسنة 2025)
CREATE TABLE pack_serials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    batch_id        UUID NOT NULL REFERENCES medication_batches(id),
    serial_number   VARCHAR(64) NOT NULL,        -- الرقم التسلسلي العشوائي من 2D code
    gtin            VARCHAR(14) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'in_stock',
                    -- in_stock | dispensed | returned | quarantined | reported_destroyed
    dispensed_invoice_id UUID REFERENCES invoices(id),
    tt_report_status VARCHAR(20) DEFAULT 'pending',  -- pending | reported | failed
    -- + الأعمدة الإلزامية
    UNIQUE (gtin, serial_number)
);

-- ✅ حركات المخزون — audit trail لكل تغيير كمية (append-only)
CREATE TABLE stock_movements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    batch_id        UUID NOT NULL REFERENCES medication_batches(id),
    movement_type   VARCHAR(30) NOT NULL,
                    -- purchase_in | sale_out | return_in | return_out
                    -- adjustment | quarantine | expiry_writeoff | transfer_in | transfer_out
    quantity_delta  DECIMAL(12,3) NOT NULL,      -- موجب أو سالب، بالوحدة الأصغر
    reference_type  VARCHAR(30),                 -- invoice | purchase_order | return | manual
    reference_id    UUID,
    reason          TEXT,
    -- + الأعمدة الإلزامية
);
```

### قواعد المخزون الإلزامية
```
✅ الكمية الحقيقية = مجموع كميات الدفعات النشطة (batches هي مصدر الحقيقة)
✅ branch_inventory.cached_quantity مشتق — يُحدَّث في نفس transaction الحركة
   ويُعاد بناؤه دورياً وعند الإقلاع (drift check)
✅ كل تغيير كمية يمر عبر stock_movements (لا UPDATE مباشر للكمية أبداً)
✅ الصرف من الدفعات: FEFO — الأقرب انتهاءً يُصرف أولاً
✅ كل الكميات تُخزَّن بالوحدة الأصغر (قرص/وحدة) — التحويل عبر medication_packaging
✅ بيع شريط = خصم qty_in_parent قرصاً من الدفعة عبر التحويل
❌ لا كمية مخزنة على جدول medications إطلاقاً
```

### الجداول الأساسية المطلوبة
```
✅ Core:
  - branches, users, roles, permissions, role_permissions
  - countries, currencies, tax_profiles         (تعدد الدول والعملات)

✅ Catalog:
  - categories, units
  - medications, medication_packaging, medication_barcodes

✅ Inventory (تشغيلية):
  - branch_inventory, medication_batches, pack_serials, stock_movements
  - suppliers, purchase_orders, purchase_items

✅ Sales:
  - customers, prescriptions
  - invoices, invoice_items (تربط بـ batch_id للتتبع وFEFO)
  - returns, return_items, payments

✅ Financial:
  - cash_sessions, expenses, expense_categories

✅ Compliance (مصر):
  - ereceipt_queue        (طابور إرسال الإيصال الإلكتروني ETA)
  - tt_events             (أحداث منظومة تتبع الدواء: استلام/صرف/إتلاف)

✅ System:
  - audit_logs, sync_queue, settings, notifications
```

---

## 🔍 البحث العربي (Arabic Full-Text Search)

> ⚠️ **PostgreSQL لا يملك إعداد بحث نصي اسمه `arabic`** —
> `to_tsvector('arabic', ...)` يفشل فوراً. الحل أدناه إلزامي.

```sql
-- 1) إعداد بحث مخصص مبني على simple (بلا stemming — العربية غير مدعومة أصلاً)
CREATE TEXT SEARCH CONFIGURATION arabic_simple (COPY = simple);

-- 2) دالة تطبيع عربي IMMUTABLE (شرط لاستخدامها في فهارس وأعمدة مولدة)
CREATE OR REPLACE FUNCTION normalize_arabic(input TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
  SELECT translate(
    regexp_replace(
      regexp_replace(coalesce(input, ''), '[ً-ْٰ]', '', 'g'), -- التشكيل
      'ـ', '', 'g'),                                                     -- التطويل ـ
    'أإآٱىة', 'اااايه'                                                        -- توحيد الألف/الياء/التاء
  );
$$;

-- 3) عمود tsvector مُولَّد على medications
ALTER TABLE medications ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
  GENERATED ALWAYS AS (
    to_tsvector('arabic_simple',
      normalize_arabic(coalesce(trade_name,'') || ' ' ||
                       coalesce(trade_name_ar,'') || ' ' ||
                       coalesce(scientific_name,''))
    )
  ) STORED;

-- 4) الفهارس: FTS + trigram للبحث الجزئي/الأخطاء الإملائية
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_medications_fts ON medications USING GIN(search_vector);
CREATE INDEX idx_medications_trgm ON medications
  USING GIN (normalize_arabic(trade_name_ar) gin_trgm_ops);
```

```
قواعد البحث:
✅ استعلام المستخدم يمر بنفس normalize_arabic قبل المطابقة
✅ بحث بالباركود = مطابقة تامة على medication_barcodes.barcode (أسرع مسار)
✅ بحث بالاسم = FTS أولاً ثم trigram fallback للنتائج الأقل من 3
✅ الهدف: نتائج < 100ms
```

---

## 🔐 نظام الصلاحيات (RBAC)

### مصدر الحقيقة الوحيد (v1.1)
```
✅ مصفوفة الصلاحيات تُعرَّف في الكود (packages/shared/permissions.ts)
✅ جداول roles/permissions في القاعدة تُبذَر (seed) من الكود عند كل migration
✅ ممنوع تعديل الصلاحيات المدمجة يدوياً في القاعدة — الكود يفوز دائماً
✅ الأدوار المخصصة (custom roles) تُنشأ في القاعدة فوق الصلاحيات المُعرَّفة بالكود
```

### الأدوار المدمجة (Built-in Roles)
```typescript
enum SystemRole {
  SUPER_ADMIN = 'super_admin',       // مالك النظام — كل الصلاحيات
  BRANCH_MANAGER = 'branch_manager', // مدير الفرع
  PHARMACIST = 'pharmacist',         // صيدلاني
  CASHIER = 'cashier',               // كاشير
  DATA_ENTRY = 'data_entry',         // مدخل بيانات
  VIEWER = 'viewer',                 // مشاهدة فقط
}
```

### مصفوفة الصلاحيات التفصيلية
```typescript
const PERMISSIONS = {
  // =================== المخزون ===================
  'inventory.view':          [ALL_ROLES],
  'inventory.add':           ['super_admin', 'branch_manager', 'pharmacist', 'data_entry'],
  'inventory.edit':          ['super_admin', 'branch_manager', 'pharmacist'],
  'inventory.delete':        ['super_admin', 'branch_manager'],
  'inventory.adjust':        ['super_admin', 'branch_manager', 'pharmacist'],
  'inventory.purchase':      ['super_admin', 'branch_manager'],

  // =================== المبيعات ===================
  'sales.view':              [ALL_ROLES],
  'sales.create':            ['super_admin', 'branch_manager', 'pharmacist', 'cashier'],
  'sales.cancel':            ['super_admin', 'branch_manager'],
  'sales.discount':          ['super_admin', 'branch_manager', 'pharmacist'],
  'sales.return':            ['super_admin', 'branch_manager', 'pharmacist'],
  'sales.override_price':    ['super_admin', 'branch_manager'],

  // =================== العملاء ===================
  'customers.view':          [ALL_ROLES],
  'customers.create':        ['super_admin', 'branch_manager', 'pharmacist', 'cashier'],
  'customers.edit':          ['super_admin', 'branch_manager', 'pharmacist'],
  'customers.delete':        ['super_admin'],

  // =================== التقارير ===================
  'reports.sales':           ['super_admin', 'branch_manager'],
  'reports.inventory':       ['super_admin', 'branch_manager', 'pharmacist'],
  'reports.financial':       ['super_admin', 'branch_manager'],
  'reports.audit':           ['super_admin'],
  'reports.export':          ['super_admin', 'branch_manager'],

  // =================== الإعدادات ===================
  'settings.view':           ['super_admin', 'branch_manager'],
  'settings.edit':           ['super_admin'],
  'settings.users':          ['super_admin'],
  'settings.backup':         ['super_admin'],

  // =================== الكاشير ===================
  'cashier.open_session':    ['super_admin', 'branch_manager', 'cashier'],
  'cashier.close_session':   ['super_admin', 'branch_manager'],
  'cashier.view_cash':       ['super_admin', 'branch_manager'],

  // =================== المشتريات ===================
  'purchases.view':          ['super_admin', 'branch_manager', 'pharmacist'],
  'purchases.create':        ['super_admin', 'branch_manager'],
  'purchases.approve':       ['super_admin', 'branch_manager'],
  'purchases.receive':       ['super_admin', 'branch_manager', 'pharmacist'],

  // =================== المالية ===================
  'finance.expenses':        ['super_admin', 'branch_manager'],
  'finance.reports':         ['super_admin'],

  // =================== الامتثال (v1.1) ===================
  'compliance.ereceipt':     ['super_admin', 'branch_manager'],
  'compliance.tt_report':    ['super_admin', 'branch_manager', 'pharmacist'],
} as const;
```

### تطبيق الصلاحيات — قواعد إلزامية
```python
# ✅ صح — التحقق في كل API endpoint
@router.get("/medications")
async def get_medications(
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_permission("inventory.view"))
):
    ...
```
```typescript
// ✅ صح — Guard في Frontend
const SalesPage = () => {
  const { hasPermission } = useAuth();
  if (!hasPermission('sales.create')) return <Unauthorized />;
  return <SalesForm />;
};

// ❌ خطأ — لا تثق بالـ Frontend وحده
// لا تحذف backend permission checks بحجة وجود frontend guards
```

---

## 🔄 نظام المزامنة (Sync Engine)

### استراتيجية Offline-First (v1.1 — بلا CRDT)
```
الأولوية: Local Database أولاً دائماً

1. كل العمليات تُحفظ locally فوراً (داخل transaction واحدة مع outbox entry)
2. تُضاف إلى sync_queue (نمط Outbox) مع timestamp
3. عند توفر الإنترنت: background worker يدفع التغييرات للسحابة
4. Conflict Resolution: Last-Write-Wins (حسب updated_at + sync_version)
   + Manual Override queue للكيانات الحرجة:
     (invoices, payments, cash_sessions, controlled substances, pack_serials)
     → تعارضها لا يُحسم تلقائياً بل يدخل قائمة مراجعة يدوية
5. المستخدم يرى مؤشر sync status في كل وقت

⚠️ قرار معماري نهائي: لا CRDT.
   نمط الصيدلية = جهاز رئيسي واحد لكل فرع + dashboard سحابي للقراءة غالباً.
   LWW + مراجعة يدوية للحالات الحرجة كافيان وأبسط بكثير صيانةً لمطوّر واحد.
```

```typescript
// Sync Queue Entry Model
interface SyncEntry {
  id: string;
  operation: 'INSERT' | 'UPDATE' | 'DELETE';   // DELETE = soft delete دائماً
  table_name: string;
  record_id: string;
  payload: Record<string, unknown>;
  branch_id: string;
  created_at: Date;
  sync_status: 'pending' | 'syncing' | 'synced' | 'failed' | 'conflict';
  retry_count: number;
  error_message?: string;
}
```

### قواعد Sync إلزامية
```
✅ كل record له sync_version (monotonic counter يُزاد في كل UPDATE عبر trigger)
✅ Soft Delete فقط — لا حذف فعلي من local DB أبداً
✅ الـ sync_queue يحتوي على كل التغييرات (event log)
✅ Idempotent operations — نفس العملية مرتين = نفس النتيجة (upsert بـ record_id + sync_version)
✅ Sync يعمل في background worker — لا يوقف الـ UI
✅ إشعار فوري للمستخدم عند فشل الـ sync مع سبب واضح
✅ نسخ سحابي أحادي الاتجاه (one-way encrypted backup) يعمل من Phase 0 —
   قبل اكتمال المزامنة الكاملة ثنائية الاتجاه (حماية من فقد/سرقة الجهاز)
❌ لا تحذف من sync_queue قبل تأكيد Cloud receipt
❌ لا تفترض أن الـ cloud دائماً متاح
❌ لا تُعدّل sync_version يدوياً
```

---

## ⚡ معايير الأداء (Performance Standards)

### أهداف قياس الأداء (لا تفاوض)
```
⚡ Time to Interactive (TTI):          < 2 ثانية
⚡ API Response Time (P95):            < 200ms (local) / < 500ms (cloud)
⚡ Search Results:                      < 100ms
⚡ Barcode Scan to Product Display:    < 50ms
⚡ Invoice Print Generation:           < 1 ثانية
⚡ Report Generation (daily):          < 3 ثوانٍ
⚡ Database Queries:                   < 50ms (مع index)
⚡ App Startup Time:                   < 3 ثوانٍ
```

### قواعد الفهارس (v1.1)
```sql
-- ✅ في migrations الإنشاء: CREATE INDEX عادي (الجداول فارغة — لا حاجة لـ CONCURRENTLY)
-- ⚠️ CONCURRENTLY لا يعمل داخل transaction — وmigrations تعمل داخل transactions
CREATE INDEX idx_barcodes_barcode      ON medication_barcodes(barcode);
CREATE INDEX idx_batches_branch_med    ON medication_batches(branch_id, medication_id)
    WHERE NOT is_deleted AND status = 'active';
CREATE INDEX idx_batches_expiry        ON medication_batches(branch_id, expiry_date)
    WHERE status = 'active';
CREATE INDEX idx_inventory_branch      ON branch_inventory(branch_id, medication_id);
CREATE INDEX idx_invoices_date         ON invoices(created_at DESC, branch_id);
CREATE INDEX idx_invoice_items_invoice ON invoice_items(invoice_id);
CREATE INDEX idx_serials_lookup        ON pack_serials(gtin, serial_number);
CREATE INDEX idx_sync_queue_status     ON sync_queue(sync_status, created_at)
    WHERE sync_status = 'pending';

-- ⚠️ على جدول إنتاجي حي (بيانات موجودة + استخدام فعلي):
-- CREATE INDEX CONCURRENTLY خارج transaction فقط (migration بعلم صريح
-- transaction=off أو تنفيذ يدوي مجدول)
```

```python
# ✅ Query optimization rules
# 1. دائماً استخدم selectinload / joinedload لتجنب N+1
# 2. Pagination إلزامي لكل list endpoint (max 100 per page)
# 3. استخدم EXPLAIN ANALYZE لأي query تأخذ > 50ms

# ❌ ممنوع
result = db.query(Medication).all()  # No pagination!

# ✅ صح
stmt = (
    select(Medication)
    .where(Medication.is_deleted == False)
    .options(selectinload(Medication.packaging))
    .offset(skip).limit(min(limit, 100))
)
```

---

## 🔒 معايير الأمان (Security Standards)

### Authentication & Authorization
```python
# JWT Settings (إلزامية)
JWT_ALGORITHM = "RS256"           # Asymmetric — يسمح للسحابة/خدمات أخرى بالتحقق
                                  # دون امتلاك مفتاح التوقيع
ACCESS_TOKEN_EXPIRE = 15          # 15 دقيقة فقط
REFRESH_TOKEN_EXPIRE = 7 * 24     # 7 أيام (ساعات)
TOKEN_VERSION_ENABLED = True      # لإلغاء جلسات محددة

# Password Policy
MIN_PASSWORD_LENGTH = 8
REQUIRE_UPPERCASE = True
REQUIRE_NUMBER = True
REQUIRE_SPECIAL = True
MAX_LOGIN_ATTEMPTS = 5            # بعدها: lock لـ 15 دقيقة
PASSWORD_HASH = "argon2id"        # أو bcrypt — argon2id مفضل
```

### حماية المفاتيح على جهاز الصيدلية (v1.1 — إلزامي)
```
⚠️ ملفات .env نصية مقروءة لأي شخص يصل للجهاز — لا تكفي وحدها للمفاتيح الحرجة

✅ مفتاح JWT الخاص + ENCRYPTION_KEY يُخزَّنان عبر مخزن نظام التشغيل:
   - Electron safeStorage API (يستخدم DPAPI على Windows / Keychain على macOS)
   - أو Windows DPAPI مباشرة من جانب Python (keyring)
✅ .env يحمل فقط: مسارات، أسماء قواعد، إعدادات غير سرية، ومراجع للمفاتيح
✅ عند أول تشغيل: توليد المفاتيح وتخزينها في المخزن الآمن تلقائياً
✅ نسخة طوارئ من المفاتيح ضمن النسخ الاحتياطي المشفّر (وإلا تستحيل الاستعادة)
❌ لا مفاتيح خاصة في git أو في .env بنص صريح على أجهزة الإنتاج
```

### تصنيف الحقول المشفرة AES-256 (v1.1 — محدد وقابل للتنفيذ)
```
⚠️ "شفّر كل البيانات الحساسة" غير قابل للتنفيذ — التشفير يكسر الفهرسة والبحث.
التصنيف الإلزامي:

مشفّر (AES-256-GCM على مستوى الحقل):
  - customers.national_id          (الرقم القومي)
  - customers.insurance_number     (رقم التأمين)
  - prescriptions.notes            (ملاحظات طبية حساسة)
  - users.phone                    (اختياري حسب سياسة الخصوصية)
  - إعدادات الاعتماد: ETA client_secret، شهادة الختم، مفاتيح API خارجية

غير مشفّر (لأنه مطلوب للبحث/الفهرسة/التقارير):
  - أسماء الأدوية والأسعار والكميات وأرقام الفواتير
  - أسماء العملاء (بحث) وأرقام هواتفهم إن كانت مفتاح بحث أساسي في الصيدلية

قواعد:
✅ التشفير في service layer قبل الكتابة — والفك عند القراءة المصرّح بها فقط
✅ القرص كاملاً: شجّع تفعيل BitLocker على أجهزة الإنتاج (دفاع طبقة ثانية)
✅ النسخ الاحتياطية مشفّرة دائماً بمفتاح مستقل
```

```typescript
// API Security Rules
const securityHeaders = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains', // cloud فقط
  'Content-Security-Policy': "default-src 'self'", // مع nonce لسكربتات Next
};

// Rate Limiting
const rateLimits = {
  login: '5 per minute',
  api: '200 per minute per user',
  reports: '10 per minute',
  export: '3 per minute',
};
```

### Data Security Rules (إلزامية)
```
✅ Audit Log لكل عملية: من فعل ماذا ومتى (append-only — انظر أدناه)
✅ Soft Delete فقط — مع audit trail
✅ SQL Injection: استخدم parameterized queries دائماً
✅ HTTPS فقط للـ Cloud — محلياً: localhost only (الـ API لا يستمع إلا على 127.0.0.1)
✅ Database Backup: تلقائي يومي + تشفير + نسخة سحابية أحادية الاتجاه
✅ Session Invalidation عند تغيير كلمة المرور
✅ حماية Controlled Substances: سجل منفصل + إشعارات + لا حذف نهائياً
❌ لا تخزن passwords بدون argon2id/bcrypt
❌ لا تُعيد sensitive data في error messages
❌ لا تُعيد stack traces في production
```

### Audit Log (v1.1 — append-only + احتفاظ)
```python
# كل عملية حرجة يجب أن تُسجل
AUDITED_OPERATIONS = [
    # المبيعات
    'invoice.created', 'invoice.cancelled', 'invoice.discount_applied',
    'invoice.price_overridden', 'return.created',
    # المخزون
    'stock.adjusted', 'medication.deleted', 'batch.expired_dispensed',
    'batch.quarantined', 'controlled_substance.dispensed',
    # الامتثال
    'ereceipt.submitted', 'ereceipt.failed', 'tt_event.reported',
    # الإعدادات
    'user.created', 'user.role_changed', 'user.deactivated',
    'settings.changed', 'backup.created', 'backup.restored',
    # الكاشير
    'cash_session.opened', 'cash_session.closed', 'cash_session.discrepancy',
    # المزامنة
    'sync.conflict_resolved', 'sync.failed',
]
```

```sql
-- ✅ حماية append-only على مستوى القاعدة (لا يكفي الاتفاق البرمجي)
REVOKE UPDATE, DELETE ON audit_logs FROM app_user;
CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_immutable
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
```

```
سياسة الاحتفاظ:
✅ audit_logs العامة: 5 سنوات على الأقل
✅ سجلات Controlled Substances وtt_events: بلا حذف إطلاقاً (أرشفة فقط)
✅ الأرشفة إلى جداول أرشيف/ملفات مشفرة — ليست حذفاً
```

---

## 💱 تعدد الدول والعملات (Multi-Country & Currency)

> **القرار:** النظام متعدد العملات بمعيار ISO 4217. أي عملة عالمية مدعومة تصميمياً.
> **الافتراضي الحالي: مصر — EGP.** التوسع لاحقاً (SAR، AED، ...) إعداد لا إعادة بناء.

```sql
CREATE TABLE currencies (
    code            CHAR(3) PRIMARY KEY,      -- ISO 4217: EGP, SAR, AED, USD...
    name_ar         VARCHAR(50) NOT NULL,
    symbol          VARCHAR(8) NOT NULL,      -- ج.م / ر.س / د.إ
    decimal_places  SMALLINT NOT NULL DEFAULT 2
);

CREATE TABLE countries (
    code            CHAR(2) PRIMARY KEY,      -- ISO 3166-1: EG, SA, AE...
    name_ar         VARCHAR(80) NOT NULL,
    default_currency CHAR(3) NOT NULL REFERENCES currencies(code),
    tax_profile_id  UUID REFERENCES tax_profiles(id),
    calendar        VARCHAR(10) DEFAULT 'gregory' -- gregory | islamic
);

CREATE TABLE tax_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(80) NOT NULL,      -- "VAT مصر 14%" مثالاً — القيمة إعداد لا ثابت
    vat_rate        DECIMAL(5,2) NOT NULL,
    medicine_vat_rate DECIMAL(5,2),            -- الدواء قد يُعفى أو يُخفض حسب الدولة
    einvoice_system VARCHAR(20)                -- 'eta_ereceipt' | 'zatca' | NULL
);
```

```
قواعد:
✅ كل mounts المالية DECIMAL(12,2) — لا FLOAT أبداً
✅ كل فاتورة تحمل currency_code + قيم الضريبة وقت الإصدار (snapshot لا مرجع حي)
✅ الفرع يرث country/currency من إعداده — قابل للتهيئة لكل فرع
✅ الأسعار في مصر مقرَّرة حكومياً — التسعير يتبع تحديثات قاعدة الأدوية، مع
   سجل تاريخ أسعار (price history) لكل تغيير
```

```typescript
// Number Formatting — العملة من إعداد الفرع (EGP افتراضياً)
const formatCurrency = (amount: number, currency = 'EGP', locale = 'ar-EG') =>
  new Intl.NumberFormat(locale, {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
```

---

## 🇪🇬 الامتثال التنظيمي المصري (Egypt Compliance)

> مبني على بحث موثّق (يوليو 2026). المصادر الرسمية: eta.gov.eg و edaegypt.gov.eg.
> **راجع هذه المتطلبات كل ربع سنة — القرارات تتغير.**

### أ) الإيصال الإلكتروني — ETA e-Receipt (B2C)
```
الوضع القانوني:
- منظومة B2C منفصلة عن الفاتورة الإلكترونية B2B
- الإلزام بقوائم ممولين اسمية عبر قرارات (القطاع الصحي من المرحلة 4 — أبريل 2023،
  والمراحل مستمرة: المرحلة 8 في يوليو/سبتمبر 2025)
- ✅ إجراء أول إلزامي: التحقق من حالة الصيدلية عبر
  eta.gov.eg/ar/ereceipt-inquiry برقم التسجيل الضريبي

التكامل التقني (وحدة ereceipt في Phase 2):
1. تسجيل الملف الرقمي للممول + تسجيل أجهزة POS → client_id/client_secret لكل جهاز
2. شهادة ختم إلكتروني X.509 (من مقدم خدمة معتمد) لتوقيع الإيصالات
3. OAuth2 client credentials: POST /connect/token → Bearer access token
4. بناء الإيصال JSON: UUID، رقم التسجيل، بيانات الأصناف بأكواد GS1/EGS،
   الكميات، الأسعار، ض.ق.م، الإجمالي
5. الإرسال: POST /api/v1/receiptsubmissions (TLS 1.2+)
6. طباعة QR على الإيصال للتحقق
7. طابور offline (ereceipt_queue): الإيصالات تُبنى وتُوقَّع محلياً وتُرسَل
   عند توفر الاتصال — البيع لا يتوقف على الإنترنت أبداً
8. SDK وIntegration Toolkit: sdk.invoicing.eta.gov.eg | دعم: 16395

⚠️ مهام ورقية على مالك الصيدلية (ليست مهام تطوير — تُبدأ مبكراً بالتوازي):
   التسجيل الضريبي الرقمي، استخراج شهادة الختم، تسجيل أجهزة POS
```

### ب) منظومة تتبع الدواء — EDA Track & Trace 🔴
```
الأساس القانوني:
- قرار 161/2025: ترميز موحد إلزامي — باركود 2D (GS1 DataMatrix) على كل عبوة:
  معرف المنتج (GTIN) + تاريخ الانتهاء + رقم التشغيلة + رقم تسلسلي عشوائي
- قرار 475/2025: المنظومة القومية الموحدة للتتبع — كل عبوة تُتتبع من الإنتاج
  حتى الصرف للمريض. الصيدليات ضمن المنشآت الملزمة (تسجيل + بنية تقنية:
  ماسحات + برمجيات متصلة بالمنظومة)
- قرار 804/2025: الدليل التنظيمي — يُعرّف "المنتج غير الممتثل" (كود غير سليم/
  تسلسل مكرر/حدث غير مسجل) ويجيز وقف تداوله وحجزه وإتلافه

المواعيد:
- 1 فبراير 2026: المستورد تام الصنع (سارٍ بالفعل)
- 1 أغسطس 2026: المحلي والمعبأ محلياً (وشيك)
- المنتجات المتداولة قبل الموعد تستمر حتى نفادها/انتهائها (نافذة انتقالية)

الأثر على PharmaOS:
✅ ماسح 2D (DataMatrix) مدعوم من Phase 1 — شراء ماسحات 2D حصراً
✅ pack_serials + tt_events في المخطط من البداية
✅ الاستلام: مسح كل عبوة → التقاط GTIN/التشغيلة/الانتهاء/التسلسلي
✅ الصرف: ربط التسلسلي بالفاتورة + إبلاغ حدث الصرف
✅ وحدة التكامل القومي في Phase 2 (طابور tt_events بنمط outbox نفسه)

⚠️ الامتثال المؤقت (قبل جاهزية PharmaOS): البحث العام لا يذكر تطبيقاً رسمياً
   للصيدليات — يجب التحقق المباشر من EDA/نقابة الصيادلة عن قناة الإبلاغ
   المعتمدة للصيدليات (بوابة/تطبيق/مزود معتمد) واستخدامها مؤقتاً.
   ملاحظة مطمئنة: مخزون الصيدلية المشترى قبل المواعيد يتداول حتى نفاده.
```

### ج) مصدر قاعدة الأدوية المصرية
```
- لا يوجد API رسمي من هيئة الدواء (EDA) — أداة بحث ويب فقط (EDDB)
- البذر الأولي: مجموعة بيانات مفتوحة CC0 (24,868 صنفاً بالأسماء العربية
  والإنجليزية والمادة الفعالة والسعر EGP) — github.com/karem505/egyptian-drug-database
  ⚠️ بلا باركود — الباركود يُستكمل من مزود تجاري أو بالمسح التراكمي عند الاستلام
- الترقية: مزود تجاري (اشتراك) للباركود + تحديث الأسعار اليومي عند الحاجة
- الأسعار مقررة حكومياً — حقل price_source + تاريخ التحديث على كل سعر
```

---

## 🎨 معايير تجربة المستخدم (UX Standards)

### Design System — القرارات الثابتة
```css
/* Color Palette — Pharmacy Professional Theme */
:root {
  /* Primary — Pharmacy Blue */
  --primary-50: #EFF6FF;
  --primary-500: #2563EB;
  --primary-600: #1D4ED8;
  --primary-700: #1E40AF;

  /* Success — Stock Available */
  --success: #16A34A;

  /* Warning — Low Stock / Expiry Soon */
  --warning: #D97706;

  /* Danger — Out of Stock / Expired */
  --danger: #DC2626;

  /* Neutral */
  --surface: #F8FAFC;
  --border: #E2E8F0;

  /* Typography */
  --font-arabic: 'Cairo', 'IBM Plex Arabic', sans-serif;
  --font-numbers: 'Inter', sans-serif;  /* للأرقام دائماً */

  /* Spacing Scale */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
}
```

### UX Rules (إلزامية)
```
✅ RTL Layout كامل — لا mixed RTL/LTR في نفس الصفحة
✅ الأرقام والعملة: font-variant-numeric: tabular-nums
✅ Loading States على كل async operation
✅ Error Messages: واضحة + مع خطوات الحل (عبر كود خطأ مترجم — انظر قواعد التطوير)
✅ Success Feedback: toast notification < 3 ثوانٍ
✅ Keyboard Navigation كاملة (POS يعمل بدون ماوس)
✅ Barcode Scanner: التركيز التلقائي على حقل البحث
✅ Print Preview قبل الطباعة (للتقارير — الإيصال الحراري يُطبع مباشرة)
✅ Confirmation Dialog لأي عملية لا رجعة فيها
✅ Empty States مع CTA واضح
✅ Offline Badge واضح عند انقطاع الاتصال + مؤشر sync status دائم

❌ لا تُخفي errors خلف technical messages
❌ لا تُعيد تحميل الصفحة عند حفظ البيانات
❌ لا تستخدم alerts/confirms المتصفح الافتراضية
```

### POS (نقطة البيع) — متطلبات خاصة
```
السرعة أولاً في POS:
- Barcode scan → إضافة للفاتورة: < 50ms
- اختيار مستوى البيع: علبة / شريط / قرص (المستوى الافتراضي من medication_packaging)
- Shortcut Keys:
    F2: بحث عن دواء
    F3: خصم
    F4: إتمام البيع
    F5: طباعة
    F6: مرتجع
    F7: تبديل مستوى الوحدة (علبة/شريط/قرص)
    F8: فتح الدرج
    ESC: إلغاء
- Barcode Scanner: HID mode (keyboard input) — يدعم 1D + 2D DataMatrix
- مسح 2D: يلتقط GTIN + تشغيلة + انتهاء + تسلسلي في خطوة واحدة
- Cash drawer: تفتح تلقائياً عند اكتمال البيع (ESC/POS pulse — انظر الطباعة)
- Invoice print: تلقائية بعد إتمام البيع
```

---

## 📊 الوحدات الأساسية (Core Modules)

> ⚠️ v1.1: كل المراحل غير منفذة — المشروع يبدأ من الصفر.
> الترتيب أدناه هو ترتيب التنفيذ المعتمد (متوافق مع خارطة الطريق التفصيلية).

```
Phase 0 — التأسيس (إلزامية قبل أي ميزة):
  [ ] Monorepo (pnpm + Turborepo) بالبنية المحددة
  [ ] Docker Compose: PostgreSQL 17 + Redis 7
  [ ] Supabase project + سير migrations موحد (نفس المخطط محلياً وسحابياً)
  [ ] المخطط الأساسي (الأعمدة الإلزامية + Core + Catalog tables)
  [ ] JWT RS256 + RBAC (الكود مصدر الصلاحيات)
  [ ] نظام التصميم RTL + خطوط عربية
  [ ] CI (GitHub Actions): lint + types + tests + migration up/down
  [ ] النسخ الاحتياطي اليومي المشفر + نسخ سحابي أحادي + اختبار استعادة
  [ ] حماية المفاتيح عبر مخزن نظام التشغيل
  [ ] Walking Skeleton: مسح → فاتورة → طباعة → حفظ offline (أول أسبوعين)

Phase 1 — Core (النواة):
  [ ] Auth & Users Management (واجهات)
  [ ] Branch Setup
  [ ] Medications Catalog (CRUD + باركود 1D/2D + بحث عربي مطبّع)
  [ ] بذر الكتالوج من قاعدة الأدوية المصرية + استيراد Excel
  [ ] تسلسل الوحدات: علبة/شريط/قرص بالأسعار
  [ ] Inventory Management (دفعات + حركات + FEFO)
  [ ] POS — Point of Sale (بيع بالمستويات + اختصارات + بلا ماوس)
  [ ] Invoice + طباعة حرارية ESC/POS + فتح الدرج
  [ ] Cash Session Management
  [ ] Audit Log فعّال من أول عملية

Phase 2 — Business + Compliance (الأعمال والامتثال):
  [ ] Suppliers Management + Purchase Orders (استلام بالدفعات + مسح 2D)
  [ ] Batch Tracking كامل (FEFO + تنبيهات انتهاء + حجر تلقائي)
  [ ] Customer Management + Loyalty
  [ ] Prescriptions Management + سجل المواد الخاضعة للرقابة
  [ ] Returns Management (credit notes — لا تعديل فواتير)
  [ ] Expenses Tracking
  [ ] ض.ق.م حسب tax_profile
  [ ] 🇪🇬 وحدة الإيصال الإلكتروني ETA (بعد التحقق من الإلزام)
  [ ] 🇪🇬 وحدة تتبع الدواء EDA (tt_events + إبلاغ)

Phase 3 — Analytics (التحليلات):
  [ ] Sales Reports (Daily/Monthly/Annual)
  [ ] Inventory Reports (Stock Level, Movement)
  [ ] Expiry Alerts (30/60/90 يوماً)
  [ ] Profit/Loss Analysis
  [ ] Supplier Performance
  [ ] Customer Purchase History
  [ ] نظام الإشعارات الكامل

Phase 4 — Enterprise (المؤسسية):
  [ ] Multi-Branch Management
  [ ] Cloud Sync كامل ثنائي الاتجاه (Outbox + LWW + مراجعة يدوية)
  [ ] Mobile App (React Native) — مؤجل حتى ثبات النواة
  [ ] Insurance Integration
  [ ] توسيع دول/عملات (SAR, AED, ...)
```

---

## 🖨️ الفواتير والطباعة (Invoices & Printing)

### أنواع الفواتير
```typescript
enum InvoiceType {
  RETAIL = 'retail',             // بيع تجزئة
  WHOLESALE = 'wholesale',       // بيع جملة
  PRESCRIPTION = 'prescription', // وصفة طبية
  RETURN = 'return',             // مرتجع (credit note)
}

interface InvoiceTemplate {
  pharmacy_name: string;
  pharmacy_logo: string;
  license_number: string;        // رقم الترخيص
  address: string;
  phone: string;
  tax_registration_no?: string;  // رقم التسجيل الضريبي (إلزامي مع ETA)

  // Footer
  return_policy: string;
  thank_you_message: string;

  // Print Settings
  paper_size: '80mm' | 'A4' | 'A5';
  show_pharmacist_signature: boolean;
  show_qr_code: boolean;         // QR — إلزامي للإيصال الإلكتروني ETA
}
```

### معمارية الطباعة (v1.1 — قرار ملزم)
```
⚠️ طباعة المتصفح (React-to-Print) لا تستطيع فتح درج النقود ولا تضمن
   جودة/سرعة الإيصال الحراري.

مساران منفصلان:

1) الإيصال الحراري 80mm + درج النقود → ESC/POS مباشرة:
   - من Electron main process (node escpos / بروتوكول raw عبر USB/شبكة)
   - الدرج يُفتح بأمر pulse عبر الطابعة (ESC p)
   - قالب الإيصال: نص + QR مولّد محلياً — الهدف < 1 ثانية
   - fallback: طباعة نظام التشغيل إن فشل المسار المباشر (مع تحذير أن
     الدرج لن يفتح تلقائياً)

2) التقارير والفواتير A4/A5 → WeasyPrint (PDF) أو معاينة المتصفح
   - معاينة قبل الطباعة إلزامية

✅ الطباعة لا تعتمد على الإنترنت إطلاقاً
✅ مصفوفة أجهزة معتمدة تُختبر فعلياً (طابعة/ماسح/درج) قبل الإنتاج
```

---

## 🚨 التنبيهات الذكية (Smart Alerts)

```typescript
const ALERT_RULES = {
  // المخزون (المصدر: batches + branch_inventory)
  low_stock: {
    trigger: 'cached_quantity <= reorder_point',
    severity: 'warning',
    notification: 'push + email',
  },
  out_of_stock: {
    trigger: 'cached_quantity === 0',
    severity: 'critical',
    notification: 'push + email + dashboard_banner',
  },

  // تاريخ الانتهاء (على مستوى الدفعة)
  expiry_critical: {
    trigger: 'batch.expiry_date <= now() + 30 days',
    severity: 'critical',
  },
  expiry_warning: {
    trigger: 'batch.expiry_date <= now() + 90 days',
    severity: 'warning',
  },
  expired: {
    trigger: 'batch.expiry_date < now()',
    severity: 'danger',
    action: 'batch.status = quarantined',  // ✅ الحجر = تغيير حالة الدفعة
                                           // (مدعوم في المخطط) — لا تُباع
  },

  // المبيعات والمالية
  high_discount: {
    trigger: 'discount > branch_max_discount',
    severity: 'warning',
    require_approval: true,
  },
  cash_discrepancy: {
    trigger: 'actual_cash !== expected_cash',
    severity: 'critical',
    notify_manager: true,
  },

  // الامتثال (v1.1)
  ereceipt_backlog: {
    trigger: 'ereceipt_queue pending > 24h',
    severity: 'critical',
  },
  tt_report_failed: {
    trigger: 'tt_events failed > 3 retries',
    severity: 'critical',
  },

  // النظام
  sync_failed: {
    trigger: 'sync_retry_count > 3',
    severity: 'warning',
  },
  backup_overdue: {
    trigger: 'last_backup > 24 hours',
    severity: 'critical',
  },
  inventory_drift: {
    trigger: 'cached_quantity != SUM(batches)',   // فحص دوري
    severity: 'critical',
    action: 'rebuild + audit entry',
  },
};
```

---

## 🔧 قواعد التطوير (Development Rules)

### Code Style
```typescript
// ✅ Function Naming
const getMedicationById = async (id: string) => {}  // camelCase للـ functions
const MAX_DISCOUNT = 30;                             // SCREAMING_SNAKE للـ constants
interface MedicationResponse { ... }                 // PascalCase للـ interfaces
```

### أكواد الأخطاء والترجمة (v1.1)
```typescript
// ✅ الـ API يُعيد كود خطأ ثابتاً — والواجهة تترجمه حسب لغة المستخدم
// ❌ لا نصوص عربية hardcoded في طبقة الـ API (النظام ثنائي اللغة)

// سجل أكواد الأخطاء (packages/shared/errors.ts) — يُضاف إليه ولا يُعدَّل
const ERROR_CODES = {
  STOCK_INSUFFICIENT:   'E-STK-001',   // المخزون غير كافٍ
  BATCH_EXPIRED:        'E-STK-002',   // الدفعة منتهية/محجورة
  VALIDATION_FAILED:    'E-VAL-001',
  UNAUTHORIZED:         'E-AUTH-001',
  PERMISSION_DENIED:    'E-AUTH-002',
  ERECEIPT_REJECTED:    'E-ETA-001',
  TT_REPORT_FAILED:     'E-TT-001',
  SYNC_CONFLICT:        'E-SYN-001',
  // ...
} as const;

// ✅ Error Handling — لا تُهمل الأخطاء أبداً
try {
  const result = await createInvoice(data);
  toast.success(t('invoice.created'));
  return result;
} catch (error) {
  if (error instanceof ApiError) {
    toast.error(t(`errors.${error.code}`, error.params)); // ترجمة حسب اللغة
  } else {
    toast.error(t('errors.unexpected'));
    logger.error(error); // الخطأ الحقيقي للسجل فقط
  }
}

// ✅ API Response Format (موحد دائماً)
interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: {
    code: string;            // من سجل الأكواد — الواجهة تترجم
    message: string;         // نص احتياطي بلغة الطلب (Accept-Language)
    details?: unknown;       // للـ debugging فقط — لا يظهر للمستخدم
  };
  meta?: {
    page: number;
    total: number;
    per_page: number;
  };
}
```

### Git Commit Convention
```
feat(inventory): إضافة نظام تتبع الدفعات
fix(pos): إصلاح مشكلة الباركود مع اللغة العربية
perf(reports): تحسين أداء تقرير المبيعات الشهري
security(auth): إضافة rate limiting على نقطة تسجيل الدخول
docs(api): تحديث توثيق endpoints المخزون
test(sales): إضافة tests لوحدة المرتجعات
compliance(eta): ربط طابور الإيصال الإلكتروني
```

---

## 🧪 معايير الجودة (Quality Standards)

### Testing Requirements (v1.1 — معايرة على طبيعة النظام)
```
Unit Tests:     coverage > 80% للـ business logic
Integration:    كل API endpoint له test
E2E:            المسارات الحرجة: login، بيع كامل بالباركود، طباعة، مرتجع،
                فتح/إغلاق جلسة كاشير

⚠️ الأهم لنظام offline بجهاز واحد (بديل اختبار الحمل غير الملائم):
Offline Correctness:  بيع كامل بلا إنترنت → عودة الاتصال → لا فقد ولا تكرار
Print Reliability:    100 إيصال متتالٍ بلا فشل على الأجهزة المعتمدة
Crash Recovery:       قطع كهرباء أثناء بيع → إعادة تشغيل → لا فاتورة ناقصة
                      (transactions ذرّية + journal)
Drift Check:          cached_quantity == SUM(batches) بعد 1000 عملية عشوائية
Load (cloud فقط):     dashboard السحابي: 100 مستخدم متزامن (لا ينطبق على POS المحلي)
```

### قبل أي Production Deployment
```
الـ Checklist الإلزامي:
[ ] كل الـ tests تعمل (100% pass)
[ ] لا errors في console
[ ] Database migrations تعمل وتُعكس (up/down)
[ ] Backup يعمل ويُستعاد بنجاح (تمرين استعادة فعلي)
[ ] الطباعة + فتح الدرج على الأجهزة المعتمدة
[ ] Offline mode كامل (بيع + طباعة بلا إنترنت)
[ ] Sync يعمل عند عودة الإنترنت (بلا فقد/تكرار)
[ ] كل الـ permissions تعمل (test suite للمصفوفة كاملة)
[ ] Performance: TTI < 2s، مسح → عرض < 50ms
[ ] Security: OWASP Top 10 checklist
[ ] الإيصال الإلكتروني مقبول على بيئة اختبار ETA (إن كانت الصيدلية ملزمة)
[ ] التقاط 2D وتسجيل tt_events يعمل (إن لزم)
```

---

## 📱 نظام الإشعارات

```typescript
enum NotificationChannel {
  IN_APP = 'in_app',     // داخل التطبيق
  DESKTOP = 'desktop',   // Windows/Mac notification
  EMAIL = 'email',       // بريد إلكتروني
  SMS = 'sms',           // رسائل نصية (اختياري)
}

enum Priority {
  LOW = 'low',
  MEDIUM = 'medium',
  HIGH = 'high',
  CRITICAL = 'critical', // يتطلب تصرف فوري
}
```

---

## ⚠️ القواعد المحظورة (Forbidden Actions)

```
❌ NEVER — لا تفعل هذا أبداً:

1.  لا تحذف سجلات من قاعدة البيانات (Soft Delete فقط)
2.  لا تغيّر sync_version يدوياً
3.  لا تتجاوز permission checks
4.  لا تخزن API Keys أو passwords في الكود
5.  لا تستخدم SELECT * في queries الإنتاج
6.  لا تُعيد stack traces للـ frontend
7.  لا تترك TODO أو FIXME في production code
8.  لا تُعدّل /supabase/ directory مباشرة
9.  لا تُوقف الـ Audit Log لأي سبب (append-only بقيد قاعدة بيانات)
10. لا تحفظ sensitive data في localStorage
11. لا تطبع console.log في production
12. لا تُعطّل migrations — أضف migrations جديدة فقط
13. لا تحذف Controlled Substances records نهائياً
14. لا تُغيّر invoice بعد إتمامها (أنشئ credit note بدلاً)
15. لا تخزن كمية مخزون على medications — الدفعات مصدر الحقيقة الوحيد (v1.1)
16. لا تُحدّث كمية دفعة بلا stock_movement مقابل (v1.1)
17. لا تستخدم FLOAT للمال — DECIMAL فقط (v1.1)
18. لا تبعْ من دفعة status != 'active' (محجورة/منتهية/مسحوبة) (v1.1)
19. لا ترقَّ حزمة major منفردة خارج موجة ترقية مُختبَرة (v1.1)
20. لا تُصدر إيصالاً ضريبياً بلا توقيع صالح عند تفعيل وحدة ETA (v1.1)
```

---

## 🚫 خارج النطاق (Non-Goals)

> صمام ضد تضخم النطاق — هذه الأشياء **لن تُبنى** في النسخ الحالية:

```
✗ متجر إلكتروني / طلبات أونلاين للعملاء
✗ توصيل ودليفري وتتبع مناديب
✗ محاسبة عامة كاملة (دفتر أستاذ/ميزانية) — نصدّر للمحاسب بدلاً
✗ إدارة عيادات أو مواعيد أطباء
✗ تصنيع/تركيب مستحضرات
✗ تكامل موردين آلي (EDI) في النسخ الأولى
✗ ذكاء اصطناعي تنبؤي للطلب (بعد اكتمال التحليلات الأساسية فقط)
✗ دعم iOS/Android قبل Phase 4
```

---

## 🌐 دعم اللغة العربية

```typescript
// i18n Configuration
const i18nConfig = {
  defaultLocale: 'ar',
  locales: ['ar', 'en'],
  direction: { ar: 'rtl', en: 'ltr' },
};

// Date Formatting — التقويم من إعداد الدولة (مصر: ميلادي)
const formatDate = (date: Date, calendar: 'gregory' | 'islamic' = 'gregory') =>
  new Intl.DateTimeFormat('ar-EG', {
    dateStyle: 'long',
    timeStyle: 'short',
    calendar,
  }).format(date);

// تطبيع البحث العربي في الواجهة يطابق normalize_arabic في القاعدة
// (نفس قواعد الهمزة/الألف/التاء/التطويل/التشكيل)
```

---

## 📁 ملفات الإعداد المطلوبة

```
.env.local (Local Development):
  DATABASE_URL=postgresql://localhost:5432/pharmaos
  REDIS_URL=redis://localhost:6379
  SUPABASE_URL=<cloud url>
  SUPABASE_ANON_KEY=<anon key>
  BACKUP_PATH=/var/pharmaos/backups
  BACKUP_CLOUD_BUCKET=<one-way encrypted backup bucket>
  COUNTRY_CODE=EG
  DEFAULT_CURRENCY=EGP

  # مصر — الامتثال (تُملأ عند التفعيل)
  ETA_API_BASE=<preprod|prod>
  ETA_CLIENT_ID=<pos client id>
  EDA_TT_API_BASE=<حسب دليل المنظومة>

⚠️ الأسرار الحرجة ليست في .env على أجهزة الإنتاج:
  JWT_PRIVATE_KEY / JWT_PUBLIC_KEY   → مخزن نظام التشغيل (safeStorage/DPAPI)
  ENCRYPTION_KEY                     → مخزن نظام التشغيل
  ETA_CLIENT_SECRET + شهادة الختم    → مخزن نظام التشغيل
  SUPABASE_SERVICE_ROLE_KEY          → سحابياً فقط (لا يوضع على جهاز الصيدلية)

.env.production:
  NODE_ENV=production
  (نفس البنية بقيم الإنتاج — والأسرار في المخزن الآمن)
```

---

## 🆘 الدعم الفني

```
Log Levels:
  ERROR: أخطاء تمنع عمل النظام → يحتاج تدخل فوري
  WARN:  مشاكل لا تمنع العمل → مراجعة خلال 24 ساعة
  INFO:  عمليات طبيعية مهمة → للـ audit
  DEBUG: تفاصيل للـ development فقط (OFF في production)

Support Contact في النظام:
  - كود خطأ (من سجل الأكواد) + رسالة واضحة للمستخدم
  - رابط لـ Knowledge Base
  - زر "تواصل مع الدعم" يفتح ticket تلقائياً مع context (كود الخطأ + آخر
    عمليات + حالة sync — بلا بيانات حساسة)
```

---

*آخر تحديث: يوليو 2026 | الإصدار: 1.1.0*
*يجب مراجعة هذا الملف مع كل major release — وقسم الامتثال المصري كل ربع سنة*
