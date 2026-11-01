/**
 * Display formatting helpers (wave UX-1) — Arabic UI, LATIN digits always:
 * tabular-nums + Inter + the printed receipt + numeric inputs all assume
 * Western numerals, so number formatting is pinned to `nu-latn` (plan decision,
 * CLAUDE.md money rule — the SERVER remains the money authority; these only
 * polish display).
 *
 * `getLocale()` is a deliberate stub returning 'ar' until the language
 * switcher lands (plan phase 5) — a single swap point, nothing else changes.
 */

export type Locale = 'ar' | 'en';

export function getLocale(): Locale {
  return 'ar';
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  EGP: 'ج.م',
  SAR: 'ر.س',
  AED: 'د.إ',
  KWD: 'د.ك',
  QAR: 'ر.ق',
  OMR: 'ر.ع',
  BHD: 'د.ب',
  JOD: 'د.أ',
  USD: '$',
  EUR: '€',
  GBP: '£',
};

/** Currency symbol for display; unknown codes fall back to the ISO code. */
export function currencySymbol(code: string): string {
  return CURRENCY_SYMBOLS[code] ?? code;
}

const moneyFormatters = new Map<string, Intl.NumberFormat>();

/**
 * Amount + currency for display: "443.74 ج.م". Amounts arrive as server
 * DECIMAL strings or client numbers — both accepted; invalid input renders
 * the raw text rather than NaN.
 */
export function formatMoney(amount: string | number, code: string): string {
  const symbol = currencySymbol(code);
  const raw = typeof amount === 'number' ? String(amount) : amount.trim();
  if (raw === '' || !Number.isFinite(Number(raw))) return `${raw} ${symbol}`.trim();
  const locale = `${getLocale()}-u-nu-latn`;
  let fmt = moneyFormatters.get(locale);
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    moneyFormatters.set(locale, fmt);
  }
  return `${fmt.format(Number(raw))} ${symbol}`;
}

const dateTimeFormatters = new Map<
  string,
  { date: Intl.DateTimeFormat; time: Intl.DateTimeFormat }
>();

function dateTimeFmt(): { date: Intl.DateTimeFormat; time: Intl.DateTimeFormat } {
  const locale = `${getLocale()}-u-nu-latn`;
  let fmt = dateTimeFormatters.get(locale);
  if (!fmt) {
    fmt = {
      date: new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }),
      time: new Intl.DateTimeFormat(locale, { timeStyle: 'short' }),
    };
    dateTimeFormatters.set(locale, fmt);
  }
  return fmt;
}

/** LOCAL calendar day key of an instant — "today/last-seen" comparisons use
 * the device's own day (pilot-checklist contract), never UTC. */
function localDayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/**
 * Timestamps (created_at / last_seen, server TIMESTAMPTZ) in device-local
 * time: "اليوم 3:41 م" / "أمس 9:02 ص" / otherwise "1 نوفمبر 2026، 3:41 م" —
 * Latin digits, pinned locale. (Phase 5 routes the اليوم/أمس literals
 * through i18n when the language switcher lands.)
 */
export function formatDateTime(value: string | Date): string {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const { date, time } = dateTimeFmt();
  const now = new Date();
  if (localDayKey(d) === localDayKey(now)) return `اليوم ${time.format(d)}`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (localDayKey(d) === localDayKey(yesterday)) return `أمس ${time.format(d)}`;
  return `${date.format(d)}، ${time.format(d)}`;
}

/**
 * Quantity-like DECIMAL strings rendered to humans: "240.000" → "240",
 * "12.500" → "12.5". Keeps integers tight in badges/alerts without touching
 * server money formatting.
 */
export function formatQty(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const raw = String(value).trim();
  if (raw === '' || !Number.isFinite(Number(raw))) return raw;
  return String(Number(raw));
}
