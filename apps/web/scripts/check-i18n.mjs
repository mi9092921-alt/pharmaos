/**
 * i18n integrity check, two gates in one:
 *  1. KEY PARITY — every key in the `ar` dictionary must exist in `en` and
 *     vice versa (the UI is RTL-first but ships both locales).
 *  2. USAGE — every STATIC t('key') call in src/** must exist in the
 *     dictionary. A missing key renders the RAW key to the user (the t()
 *     fallback), so this class of bug is user-visible. Dynamic keys
 *     (t(`prefix_${x}`) or 'errors.' + code) are intentionally not scanned.
 *
 * Run via `pnpm --filter @pharmaos/web check:i18n` / turbo `check:i18n`.
 * Exits 1 listing the problems.
 */
import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.join(here, '../src');
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
let failed = false;

const missingInEn = [...ar].filter((k) => !en.has(k));
const missingInAr = [...en].filter((k) => !ar.has(k));

if (missingInEn.length || missingInAr.length) {
  failed = true;
  if (missingInEn.length) console.error('keys missing in EN:', missingInEn);
  if (missingInAr.length) console.error('keys missing in AR:', missingInAr);
}

// USAGE gate: static t('key') calls must resolve to a defined key.
async function walk(dir) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...(await walk(p)));
    else if (/\.(tsx|ts)$/.test(e.name) && !p.endsWith('lib' + path.sep + 'i18n.ts')) out.push(p);
  }
  return out;
}

const files = await walk(srcDir);
const missingUsage = new Map();
for (const f of files) {
  const src = await readFile(f, 'utf8');
  for (const m of src.matchAll(/\bt\('([^']+)'\)/g)) {
    const key = m[1];
    if (!ar.has(key)) {
      if (!missingUsage.has(key)) missingUsage.set(key, []);
      missingUsage.get(key).push(path.relative(srcDir, f).replaceAll('\\', '/'));
    }
  }
}
if (missingUsage.size) {
  failed = true;
  for (const [key, filesAt] of [...missingUsage.entries()].sort()) {
    console.error(`missing key used in code: ${key} <- ${[...new Set(filesAt)].join(', ')}`);
  }
}

if (failed) process.exit(1);
process.stdout.write(`i18n OK — ${ar.size} keys in both dictionaries, all static usages resolved
`);
