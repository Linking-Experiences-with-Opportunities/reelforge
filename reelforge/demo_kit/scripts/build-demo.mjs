#!/usr/bin/env node
// build-demo — turn a demo.json spec + beats.json + raw captures into a branded MP4 with HyperFrames.
//
//   node build-demo.mjs --spec demo.json --beats beats.json --project <dir> [--stage prepare|render|all] [--out final.mp4]
//
// stage=prepare  generate the project, hyperframes check, brand-lint, chapter snapshots (LOOK AT THE CONTACT SHEET before rendering)
// stage=render   render the prepared project to --out
// stage=all      both (default)
//
// Exit codes: 0 ok · 1 check/brand-lint failed · 2 bad input · 3 hyperframes/ffmpeg unavailable.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, existsSync, cpSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join, resolve, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { lintProject } from './brand-lint.mjs';
import { loadBrand, stageKit, tokenBlock } from './brand.mjs';

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const kit = JSON.parse(readFileSync(join(KIT, 'kit.json'), 'utf8'));

// ---------- args ----------
const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const specPath = opt('spec'), beatsPath = opt('beats'), projectDir = opt('project');
const stage = opt('stage', 'all'), outPath = opt('out');
if (!specPath || !projectDir) die(2, 'usage: build-demo.mjs --spec demo.json --project <dir> [--beats beats.json] [--stage prepare|render|all] [--out final.mp4]');
function die(code, msg) { console.error(`build-demo: ${msg}`); process.exit(code); }

const spec = JSON.parse(readFileSync(specPath, 'utf8'));
const specDir = dirname(resolve(specPath));
const project = resolve(projectDir);
const beats = beatsPath && existsSync(beatsPath) ? JSON.parse(readFileSync(beatsPath, 'utf8')) : { seats: {} };

// ---------- tokens (resolve colors from the design-system file, never hardcode hex) ----------
const BRAND_PACK = loadBrand();
const tokenCss = BRAND_PACK.tokensCss;
function tokenMap() {
  const m = {}; for (const [, k, v] of tokenCss.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) m[k] = v.trim();
  const dark = tokenCss.match(/\[data-theme="dark"\]\s*\{([^}]*)\}/)?.[1] || '';
  for (const [, k, v] of dark.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) m[k] = v.trim();
  return m;
}
const TOK = tokenMap();
const hex = (name) => { let v = TOK[name]; for (let i = 0; i < 8 && v && v.startsWith('var('); i++) v = TOK[v.match(/var\((--[a-z0-9-]+)/i)[1]]; if (!v || !v.startsWith('#')) die(2, `token ${name} does not resolve to a hex color`); return v; };
const C = { active: hex('--accent-active'), done: hex('--status-success'), idle: hex('--border-default') };

// ---------- spec normalisation ----------
const seatNames = Object.keys(spec.seats || {});
if (!seatNames.length) die(2, 'spec.seats is empty');
const chapters = spec.chapters || [];
if (!chapters.length) die(2, 'spec.chapters is empty');
const START_PAD = spec.startPad ?? 0.55, END_PAD = spec.endPad ?? 0.15;
const seatVideo = (seat) => { const p = spec.seats[seat]?.video; if (!p) die(2, `seat ${seat} has no video`); return p; };
// Assets are copied under names derived from the seat / segment, never from the source filename: two different
// captures both called main.mp4 (or clip.mp4) must not overwrite each other.
const seatAsset = (seat) => `assets/seat-${seat}${extname(seatVideo(seat))}`;

function beatVideoTime(seat, name) {
  const b = beats.seats?.[seat]?.beats?.find((x) => x.name === name);
  if (!b) die(2, `beat "${name}" not found for seat "${seat}" in beats.json`);
  return b.video;
}
function resolveTime(seat, v, pad) {
  if (typeof v === 'number') return v;
  if (typeof v === 'string') return beatVideoTime(seat, v) + pad;
  if (v && v.beat) return beatVideoTime(seat, v.beat) + (v.plus ?? 0);
  die(2, `bad time ${JSON.stringify(v)}`);
}
// script-time <-> video-time maps per seat (piecewise linear through that seat's own beats), for cross-seat picture-in-picture
function lerp(xs, ys, x) {
  if (xs.length < 2) return ys[0] + (x - xs[0]);
  const i = x <= xs[0] ? 0 : x >= xs[xs.length - 1] ? xs.length - 2 : xs.findLastIndex((v) => v <= x);
  return ys[i] + ((ys[i + 1] - ys[i]) * (x - xs[i])) / (xs[i + 1] - xs[i] || 1);
}
function otherTime(seat, other, v) {
  const a = beats.seats?.[seat]?.beats || [], b = beats.seats?.[other]?.beats || [];
  if (!a.length || !b.length) return v;
  const t = lerp(a.map((x) => x.video), a.map((x) => x.t), v);
  return Math.max(0, lerp(b.map((x) => x.t), b.map((x) => x.video), t));
}

const INTRO = spec.intro?.dur ?? kit.intro, OUTRO = spec.outro?.dur ?? kit.outro;
const segs = []; let cursor = INTRO; const chapWin = {};
chapters.forEach((ch, ci) => {
  for (const s of ch.segments || []) {
    const seat = s.seat || seatNames[0];
    const file = s.file || null; // standalone clip override (relative to spec)
    const from = resolveTime(seat, s.from, START_PAD);
    const to = s.dur != null ? from + s.dur : resolveTime(seat, s.to, -END_PAD);
    const rate = s.rate ?? 1;
    if (rate > kit.limits.maxRate) console.warn(`build-demo: warning: chapter ${ci + 1} rate ${rate} exceeds ${kit.limits.maxRate}x`);
    const md = to - from; if (!(md > 0.3)) die(2, `chapter ${ci + 1}: segment is empty (${from.toFixed(2)} -> ${to.toFixed(2)})`);
    const cd = md / rate;
    const others = seatNames.filter((n) => n !== seat);
    let pip = null;
    if (s.pip !== 'off' && others.length) {
      if (s.pip && typeof s.pip === 'object') pip = { seat: s.pip.seat, at: resolveTime(s.pip.seat, s.pip.at, START_PAD) };
      else pip = { seat: others[0], at: otherTime(seat, others[0], from) };
    }
    const k = segs.length;
    segs.push({ k, ch: ci + 1, seat, file, asset: file ? `assets/clip-${k}${extname(file)}` : null, ms: from, md, rate, cs: cursor, cd, pip });
    const w = chapWin[ci + 1]; chapWin[ci + 1] = w ? [Math.min(w[0], cursor), Math.max(w[1], cursor + cd)] : [cursor, cursor + cd];
    cursor += cd;
  }
});
const END = cursor, TOTAL = END + OUTRO;
if (TOTAL > kit.limits.walkthroughSeconds) console.warn(`build-demo: warning: ${TOTAL.toFixed(0)}s is over the ${kit.limits.walkthroughSeconds}s standard`);
if (chapters.length === 1 && TOTAL > kit.limits.singleFeatureSeconds) console.warn(`build-demo: warning: single-feature demo is ${TOTAL.toFixed(0)}s (standard is <= ${kit.limits.singleFeatureSeconds}s)`);
for (const [i, ch] of chapters.entries()) {
  if ((ch.title || '').trim().split(/\s+/).length > kit.limits.titleWords + 3) console.warn(`build-demo: warning: chapter ${i + 1} title is long`);
  if ((ch.sub || '').length > kit.limits.subChars + 20) console.warn(`build-demo: warning: chapter ${i + 1} sub is ${(ch.sub || '').length} chars (standard <= ${kit.limits.subChars})`);
}

// ---------- HTML ----------
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const f3 = (n) => Number(n).toFixed(3);
const twoSeat = seatNames.length > 1;
const srcOf = (seat, asset) => asset || seatAsset(seat);
const primary = segs.map((s) => `<div class="crop"><video id="p${s.k}" class="clip" src="${srcOf(s.seat, s.asset)}" data-start="${f3(s.cs)}" data-duration="${f3(s.cd)}" data-media-start="${f3(s.ms)}" data-playback-rate="${s.rate}" data-track-index="0" muted playsinline style="position:absolute;left:0;top:0;width:1440px;height:900px"></video></div>`).join('\n');
const pips = twoSeat ? segs.filter((s) => s.pip).map((s) => `<div class="pipcrop"><video id="q${s.k}" class="clip" src="${srcOf(s.pip.seat)}" data-start="${f3(s.cs)}" data-duration="${f3(s.cd)}" data-media-start="${f3(Math.max(0, s.pip.at))}" data-playback-rate="${s.rate}" data-track-index="1" muted playsinline style="position:absolute;left:0;top:0;width:320px;height:200px"></video></div>`).join('\n') : '';
const N = chapters.length;
const pad2 = (n) => String(n).padStart(2, '0');
const chapHtml = chapters.map((c, i) => `<div class="chap" id="ch${i + 1}"><div class="cnt mono">${pad2(i + 1)} / ${pad2(N)}</div><div class="ct">${esc(c.title)}</div><div class="cs">${esc(c.sub)}</div></div>`).join('');
const ticks = chapters.map((_, i) => `<i class="tick" id="tk${i + 1}"></i>`).join('');
const pills = twoSeat ? segs.map((s) => `<div class="pill mono" id="pl${s.k}">${esc(spec.seats[s.seat]?.label || s.seat)}</div>`).join('') : '';
const plabels = twoSeat ? segs.filter((s) => s.pip).map((s) => `<div class="plabel mono" id="pb${s.k}">${esc(spec.seats[s.pip.seat]?.label || s.pip.seat)} screen</div>`).join('') : '';
const outroChips = (spec.outro?.chips || []).map((c) => `<div class="chip">${esc(c)}</div>`).join('');
const logo = (id) => `<img class="logo" id="${id}" src="${BRAND_PACK.logo('dark')}" alt="${esc(BRAND_PACK.name)}" />`;

const js = [];
js.push(`const tl = gsap.timeline({ paused: true }); const E = "expo.out";`);
js.push(`tl.fromTo("#i-logo,#i-eye",{opacity:0,y:14},{opacity:1,y:0,duration:.5,ease:E},.15);tl.fromTo("#i-title",{opacity:0,y:34},{opacity:1,y:0,duration:.7,ease:E},.3);tl.fromTo("#i-sub",{opacity:0,y:20},{opacity:1,y:0,duration:.6,ease:E},.75);tl.to("#intro",{opacity:0,duration:.5,ease:"power2.in"},${f3(INTRO - 0.5)});`);
js.push(`tl.fromTo("#chrome",{opacity:0},{opacity:1,duration:.6},${f3(INTRO - 0.4)});`);
for (const [n, [a, b]] of Object.entries(chapWin)) {
  js.push(`tl.fromTo("#ch${n}",{opacity:0,y:12},{opacity:1,y:0,duration:.35,ease:E},${f3(a)});tl.to("#ch${n}",{opacity:0,duration:.25},${f3(b - 0.25)});`);
  js.push(`tl.to("#tk${n}",{backgroundColor:"${C.active}",duration:.2},${f3(a)});tl.to("#tk${n}",{backgroundColor:"${C.done}",duration:.3},${f3(b)});`);
}
if (twoSeat) for (const s of segs) {
  const sel = s.pip ? `#pl${s.k},#pb${s.k}` : `#pl${s.k}`;
  js.push(`tl.fromTo("${sel}",{opacity:0},{opacity:1,duration:.25},${f3(s.cs)});tl.to("${sel}",{opacity:0,duration:.15},${f3(s.cs + s.cd - 0.15)});`);
}
js.push(`tl.to("#chrome",{opacity:0,duration:.5,ease:"power2.in"},${f3(END - 0.1)});`);
js.push(`tl.fromTo("#o-logo,#o-eye",{opacity:0,y:14},{opacity:1,y:0,duration:.5,ease:E},${f3(END + 0.4)});tl.fromTo("#o-title",{opacity:0,y:30},{opacity:1,y:0,duration:.7,ease:E},${f3(END + 0.55)});tl.fromTo(".chip",{opacity:0,y:16},{opacity:1,y:0,duration:.45,stagger:.09,ease:E},${f3(END + 1.1)});`);
js.push(`window.__timelines["main"] = tl; tl.seek(0);`);

const html = `<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="UTF-8" /><meta name="viewport" content="width=${kit.canvas.width}, height=${kit.canvas.height}" />
<script src="kit/vendor/gsap.min.js"></script>
<style>
/*kit:tokens:start*/
${tokenBlock(BRAND_PACK)}
/*kit:tokens:end*/
${readFileSync(join(KIT, 'template/composition.css'), 'utf8')}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="${f3(TOTAL)}" data-width="${kit.canvas.width}" data-height="${kit.canvas.height}">
${primary}
${pips}
<div id="chrome" class="abs" style="inset:0;pointer-events:none">
  <div id="hdr-l" class="abs mono">${esc(spec.header || spec.intro?.title || spec.feature)}</div>
  <div id="hdr-r" class="abs"><img src="${BRAND_PACK.logo('dark')}" alt="${esc(BRAND_PACK.name)}" /></div>
  ${spec.footnote ? `<div id="foot" class="abs mono">${esc(spec.footnote)}</div>` : ''}
  <div id="frame" class="abs"></div>${twoSeat ? '<div id="pipframe" class="abs"></div>' : ''}
  <div id="ticks" class="abs">${ticks}</div>
  ${pills}
  ${chapHtml}
  ${plabels}
</div>
<div id="intro" class="clip center" data-start="0" data-duration="${f3(INTRO)}" data-track-index="5">${logo('i-logo')}<div id="i-eye" class="eyebrow mono">${esc(spec.intro?.eyebrow)}</div><div id="i-title" class="big">${esc(spec.intro?.title)}</div><div id="i-sub" class="sub">${esc(spec.intro?.subtitle)}</div></div>
<div id="outro" class="clip center" data-start="${f3(END - 0.2)}" data-duration="${f3(OUTRO + 0.2)}" data-track-index="6">${logo('o-logo')}<div id="o-eye" class="eyebrow mono">${esc(spec.outro?.eyebrow)}</div><div id="o-title" class="big" style="font-size:104px">${esc(spec.outro?.title)}</div><div class="chips">${outroChips}</div></div>
</div>
<script>
${js.join('\n')}
</script></body></html>
`;

// ---------- project scaffold ----------
function hf(a, opts = {}) {
  const r = spawnSync('npx', ['-y', `hyperframes@${kit.hyperframes}`, ...a], { cwd: opts.cwd || project, encoding: 'utf8', maxBuffer: 1 << 28 });
  return { code: r.status ?? 1, out: `${r.stdout || ''}${r.stderr || ''}` };
}
function prepare() {
  if (!existsSync(join(project, 'hyperframes.json'))) {
    mkdirSync(dirname(project), { recursive: true });
    const r = hf(['init', project, '--non-interactive', '--example=blank'], { cwd: dirname(project) });
    if (r.code !== 0 || !existsSync(join(project, 'hyperframes.json'))) die(3, `hyperframes init failed:\n${r.out.slice(-600)}`);
  }
  mkdirSync(join(project, 'assets'), { recursive: true });
  for (const seat of seatNames) copyFileSync(resolve(specDir, seatVideo(seat)), join(project, seatAsset(seat)));
  for (const s of segs) if (s.file) copyFileSync(resolve(specDir, s.file), join(project, s.asset));
  mkdirSync(join(project, 'kit'), { recursive: true });
  stageKit(project, BRAND_PACK);
  writeFileSync(join(project, 'index.html'), html);
  writeFileSync(join(project, 'demo.json'), JSON.stringify(spec, null, 2));

  const findings = lintProject(join(project, 'index.html'), BRAND_PACK);
  const chk = hf(['check']);
  const passed = /Check passed/.test(chk.out);
  const at = [Math.min(1.6, INTRO - 0.4), ...Object.values(chapWin).map(([a, b]) => (a + b) / 2), END + 1.6].map((x) => x.toFixed(1)).join(',');
  rmSync(join(project, 'snapshots'), { recursive: true, force: true }); // stale sheets from an earlier run must never look current
  const snap = hf(['snapshot', '--at', at]);
  const sheets = existsSync(join(project, 'snapshots')) ? readdirSync(join(project, 'snapshots')).filter((f) => /^contact-sheet/.test(f)).map((f) => join(project, 'snapshots', f)) : [];
  const summary = { project, duration: Number(TOTAL.toFixed(2)), chapters: N, seats: seatNames, hyperframes: kit.hyperframes, check: passed ? 'passed' : 'FAILED', brandLint: findings.length ? findings : 'clean', contactSheets: sheets };
  console.log(JSON.stringify(summary, null, 2));
  if (!passed) { console.error(chk.out.slice(-1500)); process.exit(1); }
  if (findings.length) process.exit(1);
  if (snap.code !== 0 || !sheets.length) { console.error(`build-demo: snapshot step failed (exit ${snap.code}, ${sheets.length} contact sheets) — the visual review cannot be done, so this is not a passing prepare:\n${snap.out.slice(-800)}`); process.exit(1); }
}
function render() {
  const out = resolve(outPath || join(process.env.HOME, 'Documents/Demos/features', `${spec.feature}-walkthrough.mp4`));
  mkdirSync(dirname(out), { recursive: true });
  const r = hf(['render', '--output', out, '--workers', '4']);
  if (r.code !== 0 || !existsSync(out)) { console.error(r.out.slice(-1200)); die(1, 'render failed'); }
  const pr = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=width,height,duration', '-of', 'csv=p=0', out], { encoding: 'utf8' });
  console.log(JSON.stringify({ rendered: out, probe: pr.stdout.trim() }));
}
if (stage === 'prepare' || stage === 'all') prepare();
if (stage === 'render' || stage === 'all') render();
