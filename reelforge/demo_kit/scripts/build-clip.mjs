#!/usr/bin/env node
// build-clip — turn a hi-res capture + focus events into a clean, zoomed 16:10 clip for the changelog (and, later, reels).
//
//   node build-clip.mjs --spec clip.json --beats beats.json --project <dir> [--stage prepare|render|all] [--out clip.mp4] [--poster poster.jpg]
//
// Why zoom: a changelog clip is shown ~645 CSS px wide (about 300 px for breakout clips). A full 1440px window shrunk to that is unreadable,
// so the camera follows the action and every shot shows at most `maxWidth` CSS px of the app (default 900, use 500 for breakout clips).
// The capture must be hi-res (capture-kit `hires: true`, 2880x1800) so the zoom stays sharp.
//
// Exit codes: 0 ok · 1 check/lint failed · 2 bad input · 3 hyperframes/ffmpeg unavailable.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, existsSync, cpSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, resolve, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { lintProject } from './brand-lint.mjs';
import { loadBrand, stageKit, tokenBlock } from './brand.mjs';

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const kit = JSON.parse(readFileSync(join(KIT, 'kit.json'), 'utf8'));
const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(`--${n}`); return i >= 0 ? args[i + 1] : d; };
const die = (code, msg) => { console.error(`build-clip: ${msg}`); process.exit(code); };
const specPath = opt('spec'), beatsPath = opt('beats'), projectDir = opt('project'), stage = opt('stage', 'all');
if (!specPath || !projectDir) die(2, 'usage: build-clip.mjs --spec clip.json --beats beats.json --project <dir> [--stage prepare|render|all] [--out clip.mp4] [--poster poster.jpg]');

const spec = JSON.parse(readFileSync(specPath, 'utf8'));
const specDir = dirname(resolve(specPath)), project = resolve(projectDir);
const beats = beatsPath && existsSync(beatsPath) ? JSON.parse(readFileSync(beatsPath, 'utf8')) : { seats: {}, focus: [] };
const seat = spec.seat || Object.keys(beats.seats)[0] || 'main';
const CW = spec.canvas?.width ?? 1440, CH = spec.canvas?.height ?? 900;         // output canvas (16:10 by default: same ratio as the old clips)
const VW = spec.source?.width ?? 1440, VH = spec.source?.height ?? 900;         // captured viewport in CSS px
const K = spec.source?.scale ?? 2;                                              // capture device-pixel ratio
const FLASH = 4;                                                                // capture-kit's sync strip (CSS px) at the bottom edge: cropped
const maxW = Math.min(spec.maxWidth ?? kit.clip?.maxWidth ?? 900, VW), minW = Math.min(spec.minWidth ?? 560, maxW);
const MOVE = 0.6;

const BRAND_PACK = loadBrand();
const tokenCss = BRAND_PACK.tokensCss;

// ---------- time helpers ----------
const seatBeats = beats.seats?.[seat]?.beats || [];
const beatVideo = (name) => { const b = seatBeats.find((x) => x.name === name); if (!b) die(2, `beat "${name}" not found for seat "${seat}"`); return b.video; };
const at = (v, pad = 0) => typeof v === 'number' ? v : typeof v === 'string' ? beatVideo(v) + pad : v && v.beat ? beatVideo(v.beat) + (v.plus ?? 0) : die(2, `bad time ${JSON.stringify(v)}`);
const dur = beats.seats?.[seat]?.duration ?? 0;
const from = spec.from == null ? (seatBeats[0] ? seatBeats[0].video + 0.55 : 0) : at(spec.from, 0.55);
const to = spec.dur != null ? from + spec.dur : spec.to == null ? Math.max(from + 1, dur - 0.2) : at(spec.to, -0.15);
if (!(to - from > 1)) die(2, `clip is empty (${from.toFixed(2)} -> ${to.toFixed(2)})`);
const D = to - from;

// ---------- camera ----------
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function windowFor(r) {
  const W = clamp(r.w * 2 + 240, Math.max(minW, r.w + 100), maxW), H = W * CH / CW;
  return { x: clamp(r.x + r.w / 2 - W / 2, 0, VW - W), y: clamp(r.y + r.h / 2 - H / 2, 0, VH - FLASH - H), w: W, h: H };
}
const wide = { x: 0, y: 0, w: VW, h: VW * CH / CW };
let shots = [];        // { t, rect }
let rings = [];        // { t, x, y, w, h }
const LOOKBACK = 0.7;   // a cam()/look() issued right after the start beat sits ~0.55 s before `from`; anything earlier belongs to the previous scene and must not direct this clip
const ev = (beats.focus || []).filter((f) => f.seat === seat && f.video >= from - LOOKBACK && f.video <= to);   // includes a cam() issued right at the beat (which is 0.55 s before `from`)
for (const f of ev) if ((f.kind === 'click' || f.kind === 'type') && f.video >= from) rings.push({ t: f.video - from, x: f.x, y: f.y, w: f.w, h: f.h, kind: f.kind });   // accent rings do not depend on who directs the camera; only actions inside the clip get one (the short lookback is for a cam() issued at the beat)
if (spec.camera) {
  shots = spec.camera.map((c) => ({ t: at(c.at) - from, rect: c.rect ? { x: c.rect[0], y: c.rect[1], w: c.rect[2], h: c.rect[3] } : c.wide ? wide : windowFor(c.target) }));
} else {
  const directed = ev.filter((f) => f.kind === 'cam' || f.kind === 'look');   // the capture script said where to look: trust it over inference
  const startWide = spec.wideStart ?? 0.9;
  shots.push({ t: 0, rect: wide });
  let prev = wide;
  if (directed.length) {
    for (const f of directed) {
      const w = f.kind === 'cam' ? { x: clamp(f.x, 0, VW - f.w), y: clamp(f.y, 0, VH - FLASH - f.w * CH / CW), w: f.w, h: f.w * CH / CW } : windowFor(f);
      const rel = f.video - from - 0.15;
      if (rel <= 0) { shots.length = 0; shots.push({ t: 0, rect: w }); prev = w; continue; }   // asked for before the clip starts: that is the opening framing (no wide intro)
      shots.push({ t: Math.max(startWide, rel), rect: w }); prev = w;
    }
  } else {
    const targets = []; // consecutive events on the same element collapse into one camera target
    for (const f of ev) {
      const last = targets[targets.length - 1];
      const same = last && Math.abs(last.x - f.x) < 6 && Math.abs(last.y - f.y) < 6 && Math.abs(last.w - f.w) < 6;
      if (!same) targets.push({ ...f });
    }
    for (const tg of targets) {
      const w = windowFor(tg), t = Math.max(startWide, tg.video - from);
      const still = Math.abs((w.x + w.w / 2) - (prev.x + prev.w / 2)) < prev.w * 0.18 && Math.abs((w.y + w.h / 2) - (prev.y + prev.h / 2)) < prev.h * 0.18 && Math.abs(w.w - prev.w) < prev.w * 0.25;
      const inside = tg.x >= prev.x + 20 && tg.y >= prev.y + 20 && tg.x + tg.w <= prev.x + prev.w - 20 && tg.y + tg.h <= prev.y + prev.h - 20;
      if (prev !== wide && (still || inside)) continue; // already framed
      shots.push({ t, rect: w }); prev = w;
    }
  }
  if ((spec.wideEnd ?? 0) > 0) shots.push({ t: D - spec.wideEnd, rect: wide });
}
shots.sort((a, b) => a.t - b.t);
for (const s of shots) if (s.rect.w > maxW + 0.5 && s.rect !== wide) die(1, `shot at ${s.t.toFixed(1)}s shows ${Math.round(s.rect.w)} CSS px (> ${maxW}): too small to read at 645 px`);
const zoomed = shots.filter((s) => s.rect.w <= maxW + 0.5).length;
if (!zoomed) console.warn('build-clip: warning: no zoomed shots (no focus events in range): the clip is a full-window view and will be hard to read at 645 px');

// ---------- composition ----------
const hex = (name) => { let v = tokenCss.match(new RegExp(`${name}\\s*:\\s*([^;]+);`))?.[1]?.trim(); for (let i = 0; i < 8 && v && v.startsWith('var('); i++) v = tokenCss.match(new RegExp(`${v.match(/var\((--[a-z0-9-]+)/i)[1]}\\s*:\\s*([^;]+);`))?.[1]?.trim(); if (!v || !v.startsWith('#')) die(2, `token ${name} unresolved`); return v; };
const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).join(',');
const BLUE = hex('--brand-accent');
const f3 = (n) => Number(n).toFixed(3);
const tx = (r) => ({ s: CW / (r.w * K), x: -r.x * CW / r.w, y: -r.y * CW / r.w });   // source is VW*K device px wide; region r (CSS px) -> canvas
const ringDivs = rings.map((r, i) => `<div class="ring" id="rg${i}" style="left:${(r.x - 6) * K}px;top:${(r.y - 6) * K}px;width:${(r.w + 12) * K}px;height:${(r.h + 12) * K}px"></div>`).join('\n');
const js = [`const tl = gsap.timeline({ paused: true });`];
const t0 = tx(shots[0].rect);
js.push(`tl.set("#cam",{x:${f3(t0.x)},y:${f3(t0.y)},scale:${t0.s.toFixed(5)},transformOrigin:"0 0"},0);`);
for (const s of shots.slice(1)) { const t = tx(s.rect); js.push(`tl.to("#cam",{x:${f3(t.x)},y:${f3(t.y)},scale:${t.s.toFixed(5)},duration:${MOVE},ease:"power3.inOut"},${f3(Math.max(0, s.t))});`); }
if (spec.ring !== false) rings.forEach((r, i) => js.push(`tl.fromTo("#rg${i}",{opacity:0,scale:.96},{opacity:1,scale:1,duration:.25,ease:"expo.out",transformOrigin:"50% 50%"},${f3(Math.max(0, r.t + 0.35))});tl.to("#rg${i}",{opacity:0,duration:.35},${f3(Math.max(0, r.t + 1.3))});`));
js.push('window.__timelines["main"] = tl; tl.seek(0);');

const html = `<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="UTF-8" /><meta name="viewport" content="width=${CW}, height=${CH}" />
<script src="kit/vendor/gsap.min.js"></script>
<style>
/*kit:tokens:start*/
${tokenBlock(BRAND_PACK)}
/*kit:tokens:end*/
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { margin: 0; width: ${CW}px; height: ${CH}px; overflow: hidden; background: var(--bg-page); }
#root { position: relative; width: 100%; height: 100%; overflow: hidden; background: var(--bg-page); }
/* the source is ${VW * K}x${VH * K} device px; the bottom ${FLASH * K}px hold capture-kit's sync strip and are cropped away */
#cam { position: absolute; left: 0; top: 0; width: ${VW * K}px; height: ${(VH - FLASH) * K}px; overflow: hidden; }
.ring { position: absolute; border: ${3 * K}px solid var(--brand-accent); border-radius: ${10 * K}px; box-shadow: 0 0 0 ${5 * K}px rgba(${rgb(BLUE)},0.22); opacity: 0; }
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="${f3(D)}" data-width="${CW}" data-height="${CH}">
<div id="cam">
<video id="src" class="clip" src="assets/source${extname(spec.video || '.mp4') || '.mp4'}" data-start="0" data-duration="${f3(D)}" data-media-start="${f3(from)}" data-track-index="0" muted playsinline style="position:absolute;left:0;top:0;width:${VW * K}px;height:${VH * K}px"></video>
${spec.ring === false ? '' : ringDivs}
</div>
</div>
<script>
${js.join('\n')}
</script></body></html>
`;

const hf = (a, cwd = project) => { const r = spawnSync('npx', ['-y', `hyperframes@${kit.hyperframes}`, ...a], { cwd, encoding: 'utf8', maxBuffer: 1 << 28 }); return { code: r.status ?? 1, out: `${r.stdout || ''}${r.stderr || ''}` }; };
const meta = { seat, from: Number(from.toFixed(2)), to: Number(to.toFixed(2)), duration: Number(D.toFixed(2)), canvas: [CW, CH], maxWidth: maxW, shots: shots.map((s) => ({ t: Number(s.t.toFixed(2)), x: Math.round(s.rect.x), y: Math.round(s.rect.y), w: Math.round(s.rect.w), h: Math.round(s.rect.h) })), rings: rings.length };

function prepare() {
  if (!existsSync(join(project, 'hyperframes.json'))) {
    mkdirSync(dirname(project), { recursive: true });
    const r = hf(['init', project, '--non-interactive', '--example=blank'], dirname(project));
    if (r.code !== 0 || !existsSync(join(project, 'hyperframes.json'))) die(3, `hyperframes init failed:\n${r.out.slice(-600)}`);
  }
  mkdirSync(join(project, 'assets'), { recursive: true });
  copyFileSync(resolve(specDir, spec.video), join(project, 'assets', `source${extname(spec.video)}`));
  mkdirSync(join(project, 'kit'), { recursive: true });
  stageKit(project, BRAND_PACK);
  writeFileSync(join(project, 'index.html'), html);
  writeFileSync(join(project, 'clip.meta.json'), JSON.stringify(meta, null, 2));
  const findings = lintProject(join(project, 'index.html'), BRAND_PACK).filter((f) => f.rule !== 'logo');   // clips carry no logo by design
  const chk = hf(['check']);
  const passed = /Check passed/.test(chk.out);
  const times = [...new Set([...shots.map((s) => Math.min(D - 0.2, s.t + MOVE + 0.5)), D - 0.3])].map((x) => Math.max(0.1, x).toFixed(1)).join(',');
  rmSync(join(project, 'snapshots'), { recursive: true, force: true });
  const snap = hf(['snapshot', '--at', times]);
  const sheets = existsSync(join(project, 'snapshots')) ? readdirSync(join(project, 'snapshots')).filter((f) => /^contact-sheet/.test(f)).map((f) => join(project, 'snapshots', f)) : [];
  console.log(JSON.stringify({ project, duration: meta.duration, shots: meta.shots.length, zoomed, rings: meta.rings, check: passed ? 'passed' : 'FAILED', brandLint: findings.length ? findings : 'clean', contactSheets: sheets }, null, 2));
  if (!passed) { console.error(chk.out.slice(-1500)); process.exit(1); }
  if (findings.length) process.exit(1);
  if (snap.code !== 0 || !sheets.length) { console.error(`build-clip: snapshot step failed — the visual review cannot be done:\n${snap.out.slice(-600)}`); process.exit(1); }
}

function render() {
  const out = resolve(opt('out') || join(project, `${spec.feature || 'clip'}.mp4`)), poster = resolve(opt('poster') || out.replace(/\.mp4$/, '-poster.jpg'));
  mkdirSync(dirname(out), { recursive: true });
  const raw = join(project, 'renders-raw.mp4');
  const r = hf(['render', '--output', raw, '--fps', '30', '--crf', String(kit.clip?.crf ?? 20), '--video-frame-format', 'png', '--workers', '4']);
  if (r.code !== 0 || !existsSync(raw)) { console.error(r.out.slice(-1200)); die(1, 'render failed'); }
  const fs1 = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', raw, '-c', 'copy', '-movflags', '+faststart', out]);
  if (fs1.status !== 0) die(3, 'ffmpeg faststart failed');
  const pAt = spec.poster?.at === 'end' ? Math.max(0, D - 0.4) : typeof spec.poster?.at === 'number' ? spec.poster.at : Math.min(D - 0.3, D * 0.72);
  spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', String(pAt), '-i', out, '-frames:v', '1', '-q:v', '3', poster]);
  const pr = spawnSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate:format=duration,bit_rate,size', '-of', 'json', out], { encoding: 'utf8' });
  const j = JSON.parse(pr.stdout || '{}'); const st = j.streams?.[0] || {}, fm = j.format || {};
  const res = { rendered: out, poster, width: st.width, height: st.height, fps: st.r_frame_rate, seconds: Number(fm.duration), kbps: Math.round(Number(fm.bit_rate) / 1000), kb: Math.round(Number(fm.size) / 1024) };
  rmSync(raw, { force: true });
  console.log(JSON.stringify(res));
  const problems = [];
  if (res.width !== CW || res.height !== CH) problems.push(`size ${res.width}x${res.height} != ${CW}x${CH}`);
  if (res.kbps < (kit.clip?.warnKbps ?? 250)) console.warn(`build-clip: warning: ${res.kbps} kbps is very low (the old clips were 43-150 and looked blocky): check the output by eye`);
  if (!existsSync(poster) || statSync(poster).size < 5000) problems.push('poster missing');
  if (problems.length) { console.error('build-clip: clip-lint failed: ' + problems.join('; ')); process.exit(1); }
}

if (stage === 'prepare' || stage === 'all') prepare();
if (stage === 'render' || stage === 'all') render();
