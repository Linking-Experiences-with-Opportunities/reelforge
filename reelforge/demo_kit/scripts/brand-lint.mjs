#!/usr/bin/env node
// brand-lint — fail a demo composition that leaves the active brand pack (see brand.mjs).
//   node brand-lint.mjs <index.html> [--brand <brand pack dir>] [--json]
// Rules: only colors that exist in the pack's tokens.css · only the brand font variables · no pure-white/named colors ·
//        the brand logo on intro AND outro (the light-background variant on light compositions) · no external network resources.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadBrand } from './brand.mjs';

const norm = (h) => { h = h.toLowerCase(); return h.length === 4 ? '#' + [...h.slice(1)].map((c) => c + c).join('') : h.slice(0, 7); };
const rgbOf = (h) => [1, 3, 5].map((i) => parseInt(norm(h).slice(i, i + 2), 16)).join(',');
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export function lintProject(htmlPath, brand = loadBrand()) {
  const allowedHex = new Set([...brand.tokensCss.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => norm(m[0])));
  const allowedRgb = new Set([...allowedHex].map(rgbOf));
  let html = readFileSync(htmlPath, 'utf8');
  const lineOf = (idx) => html.slice(0, idx).split('\n').length;
  // blank out the embedded token block (same length, so line numbers stay right)
  const blank = (m) => m.replace(/[^\n]/g, ' ');
  html = html.replace(/\/\*kit:tokens:start\*\/[\s\S]*?\/\*kit:tokens:end\*\//, blank);
  // visible text ("PR #1039") is not CSS: blank text nodes, keep <style>/<script> bodies and tag attributes
  html = html.replace(/(<(style|script)\b[^>]*>[\s\S]*?<\/\2>)|>([^<]+)(?=<)/g, (m, block, _t, text) => (block ? m : '>' + blank(text)));
  const findings = [];
  const add = (rule, msg, idx) => findings.push({ rule, line: lineOf(idx), message: msg });

  for (const m of html.matchAll(/#[0-9a-fA-F]{3,8}\b/g)) if (!allowedHex.has(norm(m[0]))) add('palette', `color ${m[0]} is not in the brand's tokens.css`, m.index);
  for (const m of html.matchAll(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/g)) if (!allowedRgb.has(`${m[1]},${m[2]},${m[3]}`)) add('palette', `${m[0]}) is not a brand color`, m.index);
  for (const m of html.matchAll(/\b(?:color|background(?:-color)?|border(?:-color)?)\s*:\s*(#fff(?:fff)?|white|black|red|blue|green|gray|grey|yellow|orange|purple)\b/gi)) add('no-raw-colors', `raw color "${m[1]}" — use a token`, m.index);
  for (const m of html.matchAll(/font-family\s*:\s*([^;}"]+)/gi)) if (!/^\s*var\(--font-(sans|mono|display)\)\s*$/.test(m[1])) add('fonts', `font-family "${m[1].trim()}" — use var(--font-sans) or var(--font-mono)`, m.index);
  for (const m of html.matchAll(/\b(?:src|href)\s*=\s*["'](?:https?:)?(\/\/[^"']+|https?:\/\/[^"']+)["']/gi)) add('offline', `external resource ${m[1]} — vendor it (renders must not need the network)`, m.index);
  for (const m of html.matchAll(/url\(\s*["']?((?:https?:)?\/\/[^)"']+)/gi)) add('offline', `CSS url() to external resource ${m[1]} — vendor it`, m.index);
  for (const m of html.matchAll(/@import\s+(?:url\(\s*)?["']?((?:https?:)?\/\/[^)"';\s]+)/gi)) add('offline', `CSS @import of external resource ${m[1]} — vendor it`, m.index);
  // the dark-background logo on dark compositions, the light-background logo on light ones (launch films)
  const theme = html.match(/<html[^>]*data-theme="(light|dark)"/)?.[1] || 'dark';
  const LOGO = `(?:${reEsc(brand.logo('dark'))}|${reEsc(brand.logo('light'))})`;
  for (const id of ['i-logo', 'o-logo']) if (!new RegExp(`id="${id}"[^>]*${LOGO}|${LOGO}[^>]*id="${id}"`).test(html)) findings.push({ rule: 'logo', line: 0, message: `${brand.name} logo missing on ${id === 'i-logo' ? 'intro' : 'outro'} (#${id})` });
  const wrong = brand.logo(theme === 'light' ? 'dark' : 'light');
  if (brand.logo('dark') !== brand.logo('light') && html.includes(wrong)) findings.push({ rule: 'logo', line: 0, message: `${wrong} is the ${theme === 'light' ? 'dark' : 'light'}-background logo, but this composition is ${theme}: use ${brand.logo(theme)}` });
  return findings;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const a = process.argv.slice(2), file = a.find((x) => !x.startsWith('--') && a[a.indexOf(x) - 1] !== '--brand');
  const bi = a.indexOf('--brand'); const brand = loadBrand(bi >= 0 ? resolve(a[bi + 1]) : undefined);
  if (!file) { console.error('usage: brand-lint.mjs <index.html> [--brand dir] [--json]'); process.exit(2); }
  const f = lintProject(resolve(file), brand);
  if (a.includes('--json')) console.log(JSON.stringify(f, null, 2));
  else if (f.length) { for (const x of f) console.error(`brand-lint: [${x.rule}] line ${x.line}: ${x.message}`); console.error(`brand-lint: ${f.length} finding(s)`); }
  else console.log('brand-lint: clean');
  process.exit(f.length ? 1 : 0);
}
