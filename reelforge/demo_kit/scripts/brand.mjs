// brand — load the active brand pack and stage it (plus GSAP) into a HyperFrames project.
//
// A brand pack is a folder with brand.json, tokens.css, fonts.css, fonts/ and the logo files brand.json names.
// Which pack is active, first hit wins:
//   1. $DEMO_BRAND_DIR
//   2. ~/.config/record-demo/brand   (where `init-brand.mjs` puts yours, so plugin updates never overwrite it)
//   3. <kit>/brand                   (the neutral example brand that ships with the kit)
import { readFileSync, existsSync, mkdirSync, cpSync, copyFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

export const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const USER_BRAND_DIR = join(homedir(), '.config', 'record-demo', 'brand');

export function brandDir() {
  for (const d of [process.env.DEMO_BRAND_DIR, USER_BRAND_DIR, join(KIT, 'brand')]) if (d && existsSync(join(d, 'brand.json'))) return resolve(d);
  throw new Error('no brand pack found (expected brand.json in $DEMO_BRAND_DIR, ~/.config/record-demo/brand or the kit)');
}

export function loadBrand(dir = brandDir()) {
  const json = JSON.parse(readFileSync(join(dir, 'brand.json'), 'utf8'));
  for (const k of ['name', 'logo']) if (!json[k]) throw new Error(`brand.json is missing "${k}"`);
  for (const t of ['dark', 'light']) if (!json.logo[t]) throw new Error(`brand.json logo.${t} is missing`);
  for (const f of [json.logo.dark, json.logo.light, json.mark].filter(Boolean)) if (!existsSync(join(dir, f))) throw new Error(`brand file ${f} not found in ${dir}`);
  const tokensCss = readFileSync(join(dir, 'tokens.css'), 'utf8');
  const fontsCss = existsSync(join(dir, 'fonts.css')) ? readFileSync(join(dir, 'fonts.css'), 'utf8') : '';
  return {
    dir, json, name: json.name, tokensCss, fontsCss,
    // paths as the composition references them (the pack is copied to <project>/kit/brand)
    logo: (theme) => `kit/brand/${json.logo[theme === 'light' ? 'light' : 'dark']}`,
    mark: `kit/brand/${json.mark || json.logo.dark}`,
    stripes: (theme) => json.stripes?.[theme === 'dark' ? 'dark' : 'light'] || ['--brand-accent', '--brand-alt-1', '--brand-alt-2', theme === 'dark' ? '--brand-paper' : '--brand-ink'],
  };
}

// GSAP is not redistributed in this repo: it is fetched once (pinned) into a cache and copied into each project.
export function ensureGsap() {
  const kit = JSON.parse(readFileSync(join(KIT, 'kit.json'), 'utf8'));
  const v = kit.gsap || '3.14.2';
  const cache = join(process.env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'record-demo-kit', `gsap-${v}.min.js`);
  if (!existsSync(cache)) {
    mkdirSync(dirname(cache), { recursive: true });
    const r = spawnSync('curl', ['-fsSL', '-o', cache, `https://cdn.jsdelivr.net/npm/gsap@${v}/dist/gsap.min.js`], { encoding: 'utf8' });
    if (r.status !== 0 || !existsSync(cache)) throw new Error(`could not download GSAP ${v} (network needed on first use): ${(r.stderr || '').trim()}`);
  }
  return cache;
}

// Copy the brand pack and GSAP into <project>/kit so the composition renders offline.
export function stageKit(project, brand = loadBrand()) {
  const kitOut = join(project, 'kit');
  mkdirSync(join(kitOut, 'vendor'), { recursive: true });
  cpSync(brand.dir, join(kitOut, 'brand'), { recursive: true });
  copyFileSync(ensureGsap(), join(kitOut, 'vendor', 'gsap.min.js'));
}

// The block every composition embeds between /*kit:tokens:start*/ and /*kit:tokens:end*/.
export function tokenBlock(brand = loadBrand()) {
  return `${brand.fontsCss}\n${readFileSync(join(KIT, 'video.css'), 'utf8')}\n${brand.tokensCss}`;
}
