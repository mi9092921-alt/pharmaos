/**
 * Error-code registry (CLAUDE.md: packages/shared/errors.ts).
 * Append-only — codes are added, never modified.
 *
 * The API returns these stable codes; the UI translates them per locale
 * (t(`errors.${code}`)). Kept identical to apps/api/.../errors.py.
 */

export const ERROR_CODES = {
  STOCK_INSUFFICIENT: 'E-STK-001', // المخزون غير كافٍ
  BATCH_EXPIRED: 'E-STK-002', // الدفعة منتهية/محجورة
  VALIDATION_FAILED: 'E-VAL-001',
  UNAUTHORIZED: 'E-AUTH-001',
  PERMISSION_DENIED: 'E-AUTH-002',
  ACCOUNT_LOCKED: 'E-AUTH-003', // قفل بعد محاولات فاشلة متكررة
  CSRF_FAILED: 'E-AUTH-004',
  RATE_LIMITED: 'E-AUTH-005',
  USERNAME_TAKEN: 'E-USR-001',
  BARCODE_TAKEN: 'E-CAT-001',
  ERECEIPT_REJECTED: 'E-ETA-001',
  TT_REPORT_FAILED: 'E-TT-001',
  PACK_SERIAL_DUPLICATE: 'E-TT-002', // تسلسل عبوة مكرر (منتج غير ممتثل — قرار 804)
  PACK_SERIAL_MISMATCH: 'E-TT-003', // تسلسل ممسوح ليس من دفعة مصروفة في هذا البيع
  SYNC_CONFLICT: 'E-SYN-001',
  PRINTER_NOT_CONFIGURED: 'E-PRN-001', // لا توجد طابعة مهيأة على الجهاز
  PRINTER_UNREACHABLE: 'E-PRN-002', // تعذر الوصول لطابعة الإيصالات
  PAPER_NOT_THERMAL: 'E-PRN-003', // مقاس الورق ليس 80mm حرارياً
  SESSION_ALREADY_OPEN: 'E-CSH-001', // لدى الكاشير جلسة مفتوحة بالفعل
  SESSION_NOT_OPEN: 'E-CSH-002', // الجلسة ليست مفتوحة
  PRESCRIPTION_REQUIRED: 'E-RX-001', // الدواء يتطلب وصفة ولم تُربط
  PRESCRIPTION_EXCEEDED: 'E-RX-002', // الكمية تتجاوز المتبقي من الوصفة
  PRESCRIPTION_INVALID: 'E-RX-003', // بند الوصفة غير مطابق لهذا الصنف
  NOT_FOUND: 'E-GEN-001', // مسار/مورد غير موجود (404/405) — مغلف موحد حتى هنا
  UNEXPECTED: 'E-SYS-001',
  // P4-M1 (licensing) — mirrors errors.py; statuses/HTTP mapping in the gate (M2)
  // per docs/phase4-execution-plan-licensing.md §7.
  LICENSE_REQUIRED: 'E-LIC-001', // مطلوب تفعيل الترخيص
  LICENSE_READ_ONLY: 'E-LIC-002', // انتهت فترة السماح — وضع القراءة فقط
  LICENSE_INVALID_SIGNATURE: 'E-LIC-003', // ملف الترخيص غير صالح (توقيع/بنية)
  LICENSE_DEVICE_MISMATCH: 'E-LIC-004', // الترخيص مربوط بجهاز آخر
  LICENSE_EXPIRED: 'E-LIC-005', // الترخيص منتهي
  LICENSE_TAMPER_DETECTED: 'E-LIC-006', // فشل فحص السلامة — تواصل مع الدعم
  LICENSE_STATE_ERROR: 'E-LIC-007', // حالة الترخيص غير متاحة مؤقتاً — إعادة محاولة
  LICENSE_KEY_LOST: 'E-LIC-008', // مفتاح الترخيص مفقود من الجهاز — استعادته أو الدعم
} as const;

export type ErrorCode = (typeof ERROR_CODES)[keyof typeof ERROR_CODES];

/** Unified API response shape (CLAUDE.md). */
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: {
    code: string; // from the registry — the UI translates
    message: string; // request-language fallback text
    details?: unknown; // debugging only — never shown to the user
  };
  meta?: {
    page: number;
    total: number;
    per_page: number;
  };
}
