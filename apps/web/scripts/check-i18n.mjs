/**
 * i18n gate (wave هـ):
 *   1. key parity  — every key in `ar` exists in `en` and vice versa.
 *   2. token parity — the SAME set of {tokens} per key in both dictionaries
 *      (a missing token silently renders "{token}" raw to the user).
 *   3. used-key existence — every STATIC t('key') call site in src resolves
 *      in the dictionary (dynamic t(`prefix.${x}`) keys are covered by the
 *      prefix rule: at least one dictionary key must start with the prefix).
 *
 * Exits 1 listing every problem found. Run via `pnpm check:i18n` / turbo.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(path.join(here, '../src/lib/i18n.ts'), 'utf8');

function keysOf(dictName) {
  const start = source.indexOf(`${dictName}: {`);
  if (start < 0) throw new Error(`dictionary ${dictName} not found`);
  const lines = source.slice(start).split('\n');
  const keys = new Map(); // key -> joined value text (handles multi-line wraps)
  let currentKey = null;
  let currentVal = [];
  for (const line of lines.slice(1)) {
    if (/^ {4}\},/.test(line)) {
      if (currentKey) keys.set(currentKey, currentVal.join(' '));
      break;
    }
    // EXACTLY 4 spaces — continuation lines of wrapped values are indented
    // deeper and start with a quote; \s{4} would swallow them as new keys.
    const entry = line.match(/^ {4}'([^']+)':(.*)$/);
    if (entry) {
      if (currentKey) keys.set(currentKey, currentVal.join(' '));
      currentKey = entry[1];
      currentVal = [entry[2]];
    } else if (currentKey) {
      currentVal.push(line);
    }
  }
  // Final flush — the last entry may be followed by a close line whose
  // indentation differs (the dictionaries close at two spaces).
  if (currentKey) keys.set(currentKey, currentVal.join(' '));
  return keys;
}

function tokensOf(raw) {
  return new Set([...raw.matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
}

const ar = keysOf('ar');
const en = keysOf('en');

const problems = [];

// 1) key parity
for (const k of ar.keys()) if (!en.has(k)) problems.push(`key missing in EN: ${k}`);
for (const k of en.keys()) if (!ar.has(k)) problems.push(`key missing in AR: ${k}`);

// 2) token parity
for (const [k, arTpl] of ar) {
  const enTpl = en.get(k);
  if (enTpl === undefined) continue;
  const a = [...tokensOf(arTpl)].sort().join(',');
  const e = [...tokensOf(enTpl)].sort().join(',');
  if (a !== e) problems.push(`token mismatch for ${k}: ar=[${a}] en=[${e}]`);
}

// 3) used-key existence
function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(tsx?|mjs)$/.test(entry)) out.push(full);
  }
  return out;
}

const srcDir = path.join(here, '../src');
const usedLiteral = new Map();
const usedPrefixes = [];
for (const file of walk(srcDir)) {
  if (file.endsWith('lib\\i18n.ts') || file.endsWith('lib/i18n.ts')) continue;
  const text = readFileSync(file, 'utf8');
  for (const m of text.matchAll(/\bt\(\s*'([^']+)'/g)) {
    if (!usedLiteral.has(m[1])) usedLiteral.set(m[1], path.relative(srcDir, file));
  }
  for (const m of text.matchAll(/\bt\(\s*`([^`$]+)\$\{/g)) {
    usedPrefixes.push(m[1]);
  }
}

for (const [key, file] of usedLiteral) {
  if (!ar.has(key) && !usedPrefixes.some((p) => key.startsWith(p))) {
    problems.push(`used but undefined key: ${key} (${file})`);
  }
}
for (const prefix of usedPrefixes) {
  const any = [...ar.keys()].some((k) => k.startsWith(prefix));
  if (!any) problems.push(`dynamic t(\`${prefix}…\`) has no dictionary keys with this prefix`);
}

if (problems.length) {
  for (const p of problems) console.error(p);
  process.exit(1);
}
process.stdout.write(
  `i18n gate OK — ${ar.size} keys in both dictionaries, tokens aligned, ` +
    `${usedLiteral.size} static + ${usedPrefixes.length} dynamic usages resolve\n`,
);
