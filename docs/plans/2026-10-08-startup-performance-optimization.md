# خطة التنفيذ الدقيقة: تسريع إقلاع PharmaOS وإضافة Splash Screen

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** خفض زمن إقلاع تطبيق PharmaOS من ~21-40 ثانية إلى ~3.5-5 ثوانٍ، وإظهار شاشة تحميل (Splash Screen) فورية خلال أول 200ms لمنح المستخدم تجربة سريعة وسلسة مع إشعارات تقدم باللغة العربية.

**Architecture:**

1. إظهار نافذة Splash خفيفة بدون إطار فوراً عند تشغيل Electron (`app.whenReady()`).
2. تحسين فحص المنافذ في `orchestrator.ts` لتشغيلها بالتوازي، وتخطي تشغيل PowerShell والتأخير الإجباري عند الإقلاع النظيف.
3. دمج تشغيل الهجرات (Migrations) داخل الـ API عند الإقلاع وجعل مهام الصيانة (التحقق من المخزون، تقييم التنبيهات، تصريف البريد) مهام خلفية غير حاجزة في FastAPI `lifespan`.
4. توفير نقطة نهاية HTTP خفيفة `GET /api/v1/system/setup-status` لإلغاء استدعاء CLI البارد لـ `setup-status`.
5. تشغيل خادمي الـ API والـ Web بالتوازي بعد جاهزية PostgreSQL مباشرة.
6. إضافة استثناءات Windows Defender في سكربت التثبيت لمنع الفحص الدوري للملفات التنفيذية.

**Tech Stack:** Electron (TypeScript), FastAPI / Uvicorn (Python, asyncpg), Next.js Standalone (Node.js), PowerShell / NSIS.

---

### Task 1: إنشاء شاشة التحميل (Splash Screen UI & Preload) في تطبيق Desktop

**Files:**

- Create: `apps/desktop/resources/splash.html`
- Create: `apps/desktop/src/splash-preload.ts`
- Modify: `apps/desktop/src/main.ts`

**Step 1: إنشاء واجهة Splash Screen خفيفة وسريعة (`splash.html`)**
ملف HTML مكتفٍ ذاتياً (Inline CSS & SVG) لا يحتاج أي اتصال شبكي أو مكتبات خارجية، بتصميم داكن فاخر متناسق مع هوية PharmaOS، يحتوي على الشعار، شريط تقدم متحرك، ومؤشر نصي للمرحلة الحالية.

**Step 2: إنشاء جسر الـ IPC الخاص بالـ Splash (`splash-preload.ts`)**
كود Preload آمن (`contextIsolation: true`) يعرض دالة للاستماع لتحديثات المرحلة:

```typescript
import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('splashApi', {
  onStatus: (callback: (status: { stage: string; message: string }) => void) => {
    ipcRenderer.on('splash:status', (_event, data) => callback(data));
  },
});
```

**Step 3: تعديل `apps/desktop/src/main.ts` لإنشاء Splash Screen فوراً**

- دالة `createSplashWindow()` تُستدعى في أول سطر من `app.whenReady()`.
- تمرير دالة `onProgress(stage, message)` إلى `orchestrator.boot()`.
- عند إطلاق النافذة الرئيسية وجاهزيتها (`ready-to-show`)، إغلاق نافذة الـ Splash بانتقال ناعم.

**Step 4: التحقق والاختبار**
تشغيل: `pnpm --filter @pharmaos/desktop typecheck`
التأكد من خلو الكود من أي أخطاء في الـ TypeScript.

---

### Task 2: تحسين فحص المنافذ والتنظيف الذكي في `orchestrator.ts`

**Files:**

- Modify: `apps/desktop/src/runtime/ports.ts`
- Modify: `apps/desktop/src/runtime/orchestrator.ts:65-98`

**Step 1: جعل فحص المنافذ بالتوازي (`ports.ts`)**
تعديل `preflightPorts` لاستخدام `Promise.all`:

```typescript
export async function preflightPorts(p: {
  pgPort: number;
  apiPort: number;
  webPort: number;
}): Promise<PortCheck[]> {
  const [pgFree, apiFree, webFree] = await Promise.all([
    isPortFree(p.pgPort),
    isPortFree(p.apiPort),
    isPortFree(p.webPort),
  ]);
  return [
    { port: p.pgPort, label: 'PostgreSQL (5433)', free: pgFree },
    { port: p.apiPort, label: 'API (8000)', free: apiFree },
    { port: p.webPort, label: 'الواجهة (3000)', free: webFree },
  ];
}
```

**Step 2: تخطي PowerShell والتأخير الإجباري عند الإقلاع النظيف**
في `orchestrator.ts`:

- فحص المنافذ أولاً (`preflightPorts`).
- إذا كانت جميع المنافذ خالية (وهو الوضع الطبيعي في 99% من الحالات): تخطي استدعاء `powershell.exe -File watchdog.ps1 -Cleanup` وتخطي `setTimeout 1000ms` تماماً (توفير فوري لـ ~3 ثوانٍ).
- فقط في حال وجود منفذ مشغول: استدعاء التنظيف بمهلة قصيرة (10 ثوانٍ بدلاً من 60).

**Step 3: التحقق والاختبار**
تشغيل: `pnpm --filter @pharmaos/desktop typecheck`

---

### Task 3: إضافة نقطة نهاية سريعة `setup-status` ونقل مهام الصيانة في FastAPI

**Files:**

- Modify: `apps/api/src/pharmaos_api/main.py`
- Modify: `apps/api/src/pharmaos_api/routers/system.py` (أو إضافتها ضمن الراوتر المناسب)
- Test: `apps/api/tests/test_system_status.py`

**Step 1: كتابة اختبار وحدة لنقطة النهاية `setup-status`**
اختبار يتأكد من استرجاع حالة الإعداد عبر HTTP بنجاح:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_get_setup_status(async_client: AsyncClient):
    response = await async_client.get("/api/v1/system/setup-status")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "setup_complete" in data
    assert "users" in data
```

**Step 2: إنشاء نقطة النهاية `GET /api/v1/system/setup-status`**
نقطة نهاية محلية سريعة تستعلم عن `users`, `branches`, و `installation_state` دون الحاجة لتشغيل عملية CLI منفصلة.

**Step 3: جعل `lifespan` في `apps/api/src/pharmaos_api/main.py` غير حاجب**
تحويل استدعاءات:

- `_boot_inventory_maintenance`
- `_boot_alert_evaluation`
- `_boot_email_drain`
  إلى خلفية التطبيق (`asyncio.create_task`) بحيث لا تعطل فتح المنفذ واستجابة `/api/v1/health`.
  تشغيل الهجرات السريعة تلقائياً في مرحلة البدء إذا لزم الأمر قبل فتح الاستقبال.

**Step 4: تشغيل الاختبارات**
تشغيل: `pytest apps/api/tests/test_system_status.py` و `pytest apps/api/tests/test_hardening_m8.py`

---

### Task 4: الإقلاع بالتوازي في `orchestrator.ts` واستدعاء `setup-status` عبر HTTP

**Files:**

- Modify: `apps/desktop/src/runtime/orchestrator.ts:139-195`

**Step 1: إطلاق الـ API والـ Web بالتوازي بعد جاهزية PostgreSQL**

- فور جاهزية PostgreSQL:
  - إطلاق `apiExe`
  - إطلاق `nodeExe` للـ Web
  - انتظار الاثنين بالتوازي:
    ```typescript
    await Promise.all([
      waitHttp(`http://127.0.0.1:${API_PORT}/api/v1/health`, 90000, 'API'),
      waitHttp(`http://127.0.0.1:${WEB_PORT}/`, 90000, 'Web UI'),
    ]);
    ```
- هذا يخفي زمن إقلاع Next.js بالكامل ويوفر ~2.5 ثانية إضافية.

**Step 2: استبدال استدعاء CLI لـ `setup-status` بطلب HTTP محلي**
بدلاً من `runCapture(this.p.apiExe, ['setup-status'])`، إرسال طلب HTTP محلي إلى:
`http://127.0.0.1:${API_PORT}/api/v1/system/setup-status` (يستغرق < 10ms بدلاً من 3800ms).

**Step 3: إلغاء إطلاق عملية `migrate` المنفصلة**
الـ API يتكفل بالهجرات عند إقلاعه (أو عبر استدعاء HTTP داخلي)، وبالتالي لا يتم تشغيل exe إضافي.

**Step 4: التحقق من الأنواع وبناء حزمة الـ Desktop**
تشغيل: `pnpm --filter @pharmaos/desktop build`

---

### Task 5: استثناء مجلدات PharmaOS من فحص Windows Defender في التثبيت

**Files:**

- Modify: `installer/install-steps.ps1`

**Step 1: إضافة أمر استبعاد Defender**
في `installer/install-steps.ps1`:

```powershell
# Exclude PharmaOS program & data directories from Windows Defender real-time scanning
Add-MpPreference -ExclusionPath (Join-Path $env:ProgramFiles 'PharmaOS'), $data -ErrorAction SilentlyContinue
```

هذا يمنع فحص Defender المتكرر لملفات `.exe` ومكتبات بايثون وقاعدة البيانات في كل تشغيل.

---

### Task 6: اختبار شامل للتشغيل وقياس التوقيتات الجديدة

**Files:**

- فحص ملفات السجلات: `C:\ProgramData\PharmaOS\logs\pharmaos.log`

**Step 1: تشغيل التطبيق واختبار تدفق الإقلاع**

- التأكد من ظهور نافذة الـ Splash فوراً مع رسائل التقدم بالعربية.
- التأكد من الانتقال السلس إلى الواجهة الرئيسية بعد اكتمال التحميل.
- قراءة التوقيتات الجديدة في `pharmaos.log` للتحقق من انخفاض الزمن من 21+ ثانية إلى أقل من 5 ثوانٍ.
