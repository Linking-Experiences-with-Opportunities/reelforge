#!/usr/bin/env node
// selftest — end-to-end proof that the demo kit works on this machine, with synthetic footage (no app, no Playwright, no network
// beyond the first `npx hyperframes` download).   node selftest.mjs [--quick] [--capture]
//   --quick    skip the render
//   --capture  also drive a real Playwright browser through capture-kit.cjs on a local file:// page (run from a dir where `playwright` resolves, e.g. your app's repo)
// Runs against the ACTIVE brand pack (see brand.mjs), so it also proves your own brand pack renders and lints clean.
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { lintProject } from './brand-lint.mjs';
import { loadBrand } from './brand.mjs';

const S = dirname(fileURLToPath(import.meta.url)), KIT = resolve(S, '..'), quick = process.argv.includes('--quick');
const BRAND = loadBrand();
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const kit = JSON.parse(readFileSync(join(KIT, 'kit.json'), 'utf8'));
let failed = 0; const ok = (c, m) => { console.log(`${c ? 'PASS' : 'FAIL'}  ${m}`); if (!c) failed++; };
const run = (cmd, a, o = {}) => spawnSync(cmd, a, { encoding: 'utf8', maxBuffer: 1 << 28, ...o });
const work = mkdtempSync(join(tmpdir(), 'demo-kit-selftest-'));

// 0 prerequisites
ok(Number(process.versions.node.split('.')[0]) >= 22, `node ${process.versions.node} >= 22`);
const enc = run('ffmpeg', ['-hide_banner', '-encoders']);
ok(enc.status === 0 && /libx264/.test(enc.stdout), 'ffmpeg with libx264');
if (failed) { console.log('selftest: prerequisites missing'); process.exit(1); }

// 1 resolver (plugin-user simulation: cwd elsewhere, only the plugin cache layout present; then nothing present)
{
  const home = join(work, 'home'), cache = join(home, '.claude/plugins/cache/record-demo-kit/record-demo/9.9.9/demo-kit');
  mkdirSync(cache, { recursive: true }); writeFileSync(join(cache, 'kit.json'), '{}');
  const env = { PATH: process.env.PATH, HOME: home }; const cwd = join(work, 'elsewhere'); mkdirSync(cwd);
  const r1 = run('bash', [join(S, 'resolve-kit.sh')], { env, cwd });
  ok(r1.status === 0 && r1.stdout.trim().endsWith('record-demo/9.9.9/demo-kit'), `resolver finds the plugin-cache kit from an unrelated cwd (${r1.stdout.trim().slice(-40)})`);
  const r2 = run('bash', [join(S, 'resolve-kit.sh')], { env: { PATH: process.env.PATH, HOME: join(work, 'empty') }, cwd });
  ok(r2.status === 1, 'resolver exits 1 when no kit exists (skill then falls back to polish=off)');
}

// 2 synthetic footage with sync flashes: interviewer flashes at 2.0s and 7.0s, candidate at 4.0s
const clip = (file, flashes) => run('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1440x900:rate=25:duration=12', '-vf', `drawbox=x=0:y=ih-4:w=iw:h=4:color=black:t=fill,drawbox=x=0:y=ih-4:w=iw:h=4:color=0xff00ff:t=fill:enable='${flashes.map((t) => `between(t,${t},${t + 0.4})`).join('+')}'`, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', file]);
ok(clip(join(work, 'interviewer.mp4'), [2, 7]).status === 0 && clip(join(work, 'candidate.mp4'), [4]).status === 0, 'synthetic 1440x900 captures rendered');
writeFileSync(join(work, 'marks.json'), JSON.stringify([{ beat: 'a', seat: 'interviewer', t: 2.4 }, { beat: 'b', seat: 'interviewer', t: 7.4 }, { beat: 'x', seat: 'candidate', t: 4.4 }]));
const det = run('node', [join(S, 'detect-beats.mjs'), '--marks', join(work, 'marks.json'), '--video', `interviewer=${join(work, 'interviewer.mp4')}`, '--video', `candidate=${join(work, 'candidate.mp4')}`, '--out', join(work, 'beats.json')]);
ok(det.status === 0, 'detect-beats pairs every flash with its beat');
const beats = existsSync(join(work, 'beats.json')) ? JSON.parse(readFileSync(join(work, 'beats.json'), 'utf8')) : null;
const near = (v, t) => Math.abs(v - t) <= 0.1;
ok(beats && near(beats.seats.interviewer.beats[0].video, 2) && near(beats.seats.interviewer.beats[1].video, 7) && near(beats.seats.candidate.beats[0].video, 4), `beat times within 0.1s of truth (${beats ? beats.seats.interviewer.beats.map((b) => b.video).join(', ') + ' | ' + beats.seats.candidate.beats[0].video : 'n/a'})`);
// mismatch must fail loudly, not silently mis-cut
writeFileSync(join(work, 'marks-bad.json'), JSON.stringify([{ beat: 'a', seat: 'interviewer', t: 2.4 }]));
ok(run('node', [join(S, 'detect-beats.mjs'), '--marks', join(work, 'marks-bad.json'), '--video', `interviewer=${join(work, 'interviewer.mp4')}`, '--out', join(work, 'bad.json')]).status === 1, 'detect-beats refuses a beat/flash count mismatch');

// 3 build (prepare = generate + hyperframes check + brand-lint + snapshots)
const spec = { feature: 'selftest', header: 'Selftest · Walkthrough', footnote: 'Synthetic footage', intro: { dur: 1.6, eyebrow: 'Selftest', title: 'Demo kit', subtitle: 'Brand pack check.' }, outro: { dur: 1.6, eyebrow: 'Merged · PR #1039 + #1042', title: 'All good.', chips: ['Beats', 'Brand'] },
  seats: { interviewer: { label: 'Interviewer', video: 'interviewer.mp4' }, candidate: { label: 'Candidate', video: 'candidate.mp4' } },
  chapters: [{ title: 'First chapter', sub: 'Interviewer seat with the candidate picture-in-picture.', segments: [{ seat: 'interviewer', from: 'a', to: 'b' }] }, { title: 'Second chapter', sub: 'Candidate seat, faster.', segments: [{ seat: 'candidate', from: 'x', dur: 4, rate: 1.25 }] }] };
writeFileSync(join(work, 'demo.json'), JSON.stringify(spec, null, 2));
const proj = join(work, 'project');
const prep = run('node', [join(S, 'build-demo.mjs'), '--spec', join(work, 'demo.json'), '--beats', join(work, 'beats.json'), '--project', proj, '--stage', 'prepare']);
ok(prep.status === 0, `build-demo prepare (hyperframes check + brand-lint clean)${prep.status === 0 ? '' : '\n' + (prep.stderr || prep.stdout).slice(-800)}`);

// 4 brand-lint negatives on the generated composition
if (existsSync(join(proj, 'index.html'))) {
  const html = readFileSync(join(proj, 'index.html'), 'utf8');
  const bad = join(work, 'bad.html');
  writeFileSync(bad, html.replace('</style>', '.x{color:#ff3300;font-family:Inter,sans-serif;background:#4d7cff}</style>'));
  const f = lintProject(bad, BRAND);
  ok(f.some((x) => x.rule === 'palette') && f.some((x) => x.rule === 'fonts'), `brand-lint rejects off-palette hex and non-brand font (${f.map((x) => x.rule).join(',')})`);
  writeFileSync(bad, html.replace('</style>', "@import url(https://fonts.example/x.css);.y{background:url('//cdn.example/y.png')}</style>"));
  ok(lintProject(bad, BRAND).filter((x) => x.rule === 'offline').length >= 2, 'brand-lint rejects CSS @import and url() to external hosts');
  writeFileSync(bad, html.replace(new RegExp(reEsc(BRAND.logo('dark')), 'g'), 'kit/brand/other.svg'));
  ok(lintProject(bad, BRAND).some((x) => x.rule === 'logo'), `brand-lint rejects a missing ${BRAND.name} logo`);
}

// 4a a second brand pack via DEMO_BRAND_DIR: init-brand copies the example, a new name + accent show up in the composition
{
  const alt = join(work, 'acme-brand');
  const ib = run('node', [join(S, 'init-brand.mjs'), '--out', alt, '--name', 'Acme']);
  if (ib.status === 0) writeFileSync(join(alt, 'tokens.css'), readFileSync(join(alt, 'tokens.css'), 'utf8').replace(/--brand-accent:\s*#[0-9a-fA-F]+;/, '--brand-accent: #12b886;'));
  const pa = run('node', [join(S, 'build-demo.mjs'), '--spec', join(work, 'demo.json'), '--beats', join(work, 'beats.json'), '--project', join(work, 'project-acme'), '--stage', 'prepare'], { env: { ...process.env, DEMO_BRAND_DIR: alt } });
  const ah = existsSync(join(work, 'project-acme/index.html')) ? readFileSync(join(work, 'project-acme/index.html'), 'utf8') : '';
  ok(ib.status === 0 && pa.status === 0 && /alt="Acme"/.test(ah) && /#12b886/.test(ah) && existsSync(join(work, 'project-acme/kit/brand/brand.json')), `DEMO_BRAND_DIR swaps the brand pack (name, colors, files)${pa.status === 0 ? '' : '\n' + (pa.stderr || pa.stdout).slice(-500)}`);
  ok(run('node', [join(S, 'init-brand.mjs'), '--out', alt]).status === 1, 'init-brand refuses to overwrite an existing pack without --force');
}

// 4b two different captures with the same filename must not overwrite each other
{
  mkdirSync(join(work, 'dup/a'), { recursive: true }); mkdirSync(join(work, 'dup/b'), { recursive: true });
  const c = (f, color) => run('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', `color=c=${color}:s=1440x900:r=25:d=3`, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', f]);
  c(join(work, 'dup/a/clip.mp4'), 'red'); c(join(work, 'dup/b/clip.mp4'), 'blue');
  const dspec = { feature: 'dup', intro: { dur: 1.6, eyebrow: 'x', title: 'Dup', subtitle: 'x' }, outro: { dur: 1.6, eyebrow: 'x', title: 'Dup', chips: [] }, seats: { a: { video: 'a/clip.mp4' }, b: { video: 'b/clip.mp4' } }, chapters: [{ title: 'One', sub: 'x', segments: [{ seat: 'a', from: 0.2, dur: 2 }, { seat: 'b', from: 0.2, dur: 2 }] }] };
  writeFileSync(join(work, 'dup/demo.json'), JSON.stringify(dspec));
  const pd = run('node', [join(S, 'build-demo.mjs'), '--spec', join(work, 'dup/demo.json'), '--project', join(work, 'dup/project'), '--stage', 'prepare']);
  const ha = existsSync(join(work, 'dup/project/assets/seat-a.mp4')) && existsSync(join(work, 'dup/project/assets/seat-b.mp4'));
  ok(pd.status === 0 && ha && readFileSync(join(work, 'dup/project/assets/seat-a.mp4')).compare(readFileSync(join(work, 'dup/project/assets/seat-b.mp4'))) !== 0, 'same-named captures keep separate assets (no silent overwrite)');
}

// 5 render
if (!quick && prep.status === 0) {
  const out = join(work, 'selftest.mp4');
  const r = run('node', [join(S, 'build-demo.mjs'), '--spec', join(work, 'demo.json'), '--beats', join(work, 'beats.json'), '--project', proj, '--stage', 'render', '--out', out]);
  const pr = run('ffprobe', ['-v', 'error', '-show_entries', 'stream=width,height:format=duration', '-of', 'csv=p=0', out]);
  const [w, h] = (pr.stdout.split('\n')[0] || '').split(','), dur = Number((pr.stdout.match(/^[\d.]+$/m) || ['0'])[0]);
  const expected = 1.6 + (7 - 0.15 - (2 + 0.55)) + 4 / 1.25 + 1.6;
  ok(r.status === 0 && w === '1920' && h === '1080', `rendered 1920x1080 (${w}x${h})`);
  ok(Math.abs(dur - expected) < 0.5, `duration ${dur.toFixed(1)}s ≈ expected ${expected.toFixed(1)}s`);
}
// 5b clip format: hi-res synthetic source + directed camera -> zoomed 1440x900 clip
{
  const dir = join(work, 'clip'); mkdirSync(dir, { recursive: true }); const src = join(dir, 'main.mp4');
  const mk = run('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=2880x1800:rate=30:duration=8', '-vf', "drawbox=x=0:y=ih-8:w=iw:h=8:color=black:t=fill,drawbox=x=0:y=ih-8:w=iw:h=8:color=0xff00ff:t=fill:enable='between(t,1,1.4)'", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', src]);
  writeFileSync(join(dir, 'marks.json'), JSON.stringify([{ beat: 'go', seat: 'main', t: 1.4 }]));
  const focus = (cams) => JSON.stringify([{ seat: 'main', kind: 'cam', t: 2.5, x: 100, y: 100, w: cams[0], h: 400 }, { seat: 'main', kind: 'click', t: 3.2, x: 300, y: 200, w: 120, h: 40 }, { seat: 'main', kind: 'cam', t: 5, x: 600, y: 300, w: cams[1], h: 400 }]);
  writeFileSync(join(dir, 'focus.json'), focus([700, 900]));
  const det2 = run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus.json'), '--video', `main=${src}`, '--out', join(dir, 'beats.json')]);
  ok(mk.status === 0 && det2.status === 0, 'hi-res 2880x1800 synthetic source with a beat flash + focus events mapped to video time');
  writeFileSync(join(dir, 'clip.json'), JSON.stringify({ feature: 'c', video: 'main.mp4', from: 'go', dur: 6, maxWidth: 900 }));
  const cp = run('node', [join(S, 'build-clip.mjs'), '--spec', join(dir, 'clip.json'), '--beats', join(dir, 'beats.json'), '--project', join(dir, 'proj'), '--stage', 'prepare']);
  const meta = existsSync(join(dir, 'proj/clip.meta.json')) ? JSON.parse(readFileSync(join(dir, 'proj/clip.meta.json'), 'utf8')) : null;
  ok(cp.status === 0 && meta && meta.shots.filter((x) => x.w < 1440).length === 2 && meta.shots.every((x) => x.w <= 1440) && meta.rings === 1, `clip prepare: 2 zoomed shots from directed cam events + 1 ring (${meta ? meta.shots.map((x) => Math.round(x.w)).join('/') : 'n/a'})${cp.status === 0 ? '' : '\n' + (cp.stderr || cp.stdout).slice(-500)}`);
  writeFileSync(join(dir, 'focus-bad.json'), focus([700, 1200]));
  run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus-bad.json'), '--video', `main=${src}`, '--out', join(dir, 'beats-bad.json')]);
  const bad2 = run('node', [join(S, 'build-clip.mjs'), '--spec', join(dir, 'clip.json'), '--beats', join(dir, 'beats-bad.json'), '--project', join(dir, 'proj-bad'), '--stage', 'prepare']);
  ok(bad2.status === 1 && /too small to read/.test(bad2.stderr), 'clip-lint refuses a shot wider than maxWidth (unreadable at 645 px)');
  // explicit camera: accent rings still appear, and one legal `wide` shot does not exempt a separate over-wide shot (regressions from review)
  const explicit = (camera) => { writeFileSync(join(dir, 'clip-x.json'), JSON.stringify({ feature: 'cx', video: 'main.mp4', from: 'go', dur: 6, maxWidth: 900, camera })); return run('node', [join(S, 'build-clip.mjs'), '--spec', join(dir, 'clip-x.json'), '--beats', join(dir, 'beats.json'), '--project', join(dir, 'proj-x'), '--stage', 'prepare']); };
  const ex1 = explicit([{ at: 2, rect: [100, 100, 700, 437.5] }, { at: 5, wide: true }]);
  const exMeta = existsSync(join(dir, 'proj-x/clip.meta.json')) ? JSON.parse(readFileSync(join(dir, 'proj-x/clip.meta.json'), 'utf8')) : null;
  ok(ex1.status === 0 && exMeta && exMeta.rings === 1, `explicit camera keeps accent rings (${exMeta ? exMeta.rings : 'n/a'} ring)${ex1.status === 0 ? '' : '\n' + (ex1.stderr || ex1.stdout).slice(-300)}`);
  const ex2 = explicit([{ at: 2, wide: true }, { at: 4, rect: [0, 0, 1200, 750] }]);
  ok(ex2.status === 1 && /too small to read/.test(ex2.stderr), 'a wide shot does not exempt a separate over-wide shot');
  if (!quick && cp.status === 0) {
    const out = join(dir, 'clip.mp4');
    const rr = run('node', [join(S, 'build-clip.mjs'), '--spec', join(dir, 'clip.json'), '--beats', join(dir, 'beats.json'), '--project', join(dir, 'proj'), '--stage', 'render', '--out', out]);
    const pr2 = run('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'csv=p=0', out]);
    ok(rr.status === 0 && pr2.stdout.trim().startsWith('1440,900,30/1') && existsSync(join(dir, 'clip-poster.jpg')), `clip rendered 1440x900 @30fps with a poster (${pr2.stdout.trim()})`);
  }
}

// 5c launch format: hi-res synthetic source + shot list -> silent 1080p launch film
{
  const dir = join(work, 'launch'); mkdirSync(dir, { recursive: true }); const src = join(dir, 'main.mp4');
  const mk = run('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=2880x1800:rate=30:duration=12', '-vf', "drawbox=x=0:y=ih-8:w=iw:h=8:color=black:t=fill,drawbox=x=0:y=ih-8:w=iw:h=8:color=0xff00ff:t=fill:enable='between(t,1,1.4)+between(t,6,6.4)'", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', src]);
  writeFileSync(join(dir, 'marks.json'), JSON.stringify([{ beat: 'open', seat: 'main', t: 1.4 }, { beat: 'press', seat: 'main', t: 6.4 }]));
  const focusEv = (camW) => JSON.stringify([{ seat: 'main', kind: 'cam', t: 3.0, x: 120, y: 80, w: camW, h: 440 }, { seat: 'main', kind: 'click', t: 3.8, x: 300, y: 200, w: 120, h: 40 },
    { seat: 'main', kind: 'look', t: 6.3, x: 100, y: 100, w: 300, h: 60 },   // the PREVIOUS shot's look(), inside the focus shot's lookback window
    { seat: 'main', kind: 'look', t: 6.6, x: 600, y: 300, w: 140, h: 40 }, { seat: 'main', kind: 'click', t: 7.6, x: 600, y: 300, w: 140, h: 40, hold: 0.35 }]);
  writeFileSync(join(dir, 'focus.json'), focusEv(700));
  const d3 = run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus.json'), '--video', `main=${src}`, '--out', join(dir, 'beats.json')]);
  ok(mk.status === 0 && d3.status === 0, 'launch: hi-res synthetic source with two beats + focus events');
  const lspec = (focusDur) => ({ feature: 'selftest', video: 'main.mp4', opener: { words: ['Selftest'], line: 'A launch film from synthetic footage.' },
    shots: [{ type: 'screen', from: 'open', to: 'press', say: [{ title: 'Open the page', sub: 'The first line of narration.' }, { at: { beat: 'open', plus: 2 }, title: 'Zoom in', sub: 'It switches mid-shot, with the camera.' }] },
      { type: 'focus', from: 'press', dur: focusDur, say: { title: 'Press it', sub: 'A focus shot keeps the rail.' } }, { type: 'title', text: 'Every frame, on brand.', accent: [1] },
      { type: 'card', eyebrow: 'Done', title: 'It rendered.', sub: 'Synthetic source', row: { label: 'Selftest', meta: 'demo kit', cta: 'Open' } }],
    outro: { line: 'All good.', url: 'example.com' } });
  const prep = (spec, beats, name) => { writeFileSync(join(dir, `${name}.json`), JSON.stringify(spec)); return run('node', [join(S, 'build-launch.mjs'), '--spec', join(dir, `${name}.json`), '--beats', join(dir, beats), '--project', join(dir, name), '--stage', 'prepare']); };
  const lp = prep(lspec(0.9), 'beats.json', 'film');
  const lmeta = existsSync(join(dir, 'film/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film/launch.meta.json'), 'utf8')) : null;
  const scr = lmeta?.shots.find((x) => x.type === 'screen'), foc = lmeta?.shots.find((x) => x.type === 'focus');
  ok(lp.status === 0 && scr && scr.camera[0].wide && scr.camera.some((c) => !c.wide && c.w <= 900) && foc.zoom > 1.2, `launch prepare: wide establishing framing + zoomed framing + isolated focus shot (${scr ? scr.camera.map((c) => c.w).join('/') : 'n/a'}, focus ${foc ? foc.zoom : 'n/a'}x)${lp.status === 0 ? '' : '\n' + (lp.stderr || lp.stdout).slice(-600)}`);
  ok(lp.status === 0 && !/past the click/.test(lp.stderr), 'launch: a focus shot that ends right after its click raises no warning');
  ok(foc && foc.crop.x === 582 && foc.crop.w === 176, `launch: a focus shot isolates its own look(), not the previous shot's inside the window (crop ${foc ? JSON.stringify(foc.crop) : 'n/a'})`);
  ok(!/past the click/.test(prep(lspec(1.25), 'beats.json', 'film-held').stderr), 'launch: a held press() is measured from its release (no warning for a shot ending right after it)');
  { const sp = lspec(0.9); sp.shots[0].say = [{ title: 'Same', sub: 'First explanation.' }, { at: { beat: 'open', plus: 2 }, title: 'Same', sub: 'A new explanation.' }];
    const r1 = prep(sp, 'beats.json', 'film-sub'); const m1 = existsSync(join(dir, 'film-sub/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film-sub/launch.meta.json'), 'utf8')) : null;
    ok(r1.status === 0 && m1 && m1.rail.filter((x) => x.title === 'Same').length === 2, 'launch narration: a new explanation under the same title is a new line, not dropped'); }
  { const sp = lspec(0.9); sp.shots = [sp.shots[0], sp.shots[2], { type: 'focus', from: 'press', dur: 0.9 }];
    const r2 = prep(sp, 'beats.json', 'film-restore'); const m2 = existsSync(join(dir, 'film-restore/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film-restore/launch.meta.json'), 'utf8')) : null;
    const last = m2?.rail?.[m2.rail.length - 1];
    ok(r2.status === 0 && last && last.shown.length === 2 && last.shown[1][0] > last.shown[0][1], `launch narration: a title hides the line and the next footage without its own say brings it back (${last ? JSON.stringify(last.shown) : 'n/a'})`); }
  { const stale = lspec(0.9); stale.shots[2].text = 'Copy changed after prepare.'; writeFileSync(join(dir, 'film-stale.json'), JSON.stringify(stale));
    const rs = run('node', [join(S, 'build-launch.mjs'), '--spec', join(dir, 'film-stale.json'), '--beats', join(dir, 'beats.json'), '--project', join(dir, 'film'), '--stage', 'render', '--out', join(dir, 'stale.mp4')]);
    ok(rs.status === 2 && /changed since --stage prepare/.test(rs.stderr), 'launch render-only refuses a spec that changed since prepare (no film with stale copy)'); }
  { const nv = lspec(0.9); nv.video = 'nowhere/main.mp4'; const rn = prep(nv, 'beats.json', 'film-novideo');
    ok(rn.status === 2 && /resolve against the spec's own folder/.test(rn.stderr), 'launch: a video path that does not resolve fails with where to put the spec'); }
  ok(lmeta && lmeta.stage[0] === 1500 && lmeta.rail.length === 3 && Math.abs(lmeta.rail[1].t - (lmeta.rail[0].t + 1.45)) < 0.05 && lmeta.rail[1].t < lmeta.rail[2].t, `launch narration rail: 3 lines in order, one switching mid-shot, stage 1500px (${lmeta ? JSON.stringify(lmeta.rail.map((r) => r.t)) : 'n/a'})`);
  { const bare = lspec(0.9); bare.shots.forEach((x) => delete x.say); const lb = prep(bare, 'beats.json', 'film-bare'); const bm = existsSync(join(dir, 'film-bare/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film-bare/launch.meta.json'), 'utf8')) : null;
    ok(lb.status === 0 && bm && bm.rail === false && bm.stage[0] === 1920, 'launch without narration: no rail, full-width stage'); }
  const lw = prep(lspec(2.5), 'beats.json', 'film-late');
  ok(/past the click/.test(lw.stderr), 'launch warns when a focus shot runs on past a click inside it (the crop would show whatever moves into place)');
  writeFileSync(join(dir, 'focus-wide.json'), focusEv(1200));
  run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus-wide.json'), '--video', `main=${src}`, '--out', join(dir, 'beats-wide.json')]);
  const lbad = prep(lspec(0.9), 'beats-wide.json', 'film-wide');
  ok(lbad.status === 1 && /unreadable in a feed/.test(lbad.stderr), 'launch refuses a zoomed framing wider than maxWidth');
  // the oversized cursor, drawn from the recorded mouse path: a click in the screen shot, a glide + held press in the focus shot
  {
    const glideEv = (t, a, b, n = 16) => Array.from({ length: n + 1 }, (_, k) => ({ seat: 'main', type: 'move', t: Number((t + k * 0.03).toFixed(3)), x: Math.round(a[0] + (b[0] - a[0]) * k / n), y: Math.round(a[1] + (b[1] - a[1]) * k / n) }));
    const events = [{ seat: 'main', type: 'move', t: 1.0, x: 300, y: 200 }, ...glideEv(3.0, [300, 200], [360, 220]), { seat: 'main', type: 'down', t: 3.8, x: 360, y: 220 }, { seat: 'main', type: 'up', t: 3.86, x: 360, y: 220 },
      ...glideEv(6.6, [360, 220], [670, 320]), { seat: 'main', type: 'down', t: 7.6, x: 670, y: 320 }, { seat: 'main', type: 'up', t: 7.95, x: 670, y: 320 }];
    const withMouse = (cursor, name) => { writeFileSync(join(dir, `mouse-${name}.json`), JSON.stringify({ cursor, events })); return run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus.json'), '--mouse', join(dir, `mouse-${name}.json`), '--video', `main=${src}`, '--out', join(dir, `beats-${name}.json`)]); };
    withMouse('none', 'cursor');
    const bm = JSON.parse(readFileSync(join(dir, 'beats-cursor.json'), 'utf8'));
    const lc = prep(lspec(0.9), 'beats-cursor.json', 'film-cursor');
    const cm = existsSync(join(dir, 'film-cursor/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film-cursor/launch.meta.json'), 'utf8')) : null;
    const doc = existsSync(join(dir, 'film-cursor/index.html')) ? readFileSync(join(dir, 'film-cursor/index.html'), 'utf8') : '';
    // replay the emitted tweens: where is the tip at film time T?
    const tw = [...doc.matchAll(/tl\.fromTo\("#cur",\{x:([-\d.]+),y:([-\d.]+)\},\{x:([-\d.]+),y:([-\d.]+),duration:([\d.]+),ease:"none",immediateRender:false\},([\d.]+)\)/g)].map((m) => m.slice(1).map(Number));
    const tipAt = (T) => { const w = tw.filter((x) => x[5] <= T).pop(); if (!w) return null; const u = Math.min(1, (T - w[5]) / w[4]); return { x: w[0] + (w[2] - w[0]) * u, y: w[1] + (w[3] - w[1]) * u }; };
    const foc2 = cm?.shots.find((x) => x.type === 'focus'), pressV = bm.seats.main.beats.find((b) => b.name === 'press').video, downV = bm.mouse.events.filter((e) => e.type === 'down')[1].video;
    const T = foc2 ? foc2.at + (downV - (pressV + 0.55)) : 0, tip = tipAt(T);
    ok(lc.status === 0 && cm?.cursor?.presses === 2 && tip && Math.hypot(tip.x - 750, tip.y - 540) < 4, `launch cursor: drawn from the mouse path, 2 presses, tip on the clicked element at the focus zoom (${tip ? `${tip.x.toFixed(1)},${tip.y.toFixed(1)}` : 'n/a'} vs 750,540)${lc.status === 0 ? '' : '\n' + (lc.stderr || lc.stdout).slice(-600)}`);
    const gone = foc2 && tipAt(foc2.at + foc2.dur + 0.04);   // the focus shot's release is after its cut: the exit must still be done by then
    ok(gone && gone.y > 1080, `launch cursor: a late release still leaves the stage by the cut, never over the next title (tip y ${gone ? gone.y.toFixed(0) : 'n/a'} just after the cut)`);
    { // a release 0.15 s before the cut: the arrow stays on the control until it lets go, and the builder warns the shot ends too soon
      const vb = bm.seats.main.beats, vO = vb.find((x) => x.name === 'open').video, vP = vb.find((x) => x.name === 'press').video;
      const wallOf = (v) => 1.4 + ((v - vO) * (6.4 - 1.4)) / (vP - vO), upV = vP + 0.55 + 0.75;
      const late = events.map((e) => (e.type === 'up' && e.t > 7 ? { ...e, t: Number(wallOf(upV).toFixed(3)) } : e));
      writeFileSync(join(dir, 'mouse-late.json'), JSON.stringify({ cursor: 'none', events: late }));
      run('node', [join(S, 'detect-beats.mjs'), '--marks', join(dir, 'marks.json'), '--focus', join(dir, 'focus.json'), '--mouse', join(dir, 'mouse-late.json'), '--video', `main=${src}`, '--out', join(dir, 'beats-late.json')]);
      const ll = prep(lspec(0.9), 'beats-late.json', 'film-late-up');
      const ld = existsSync(join(dir, 'film-late-up/index.html')) ? readFileSync(join(dir, 'film-late-up/index.html'), 'utf8') : '';
      const ltw = [...ld.matchAll(/tl\.fromTo\("#cur",\{x:([-\d.]+),y:([-\d.]+)\},\{x:([-\d.]+),y:([-\d.]+),duration:([\d.]+),ease:"none",immediateRender:false\},([\d.]+)\)/g)].map((m) => m.slice(1).map(Number));
      const ltip = (T) => { const w = ltw.filter((x) => x[5] <= T).pop(); if (!w) return null; const u = Math.min(1, (T - w[5]) / w[4]); return { x: w[0] + (w[2] - w[0]) * u, y: w[1] + (w[3] - w[1]) * u }; };
      const lf = JSON.parse(readFileSync(join(dir, 'film-late-up/launch.meta.json'), 'utf8')).shots.find((x) => x.type === 'focus'), hold = ltip(lf.at + 0.75 - 0.02);
      ok(/too soon for the cursor/.test(ll.stderr) && hold && Math.hypot(hold.x - 750, hold.y - 540) < 4, `launch cursor: a late release keeps the arrow on the control until it lets go, and warns (${hold ? `${hold.x.toFixed(1)},${hold.y.toFixed(1)}` : 'n/a'})`);
    }
    const first = tw[0], scr0 = cm?.shots.find((x) => x.type === 'screen');
    ok(first && scr0 && first[1] > 1080 && first[5] >= scr0.at - 0.01 && first[5] < scr0.at + 0.1 && /class="ptr"/.test(doc) && !/class="pointer"/.test(doc), 'launch cursor: enters from below the stage when the first shot starts; the card CTA uses the same arrow');
    withMouse('arrow', 'baked');
    const lbk = prep(lspec(0.9), 'beats-baked.json', 'film-baked');
    const bk = existsSync(join(dir, 'film-baked/launch.meta.json')) ? JSON.parse(readFileSync(join(dir, 'film-baked/launch.meta.json'), 'utf8')) : null;
    ok(/drew its own cursor/.test(lbk.stderr) && bk?.cursor?.off && !/id="cur"/.test(readFileSync(join(dir, 'film-baked/index.html'), 'utf8')), 'launch cursor: a capture with its cursor baked in gets no second one (warns)');
    const sharp = lspec(0.9); sharp.shots[1].zoom = 4.2;
    ok(/blows the capture up/.test(prep(sharp, 'beats-cursor.json', 'film-sharp').stderr), 'launch warns when a focus zoom blows the capture up past the sharpness limit');
    const away = lspec(0.9); away.shots[1].rect = [100, 560, 300, 120];
    ok(/happens outside the frame/.test(prep(away, 'beats-cursor.json', 'film-away').stderr), 'launch warns when a click happens outside the frame (the effect without its cause)');
  }
  if (existsSync(join(dir, 'film/index.html')) && BRAND.logo('light') !== BRAND.logo('dark')) {
    const bad = join(dir, 'white-logo.html');
    writeFileSync(bad, readFileSync(join(dir, 'film/index.html'), 'utf8').replace(new RegExp(reEsc(BRAND.logo('light')), 'g'), BRAND.logo('dark')));
    ok(lintProject(bad, BRAND).some((x) => x.rule === 'logo'), 'brand-lint rejects the dark-background logo on a light launch film');
  }
  if (!quick && lp.status === 0) {
    const out = join(dir, 'film.mp4');
    const rr = run('node', [join(S, 'build-launch.mjs'), '--spec', join(dir, 'film.json'), '--beats', join(dir, 'beats.json'), '--project', join(dir, 'film'), '--stage', 'render', '--out', out]);
    const pr3 = run('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_type,width,height:format=duration', '-of', 'json', out]);
    const j3 = JSON.parse(pr3.stdout || '{}'), vs = (j3.streams || []).find((x) => x.codec_type === 'video');
    ok(rr.status === 0 && vs?.width === 1920 && vs?.height === 1080 && Math.abs(Number(j3.format?.duration) - lmeta.duration) < 0.5 && !(j3.streams || []).some((x) => x.codec_type === 'audio') && existsSync(join(dir, 'film-poster.jpg')), `launch rendered 1920x1080, ${Number(j3.format?.duration || 0).toFixed(1)}s ≈ ${lmeta.duration}s, silent, with a poster`);
  }
}

// 6 capture-kit against a real browser (optional)
if (process.argv.includes('--capture')) {
  let pw = null; try { pw = createRequire(join(process.cwd(), 'noop.js'))('playwright'); } catch { /* handled below */ }
  if (!pw) console.log('SKIP  capture-kit test: `playwright` does not resolve from cwd (run from a project with playwright installed)');
  else {
    const page = join(work, 'page.html');
    writeFileSync(page, '<!doctype html><meta charset="utf-8"><body style="background:#0c0c0c;color:#fcfef0;font:28px sans-serif;padding:60px"><h1>Capture test</h1><input id="n" placeholder="name" style="font-size:28px"> <button id="b">Save</button>');
    const cap = createRequire(import.meta.url)('./capture-kit.cjs');
    let out = null, err = '', initRan = false;
    try {
      const s = await cap.start({ playwright: pw, baseURL: 'file://', outDir: join(work, 'cap') });
      const me = await s.seat('main');
      await me.page.goto('file://' + page);
      initRan = await me.page.evaluate(() => !!document.getElementById('__ck_hide') && !!document.querySelector('body ~ div, html > div'));
      await s.beat('main', 'open');
      await me.type(me.page.locator('#n'), 'Maya', 40); await s.beat('main', 'save');
      await me.click(me.page.locator('#b')); await me.wait(1200);
      out = await s.finish();
    } catch (e) { err = e.message; }
    ok(!!out && existsSync(out.videos.main), `capture-kit recorded an MP4 (${err || 'ok'})`);
    let hi = ''; try { const s2 = await cap.start({ playwright: pw, baseURL: 'file://', outDir: join(work, 'cap-hi'), hires: true }); const m2 = await s2.seat('main'); await m2.page.goto('file://' + page); await s2.beat('main', 'a'); await m2.type(m2.page.locator('#n'), 'Hi', 40); await m2.press(m2.page.locator('#b'), 200); const o2 = await s2.finish(); ok(o2.focus.some((f) => f.kind === 'click' && f.w > 0), 'capture-kit press() records a held click'); hi = run('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'csv=p=0', o2.videos.main]).stdout.trim(); ok(o2.focus.some((f) => f.kind === 'type'), 'capture-kit records focus events (where each click/type happened)'); } catch (e) { hi = 'ERR ' + e.message; }
    ok(hi.startsWith('2880,1800,30/1'), `hi-res capture writes true device-pixel frames (${hi})`);
    try {   // the mouse path: the first glide comes up from the bottom edge; a click inside an iframe is recorded in page coordinates
      const fpage = join(work, 'frame.html');
      writeFileSync(fpage, `<!doctype html><meta charset="utf-8"><body style="margin:0;padding:80px 0 0 120px"><iframe id="f" style="width:600px;height:300px;border:7px solid #333" srcdoc="<body style='margin:0;padding:40px'><button id='ib' style='font-size:28px'>Inside</button></body>"></iframe>`);
      const s3 = await cap.start({ playwright: pw, baseURL: 'file://', outDir: join(work, 'cap-frame'), record: false, cursor: 'none' });
      const m3 = await s3.seat('main'); await m3.page.goto('file://' + fpage);
      const btn = m3.page.frameLocator('#f').locator('#ib'); const bb = await btn.boundingBox();
      await m3.click(btn); await m3.wait(300);
      const o3 = await s3.finish(), dn = o3.mouse.find((e) => e.type === 'down'), cx = bb.x + bb.width / 2, cy = bb.y + bb.height / 2;
      ok(o3.mouse[0]?.y >= 896 && dn && Math.hypot(dn.x - cx, dn.y - cy) < 3, `capture-kit mouse path: first glide from the bottom edge (y ${o3.mouse[0]?.y}), iframe click in page coordinates (${dn ? `${dn.x},${dn.y}` : 'none'} vs ${Math.round(cx)},${Math.round(cy)})`);
    } catch (e) { ok(false, `capture-kit mouse path (${e.message})`); }
    ok(initRan, 'capture-kit init script ran in the page (dev-chrome style + drawn cursor present)');
    if (out) {
      const bp = join(work, 'cap-beats.json');
      const d = run('node', [join(S, 'detect-beats.mjs'), '--marks', out.marksPath, '--video', `main=${out.videos.main}`, '--out', bp]);
      const b = d.status === 0 ? JSON.parse(readFileSync(bp, 'utf8')).seats.main.beats : [];
      ok(b.length === 2 && b[1].video > b[0].video, `real-browser beats detected in order (${b.map((x) => x.video).join(', ')})`);
      const spec2 = { feature: 'selftest-capture', intro: { dur: 1.6, eyebrow: 'Selftest', title: 'Capture', subtitle: 'Real browser.' }, outro: { dur: 1.6, eyebrow: 'Selftest', title: 'Done.', chips: ['Beat'] }, seats: { main: { video: out.videos.main } }, chapters: [{ title: 'Type a name', sub: 'Real Playwright capture.', segments: [{ from: 'open', to: 'save' }] }] };
      writeFileSync(join(work, 'demo2.json'), JSON.stringify(spec2));
      const p2 = run('node', [join(S, 'build-demo.mjs'), '--spec', join(work, 'demo2.json'), '--beats', bp, '--project', join(work, 'project2'), '--stage', 'prepare']);
      ok(p2.status === 0, `build-demo prepare from a real capture${p2.status === 0 ? '' : '\n' + (p2.stderr || p2.stdout).slice(-600)}`);
    }
  }
}
rmSync(work, { recursive: true, force: true });
console.log(failed ? `\nselftest: ${failed} FAILED` : `\nselftest: all passed (hyperframes@${kit.hyperframes})`);
process.exit(failed ? 1 : 0);
