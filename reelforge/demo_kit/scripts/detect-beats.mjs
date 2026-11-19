#!/usr/bin/env node
// detect-beats — locate every capture-kit beat() sync flash inside the recorded videos.
//   node detect-beats.mjs --marks marks.json --video seat=path.mp4 [--video seat2=other.mp4] [--focus focus.json] [--mouse mouse.json] --out beats.json
// Playwright's video clock drifts against wall-clock (~9% over a few minutes), so script timestamps cannot be used to cut.
// capture-kit flashes a 4px magenta strip at the bottom of the seat's page for 400ms at each beat; here we find those
// runs in the video itself, in order, and pair them with that seat's marks.
import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const a = process.argv.slice(2);
const one = (n) => { const i = a.indexOf(`--${n}`); return i >= 0 ? a[i + 1] : undefined; };
const many = (n) => a.flatMap((x, i) => (x === `--${n}` ? [a[i + 1]] : []));
const marksPath = one('marks'), out = one('out'), videos = Object.fromEntries(many('video').map((v) => v.split(/=(.+)/).slice(0, 2)));
if (!marksPath || !out || !Object.keys(videos).length) { console.error('usage: detect-beats.mjs --marks marks.json --video seat=file.mp4 ... --out beats.json'); process.exit(2); }
const FPS = 25;

export function flashRuns(rgb) {
  const n = Math.floor(rgb.length / 3), runs = []; let i = 0;
  const on = (k) => rgb[k * 3] > 170 && rgb[k * 3 + 1] < 110 && rgb[k * 3 + 2] > 170;
  while (i < n) { if (on(i)) { let j = i; while (j < n && on(j)) j++; runs.push(Number((i / FPS).toFixed(2))); i = j; } else i++; }
  return { runs, seconds: n / FPS };
}

const marks = JSON.parse(readFileSync(marksPath, 'utf8'));
const result = { version: 1, fps: FPS, seats: {} }; let bad = false;
for (const [seat, file] of Object.entries(videos)) {
  const r = spawnSync('ffmpeg', ['-v', 'error', '-i', file, '-vf', `fps=${FPS},crop=iw:3:0:ih-3,scale=1:1,format=rgb24`, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], { maxBuffer: 1 << 28 });
  if (r.status !== 0) { console.error(`detect-beats: ffmpeg failed for ${seat}: ${String(r.stderr).slice(0, 300)}`); process.exit(3); }
  const { runs, seconds } = flashRuns(r.stdout);
  const mine = marks.filter((m) => m.seat === seat && m.flash !== false);
  if (runs.length !== mine.length) { console.error(`detect-beats: seat "${seat}": found ${runs.length} flashes in the video but ${mine.length} beats were marked — cannot pair them`); console.error(`  flashes at: ${runs.join(', ')}`); console.error(`  beats: ${mine.map((m) => m.beat).join(', ')}`); bad = true; continue; }
  result.seats[seat] = { duration: Number(seconds.toFixed(2)), beats: mine.map((m, i) => ({ name: m.beat, t: m.t, video: runs[i] })) };
}
if (bad) process.exit(1);
// Focus events (where each action happened, CSS px) -> video time, through this seat's own beats (script time <-> video time, piecewise linear).
const focusPath = one('focus');
if (focusPath) {
  const lerp = (xs, ys, x) => { if (xs.length < 2) return ys[0] + (x - xs[0]); const i = x <= xs[0] ? 0 : x >= xs[xs.length - 1] ? xs.length - 2 : xs.findLastIndex((v) => v <= x); return ys[i] + ((ys[i + 1] - ys[i]) * (x - xs[i])) / (xs[i + 1] - xs[i] || 1); };
  result.focus = JSON.parse(readFileSync(focusPath, 'utf8')).filter((f) => result.seats[f.seat]?.beats.length).map((f) => {
    const b = result.seats[f.seat].beats; return { ...f, video: Number(Math.max(0, lerp(b.map((x) => x.t), b.map((x) => x.video), f.t)).toFixed(2)) };
  });
}
// Mouse path (capture-kit mouse.json) -> video time the same way: launch films draw their cursor from it.
const mousePath = one('mouse');
if (mousePath) {
  const m = JSON.parse(readFileSync(mousePath, 'utf8'));
  const lerp = (xs, ys, x) => { if (xs.length < 2) return ys[0] + (x - xs[0]); const i = x <= xs[0] ? 0 : x >= xs[xs.length - 1] ? xs.length - 2 : xs.findLastIndex((v) => v <= x); return ys[i] + ((ys[i + 1] - ys[i]) * (x - xs[i])) / (xs[i + 1] - xs[i] || 1); };
  result.mouse = { cursor: m.cursor, events: (m.events || []).filter((e) => result.seats[e.seat]?.beats.length).map((e) => {
    const b = result.seats[e.seat].beats; return { ...e, video: Number(Math.max(0, lerp(b.map((x) => x.t), b.map((x) => x.video), e.t)).toFixed(3)) };
  }) };
}
writeFileSync(out, JSON.stringify(result, null, 2));
console.log(`detect-beats: wrote ${out} (${Object.entries(result.seats).map(([s, v]) => `${s}: ${v.beats.length} beats`).join(', ')})`);
