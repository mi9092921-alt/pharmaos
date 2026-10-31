/**
 * i18n key-parity check: every key in the `ar` dictionary must exist in `en`
 * and vice versa (the UI is RTL-first but ships both locales). Run via
 * `pnpm --filter @pharmaos/web check:i18n` / turbo `check:i18n`.
 *
 * Exits 1 listing the missing keys on either side.
 */
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(path.join(here, '../src/lib/i18n.ts'), 'utf8');

function keysOf(dictName) {
  // locate `const dictionaries: Record<Locale, Record<string, string>> = { ar: {...}, en: {...} }`
  const start = source.indexOf(`${dictName}: {`);
  if (start < 0) throw new Error(`dictionary ${dictName} not found`);
  // walk lines until the closing "},"
  const lines = source.slice(start).split('\n');
  const keys = [];
  for (const line of lines.slice(1)) {
    if (/^\s{4}\},/.test(line)) break;
    const m = line.match(/^\s*'([^']+)':/);
    if (m) keys.push(m[1]);
  }
  return new Set(keys);
}

const ar = keysOf('ar');
const en = keysOf('en');
const missingInEn = [...ar].filter((k) => !en.has(k));
const missingInAr = [...en].filter((k) => !ar.has(k));

if (missingInEn.length || missingInAr.length) {
  if (missingInEn.length) console.error('keys missing in EN:', missingInEn);
  if (missingInAr.length) console.error('keys missing in AR:', missingInAr);
  process.exit(1);
}
console.log(`i18n parity OK — ${ar.size} keys in both dictionaries`);
