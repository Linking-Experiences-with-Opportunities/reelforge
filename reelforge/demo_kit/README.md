# demo-kit — the `/record-demo` polish stage

Turns raw Playwright captures into a branded MP4 (1080p) with [HyperFrames](https://www.npmjs.com/package/hyperframes), in **your** brand: logo, colors and fonts come from a swappable **brand pack**.

```
capture (capture-kit.cjs, beats)  →  detect-beats.mjs  →  build-demo.mjs --stage prepare  →  LOOK at the contact sheet  →  --stage render
        raw MP4 per seat            beats.json (exact)     project + check + brand-lint + snapshots                        final MP4
```

## Requirements
Node ≥ 22, ffmpeg with libx264 (`ffmpeg -encoders | grep libx264`), `curl`, and network on first use: `npx hyperframes@<pin>` downloads the CLI and a headless Chrome (~150 MB, cached in `~/.cache/hyperframes`), and GSAP is fetched once from jsDelivr into `~/.cache/record-demo-kit`. macOS or Linux. Run `node scripts/selftest.mjs --quick` to prove a machine works (synthetic footage, ~1 min). Capturing your own app additionally needs `playwright` installed in that app's repo (`npm i -D playwright && npx playwright install chromium`).

## Brand packs
A brand pack is a folder:

| File | What |
|---|---|
| `brand.json` | `name` (used in alt text and messages), `logo.dark` / `logo.light` (logo for dark / light backgrounds), `mark` (small square icon, used in launch-film cards), optional `stripes.light` / `stripes.dark` (four token names or hex colors that streak between launch-film scenes) |
| `tokens.css` | every color a video may use, as CSS variables: light set on `:root`, dark set on `[data-theme="dark"]`. **Keep the variable names**, change the values (list below) |
| `fonts.css` + `fonts/` | `@font-face` rules for self-hosted font files (`url("kit/brand/fonts/<file>")`). Renders run offline: no Google Fonts links |
| logo SVGs | whatever `brand.json` names |

The kit uses, first hit wins: `$DEMO_BRAND_DIR` → `~/.config/record-demo/brand` → the neutral example pack in `brand/`.

**Make your own:** `node scripts/init-brand.mjs --name "Acme"` copies the example to `~/.config/record-demo/brand` (plugin updates never touch it). Edit it, then run `node scripts/selftest.mjs --quick`: the self-test builds and lints against your pack.

**Token contract** (`tokens.css`):
- Brand: `--brand-accent` (rings, CTAs, active states), `--brand-alt-1`, `--brand-alt-2` (secondary stripe colors), `--brand-ink` (darkest; text and shadows on light), `--brand-paper` (lightest; canvas on light)
- Surfaces: `--bg-page`, `--bg-surface`, `--bg-surface-2`, `--border-default`
- Text: `--text-primary`, `--text-secondary`, `--text-tertiary`, `--text-accent`
- State: `--status-success`, `--accent-active` (the current chapter tick)
- Shape and type: `--radius-lg`, `--radius-full`, `--font-sans`, `--font-display`, `--font-mono`

Avoid pure white (`#fff`/`#ffffff`) as a token value: brand-lint treats it as a raw color.

## The standard (enforced by `brand-lint.mjs` and `hyperframes check`)
- Brand pack only: every color from `tokens.css`, only the `--font-sans` / `--font-mono` / `--font-display` fonts, the brand logo on intro and outro (the light-background logo on light compositions), no network resources.
- Single feature ≤ 45 s, multi-feature ≤ 3 min; chapter title ≤ 4 words, sub ≤ 90 chars; speed-ups ≤ 1.5x.
- Say only what the recording shows. No invented metrics; if something is "Coming soon" the video says so.
- Demo accounts and throwaway data only; dev chrome hidden (capture-kit does it).
- No media in git. Specs are fine to commit; recordings live in `~/Documents/Demos/`.

See `frame.md` for the visual design spec.

## Files
| Path | What |
|---|---|
| `kit.json` | pinned `hyperframes` and `gsap` versions, Node/ffmpeg minimums, canvas, limits |
| `brand/` | the example brand pack (Poppins + JetBrains Mono under the SIL Open Font License, `brand/fonts/OFL-*.txt`) |
| `video.css`, `template/composition.css` | layout CSS for the generated compositions |
| `scripts/capture-kit.cjs` | Playwright helpers: seats, an arrow cursor (`cursor: 'arrow' \| 'dot' \| 'none'`; dot on mobile), eased timed glides, the mouse path (`mouse.json`), dev-chrome hiding (`hideSelectors`, `hideText`), `beat()` sync flash, `hires` capture, `cam()/look()` camera targets, `press()` held clicks, Supabase session cookie + `mintSession` (only if your app uses Supabase) |
| `scripts/detect-beats.mjs` | finds each beat flash in the videos → `beats.json` (with `--focus` camera events and `--mouse` the mouse path, both in video time) |
| `scripts/build-demo.mjs` | `demo.json` + `beats.json` → HyperFrames project → check → brand-lint → snapshots → render (walkthrough) |
| `scripts/build-clip.mjs` | hi-res capture + camera events → zoomed 1440x900 clip + poster (clip format) |
| `scripts/build-launch.mjs` | hi-res capture + shot list → silent 1080p launch film + poster (launch format) |
| `scripts/brand.mjs` | loads the active brand pack, stages it and GSAP into a project |
| `scripts/brand-lint.mjs` | palette / fonts / logo / offline rules |
| `scripts/init-brand.mjs` | copies the example brand pack to start your own |
| `scripts/resolve-kit.sh` | locates this directory from the plugin cache, a marketplace checkout or a clone |
| `scripts/selftest.mjs` | end-to-end proof with synthetic footage |
| `examples/` | `walkthrough.demo.json` + `walkthrough-capture.example.cjs` (1 seat, beats), `two-seat.demo.json` (2 seats, picture-in-picture), `feature.clip.json` + `clip-capture.example.cjs`, `feature.launch.json` + `launch-capture.example.cjs`. Selectors are placeholders for an imaginary "team invites" feature |

## `demo.json`
```jsonc
{ "feature": "team-invites",                       // output name: ~/Documents/Demos/features/<feature>-walkthrough.mp4
  "header": "Team invites · Walkthrough", "footnote": "Recorded locally with a demo account",
  "intro": { "eyebrow": "New · Team invites", "title": "Invite your team", "subtitle": "…", "dur": 3.6 },
  "outro": { "eyebrow": "Shipped · PR #123", "title": "Bring them in.", "chips": ["…"] },
  "seats": { "main": { "video": "raw/main.mp4" } },   // 2+ seats → labelled pill + picture-in-picture of the other seat
  "chapters": [ { "title": "Send an invite", "sub": "…",
      "segments": [ { "seat": "main", "from": "invite", "to": "pending", "rate": 1 } ] } ] }
```
`from`/`to`: a **beat name** (video time of that beat's flash; `from` adds 0.55s to skip the flash, `to` subtracts 0.15s), a **number** (seconds in that seat's video), or `{ "beat": "x", "plus": 1.5 }`. `dur` replaces `to`. `file` plays a standalone clip instead of the seat video (re-record a page that loaded late). `pip: "off"` or `{ "seat", "at" }` overrides the automatic other-seat picture-in-picture (which is time-mapped through the beats of both seats).

## Capturing
```js
const kit = require('<demo-kit>/scripts/capture-kit.cjs');
const baseURL = 'http://localhost:3000';
(async () => {
  const s = await kit.start({ playwright: require('playwright'), baseURL, outDir: './raw', hideText: ['Reset onboarding'] });
  const me = await s.seat('main');                 // 1440x900, dsf 2, arrow cursor, dev chrome hidden
  await me.page.goto(baseURL + '/settings', { waitUntil: 'networkidle' });
  await s.beat('main', 'name');                    // exact cut point for the polish stage
  // … drive the flow with me.glide / me.click / me.type …
  const { videos, marksPath } = await s.finish(); // MP4s at 25 fps + marks.json
})().catch((e) => { console.error(e); process.exit(1); });
```
Why beats: Playwright's video clock drifts against wall-clock (~9% over 2–3 minutes), so script timestamps cannot be used to cut. A 4px magenta strip flashes at the bottom of the seat's page at each beat; the composition crops the bottom 4px.

## Clip format (changelog / inline clips, reels)
A walkthrough is a 1920x1080 frame with a rail: great for PRs, chat and hero videos, but **unreadable when embedded small** (a docs page shows inline clips ~645 CSS px wide, breakout clips ~300). A `clip` is a clean 1440x900 (16:10) video that **follows the action**: hi-res capture, a directed camera, an accent ring on what was clicked, no rail/intro/outro (the page supplies the caption).

```
capture (hires: true, s.cam()/look()) → detect-beats --focus → build-clip --stage prepare → LOOK → --stage render → clip.mp4 + poster.jpg
```
- **Capture hi-res:** `kit.start({ …, hires: true })` records true 2880x1800 frames (a device-pixel screenshot loop; Playwright's `recordVideo` cannot exceed CSS pixels). ~20 fps on light pages; it warns under 6 fps.
- **Direct the camera in the capture script:** `await me.cam({ x, y, w })` frames exactly that CSS-px region (height follows 16:10) right before the action to watch; `me.look(locator)` frames an element. When any cam/look exists they are the only camera targets; click/type still get a ring. Without them the camera falls back to following clicks (rougher).
- **Legibility rule (enforced):** every zoomed shot shows at most `maxWidth` CSS px of the app (900 by default, **500 for breakout clips**). A region that does not fit means the shot is too wide to read at 645 px: split it into two shots and pan.
- **Spec:** `{ "feature": "team-invites-clip", "video": "raw/main.mp4", "from": "go", "dur": 14, "maxWidth": 900, "wideStart": 0.8, "wideEnd": 1.2, "poster": { "at": "end" } }`. `from`/`to`/`dur` as in walkthroughs (beat names, numbers or `{beat, plus}`); optional explicit `camera: [{ at, rect: [x,y,w,h] }]`, `ring: false`.
- **Run:** `node scripts/detect-beats.mjs --marks raw/marks.json --focus raw/focus.json --video main=raw/main.mp4 --out beats.json` then `node scripts/build-clip.mjs --spec clip.json --beats beats.json --project <dir> --stage prepare` (look at the contact sheet), then `--stage render --out clip.mp4` (writes `clip-poster.jpg`, faststart, CRF 20). Output 1440x900 @ 30 fps, ~1-3 MB per 20 s.
- **Phone clips:** capture with `kit.start({ ..., hires: true, viewport: { width: 390, height: 844 }, dsf: 3, mobile: true })` (touch + mobile emulation, true 1170x2532 frames) and set the clip spec to `"source": { "width": 390, "height": 844, "scale": 3 }, "canvas": { "width": 780, "height": 1688 }`. Keep one size for every phone clip.

## Launch format (LinkedIn / X / site launch films)
A **launch film** sells a feature to people outside the team: ≤ 45 s, silent (music is added in the posting app), 1920x1080. Modeled on YC-style launch posts: one continuous **camera** over a light canvas — no hard cuts. The product UI floats on the canvas as a card (no browser chrome); the camera eases to a framing in **0.8 s** and **holds ≥ 1 s** while something happens; one live element can be pulled out and shown big on the bare canvas.

```
capture (hires, cursor: 'none', one beat per shot, cam()/look()/press()) → detect-beats --focus --mouse → build-launch --stage prepare → LOOK → --stage render → <feature>-launch.mp4 + poster
```
**Shots** (`shots[]`, played in order between the opener and the outro):

| type | what | fields |
|---|---|---|
| `screen` | camera over the floating UI: a short wide establishing framing, then every `cam()`/`look()` in range (or explicit `camera: [{ at, rect: [x,y,w] }]`, `{ at, wide: true }`) | `from`, `to`/`dur`, `rate`, `wideStart: false`, `minWidth`, `say` |
| `focus` | ONE live panel or input isolated on the canvas, up to `zoom` (1.4× the capture: 2.8 at dsf 2) | `from`, `to`/`dur`, `rect: [x,y,w,h]` or a `look()` in range, `pad` (18), `zoom`, `maxFill` (0.62; 0.8 for a wide input), `caption` |
| `title` | one kinetic line, words blur in | `text` (≤ 8 words), `accent: [word indexes]`, `sub`, `dur` (2.6) |
| `card` | a result callout | `eyebrow`, `title` (≤ 8 words), `sub`, `row: { label, meta, cta }` (the pointer presses the CTA), `dur` (3) |
| `device` | a phone capture in a bezel (`source: { width: 390, height: 844, scale: 3 }`) | `video`, `from`, `to`/`dur`, `source` |

See `examples/feature.launch.json`. The opener is the brand logo × `opener.words`, plus one `line`; the outro is the logo, `outro.line` and `outro.url`, with the brand stripes settling under it.

**Narration (on by default):** give every footage shot a `say` — the walkthrough's chapter rail, on the launch look. A 420 px rail on the right shows progress ticks, `NN / NN`, a title (≤ 4 words) and one line (≤ 90 chars) saying what is on screen; the camera's stage becomes the left 1500 px (footage fades into the canvas at the rail edge). `say: { title, sub }` starts at the shot; `say: [{ at, title, sub }, …]` switches mid-shot (`at` = a beat name, number or `{ beat, plus }` in source time). Footage shots without `say` keep the current line; `title` and `card` shots hide it. `"rail": false` drops the rail for a full-bleed cut.

**Enforced** (exit 1): every zoomed framing ≤ `maxWidth` (900) CSS px, `hyperframes check`, brand-lint (light films must use the light-background logo). **Warned:** over 45 s, opener over 6.5 s, long lockup/lines/cards, camera moves pushed to keep the 1 s hold, a wide framing held over 2.2 s, a screen shot with no zoom, a focus shot running past a click inside it (capture the click with `me.press()` and end the shot right after it), any framing or focus zoom that blows the capture up more than `maxUpscale` (1.4×), a focus shot on a lone small control (press it inside a screen framing of its toolbar instead), and a capture with its cursor baked in.

**The cursor:** capture launch films with `kit.start({ ..., cursor: 'none' })` and pass `--mouse <raw>/mouse.json` to detect-beats. The builder then draws one oversized arrow (5.2% of the canvas width, ink body, paper edge, soft shadow) from the recorded mouse path, replayed through the same camera math as the footage, so it keeps one size at every zoom while its tip stays on what it points at. `"cursor": false` turns it off, `{ "size": px, "fill": "dark" | "light" }` restyles it.

**Capture tips:** `hires: true`, `cursor: 'none'`; one `s.beat()` per footage shot; `me.cam()` from measured boxes before each action; hold 1–2 s after each result; force your app's light theme before the first `goto`. Code editors built on Monaco: put code in with `editor.trigger('keyboard', 'paste', { text })` after clicking in and `Meta+a` (`setValue()` changes the screen but often not the app's state).

## Maintaining
- **HyperFrames or GSAP upgrade** → change `kit.json`, run `selftest.mjs` (full, with renders), re-render an example, compare.
- **Adding a layout option** → edit `template/composition.css` + `build-demo.mjs` (or `build-launch.mjs`), keep `brand-lint.mjs` green, extend `selftest.mjs`.
