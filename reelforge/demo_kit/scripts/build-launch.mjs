#!/usr/bin/env node
// build-launch — turn a hi-res capture + a shot list into a launch film: a short, silent product announcement for LinkedIn / X / the site.
//
//   node build-launch.mjs --spec launch.json --beats beats.json --project <dir> [--stage prepare|render|all] [--out film.mp4] [--poster poster.jpg]
//
// What a launch film is (the look is modeled on YC-style product launch posts, measured from one frame by frame):
//   - one continuous "camera" over a clean light canvas: the product UI floats on it as a card (no browser chrome) and the camera eases
//     between framings — 0.8 s moves, then it HOLDS while something happens (typing, a click, a result). No hard cuts anywhere.
//   - shot vocabulary: `screen` (camera over the floating UI), `focus` (one live UI element isolated and big on the bare canvas),
//     `title` (a kinetic line), `card` (a designed callout that restates a real result), `device` (a phone capture in a bezel).
//   - a branded opener (brand logo × feature, built word by word) and a logo outro; the brand stripes (brand.json) sweep across
//     the frame between the opener, the product and the outro.
//   - one oversized arrow cursor, drawn by this builder from the capture's mouse path (capture with cursor: 'none'): it keeps one size at
//     every zoom, rides the camera so its tip stays on what it points at, enters from below, presses on every click and hands off
//     between shots.
// Exit codes: 0 ok · 1 check/lint failed · 2 bad input · 3 hyperframes/ffmpeg unavailable.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, existsSync, cpSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, resolve, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { lintProject } from './brand-lint.mjs';
import { loadBrand, stageKit, tokenBlock } from './brand.mjs';

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const kit = JSON.parse(readFileSync(join(KIT, 'kit.json'), 'utf8'));
const { ARROW_PATH, ARROW_TIP } = createRequire(import.meta.url)('./capture-kit.cjs');   // one arrow for the capture and the film
const L = kit.launch || {};
const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(`--${n}`); return i >= 0 ? args[i + 1] : d; };
const die = (code, msg) => { console.error(`build-launch: ${msg}`); process.exit(code); };
const warn = (msg) => console.warn(`build-launch: warning: ${msg}`);
const specPath = opt('spec'), beatsPath = opt('beats'), projectDir = opt('project'), stage = opt('stage', 'all');
if (!specPath || !projectDir) die(2, 'usage: build-launch.mjs --spec launch.json --beats beats.json --project <dir> [--stage prepare|render|all] [--out film.mp4] [--poster poster.jpg]');

const spec = JSON.parse(readFileSync(specPath, 'utf8'));
const specDir = dirname(resolve(specPath)), project = resolve(projectDir);
const beats = beatsPath && existsSync(beatsPath) ? JSON.parse(readFileSync(beatsPath, 'utf8')) : { seats: {}, focus: [] };
const THEME = spec.theme === 'dark' ? 'dark' : 'light';
const BRAND_PACK = loadBrand();
const LOGO = BRAND_PACK.logo(THEME);
const CW = spec.canvas?.width ?? kit.canvas.width, CH = spec.canvas?.height ?? kit.canvas.height;
const SRC = { width: spec.source?.width ?? 1440, height: spec.source?.height ?? 900, scale: spec.source?.scale ?? 2 };
const FLASH = 4;                                   // capture-kit's sync strip (CSS px) at the bottom edge: always cropped
const maxW = spec.maxWidth ?? L.maxWidth ?? 900;   // widest zoomed screen region (CSS px) — legibility in a phone-sized feed
const MOVE = L.move ?? 0.8, HOLD = L.minHold ?? 1.0;
const MAX_UP = L.maxUpscale ?? 1.4;                // the most a capture pixel may be blown up before the picture goes soft
const WIPE = 0.9, FADE_IN = 0.45, FADE_OUT = 0.35;
const f3 = (n) => Number(n).toFixed(3);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const words = (s) => String(s || '').trim().split(/\s+/).filter(Boolean);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ---------- tokens ----------
const tokenCss = BRAND_PACK.tokensCss;
const hex = (name) => { let v = tokenCss.match(new RegExp(`${name}\\s*:\\s*([^;]+);`))?.[1]?.trim(); for (let i = 0; i < 8 && v && v.startsWith('var('); i++) v = tokenCss.match(new RegExp(`${v.match(/var\((--[a-z0-9-]+)/i)[1]}\\s*:\\s*([^;]+);`))?.[1]?.trim(); if (!v || !v.startsWith('#')) die(2, `token ${name} unresolved`); return v; };
const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).join(',');
const BRAND = { accent: hex('--brand-accent'), alt1: hex('--brand-alt-1'), alt2: hex('--brand-alt-2'), ink: hex('--brand-ink'), paper: hex('--brand-paper') };
const STRIPES = BRAND_PACK.stripes(THEME).map((c) => (c.startsWith('--') ? hex(c) : c));

// ---------- time helpers (per seat; a shot may name its own seat) ----------
const seatOf = (s) => s.seat || spec.seat || Object.keys(beats.seats || {})[0] || 'main';
const beatVideo = (seat, name) => { const b = beats.seats?.[seat]?.beats?.find((x) => x.name === name); if (!b) die(2, `beat "${name}" not found for seat "${seat}"`); return b.video; };
const at = (seat, v, pad = 0) => typeof v === 'number' ? v : typeof v === 'string' ? beatVideo(seat, v) + pad : v && v.beat ? beatVideo(seat, v.beat) + (v.plus ?? 0) : die(2, `bad time ${JSON.stringify(v)}`);
const span = (s, i) => {
  const seat = seatOf(s), from = at(seat, s.from, 0.55), to = s.dur != null ? from + s.dur : at(seat, s.to, -0.15);
  if (!(to - from > 0.8)) die(2, `shot ${i + 1} (${s.type}) is empty (${from.toFixed(2)} -> ${to.toFixed(2)})`);
  return { seat, from, to };
};
const videoOf = (s) => s.video || spec.video || die(2, 'spec.video is missing');
const srcOf = (s) => ({ width: s.source?.width ?? SRC.width, height: s.source?.height ?? SRC.height, scale: s.source?.scale ?? SRC.scale });
const LOOKBACK = 0.7;
const eventsIn = (seat, from, to) => (beats.focus || []).filter((f) => f.seat === seat && f.video >= from - LOOKBACK && f.video <= to);

// ---------- text rules ----------
const opener = spec.opener || {};
const outro = spec.outro || {};
const OPEN_D = opener.dur ?? L.opener ?? 4.6, OUT_D = outro.dur ?? L.outro ?? 4.0;
const lockupWords = opener.words || [];
if (lockupWords.join(' ').split(/\s+/).length > (L.lockupWords ?? 4)) warn(`opener lockup "${lockupWords.join(' ')}" is long (≤ ${L.lockupWords ?? 4} words reads at a glance)`);
if (words(opener.line).length > (L.lineWords ?? 8)) warn(`opener line has ${words(opener.line).length} words (≤ ${L.lineWords ?? 8})`);
if (OPEN_D > 6.5) warn(`opener is ${OPEN_D}s — the product should be on screen within ~6 s`);

// ---------- shots ----------
const shots = (spec.shots || []).map((s, i) => ({ ...s, i }));
if (!shots.length) die(2, 'spec.shots is empty');
let cursor = OPEN_D;
const placed = [];
for (const s of shots) {
  const p = { ...s, cs: cursor };
  if (s.type === 'screen' || s.type === 'focus' || s.type === 'device') {
    const { seat, from, to } = span(s, s.i);
    Object.assign(p, { seat, from, to, cd: (to - from) / (s.rate ?? 1) });
    if ((s.rate ?? 1) > kit.limits.maxRate) warn(`shot ${s.i + 1} rate ${s.rate} exceeds ${kit.limits.maxRate}x`);
  } else if (s.type === 'title') {
    p.cd = s.dur ?? 2.6;
    if (words(s.text).length > (L.lineWords ?? 8)) warn(`title shot ${s.i + 1} has ${words(s.text).length} words (≤ ${L.lineWords ?? 8})`);
  } else if (s.type === 'card') {
    p.cd = s.dur ?? 3.0;
    if (words(s.title).length > (L.lineWords ?? 8)) warn(`card ${s.i + 1} title has ${words(s.title).length} words (≤ ${L.lineWords ?? 8})`);
    if ((s.sub || '').length > kit.limits.subChars) warn(`card ${s.i + 1} sub is ${(s.sub || '').length} chars (≤ ${kit.limits.subChars})`);
  } else die(2, `shot ${s.i + 1}: unknown type "${s.type}" (screen | focus | title | card | device)`);
  cursor += p.cd;
  placed.push(p);
}
const END = cursor, TOTAL = END + OUT_D;
if (TOTAL > (L.maxSeconds ?? 45)) warn(`film is ${TOTAL.toFixed(1)}s (launch standard ≤ ${L.maxSeconds ?? 45}s)`);

// ---------- narration rail: what is on screen, said beside it ----------
// say: { title, sub } at the shot's start · [{ at, title, sub }] where `at` is a source time (beat name, number, { beat, plus }) in footage
// shots or seconds into the shot otherwise · null hides the text. Footage shots without `say` keep the current line; title and card shots
// hide it (their own text is the line).
const FOOTAGE = new Set(['screen', 'focus', 'device']);
const sayEvents = [];
for (const p of placed) {
  const end = p.cs + p.cd - 0.5;
  const tAt = (item) => {
    if (item.at == null) return p.cs;
    const t = FOOTAGE.has(p.type) && typeof item.at !== 'number' ? p.cs + (at(p.seat, item.at) - p.from) / (p.rate ?? 1) : p.cs + Number(item.at);
    if (t < p.cs - 0.05 || t > end) warn(`shot ${p.i + 1}: narration "${item.title}" lands outside the shot — clamped`);
    return clamp(t, p.cs, Math.max(p.cs, end));
  };
  if (p.say === null || (p.say === undefined && !FOOTAGE.has(p.type))) { sayEvents.push({ t: p.cs, hide: true }); continue; }
  if (p.say === undefined) { sayEvents.push({ t: p.cs, restore: true }); continue; }   // footage keeps (or brings back) the current line
  for (const item of Array.isArray(p.say) ? p.say : [p.say]) {
    if (words(item.title).length > kit.limits.titleWords) warn(`narration "${item.title}" is ${words(item.title).length} words (≤ ${kit.limits.titleWords})`);
    if ((item.sub || '').length > kit.limits.subChars) warn(`narration "${item.title}" sub is ${(item.sub || '').length} chars (≤ ${kit.limits.subChars})`);
    sayEvents.push({ t: tAt(item), title: item.title, sub: item.sub || '' });
  }
}
sayEvents.sort((a, b) => a.t - b.t);
// Chapters are distinct lines (title AND sub: a new explanation under the same title is a new line), one tick each; showings are when
// each is on screen — a title or card hides the current line, and the next footage without its own `say` shows it again.
const lines = [], shows = [];
{
  let cur = -1, visible = false;
  const close = (t) => { if (visible) { shows[shows.length - 1].off = t; visible = false; } };
  for (const e of sayEvents) {
    if (e.hide) { close(e.t); continue; }
    if (e.restore) { if (cur >= 0 && !visible) { shows.push({ k: cur, on: e.t }); visible = true; } continue; }
    if (cur >= 0 && lines[cur].title === e.title && lines[cur].sub === e.sub) { if (!visible) { shows.push({ k: cur, on: e.t }); visible = true; } continue; }
    close(e.t);
    lines.push({ t: e.t, title: e.title, sub: e.sub }); cur = lines.length - 1;
    shows.push({ k: cur, on: e.t }); visible = true;
  }
  close(END - 0.45);
}
const RAIL = spec.rail !== false && lines.length > 0;
const RAIL_W = RAIL ? (spec.railWidth ?? L.railWidth ?? 420) : 0;
const SW = CW - RAIL_W;                                          // the stage the camera, focus, title and card shots use

// ---------- screen shots: world camera ----------
// World units are device px of the capture (scale K): the floating app card sits at [0,0]–[VW*K,(VH-FLASH)*K]. A camera rect is in CSS px.
function cameraFor(p) {
  const { width: VW, height: VH, scale: K } = srcOf(p);
  const AR = CH / SW;
  const wideW = VW * (L.wideMargin ?? 1.12), wide = { x: (VW - wideW) / 2, y: (VH - FLASH) / 2 - (wideW * AR) / 2, w: wideW, h: wideW * AR, wide: true };
  const minW = Math.min(p.minWidth ?? Math.max(520, Math.ceil(SW / (K * MAX_UP))), maxW);   // a look() framing never upscales past MAX_UP
  const windowFor = (r) => { const W = clamp(r.w * 1.9 + 200, Math.max(minW, r.w + 120), maxW), H = W * AR; return { x: clamp(r.x + r.w / 2 - W / 2, 0, VW - W), y: clamp(r.y + r.h / 2 - H / 2, 0, VH - FLASH - H), w: W, h: H }; };
  const D = p.cd;
  let list = [];
  if (p.camera) {
    list = p.camera.map((c, k) => {
      if (!c.wide && !c.rect) die(2, `shot ${p.i + 1}: camera[${k}] needs "rect": [x, y, w] (CSS px) or "wide": true`);
      return { t: (at(p.seat, c.at) - p.from) / (p.rate ?? 1), rect: c.wide ? wide : { x: c.rect[0], y: c.rect[1], w: c.rect[2], h: c.rect[2] * AR } };
    });
    if (!list.length) die(2, `shot ${p.i + 1}: "camera" is empty`);
  } else {
    const ev = eventsIn(p.seat, p.from, p.to).filter((f) => f.kind === 'cam' || f.kind === 'look');
    list.push({ t: 0, rect: p.wideStart === false ? null : wide });
    for (const f of ev) {
      const r = f.kind === 'cam' ? { x: clamp(f.x, 0, VW - f.w), y: clamp(f.y, 0, VH - FLASH - f.w * AR), w: f.w, h: f.w * AR } : windowFor(f);
      const rel = (f.video - p.from - 0.15) / (p.rate ?? 1);
      if (rel <= 0) { list = [{ t: 0, rect: r }]; continue; }                         // directed before the shot: that is the opening framing
      list.push({ t: rel, rect: r });
    }
    list = list.filter((x) => x.rect);
    if (!list.length) list.push({ t: 0, rect: wide });
  }
  list.sort((a, b) => a.t - b.t);
  // Rhythm: every framing holds at least HOLD after its move lands (the measured reference never moves again sooner). Push late, warn.
  for (let k = 1; k < list.length; k++) {
    const earliest = list[k - 1].t + (k - 1 ? MOVE : 0) + HOLD;
    if (list[k].t < earliest) { if (earliest - list[k].t > 0.5) warn(`shot ${p.i + 1}: camera move ${k} pushed ${(earliest - list[k].t).toFixed(1)}s later to keep a ${HOLD}s hold`); list[k].t = earliest; }
  }
  list = list.filter((x) => x.t < D - 0.4);
  for (const s of list) if (!s.rect.wide && s.rect.w > maxW + 0.5) die(1, `shot ${p.i + 1}: a camera framing shows ${Math.round(s.rect.w)} CSS px (> ${maxW}) — unreadable in a feed; split it into two framings`);
  if (list[0].rect.wide && (list[1]?.t ?? D) > (L.maxWideHold ?? 2.2)) warn(`shot ${p.i + 1}: the wide establishing framing holds ${((list[1]?.t ?? D)).toFixed(1)}s (≤ ${L.maxWideHold ?? 2.2}s; the UI is too small to read wide)`);
  if (!list.some((x) => !x.rect.wide)) warn(`shot ${p.i + 1}: no zoomed framing — add me.cam()/me.look() before the actions in the capture script`);
  return list;
}

// ---------- composition ----------
const assetName = (p) => `assets/${p.type}-${p.i}${extname(videoOf(p)) || '.mp4'}`;
const html = [];
const js = [`const tl = gsap.timeline({ paused: true }); const E = "expo.out"; const IO = "power3.inOut";`];
const blurIn = (sel, t, extra = '') => `tl.fromTo("${sel}",{opacity:0,y:18,filter:"blur(14px)"${extra ? ',' + extra : ''}},{opacity:1,y:0,filter:"blur(0px)",duration:.6,ease:E},${f3(t)});`;
const blurOut = (sel, t) => `tl.to("${sel}",{opacity:0,filter:"blur(10px)",y:-10,duration:${FADE_OUT},ease:"power2.in"},${f3(t)});`;

// ---------- the cursor (an oversized macOS-style arrow; one geometry and size for the whole film) ----------
const CUR_SIZE = Math.round(spec.cursor?.size ?? CW * (L.cursorSize ?? 0.052));
const CUR_LIGHT = (spec.cursor?.fill ?? (THEME === 'dark' ? 'light' : 'dark')) === 'light';
const ARROW = `<svg class="arrow${CUR_LIGHT ? ' light' : ''}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="${ARROW_PATH}"/></svg>`;
const TIP = `${(ARROW_TIP.x * 100).toFixed(1)}% ${(ARROW_TIP.y * 100).toFixed(1)}%`;
// A click: a quick squeeze pivoted on the tip, a slower release (1:2 reads as a real tap). Held presses stay squeezed until the release.
const pressJs = (sel, down, up) => `tl.to("${sel}",{scale:.84,duration:.1,ease:"power2.in",transformOrigin:"${TIP}"},${f3(down - 0.05)});tl.to("${sel}",{scale:1,duration:.22,ease:"power2.out",transformOrigin:"${TIP}"},${f3(Math.max(up, down + 0.05))});`;

// opener: wordmark × words (built word by word), then the one-line promise
const accent = opener.accent ?? lockupWords.length - 1;
html.push(`<div id="opener" class="stage"><div class="lockup"><img id="i-logo" class="wm" src="${LOGO}" alt="${esc(BRAND_PACK.name)}" />${lockupWords.length ? '<span class="x" id="ox">×</span>' : ''}<span class="lws">${lockupWords.map((w, k) => `<span class="lw${k === accent ? ' acc' : ''}" id="ow${k}">${esc(w)}</span>`).join(' ')}</span></div>${opener.line ? `<div class="oline" id="oline">${words(opener.line).map((w, k) => `<span class="olw" id="olw${k}">${esc(w)}</span>`).join(' ')}</div>` : ''}</div>`);
js.push(`tl.fromTo("#i-logo",{opacity:0,scale:.92,filter:"blur(12px)"},{opacity:1,scale:1,filter:"blur(0px)",duration:.7,ease:E},.2);`);
if (lockupWords.length) js.push(`tl.fromTo("#ox",{opacity:0},{opacity:1,duration:.3},.65);`);
lockupWords.forEach((_, k) => js.push(blurIn(`#ow${k}`, 0.75 + k * 0.38)));
const lineAt = Math.min(OPEN_D - 1.6, 1.0 + lockupWords.length * 0.38 + 0.5);
if (opener.line) {
  js.push(`tl.to(".lockup",{y:-${Math.round(CH * 0.07)},duration:.7,ease:IO},${f3(lineAt - 0.2)});`);
  words(opener.line).forEach((_, k) => js.push(blurIn(`#olw${k}`, lineAt + k * 0.07)));
}
js.push(blurOut('#opener', OPEN_D - FADE_OUT - 0.1));

// stripe wipe overlays at the opener→product and product→outro boundaries
const wipes = [OPEN_D, END];
wipes.forEach((b, w) => {
  html.push(`<div class="streaks" id="wp${w}">${STRIPES.map((c, k) => `<i style="background:${c}" id="wp${w}s${k}"></i>`).join('')}</div>`);
  STRIPES.forEach((_, k) => js.push(`tl.fromTo("#wp${w}s${k}",{x:${-Math.round(CW * 1.05)},opacity:1},{x:${Math.round(CW * 1.05)},duration:${WIPE},ease:"power3.inOut"},${f3(b - WIPE / 2 + k * 0.045)});tl.set("#wp${w}s${k}",{opacity:0},${f3(b + WIPE / 2 + k * 0.045 + 0.01)});`));
});

// shots
for (const p of placed) {
  const id = `s${p.i}`, t0 = p.cs, t1 = p.cs + p.cd;
  if (p.type === 'screen') {
    const { width: VW, height: VH, scale: K } = srcOf(p);
    const cam = cameraFor(p);
    p.camera = cam;
    const tx = (r) => ({ s: SW / (r.w * K), x: -r.x * SW / r.w, y: -r.y * SW / r.w });
    p.K = K; p.camTx = cam.map((c) => ({ t: c.t, ...tx(c.rect) }));
    const tight = Math.min(...cam.map((c) => c.rect.w));
    if (SW / (tight * K) > MAX_UP + 0.01) warn(`shot ${p.i + 1}: a ${Math.round(tight)} px framing blows the capture up ${(SW / (tight * K)).toFixed(2)}x — it reads soft; frame at least ${Math.ceil(SW / (K * MAX_UP))} px wide`);
    html.push(`<div class="stage" id="${id}"><div class="world" id="${id}w"><div class="appcard" style="width:${VW * K}px;height:${(VH - FLASH) * K}px;border-radius:${14 * K}px;box-shadow:0 ${24 * K}px ${64 * K}px rgba(${rgb(BRAND.ink)},.16),0 0 0 ${K}px var(--border-default)"><video class="clip" id="${id}v" src="${assetName(p)}" data-start="${f3(t0)}" data-duration="${f3(p.cd)}" data-media-start="${f3(p.from)}" data-playback-rate="${p.rate ?? 1}" data-track-index="${p.i + 1}" muted playsinline style="position:absolute;left:0;top:0;width:${VW * K}px;height:${VH * K}px"></video></div></div></div>`);
    const c0 = tx(cam[0].rect);
    js.push(`tl.set("#${id}w",{x:${f3(c0.x)},y:${f3(c0.y)},scale:${c0.s.toFixed(5)},transformOrigin:"0 0"},${f3(t0)});`);
    for (const c of cam.slice(1)) { const t = tx(c.rect); js.push(`tl.to("#${id}w",{x:${f3(t.x)},y:${f3(t.y)},scale:${t.s.toFixed(5)},duration:${MOVE},ease:IO},${f3(t0 + c.t)});`); }
    js.push(`tl.fromTo("#${id}",{opacity:0,scale:.965,filter:"blur(12px)"},{opacity:1,scale:1,filter:"blur(0px)",duration:${FADE_IN},ease:E,transformOrigin:"50% 50%"},${f3(t0)});`);
    js.push(blurOut(`#${id}`, t1 - FADE_OUT));
  } else if (p.type === 'focus' || p.type === 'device') {
    const { width: VW, height: VH, scale: K } = srcOf(p);
    let r;
    if (p.type === 'device') r = { x: 0, y: 0, w: VW, h: VH - FLASH };
    else if (p.rect) r = { x: p.rect[0], y: p.rect[1], w: p.rect[2], h: p.rect[3] };
    else {
      // The direction in force when the shot starts: its own look() lands just after its beat, so take the latest one up to shortly
      // after `from` — never an earlier shot's look() that merely falls inside the lookback window.
      const dirs = eventsIn(p.seat, p.from, p.to).filter((f) => f.kind === 'look' || f.kind === 'cam');
      const e = dirs.filter((f) => f.video <= p.from + 0.3).pop() || dirs[0];
      if (!e) die(2, `focus shot ${p.i + 1} needs "rect": [x,y,w,h] or a me.look()/me.cam() inside its time range`);
      r = { x: e.x, y: e.y, w: e.w, h: e.h };
    }
    const pad = p.type === 'device' ? 0 : (p.pad ?? 18);
    { const x = Math.max(0, r.x - pad), y = Math.max(0, r.y - pad); r = { x, y, w: Math.min(VW - x, r.x + r.w + pad - x), h: Math.min(VH - FLASH - y, r.y + r.h + pad - y) }; }   // padded, kept inside the capture
    const z = p.type === 'device' ? (CH * 0.82) / r.h : Math.min(p.zoom ?? K * MAX_UP, (SW * (p.maxFill ?? 0.62)) / r.w, (CH * 0.5) / r.h);
    if (p.type === 'focus' && z / K > MAX_UP + 0.01) warn(`focus shot ${p.i + 1}: zoom ${z.toFixed(2)} blows the capture up ${(z / K).toFixed(2)}x (> ${MAX_UP}x) — it reads soft; drop "zoom" or keep it ≤ ${(K * MAX_UP).toFixed(1)}`);
    if (p.type === 'focus' && r.w < 240 && r.h < 120) warn(`focus shot ${p.i + 1} isolates a ${Math.round(r.w)}x${Math.round(r.h)} px element — a lone button blown up reads as a sticker, not the product; show the press inside a screen framing of its toolbar or header and keep focus shots for panels`);
    p.zoom = Number(z.toFixed(2)); p.crop = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
    // A focus crop is a fixed rectangle: once a click inside it changes the layout (a button that opens a panel), whatever moves into
    // that spot shows instead. End the shot soon after the click.
    if (p.type === 'focus') {
      const ev = eventsIn(p.seat, p.from, p.to);
      const typedInto = (c) => ev.some((f) => f.kind === 'type' && f.video >= c.video && f.video - c.video < 1.2 && Math.abs(f.x - c.x) < 4 && Math.abs(f.y - c.y) < 4);   // me.type() clicks to focus first: not a layout change
      const hit = ev.find((f) => f.kind === 'click' && f.video >= p.from && !typedInto(f) && f.x < r.x + r.w && f.x + f.w > r.x && f.y < r.y + r.h && f.y + f.h > r.y);
      const released = hit ? hit.video + (hit.hold ?? 0) : 0;   // press() holds the mouse down: the layout moves at the release
      if (hit && p.to - released > 0.3) warn(`focus shot ${p.i + 1} runs ${(p.to - released).toFixed(1)}s past the click inside it — if that click changes the layout, the crop shows whatever moves into place; end it right after the click (dur ≈ ${(released - p.from + 0.15).toFixed(2)}), and capture it with me.press() so the press shows before the layout moves`);
    }
    if (p.type === 'focus' && z < 1.2) warn(`focus shot ${p.i + 1}: element is only shown at ${z.toFixed(2)}x — a focus shot should isolate something small`);
    const w = r.w * z, h = r.h * z, k = z / K;
    const frame = p.type === 'device'
      ? `<div class="phone" style="width:${f3(w + 36)}px;height:${f3(h + 36)}px;border-radius:${Math.round(56 * (h / 1600) + 40)}px">`
      : `<div class="focuscard" style="width:${f3(w)}px;height:${f3(h)}px;border-radius:${Math.round(14 * z)}px;box-shadow:0 ${Math.round(18 * z / 2)}px ${Math.round(60 * z / 2)}px rgba(${rgb(BRAND.ink)},.14),0 0 0 1px var(--border-default)">`;
    const inset = p.type === 'device' ? 18 : 0;
    p.geo = { x0: (SW - (p.type === 'device' ? w + 36 : w)) / 2 + inset, y0: (CH - (p.type === 'device' ? h + 36 : h)) / 2 + inset, z, r };   // where a capture point lands on the stage
    // The video stays at its native size and a wrapper is scaled with a transform (as the screen camera does): the renderer draws a
    // <video> at its intrinsic size, so sizing the element itself scales the picture wrongly (a focus shot then frames the wrong element).
    const crop = `<div class="crop" style="left:${inset}px;top:${inset}px;width:${f3(w)}px;height:${f3(h)}px;border-radius:${p.type === 'device' ? Math.round(56 * (h / 1600) + 22) : Math.round(14 * z)}px"><div class="vwrap" style="left:${f3(-r.x * z)}px;top:${f3(-r.y * z)}px;transform:scale(${k.toFixed(5)})"><video class="clip" id="${id}v" src="${assetName(p)}" data-start="${f3(t0)}" data-duration="${f3(p.cd)}" data-media-start="${f3(p.from)}" data-playback-rate="${p.rate ?? 1}" data-track-index="${p.i + 1}" muted playsinline style="position:absolute;left:0;top:0;width:${VW * K}px;height:${VH * K}px"></video></div></div>`;
    html.push(`<div class="stage" id="${id}">${p.caption ? `<div class="cap" id="${id}c">${esc(p.caption)}</div>` : ''}${frame}${crop}</div></div>`);
    js.push(`tl.fromTo("#${id}",{opacity:0,scale:.94,y:24,filter:"blur(14px)"},{opacity:1,scale:1,y:0,filter:"blur(0px)",duration:.6,ease:E,transformOrigin:"50% 50%"},${f3(t0)});`);
    if (p.caption) js.push(blurIn(`#${id}c`, t0 + 0.25));
    js.push(blurOut(`#${id}`, t1 - FADE_OUT));
  } else if (p.type === 'title') {
    html.push(`<div class="stage" id="${id}"><div class="tline">${words(p.text).map((w, k) => `<span class="${(p.accent || []).includes(k) ? 'acc' : ''}" id="${id}w${k}">${esc(w)}</span>`).join(' ')}</div>${p.sub ? `<div class="tsub" id="${id}sub">${esc(p.sub)}</div>` : ''}</div>`);
    js.push(`tl.set("#${id}",{opacity:1},${f3(t0)});`);
    words(p.text).forEach((_, k) => js.push(blurIn(`#${id}w${k}`, t0 + 0.1 + k * 0.09)));
    if (p.sub) js.push(blurIn(`#${id}sub`, t0 + 0.35 + words(p.text).length * 0.09));
    js.push(blurOut(`#${id}`, t1 - FADE_OUT));
  } else if (p.type === 'card') {
    const row = p.row ? `<div class="crow" id="${id}row"><img class="cmark" src="${BRAND_PACK.mark}" alt="" /><div class="cmeta"><div class="crt">${esc(p.row.label)}</div>${p.row.meta ? `<div class="crm">${esc(p.row.meta)}</div>` : ''}</div>${p.row.cta ? `<div class="ccta" id="${id}cta">${esc(p.row.cta)} ↗<span class="ptr" id="${id}p">${ARROW}</span></div>` : ''}</div>` : '';
    html.push(`<div class="stage" id="${id}"><div class="card" id="${id}k">${p.eyebrow ? `<div class="ceye mono" id="${id}e"><i class="dot"></i>${esc(p.eyebrow)}</div>` : ''}<div class="ctitle" id="${id}t">${esc(p.title)}</div>${p.sub ? `<div class="csub" id="${id}s">${esc(p.sub)}</div>` : ''}${row}</div></div>`);
    js.push(`tl.set("#${id}",{opacity:1},${f3(t0)});`);
    if (p.eyebrow) js.push(blurIn(`#${id}e`, t0 + 0.05));
    js.push(blurIn(`#${id}t`, t0 + 0.2));
    if (p.sub) js.push(blurIn(`#${id}s`, t0 + 0.4));
    if (p.row) js.push(blurIn(`#${id}row`, t0 + 0.6));
    if (p.row?.cta) {
      const press = Math.min(t1 - 0.8, t0 + 1.7);
      // the same arrow as the footage: in from below the stage, a tip-pivoted press that the button answers, then out the bottom again
      js.push(`tl.fromTo("#${id}p",{x:${Math.round(SW * 0.05)},y:${Math.round(CH * 0.62)}},{x:0,y:0,duration:.85,ease:"power3.out"},${f3(press - 0.9)});${pressJs(`#${id}p`, press, press + 0.12)}tl.to("#${id}cta",{scale:.96,duration:.12,yoyo:true,repeat:1},${f3(press)});tl.to("#${id}p",{x:${Math.round(SW * 0.04)},y:${Math.round(CH * 0.62)},duration:.6,ease:"power2.in"},${f3(Math.max(press + 0.45, t1 - 0.8))});`);
    }
    js.push(blurOut(`#${id}`, t1 - FADE_OUT));
  }
}

// ---------- the cursor track ----------
// The capture's own cursor is hidden (cursor: 'none'); its mouse path (beats.mouse) is replayed here through the same camera math as
// the footage, frame by frame, so the arrow keeps one size at every zoom while its tip stays on what it points at. It enters from below
// the stage, hands off between consecutive footage shots in one continuous glide, and leaves through the bottom before a title or card.
const mouse = {};
for (const e of beats.mouse?.events || []) (mouse[e.seat] ||= []).push(e);
for (const ev of Object.values(mouse)) ev.sort((a, b) => a.video - b.video);
const cursorOff = (() => {
  if (spec.cursor === false) return 'off in the spec';
  if (!placed.some((p) => p.type === 'screen' || p.type === 'focus')) return 'no screen or focus shots';
  if (!beats.mouse) { warn('beats.json has no mouse path, so the film has no cursor: capture with start({ cursor: "none" }) and run detect-beats with --mouse mouse.json'); return 'no mouse path'; }
  if (beats.mouse.cursor !== 'none') { warn(`the capture drew its own cursor ("${beats.mouse.cursor}") into the frames — not drawing a second one; recapture with start({ cursor: "none" }) for the launch cursor`); return `baked into the capture (${beats.mouse.cursor})`; }
  return null;
})();
const live = (p) => !cursorOff && !!p && (p.type === 'screen' || p.type === 'focus') && !!mouse[p.seat]?.length;
const mouseAt = (seat, v) => {
  const ev = mouse[seat];
  let lo = 0, hi = ev.length - 1;
  if (v < ev[0].video) return null;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ev[mid].video <= v) lo = mid; else hi = mid - 1; }
  const a = ev[lo], b = ev[lo + 1];
  if (!b || b.video - a.video > 0.15) return { x: a.x, y: a.y };   // a pause: the mouse sat still until the next event
  const u = (v - a.video) / (b.video - a.video || 1);
  return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u };
};
const ease = { io3: (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2), expoOut: (u) => (u >= 1 ? 1 : 1 - Math.pow(2, -10 * u)), in2: (u) => u * u, io2: (u) => (u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2), out3: (u) => 1 - Math.pow(1 - u, 3) };
const lerpP = (a, b, e) => ({ x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e });
// Where the tip lands on the stage at film time T in footage shot p (null before the mouse was first seen), including the shot's own
// fade-in scale/rise and fade-out lift, so the tip does not slide off its target while the stage settles.
function pointAt(p, T) {
  const m = mouseAt(p.seat, p.from + (T - p.cs) * (p.rate ?? 1));
  if (!m) return null;
  let X, Y;
  if (p.type === 'screen') {
    const k = p.camTx;
    let st = k[0];
    for (let i = 1; i < k.length; i++) {
      const t = p.cs + k[i].t;
      if (T <= t) break;
      const e = ease.io3(Math.min(1, (T - t) / MOVE)), a = k[i - 1], b = k[i];
      st = { s: a.s + (b.s - a.s) * e, x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e };
    }
    X = st.x + st.s * m.x * p.K; Y = st.y + st.s * m.y * p.K;
  } else {
    X = p.geo.x0 + (m.x - p.geo.r.x) * p.geo.z; Y = p.geo.y0 + (m.y - p.geo.r.y) * p.geo.z;
  }
  const [s0, inD, rise] = p.type === 'screen' ? [0.965, FADE_IN, 0] : [0.94, 0.6, 24];
  const u = (T - p.cs) / inD, sc = u < 1 ? s0 + (1 - s0) * ease.expoOut(Math.max(0, u)) : 1;
  const ty = (u < 1 ? rise * (1 - ease.expoOut(Math.max(0, u))) : 0) - 10 * ease.in2(clamp((T - (p.cs + p.cd - FADE_OUT)) / FADE_OUT, 0, 1));
  return { x: SW / 2 + (X - SW / 2) * sc, y: CH / 2 + (Y - CH / 2) * sc + ty };
}
const cursorMeta = { off: cursorOff || undefined, size: CUR_SIZE, presses: 0, tweens: 0 };
if (!cursorOff && placed.some(live)) {
  const FPS = 30, N = Math.ceil(TOTAL * FPS), ENTER = 0.85, HAND_IN = 0.3, HAND_OUT = 0.55, EXIT = 0.6;
  const below = (x) => ({ x, y: CH + CUR_SIZE * 0.45 });   // tip there = the whole arrow (and its shadow) is under the stage edge
  const shotAt = (T) => placed.find((p) => T >= p.cs && T < p.cs + p.cd) || null;
  const pts = [];
  let lastX = SW * 0.55;
  for (let k = 0; k <= N; k++) {
    const T = k / FPS, p = shotAt(T), P = live(p) ? pointAt(p, T) : null;
    if (P) lastX = P.x;
    pts.push({ t: T, ...(P || below(lastX)) });
  }
  const frames = (a, b) => { const out = []; for (let k = Math.max(0, Math.ceil(a * FPS)); k <= Math.min(N, Math.floor(b * FPS)); k++) out.push(k); return out; };
  // every click in each shot, in film time: an entry or hand-off lands before the first one, an exit leaves after the last release
  const clicks = new Map(placed.filter(live).map((p) => {
    const ev = mouse[p.seat], ft = (v) => p.cs + (v - p.from) / (p.rate ?? 1), out = [];
    ev.forEach((e, k) => {
      if (e.type !== 'down' || e.video < p.from || e.video > p.to) return;
      const down = ft(e.video), up = ev.slice(k + 1).find((x) => x.type === 'up');
      if (down >= p.cs + 0.15 && down <= p.cs + p.cd - 0.15) out.push({ down, up: up ? ft(up.video) : down + 0.1 });
    });
    return [p, out];
  }));
  const overlay = (k, P) => { pts[k].x = P.x; pts[k].y = P.y; };
  const exitAt = new Map();   // when each shot's cursor starts to leave: a release animation never comes after it
  // a glide starts from where the arrow IS on that frame (an earlier entry or hand-off may already have moved it), never a raw position
  const from = (t) => { const k = clamp(Math.round(t * FPS), 0, N); return { x: pts[k].x, y: pts[k].y }; };
  placed.forEach((p, j) => {
    if (!live(p)) return;
    const t0 = p.cs, t1 = p.cs + p.cd, prev = placed[j - 1], next = placed[j + 1], cl = clicks.get(p);
    const settle = (want) => clamp(Math.min(want, (cl[0]?.down ?? Infinity) - t0 - 0.1), 0.25, want);
    if (live(prev)) {
      // hand-off: start for this shot's first point a beat before the cut and arrive just after it, one continuous glide
      const A = from(t0 - HAND_IN), D = HAND_IN + settle(HAND_OUT);
      for (const k of frames(t0 - HAND_IN, t0 - HAND_IN + D)) { const B = pointAt(p, Math.max(pts[k].t, t0)); if (B) overlay(k, lerpP(A, B, ease.io2((pts[k].t - (t0 - HAND_IN)) / D))); }
    } else {
      // entry: straight up from below the stage onto the live position, decelerating
      const D = settle(ENTER), land = pointAt(p, t0 + D);
      if (land) for (const k of frames(t0, t0 + D)) overlay(k, lerpP(below(land.x), pointAt(p, pts[k].t) || land, ease.out3((pts[k].t - t0) / D)));
    }
    if (!live(next)) {
      // exit: out through the bottom edge, accelerating (never a fade in place), after the last release and gone by the cut; a release
      // late in the shot makes the exit quicker, never lets it run over the next title or card
      const lastUp = cl[cl.length - 1]?.up ?? -Infinity;
      const e0 = Math.max(clamp(lastUp + 0.12, t1 - EXIT, t1 - 0.25), Math.min(lastUp + 0.02, t1 - 0.1)), D = t1 - e0, A = from(e0);   // not before the release
      if (lastUp > t1 - 0.35) warn(`shot ${p.i + 1}: a click releases ${(t1 - lastUp).toFixed(2)}s before the cut — too soon for the cursor to let go and leave; end the shot ≥ 0.4 s after the click`);
      exitAt.set(p, e0);
      for (const k of frames(e0, t1)) overlay(k, lerpP(A, below(A.x + CUR_SIZE * 0.3), ease.in2((pts[k].t - e0) / D)));
    }
  });
  // Out of sight (tip under the stage edge = the whole arrow is), it waits where it will next come up: no hidden sideways slides.
  for (let k = pts.length - 2; k >= 0; k--) if (pts[k].y > CH + 2 && pts[k + 1].y > CH + 2) { pts[k].x = pts[k + 1].x; pts[k].y = pts[k + 1].y; }
  // Ramer-Douglas-Peucker over time: keep only the frames a straight tween cannot reproduce within half a pixel.
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  for (const stack = [[0, pts.length - 1]]; stack.length;) {
    const [i, j] = stack.pop(); let worst = 0.5, at = -1;
    for (let k = i + 1; k < j; k++) { const q = lerpP(pts[i], pts[j], (pts[k].t - pts[i].t) / (pts[j].t - pts[i].t)); const d = Math.hypot(pts[k].x - q.x, pts[k].y - q.y); if (d > worst) { worst = d; at = k; } }
    if (at > 0) { keep[at] = 1; stack.push([i, at], [at, j]); }
  }
  const kept = pts.filter((_, k) => keep[k]), f1 = (n) => n.toFixed(1);
  html.push(`<div id="curs"><div id="cur"><div id="curk">${ARROW}</div></div></div>`);
  js.push(`tl.set("#cur",{x:${f1(kept[0].x)},y:${f1(kept[0].y)}},0);`);
  for (let i = 1; i < kept.length; i++) {
    const a = kept[i - 1], b = kept[i];
    if (Math.hypot(a.x - b.x, a.y - b.y) < 0.05) continue;     // a hold: the previous tween's end state stands
    js.push(`tl.fromTo("#cur",{x:${f1(a.x)},y:${f1(a.y)}},{x:${f1(b.x)},y:${f1(b.y)},duration:${f3(b.t - a.t)},ease:"none",immediateRender:false},${f3(a.t)});`);
    cursorMeta.tweens++;
  }
  // presses from the real mouse downs/ups: every click on screen is the cursor's
  for (const [p, cl] of clicks) for (const c of cl) {
    js.push(pressJs('#curk', c.down, Math.min(c.up, exitAt.get(p) ?? Infinity))); cursorMeta.presses++;
    const at = pts[Math.round(c.down * FPS)];
    if (at.x < 0 || at.x > SW || at.y < 0 || at.y > CH) { cursorMeta.offFrame = (cursorMeta.offFrame || 0) + 1; warn(`shot ${p.i + 1}: the click at ${c.down.toFixed(1)}s happens outside the frame (cursor at ${Math.round(at.x)},${Math.round(at.y)}) — the viewer sees the effect but not the cause; frame the control and what it changes together`); }
  }
}

// narration rail (old walkthrough's chapter rail, on the launch look): ticks, NN / NN, title, one line
if (RAIL) {
  const N = lines.length, pad2 = (n) => String(n).padStart(2, '0');
  html.push(`<div id="rail"><img class="rlogo" src="${LOGO}" alt="${esc(BRAND_PACK.name)}" /><div class="ticks">${lines.map((_, k) => `<i class="tick" id="tk${k}"></i>`).join('')}</div>${lines.map((l, k) => `<div class="say" id="sy${k}"><div class="cnt mono" id="sy${k}c">${pad2(k + 1)} / ${pad2(N)}</div><div class="st" id="sy${k}t">${esc(l.title)}</div>${l.sub ? `<div class="ss" id="sy${k}s">${esc(l.sub)}</div>` : ''}</div>`).join('')}</div>`);
  js.push(`tl.fromTo("#rail",{opacity:0},{opacity:1,duration:.5,ease:E},${f3(OPEN_D + 0.1)});tl.to("#rail",{opacity:0,duration:.4,ease:"power2.in"},${f3(END - 0.45)});`);
  const ACTIVE = hex('--brand-accent'), DONE = hex('--status-success');
  const shown = new Set();
  for (const sh of shows) {
    const k = sh.k, out = Math.max(sh.on + 0.6, sh.off - 0.3);
    if (!shown.has(k)) {        // first showing: the words blur in one by one
      shown.add(k);
      js.push(`tl.set("#sy${k}",{opacity:1,filter:"blur(0px)"},${f3(sh.on)});` + blurIn(`#sy${k}c`, sh.on + 0.05) + blurIn(`#sy${k}t`, sh.on + 0.12) + (lines[k].sub ? blurIn(`#sy${k}s`, sh.on + 0.24) : ''));
    } else {                    // shown again after a title or card: the block returns as a whole
      js.push(`tl.fromTo("#sy${k}",{opacity:0,filter:"blur(10px)"},{opacity:1,filter:"blur(0px)",duration:.45,ease:E,immediateRender:false},${f3(sh.on)});`);
    }
    js.push(`tl.to("#sy${k}",{opacity:0,filter:"blur(8px)",duration:.3,ease:"power2.in"},${f3(out)});`);
  }
  lines.forEach((l, k) => js.push(`tl.to("#tk${k}",{backgroundColor:"${ACTIVE}",duration:.25},${f3(l.t)});tl.to("#tk${k}",{backgroundColor:"${DONE}",duration:.3},${f3(Math.max(l.t + 0.6, (lines[k + 1]?.t ?? END) - 0.05))});`));
}

// outro: logo, the line, the url, and the four brand stripes settling in a row
html.push(`<div id="outro" class="stage"><img id="o-logo" class="wm big" src="${LOGO}" alt="${esc(BRAND_PACK.name)}" />${outro.line ? `<div class="oline small" id="o-line">${esc(outro.line)}</div>` : ''}${outro.url ? `<div class="url mono" id="o-url">${esc(outro.url)}</div>` : ''}<div class="bars">${STRIPES.map((c, k) => `<i style="background:${c}" id="ob${k}"></i>`).join('')}</div></div>`);
js.push(`tl.fromTo("#o-logo",{opacity:0,scale:.92,filter:"blur(12px)"},{opacity:1,scale:1,filter:"blur(0px)",duration:.7,ease:E},${f3(END + 0.25)});`);
if (outro.line) js.push(blurIn('#o-line', END + 0.6));
if (outro.url) js.push(blurIn('#o-url', END + 0.85));
STRIPES.forEach((_, k) => js.push(`tl.fromTo("#ob${k}",{opacity:0,scaleX:0},{opacity:1,scaleX:1,duration:.5,ease:E,transformOrigin:"0% 50%"},${f3(END + 0.9 + k * 0.08)});`));
js.push(`tl.set("#outro",{opacity:1},${f3(END)});`);
js.push('window.__timelines["main"] = tl; tl.seek(0);');

const css = `
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { margin: 0; width: ${CW}px; height: ${CH}px; overflow: hidden; background: var(--bg-page); }
#root { position: relative; width: 100%; height: 100%; overflow: hidden; background: var(--bg-page); color: var(--text-primary); font-family: var(--font-sans); }
/* the one gradient a launch film allows: a faint brand wash at two corners of the canvas (frame.md) */
#wash { position: absolute; inset: 0; background: radial-gradient(60% 55% at 0% 0%, rgba(${rgb(BRAND.accent)},.07), transparent 70%), radial-gradient(55% 50% at 100% 100%, rgba(${rgb(BRAND.alt2)},.06), transparent 70%); }
.stage { position: absolute; left: 0; top: 0; width: ${SW}px; height: ${CH}px; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; opacity: 0; }
#opener, #outro { opacity: 1; width: ${CW}px; }
#outro { opacity: 0; }
.lockup { display: flex; align-items: center; gap: ${Math.round(CW * 0.016)}px; }
.wm { height: ${Math.round(CH * 0.075)}px; width: auto; display: block; }
.wm.big { height: ${Math.round(CH * 0.085)}px; }
.lockup .x { font-size: ${Math.round(CH * 0.045)}px; color: var(--text-tertiary); font-weight: 300; opacity: 0; }
.lws { display: flex; gap: ${Math.round(CH * 0.02)}px; }
.lockup .lw { font-size: ${Math.round(CH * 0.074)}px; font-weight: 700; letter-spacing: -.035em; opacity: 0; }
.acc { color: var(--brand-accent); }
.oline { margin-top: ${Math.round(CH * 0.035)}px; font-size: ${Math.round(CH * 0.042)}px; color: var(--text-secondary); font-weight: 400; letter-spacing: -.01em; text-align: center; max-width: ${Math.round(CW * 0.7)}px; }
.oline.small { font-size: ${Math.round(CH * 0.034)}px; opacity: 0; }
.olw { display: inline-block; opacity: 0; }
.url { margin-top: ${Math.round(CH * 0.018)}px; font-size: ${Math.round(CH * 0.019)}px; color: var(--text-accent); opacity: 0; }
.bars { margin-top: ${Math.round(CH * 0.045)}px; display: flex; gap: ${Math.round(CH * 0.008)}px; }
.bars i { display: block; width: ${Math.round(CH * 0.06)}px; height: ${Math.round(CH * 0.012)}px; border-radius: ${Math.round(CH * 0.006)}px; opacity: 0; }
/* with a rail, footage fades into the canvas at the stage's right edge instead of stopping hard against the narration */
${RAIL ? `.stage:not(#opener):not(#outro) { -webkit-mask-image: linear-gradient(to right, rgba(${rgb(BRAND.ink)},1) calc(100% - 72px), rgba(${rgb(BRAND.ink)},0)); mask-image: linear-gradient(to right, rgba(${rgb(BRAND.ink)},1) calc(100% - 72px), rgba(${rgb(BRAND.ink)},0)); }` : ''}
#rail { position: absolute; left: ${SW}px; top: 0; width: ${RAIL_W}px; height: ${CH}px; opacity: 0; }
#rail .rlogo { position: absolute; right: 56px; top: 40px; height: 30px; width: auto; }
#rail .ticks { position: absolute; left: 24px; right: 56px; top: 112px; display: flex; gap: 4px; }
#rail .tick { display: block; flex: 1; height: 4px; border-radius: 2px; background: var(--border-default); }
#rail .say { position: absolute; left: 24px; right: 56px; top: 156px; opacity: 0; }
#rail .cnt { font-size: 16px; color: var(--text-accent); margin-bottom: 20px; opacity: 0; }
#rail .st { font-size: 42px; font-weight: 700; letter-spacing: -.03em; line-height: 1.08; opacity: 0; }
#rail .ss { margin-top: 20px; font-size: 23px; line-height: 1.45; color: var(--text-secondary); font-weight: 400; opacity: 0; }
/* the brand stripes (brand.json "stripes") streak through the frame between opener, product and outro */
.streaks { position: absolute; left: 0; right: 0; top: 50%; height: 0; pointer-events: none; z-index: 20; }
.streaks i { position: absolute; left: ${Math.round(CW * 0.15)}px; width: ${Math.round(CW * 0.7)}px; height: ${Math.round(CH * 0.026)}px; border-radius: ${Math.round(CH * 0.013)}px; filter: blur(3px); opacity: 0; transform: skewX(-18deg); }
.streaks i:nth-child(1) { top: ${-Math.round(CH * 0.07)}px; } .streaks i:nth-child(2) { top: ${-Math.round(CH * 0.033)}px; } .streaks i:nth-child(3) { top: ${Math.round(CH * 0.004)}px; } .streaks i:nth-child(4) { top: ${Math.round(CH * 0.041)}px; }
.world { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
.appcard { position: absolute; left: 0; top: 0; overflow: hidden; background: var(--bg-surface); }
.focuscard, .phone { position: relative; background: var(--bg-surface); }
.phone { background: var(--brand-ink); }
.crop { position: absolute; overflow: hidden; }
.vwrap { position: absolute; transform-origin: 0 0; }
.cap { position: absolute; top: ${Math.round(CH * 0.12)}px; font-size: ${Math.round(CH * 0.036)}px; font-weight: 600; letter-spacing: -.02em; opacity: 0; }
.tline { font-size: ${Math.round(CH * 0.066)}px; font-weight: 700; letter-spacing: -.035em; text-align: center; max-width: ${Math.round(SW * 0.82)}px; line-height: 1.12; }
.tline span { display: inline-block; opacity: 0; }
.tsub { margin-top: ${Math.round(CH * 0.025)}px; font-size: ${Math.round(CH * 0.028)}px; color: var(--text-secondary); opacity: 0; }
.card { min-width: ${Math.round(SW * 0.5)}px; max-width: ${Math.round(SW * 0.86)}px; padding: ${Math.round(CH * 0.035)}px ${Math.round(CH * 0.04)}px; }
.ceye { display: flex; align-items: center; gap: 10px; font-size: ${Math.round(CH * 0.022)}px; color: var(--status-success); opacity: 0; }
.ceye .dot { width: 14px; height: 14px; border-radius: 50%; background: var(--status-success); display: inline-block; }
.ctitle { margin-top: ${Math.round(CH * 0.02)}px; font-size: ${Math.round(Math.min(CH * 0.075, SW * 0.046))}px; font-weight: 700; letter-spacing: -.03em; line-height: 1.1; opacity: 0; }
.csub { margin-top: ${Math.round(CH * 0.016)}px; font-size: ${Math.round(CH * 0.034)}px; color: var(--text-secondary); opacity: 0; }
.crow { margin-top: ${Math.round(CH * 0.04)}px; display: flex; align-items: center; gap: ${Math.round(CH * 0.022)}px; padding: ${Math.round(CH * 0.024)}px ${Math.round(CH * 0.028)}px; border-radius: var(--radius-lg); background: var(--bg-surface); box-shadow: 0 0 0 1px var(--border-default), 0 12px 32px rgba(${rgb(BRAND.ink)},.08); opacity: 0; }
.cmark { height: ${Math.round(CH * 0.05)}px; width: auto; }
.cmeta { flex: 1; }
.crt { font-size: ${Math.round(CH * 0.03)}px; font-weight: 600; }
.crm { font-size: ${Math.round(CH * 0.022)}px; color: var(--text-secondary); margin-top: 2px; }
.ccta { position: relative; font-size: ${Math.round(CH * 0.026)}px; font-weight: 600; color: var(--text-accent); }
.arrow { display: block; width: 100%; height: 100%; overflow: visible; filter: drop-shadow(0 ${Math.round(CUR_SIZE * 0.04)}px ${Math.round(CUR_SIZE * 0.06)}px rgba(${rgb(BRAND.ink)},.3)); }
.arrow path { fill: var(--brand-ink); stroke: var(--brand-paper); stroke-width: 1.4; stroke-linejoin: round; }
.arrow.light path { fill: var(--brand-paper); stroke: var(--brand-ink); }
/* the card's pointer: tip at three quarters across the CTA, resting there only after it glides in from below the stage */
.ptr { position: absolute; left: calc(75% - ${Math.round(ARROW_TIP.x * CUR_SIZE)}px); top: calc(60% - ${Math.round(ARROW_TIP.y * CUR_SIZE)}px); width: ${CUR_SIZE}px; height: ${CUR_SIZE}px; pointer-events: none; }
/* the footage cursor: positioned by its tip (x/y tweens move the tip), clipped to the stage, above the shots and below the wipes */
#curs { position: absolute; left: 0; top: 0; width: ${SW}px; height: ${CH}px; overflow: hidden; pointer-events: none; z-index: 10; }
${RAIL ? `#curs { -webkit-mask-image: linear-gradient(to right, rgba(${rgb(BRAND.ink)},1) calc(100% - 72px), rgba(${rgb(BRAND.ink)},0)); mask-image: linear-gradient(to right, rgba(${rgb(BRAND.ink)},1) calc(100% - 72px), rgba(${rgb(BRAND.ink)},0)); }` : ''}
#cur { position: absolute; left: ${-Math.round(ARROW_TIP.x * CUR_SIZE)}px; top: ${-Math.round(ARROW_TIP.y * CUR_SIZE)}px; width: ${CUR_SIZE}px; height: ${CUR_SIZE}px; will-change: transform; }
#curk { width: 100%; height: 100%; transform-origin: ${TIP}; }
`;
const doc = `<!doctype html>
<html lang="en" data-theme="${THEME}"><head><meta charset="UTF-8" /><meta name="viewport" content="width=${CW}, height=${CH}" />
<script src="kit/vendor/gsap.min.js"></script>
<style>
/*kit:tokens:start*/
${tokenBlock(BRAND_PACK)}
/*kit:tokens:end*/
${css}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="${f3(TOTAL)}" data-width="${CW}" data-height="${CH}">
<div id="wash"></div>
${html.join('\n')}
</div>
<script>
${js.join('\n')}
</script></body></html>
`;

// ---------- build ----------
const hf = (a, cwd = project) => { const r = spawnSync('npx', ['-y', `hyperframes@${kit.hyperframes}`, ...a], { cwd, encoding: 'utf8', maxBuffer: 1 << 28 }); return { code: r.status ?? 1, out: `${r.stdout || ''}${r.stderr || ''}` }; };
const meta = {
  theme: THEME, canvas: [CW, CH], stage: [SW, CH], rail: RAIL ? lines.map((l, k) => ({ t: Number(l.t.toFixed(2)), title: l.title, shown: shows.filter((x) => x.k === k).map((x) => [Number(x.on.toFixed(2)), Number(x.off.toFixed(2))]) })) : false, duration: Number(TOTAL.toFixed(2)), opener: OPEN_D, outro: OUT_D, maxWidth: maxW,
  shots: placed.map((p) => ({ type: p.type, at: Number(p.cs.toFixed(2)), dur: Number(p.cd.toFixed(2)), ...(p.camera ? { camera: p.camera.map((c) => ({ t: Number(c.t.toFixed(2)), w: Math.round(c.rect.w), wide: !!c.rect.wide })) } : {}), ...(p.zoom ? { zoom: p.zoom, crop: p.crop } : {}) })),
  cursor: cursorMeta,
};

function prepare() {
  if (!existsSync(join(project, 'hyperframes.json'))) {
    mkdirSync(dirname(project), { recursive: true });
    const r = hf(['init', project, '--non-interactive', '--example=blank'], dirname(project));
    if (r.code !== 0 || !existsSync(join(project, 'hyperframes.json'))) die(3, `hyperframes init failed:\n${r.out.slice(-600)}`);
  }
  mkdirSync(join(project, 'assets'), { recursive: true });
  for (const p of placed) if (FOOTAGE.has(p.type)) {
    const src = resolve(specDir, videoOf(p));
    if (!existsSync(src)) die(2, `video ${src} not found — paths in the spec resolve against the spec's own folder: keep launch.json beside raw/`);
    copyFileSync(src, join(project, assetName(p)));
  }
  mkdirSync(join(project, 'kit'), { recursive: true });
  stageKit(project, BRAND_PACK);
  writeFileSync(join(project, 'index.html'), doc);
  writeFileSync(join(project, 'launch.meta.json'), JSON.stringify(meta, null, 2));
  const findings = lintProject(join(project, 'index.html'), BRAND_PACK);
  const chk = hf(['check']);
  const passed = /Check passed/.test(chk.out);
  // one frame per beat of the film: opener, every shot (and every camera framing), outro
  const times = [OPEN_D - 1.2];
  for (const p of placed) {
    if (p.camera) for (const c of p.camera) times.push(p.cs + Math.min(p.cd - 0.5, c.t + (c.t ? MOVE : FADE_IN) + 0.35));
    else times.push(p.cs + Math.min(p.cd - 0.5, Math.max(1.0, p.cd * 0.6)));
  }
  times.push(TOTAL - 0.6);
  rmSync(join(project, 'snapshots'), { recursive: true, force: true });
  const snap = hf(['snapshot', '--at', [...new Set(times.map((x) => Math.max(0.1, x).toFixed(1)))].join(',')]);
  const sheets = existsSync(join(project, 'snapshots')) ? readdirSync(join(project, 'snapshots')).filter((f) => /^contact-sheet/.test(f)).map((f) => join(project, 'snapshots', f)) : [];
  console.log(JSON.stringify({ project, ...meta, check: passed ? 'passed' : 'FAILED', brandLint: findings.length ? findings : 'clean', contactSheets: sheets }, null, 2));
  if (!passed) { console.error(chk.out.slice(-1500)); process.exit(1); }
  if (findings.length) process.exit(1);
  if (snap.code !== 0 || !sheets.length) { console.error(`build-launch: snapshot step failed — the visual review cannot be done:\n${snap.out.slice(-600)}`); process.exit(1); }
}

function render() {
  // Render-only must render what was prepared AND reviewed: if the spec or beats changed since, the composition on disk is stale.
  if (stage === 'render') {
    const onDisk = existsSync(join(project, 'index.html')) ? readFileSync(join(project, 'index.html'), 'utf8') : null;
    if (onDisk === null) die(2, 'nothing prepared in this project: run --stage prepare first');
    if (onDisk !== doc) die(2, 'the spec or beats changed since --stage prepare: run prepare again (and look at the contact sheets) before rendering');
  }
  const out = resolve(opt('out') || join(process.env.HOME, 'Documents/Demos/features', `${spec.feature || 'launch'}-launch.mp4`));
  const poster = resolve(opt('poster') || out.replace(/\.mp4$/, '-poster.jpg'));
  mkdirSync(dirname(out), { recursive: true });
  const raw = join(project, 'renders-raw.mp4');
  const r = hf(['render', '--output', raw, '--fps', '30', '--crf', String(L.crf ?? 18), '--video-frame-format', 'png', '--workers', '4']);
  if (r.code !== 0 || !existsSync(raw)) { console.error(r.out.slice(-1200)); die(1, 'render failed'); }
  // silent by design (music is added in the posting app): drop any audio track, faststart for the web
  const fs1 = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', raw, '-c:v', 'copy', '-an', '-movflags', '+faststart', out]);
  if (fs1.status !== 0) die(3, 'ffmpeg faststart failed');
  const pAt = typeof spec.poster?.at === 'number' ? spec.poster.at : OPEN_D + Math.min(4, (END - OPEN_D) * 0.35);
  spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', String(pAt), '-i', out, '-frames:v', '1', '-q:v', '3', poster]);
  const pr = spawnSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate:format=duration,bit_rate,size', '-of', 'json', out], { encoding: 'utf8' });
  const j = JSON.parse(pr.stdout || '{}'); const st = j.streams?.[0] || {}, fm = j.format || {};
  const res = { rendered: out, poster, width: st.width, height: st.height, fps: st.r_frame_rate, seconds: Number(fm.duration), kbps: Math.round(Number(fm.bit_rate) / 1000), mb: Number((Number(fm.size) / 1048576).toFixed(1)) };
  rmSync(raw, { force: true });
  console.log(JSON.stringify(res));
  const problems = [];
  if (res.width !== CW || res.height !== CH) problems.push(`size ${res.width}x${res.height} != ${CW}x${CH}`);
  if (Math.abs(res.seconds - TOTAL) > 0.5) problems.push(`duration ${res.seconds}s != ${TOTAL.toFixed(2)}s`);
  if (!existsSync(poster) || statSync(poster).size < 5000) problems.push('poster missing');
  if (problems.length) { console.error('build-launch: launch-lint failed: ' + problems.join('; ')); process.exit(1); }
}

if (stage === 'prepare' || stage === 'all') prepare();
if (stage === 'render' || stage === 'all') render();
