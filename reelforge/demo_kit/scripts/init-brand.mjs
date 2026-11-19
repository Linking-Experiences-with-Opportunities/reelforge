#!/usr/bin/env node
// init-brand — copy the example brand pack somewhere you own, so you can make it yours.
//   node init-brand.mjs [--out <dir>] [--name "Acme"] [--force]
// Default --out is ~/.config/record-demo/brand, which the kit picks up automatically (plugin updates never touch it).
// Then edit brand.json (name, logo files), tokens.css (colors), fonts.css + fonts/ (typefaces) and replace the SVG logos.
import { cpSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { KIT, USER_BRAND_DIR, loadBrand } from './brand.mjs';

const a = process.argv.slice(2);
const opt = (n) => { const i = a.indexOf(`--${n}`); return i >= 0 ? a[i + 1] : undefined; };
const out = resolve(opt('out') || USER_BRAND_DIR);
if (existsSync(join(out, 'brand.json')) && !a.includes('--force')) {
  console.error(`init-brand: ${out} already has a brand pack — edit it, or pass --force to start over`);
  process.exit(1);
}
cpSync(join(KIT, 'brand'), out, { recursive: true });
const name = opt('name');
if (name) {
  const f = join(out, 'brand.json');
  const j = JSON.parse(readFileSync(f, 'utf8'));
  j.name = name;
  writeFileSync(f, JSON.stringify(j, null, 2) + '\n');
}
loadBrand(out);   // validates the copy
console.log(`init-brand: brand pack at ${out}`);
console.log('next: edit brand.json, tokens.css, fonts.css + fonts/, and replace the logo SVGs; then run selftest.mjs --quick');
if (out !== USER_BRAND_DIR) console.log(`note: point the kit at it with DEMO_BRAND_DIR="${out}"`);
