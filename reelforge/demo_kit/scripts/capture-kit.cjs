// capture-kit — Playwright helpers for /record-demo captures that will be polished with the demo kit.
//
//   const kit = require('<demo-kit>/scripts/capture-kit.cjs');
//   const s = await kit.start({ playwright: require('playwright'), baseURL: 'http://localhost:3000', outDir: '/tmp/demo-raw' });
//   const me = await s.seat('main');                       // or s.seat('candidate', { cookies: [kit.supabaseCookie(session, baseURL)] })
//   await me.page.goto(baseURL + '/settings', { waitUntil: 'networkidle' });
//   await s.beat('main', 'name');                          // sync flash + mark: the polish stage cuts on these
//   await me.type(me.page.locator('input').first(), 'Maya Chen');
//   const out = await s.finish();                          // -> { marks, videos: { main: '/tmp/demo-raw/main.mp4' }, marksPath }
//
// Every seat records 1440x900 by default (pass viewport/dsf/mobile for a phone: { viewport: {width:390,height:844}, dsf: 3, mobile: true }; deviceScaleFactor 2 is baked in: it supersamples text; 1x captures read as soft).
//
// Hi-res mode (start({ hires: true, fps: 30 })) is for zoomed clips and reels: Playwright's recordVideo cannot record above the CSS-pixel
// size, so instead a screenshot loop (Chrome DevTools, device pixels) writes TRUE 2880x1800 frames with timestamps, assembled at the end to a
// constant-fps 2880x1800 MP4. Frame rate depends on the page (~20 fps on a light page, less on heavy ones). Every glide/click/type also records
// WHERE it happened (CSS px) into focus.json: that is what the clip camera zooms to.
// Dev chrome (the Next.js dev overlay, plus anything in start({ hideSelectors, hideText })) is hidden and a large arrow cursor is drawn (a dot on mobile; none
// for launch films, which draw an oversized one from the mouse path recorded into mouse.json).
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// The arrow every demo uses (24-unit viewBox; its tip, the hot-spot, sits at 22.9% / 13.4%). build-launch draws the same one.
const ARROW_PATH = 'M5.5 3.21V20.8c0 .45.54.67.85.35l4.86-4.86a.5.5 0 0 1 .35-.15h6.87a.5.5 0 0 0 .35-.85L6.35 2.85a.5.5 0 0 0-.85.36Z';
const ARROW_TIP = { x: 0.229, y: 0.134 };

// NOTE: this function is serialized into the page by Playwright, so it must be self-contained: everything it needs arrives as the
// argument of addInitScript, never as a closure variable (a free variable throws in the page and silently disables everything).
const INIT = ({ extraHide, hideText, cursor, arrowPath, tip }) => {
  const css = 'nextjs-portal,[data-nextjs-dev-tools-button],[data-nextjs-toast]' + (extraHide ? ',' + extraHide : '') + '{display:none!important}';
  // React can drop a <style> that was added to <head> during hydration, so keep it on <html> and re-add it whenever it disappears.
  const ensureStyle = () => { if (!document.getElementById('__ck_hide')) { const st = document.createElement('style'); st.id = '__ck_hide'; st.textContent = css; document.documentElement.appendChild(st); } };
  // your app's dev-only floating pills, matched by exact text (hideText; class names change between builds): hide the fixed-position element that holds them
  const sweep = () => hideText.length && document.querySelectorAll('button,div,a,span').forEach((e) => {
    const t = (e.textContent || '').trim();
    if (e.children.length < 4 && hideText.includes(t)) {
      for (let n = e; n && n !== document.body; n = n.parentElement) if (getComputedStyle(n).position === 'fixed') { n.style.setProperty('display', 'none', 'important'); return; }
    }
  });
  const boot = () => {
    ensureStyle(); sweep();
    new MutationObserver(() => { ensureStyle(); sweep(); }).observe(document.documentElement, { childList: true, subtree: true });
    // The mouse path goes to the capture so a launch film can draw its own oversized cursor. Every frame reports in its own viewport's
    // CSS px; the capture adds an iframe's offset, so a click inside an embedded frame stays on the path.
    const send = (type, e) => { try { if (typeof window.__ckMouse === 'function') window.__ckMouse({ type, x: e.clientX, y: e.clientY }); } catch { /* recording only */ } };
    addEventListener('mousemove', (e) => send('move', e), true);
    addEventListener('mousedown', (e) => send('down', e), true);
    addEventListener('mouseup', (e) => send('up', e), true);
    if (window.top !== window || cursor === 'none') return;   // the drawn cursor lives in the top page only
    const c = document.createElement('div');
    if (cursor === 'dot') {   // touch captures: a dot is what a finger looks like
      c.style.cssText = 'position:fixed;z-index:2147483647;width:16px;height:16px;border-radius:50%;background:rgba(255,255,255,.92);box-shadow:0 0 0 2px rgba(0,0,0,.55),0 4px 14px rgba(0,0,0,.5);pointer-events:none;left:-40px;top:-40px;transform:translate(-50%,-50%);transition:transform .12s';
      addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
      addEventListener('mousedown', () => (c.style.transform = 'translate(-50%,-50%) scale(.7)'), true);
      addEventListener('mouseup', () => (c.style.transform = 'translate(-50%,-50%) scale(1)'), true);
    } else {                  // desktop: a large arrow whose tip is the click point
      const S = 30;
      c.style.cssText = `position:fixed;z-index:2147483647;width:${S}px;height:${S}px;pointer-events:none;left:-80px;top:-80px;transform-origin:${tip.x * 100}% ${tip.y * 100}%;transition:transform .1s;filter:drop-shadow(0 3px 5px rgba(0,0,0,.3))`;
      c.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="${S}" height="${S}"><path d="${arrowPath}" fill="#151515" stroke="#fcfef0" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
      addEventListener('mousemove', (e) => { c.style.left = (e.clientX - tip.x * S) + 'px'; c.style.top = (e.clientY - tip.y * S) + 'px'; }, true);
      addEventListener('mousedown', () => (c.style.transform = 'scale(.84)'), true);
      addEventListener('mouseup', () => (c.style.transform = 'scale(1)'), true);
    }
    document.documentElement.appendChild(c);
  };
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', boot) : boot();
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Cookie for a real Supabase session ({ access, refresh, ref } from mintSession) — auth-helpers 0.10 cookie format. */
function supabaseCookie(session, baseURL) {
  return { name: `sb-${session.ref}-auth-token`, value: encodeURIComponent(JSON.stringify([session.access, session.refresh, null, null, null])), url: baseURL };
}

/** Mint a real session for `email` via the admin magic-link route. Secrets come from the caller's env; never printed. Only for apps that use Supabase auth. */
async function mintSession({ url, publishableKey, secretKey, email }) {
  const h = (k) => ({ apikey: k, Authorization: `Bearer ${k}`, 'Content-Type': 'application/json' });
  // For an email that has no user yet, the FIRST generate_link creates the user and returns a signup token that /verify rejects as a magic link;
  // the second call returns a normal magic link. So try twice.
  let last = '';
  for (let attempt = 0; attempt < 5; attempt++) {
    const gl = await (await fetch(`${url}/auth/v1/admin/generate_link`, { method: 'POST', headers: h(secretKey), body: JSON.stringify({ type: 'magiclink', email }) })).json();
    if (!gl.hashed_token) { if (/rate limit/i.test(JSON.stringify(gl))) { await wait(4000 * (attempt + 1)); continue; } throw new Error('generate_link failed for ' + email); }
    const v = await (await fetch(`${url}/auth/v1/verify`, { method: 'POST', headers: h(publishableKey), body: JSON.stringify({ type: 'magiclink', token_hash: gl.hashed_token }) })).json();
    if (v.access_token) return { access: v.access_token, refresh: v.refresh_token, ref: new URL(url).hostname.split('.')[0], userId: gl.id };
    last = v.msg || v.error_description || v.code;
    if (/rate limit/i.test(String(last))) await wait(4000 * (attempt + 1));   // rate limited: back off and retry
  }
  throw new Error('verify failed: ' + last);
}

/** Screenshot loop: true device-pixel frames (2880x1800 at dsf 2) via Chrome DevTools, each with a capture timestamp. */
async function startLoop(context, page, dir, vp) {
  fs.mkdirSync(dir, { recursive: true });
  const cdp = await context.newCDPSession(page);
  const frames = []; let running = true, n = 0; const t0 = Date.now();
  // clip.scale 2 = device pixels (the default would return CSS pixels)
  const withTimeout = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('screenshot timeout')), ms))]);
  const shot = async () => {
    for (let attempt = 0; attempt < 3; attempt++) {
      // Never overlap captures: on a timeout wait for the pending one to settle (bounded) before asking again, or slow pages pile up requests.
      const pending = cdp.send('Page.captureScreenshot', { format: 'jpeg', quality: 88, optimizeForSpeed: true, fromSurface: true, clip: { x: 0, y: 0, width: vp.width, height: vp.height, scale: vp.dsf } });
      try { const r = await withTimeout(pending, 2500); return Buffer.from(r.data, 'base64'); }
      catch {
        const settled = await Promise.race([pending.then(() => true, () => true), wait(10000).then(() => false)]);
        if (!settled) throw new Error('screenshot stuck');   // the loop skips this frame and tries again
        await wait(120);
      }
    }
    return page.screenshot({ type: 'jpeg', quality: 88, caret: 'initial', scale: 'device' });
  };
  const done = (async () => {
    while (running) {
      const t = Date.now();
      let buf; try { buf = await shot(); } catch { if (!running) break; await wait(40); continue; }
      const file = path.join(dir, `f${String(n++).padStart(6, '0')}.jpg`); fs.writeFileSync(file, buf); frames.push({ file, t: (t - t0) / 1000 });
    }
  })();
  return { frames, dir, stop: async () => { running = false; await done; } };
}

/** Assemble timestamped frames into a constant-fps MP4 (frames are held for as long as the page did not change). */
function assemble(frames, out, fps, vp) {
  const list = path.join(path.dirname(out), path.basename(out, '.mp4') + '-frames.txt');
  const lines = ['ffconcat version 1.0'];
  frames.forEach((f, i) => { lines.push(`file '${f.file.replace(/'/g, "'\\''")}'`); lines.push(`duration ${Math.max(0.02, ((frames[i + 1]?.t ?? f.t + 0.25) - f.t)).toFixed(3)}`); });
  fs.writeFileSync(list, lines.join('\n'));
  const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', list, '-vf', `scale=${vp.width * vp.dsf}:${vp.height * vp.dsf}:flags=lanczos,fps=${fps},format=yuv420p`, '-c:v', 'libx264', '-crf', '15', '-preset', 'veryfast', '-an', out]);
  fs.rmSync(list, { force: true });
  if (r.status !== 0) throw new Error('capture-kit: assembling hi-res frames failed — ' + String(r.stderr).slice(0, 300));
}

/** cursor: 'arrow' (desktop default) | 'dot' (mobile default: a touch) | 'none' (launch films: build-launch draws an oversized one from the
 *  recorded mouse path, so nothing may be baked into the frames). The mouse path is recorded in every mode (mouse.json). */
async function start({ playwright, baseURL, outDir, record = true, hires = false, fps = 30, hideSelectors = '', hideText = [], acceptDialogs = true, launch = {}, viewport = { width: 1440, height: 900 }, dsf = 2, mobile = false, cursor = mobile ? 'dot' : 'arrow' }) {
  if (!['arrow', 'dot', 'none'].includes(cursor)) throw new Error(`capture-kit: cursor must be 'arrow', 'dot' or 'none' (got ${cursor})`);
  if (!playwright) throw new Error('capture-kit: pass { playwright: require("playwright") }');
  fs.mkdirSync(outDir, { recursive: true });
  const T0 = Date.now(), marks = [], seats = {}, focus = [], mouse = [];
  // The mouse path, bounded: moves are kept at most every 16 ms per seat (a glide steps every ~12-40 ms, so a glide keeps nearly every
  // step), the latest skipped move is kept as a tail and written before the next kept event (a glide still ends exactly where it
  // stopped), presses are always kept, and the whole path is capped.
  const MOUSE_CAP = 200000, kept = {}, tail = {}, here = {};
  let mouseCapped = false;
  const push = (ev) => {
    if (mouse.length >= MOUSE_CAP) { if (!mouseCapped) { mouseCapped = true; console.warn(`capture-kit: mouse path capped at ${MOUSE_CAP} events; the rest of this take has no launch cursor`); } return; }
    mouse.push(ev); kept[ev.seat] = ev;
  };
  const recordMouse = (ev) => {
    here[ev.seat] = { x: ev.x, y: ev.y };
    if (ev.type === 'move' && kept[ev.seat] && ev.t - kept[ev.seat].t < 0.016) { tail[ev.seat] = ev; return; }
    if (tail[ev.seat]) { push(tail[ev.seat]); tail[ev.seat] = null; }
    push(ev);
  };
  // launch: extra Playwright launch options. Pass { channel: 'chrome' } for pages that play H.264/AAC video (lectures, solution videos): Playwright's bundled Chromium cannot decode them.
  const browser = await playwright.chromium.launch({ args: ['--use-fake-ui-for-media-stream'], ...launch });

  async function seat(name, { cookies = [], storageState } = {}) {
    const opts = { viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: dsf, ...(mobile ? { isMobile: true, hasTouch: true } : {}), permissions: ['clipboard-read', 'clipboard-write'], ...(storageState ? { storageState } : {}) };
    if (record && !hires) opts.recordVideo = { dir: path.join(outDir, name), size: { width: viewport.width, height: viewport.height } };
    const context = await browser.newContext(opts);
    if (cookies.length) await context.addCookies(cookies);
    // An iframe's content origin in the top page's viewport (Playwright measures it, cross-origin too). A press is always measured fresh
    // (an iframe that moved must not misplace a click); moves reuse a measurement for 150 ms.
    const offsets = new Map();
    const frameOffset = (frame, fresh) => {
      const c = offsets.get(frame);
      if (!fresh && c && Date.now() - c.at < 150) return c.p;
      const p = (async () => {
        const el = await frame.frameElement().catch(() => null);
        const box = el && await el.boundingBox().catch(() => null);
        if (!box) return null;
        const inset = await el.evaluate((n) => { const st = getComputedStyle(n); return { x: parseFloat(st.borderLeftWidth) + parseFloat(st.paddingLeft), y: parseFloat(st.borderTopWidth) + parseFloat(st.paddingTop) }; }).catch(() => ({ x: 0, y: 0 }));
        return { x: box.x + inset.x, y: box.y + inset.y };
      })();
      offsets.set(frame, { at: Date.now(), p });
      return p;
    };
    await context.exposeBinding('__ckMouse', async (src, e) => {
      const t = Number(((Date.now() - T0) / 1000).toFixed(3));
      if (!e || !['move', 'down', 'up'].includes(e.type) || !Number.isFinite(e.x) || !Number.isFinite(e.y)) return;
      if (!seats[name] || src.page !== seats[name].page) return;   // the seat's own tab only (a popup has its own viewport)
      let x = e.x, y = e.y;
      if (src.frame && src.frame !== src.page.mainFrame()) { const o = await frameOffset(src.frame, e.type !== 'move'); if (!o) return; x += o.x; y += o.y; }
      recordMouse({ seat: name, type: e.type, t, x: Math.round(x), y: Math.round(y) });
    });
    await context.addInitScript(INIT, { extraHide: hideSelectors, hideText, cursor, arrowPath: ARROW_PATH, tip: ARROW_TIP });
    const page = await context.newPage();
    if (acceptDialogs) page.on('dialog', (d) => d.accept());
    const note = (kind, b, extra = {}) => focus.push({ seat: name, kind, t: Number(((Date.now() - T0) / 1000).toFixed(2)), x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height), ...extra });
    // A timed, eased move, the way a hand moves: Playwright's own `steps` fire as fast as the protocol allows, so a glide took ~0.1 s and the
    // cursor seemed to jump. 0.45-0.9 s by distance, cubic in-out, timed by the clock so a slow page cannot stretch or squash it.
    // The start is the last known position (kept across navigations); with none yet, the mouse comes up from the bottom edge.
    const moveTo = async (x, y) => {
      const a = here[name] || { x, y: viewport.height - 1 };
      const d = Math.hypot(x - a.x, y - a.y);
      if (d < 2) { await page.mouse.move(x, y); here[name] = { x, y }; return; }
      const D = Math.min(900, Math.max(450, 300 + d * 0.6)), t0 = Date.now();
      for (;;) {
        const u = Math.min(1, (Date.now() - t0) / D), e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
        const px = a.x + (x - a.x) * e, py = a.y + (y - a.y) * e;
        await page.mouse.move(px, py); here[name] = { x: px, y: py };
        if (u >= 1) return;
        await wait(12);
      }
    };
    const glide = async (loc) => { const l = loc.first(); await l.scrollIntoViewIfNeeded().catch(() => {}); const b = await l.boundingBox(); if (!b) throw new Error('capture-kit: element has no box'); note('glide', b); await moveTo(b.x + b.width / 2, b.y + b.height / 2); await wait(220); };
    const click = async (loc) => { await glide(loc); const b = await loc.first().boundingBox(); if (b) note('click', b); await loc.first().click(); };
    /** A visible press for launch films: glide, hold the mouse down (the cursor shrinks) for `holdMs`, then release. Use it on a
     *  button whose click changes the layout (it opens a panel): the press reads on screen BEFORE the layout moves, so a focus shot can
     *  end on it. Records a 'click' focus event at the press with `hold` (seconds) so the launch builder can measure from the release. */
    const press = async (loc, holdMs = 350) => { await glide(loc); const b = await loc.first().boundingBox(); if (b) note('click', b, { hold: Number((holdMs / 1000).toFixed(2)) }); await page.mouse.down(); await wait(holdMs); await page.mouse.up(); };
    const type = async (loc, text, delay = 60) => { await click(loc); const b = await loc.first().boundingBox(); if (b) note('type', b); await loc.first().pressSequentially(text, { delay }); };
    /** Direct the clip camera. cam({ x, y, w }) frames exactly that CSS-px region (height follows the 16:10 canvas); look(locator) frames an element
     *  with breathing room. Call it right before the action you want the viewer to watch. When a clip has any cam/look events they are the ONLY
     *  camera targets (click/type still get an accent ring); without them the camera falls back to following clicks and typing. */
    const cam = async (r) => { note('cam', { x: r.x, y: r.y, width: r.w, height: r.h ?? r.w * 0.625 }); };
    const look = async (target) => { const b = await target.first().boundingBox(); if (b) note('look', b); };
    seats[name] = { name, context, page, glide, moveTo, click, press, type, look, cam, wait, loop: null };
    if (record && hires) seats[name].loop = await startLoop(context, page, path.join(outDir, name + '-frames'), { ...viewport, dsf });
    return seats[name];
  }

  /** Mark a beat: records script time and (when recording) flashes a 4px magenta strip on that seat's page so the polish stage can find it in the video. */
  async function beat(seatName, beatName) {
    const t = (Date.now() - T0) / 1000;
    marks.push({ beat: beatName, seat: seatName, t: Number(t.toFixed(2)) });
    const s = seats[seatName];
    if (record && s) {
      await s.page.evaluate(() => { const d = document.createElement('div'); d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;height:4px;background:#ff00ff;z-index:2147483647'; document.documentElement.appendChild(d); setTimeout(() => d.remove(), 400); }).catch(() => {});
      await wait(450);
    }
  }

  async function finish() {
    for (const s of Object.values(seats)) if (s.loop) await s.loop.stop();
    for (const s of Object.values(seats)) await s.context.close();
    await browser.close();
    const videos = {};
    if (record && hires) for (const name of Object.keys(seats)) {
      const { frames, dir } = seats[name].loop;
      if (!frames.length) throw new Error(`capture-kit: no frames captured for seat ${name}`);
      const span = frames[frames.length - 1].t - frames[0].t;
      if (frames.length / Math.max(span, 1) < 6) console.warn(`capture-kit: warning: seat ${name} only managed ${(frames.length / span).toFixed(1)} fps (heavy page) — motion will look choppy`);
      const mp4 = path.join(outDir, `${name}.mp4`); assemble(frames, mp4, fps, { ...viewport, dsf }); fs.rmSync(dir, { recursive: true, force: true }); videos[name] = mp4;
    }
    if (record && !hires) for (const name of Object.keys(seats)) {
      const dir = path.join(outDir, name), webm = fs.readdirSync(dir).find((f) => f.endsWith('.webm'));
      if (!webm) throw new Error(`capture-kit: no video recorded for seat ${name}`);
      const mp4 = path.join(outDir, `${name}.mp4`);
      const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', path.join(dir, webm), '-c:v', 'libx264', '-crf', '17', '-pix_fmt', 'yuv420p', '-r', '25', '-an', mp4]);
      if (r.status !== 0) throw new Error('capture-kit: ffmpeg/libx264 failed — ' + String(r.stderr).slice(0, 200)); // MP4 is the only deliverable; never ship the .webm
      fs.rmSync(dir, { recursive: true, force: true });
      videos[name] = mp4;
    }
    for (const k of Object.keys(tail)) if (tail[k]) { push(tail[k]); tail[k] = null; }
    mouse.sort((a, b) => a.t - b.t);   // an iframe event can land a moment late (its offset is measured first)
    const marksPath = path.join(outDir, 'marks.json'), focusPath = path.join(outDir, 'focus.json'), mousePath = path.join(outDir, 'mouse.json');
    fs.writeFileSync(mousePath, JSON.stringify({ cursor, events: mouse }));
    fs.writeFileSync(marksPath, JSON.stringify(marks, null, 2));
    fs.writeFileSync(focusPath, JSON.stringify(focus, null, 2));
    return { marks, focus, mouse, videos, marksPath, focusPath, mousePath };
  }
  return { browser, seat, beat, finish, marks, focus };
}

module.exports = { start, supabaseCookie, mintSession, wait, ARROW_PATH, ARROW_TIP };
