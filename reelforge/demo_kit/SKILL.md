---
description: Record a feature in your web app and deliver a polished, branded demo video (walkthrough, clip or launch film, MP4)
argument-hint: <feature or route> [format=walkthrough|clip|launch] [polish=on|off] [seats=1|2] [url=<base url>] [for PR #<n>]
---

# Record Demo — record a feature in your web app, deliver a polished, branded walkthrough (MP4)

Produce a shareable recording of a feature working in the user's web app. **The standard is a polished demo: Playwright captures the real UI, then the plugin's demo kit renders it with HyperFrames in the user's brand (logo, colors, fonts from their brand pack, plus a chapter rail).** Supporting files live in the plugin's **demo kit** (`demo-kit/`); find it with the probe in Preflight, never by hardcoding a path.

**MP4 is the only delivered format.** There is no GIF lane and no WebM deliverable. If an engine produces an intermediate (Playwright's `.webm`), it is transient and must be converted and deleted in the same step — never attached, never left on disk.

**Artifacts land in `~/Documents/Demos/features/<feature>-walkthrough.mp4`.** The composition project and raw captures stay in `~/Documents/Demos/projects/<feature>/` so the demo can be re-cut without re-recording.

Argument forms: `<feature or route> [format=walkthrough|clip|launch] [polish=on|off] [seats=1|2] [url=<base url>] [for PR #<number>|<audience>]` — e.g. `team invites flow for PR #412` | `shared doc editing seats=2` | `settings page format=launch url=http://localhost:5173`.

- **`format` defaults to `walkthrough`** (1920x1080 with chapter rail: PRs, chat, hero videos). Use **`format=clip`** for anything embedded small (changelog, docs): a clean, zoomed 1440x900 clip that follows the action; it needs `hires` capture and directed `cam()` shots. See `$KIT/README.md` "Clip format".
- **`format=launch`** is the **launch film**: a ≤ 45 s, silent product announcement for LinkedIn / X / a website, in the style of YC launch posts. One continuous camera over a light canvas: the UI floats as a card with no browser chrome, the camera eases in to ≤ 900 px of the app and **holds** while something happens, one live element can be isolated big on the bare canvas (`focus`), plus a kinetic `title`, a result `card`, a logo opener and outro, joined by the brand-stripe streak. Use it when the audience is outside the team. Step 6L; reference `$KIT/README.md` "Launch format", worked example `$KIT/examples/feature.launch.json` + `$KIT/examples/launch-capture.example.cjs`.
- **`polish` defaults to `on`.** Use `polish=off` only for a throwaway raw clip (or headless bots — see Limits). Polish never blocks delivery: any polish failure falls back to the raw MP4 with one warning line.
- **`url`** is the app's base URL. If not given, find it: check running dev servers (`lsof -iTCP -sTCP:LISTEN -P | grep node`) and the repo's `package.json` dev script; ask the user if it's still ambiguous.

## Preflight (run first, once)

1. **Find the demo kit** (`${CLAUDE_PLUGIN_ROOT}` is not set in Bash, so probe; the first hit wins):
   ```bash
   KIT=""; for d in "${DEMO_KIT_DIR:-}" "$(ls -1d "$HOME"/.claude/plugins/cache/record-demo-kit/record-demo/*/demo-kit 2>/dev/null | sort -V | tail -1)" \
       "$HOME/.claude/plugins/marketplaces/record-demo-kit/plugins/record-demo/demo-kit" "$(git rev-parse --show-toplevel 2>/dev/null)/plugins/record-demo/demo-kit"; do
     [ -n "$d" ] && [ -f "$d/kit.json" ] && KIT="$(cd "$d" && pwd -P)" && break; done; echo "KIT=${KIT:-none}"
   ```
2. **Check the toolchain:** `node -v` ≥ 22, `ffmpeg -encoders | grep libx264`, `curl` present. `playwright` must resolve from the app's repo (`node -e "require('playwright')"` there); if not, tell the user and offer `npm i -D playwright && npx playwright install chromium` (it changes their repo: ask first). First polish run downloads the pinned HyperFrames CLI + headless Chrome (~150 MB, cached) and GSAP — say so before starting.
3. **Brand pack:** `node -e "import('$KIT/scripts/brand.mjs').then(b=>console.log(b.brandDir()))"`. If it prints the kit's own `brand/` folder, the user has no brand pack yet and the video will carry the neutral **Example** brand. Tell them once, and offer to set theirs up: `node "$KIT/scripts/init-brand.mjs" --name "<Company>"` creates `~/.config/record-demo/brand`; then replace the logo SVGs, set the colors in `tokens.css` and the fonts in `fonts.css` + `fonts/` (see `$KIT/README.md` "Brand packs"), and run `node "$KIT/scripts/selftest.mjs" --quick`. Don't block on it.
4. If the kit is missing or a check fails: print ONE line `⚠️ polish=off: <reason>` (e.g. `kit not found — run /plugin marketplace update record-demo-kit`), set `polish=off`, and continue with the raw capture. Do not stop the task.
5. `node "$KIT/scripts/selftest.mjs" --quick` proves the kit on a new machine (synthetic footage, no app); run it when polish fails or a brand pack changed, not every time.

## Steps

1. **Storyboard first, as a spec.** Write the shot list (URLs, clicks, expected states) AND the chapters the viewer will read, as a `demo.json` (schema and worked examples: `$KIT/README.md`, `$KIT/examples/`). One chapter per feature moment: title ≤ 4 words, sub ≤ 90 chars, **say only what the recording shows** (no invented metrics; if the UI says "Coming soon" the video says so; don't describe unshipped behaviour). Intro eyebrow = ticket or feature name, outro eyebrow = PR/release state. Check there's actually data to record — an empty database means a blank screen; seed throwaway data first. Rehearse the flow once un-recorded (screenshots, no video) — dynamic forms shift layout, and clicks recorded blind will miss. Length standard: single feature ≤ 45 s, multi-feature ≤ 3 min, speed-ups ≤ 1.5x.

2. **Stabilize the app BEFORE recording — the #1 failure mode.** Any dev-server restart kills the Playwright run. Make sure the app is up and serving the code you mean to show: check what owns the port (`lsof -ti:<port> | xargs ps -o command= -p`) — another checkout or a stale worktree may be serving it. If another session owns the port, start yours on a different port rather than killing it.

3. **Auth.** Use the lightest thing that works, and never a real person's account:
   - The app's own dev/test login bypass if it has one.
   - Otherwise log in once in the capture script (off camera, before the first beat) with a demo account, or reuse a saved Playwright `storageState`.
   - Supabase apps: `capture-kit.cjs` has `mintSession()` (admin magic link for a demo account; secrets from env, never printed) and `supabaseCookie()` to inject it into a seat.
   Ask the user for demo credentials if none are obvious; never guess passwords or read secrets into the transcript.

4. **Safety ritual when the flow has real side effects** (emails, payments, notifications, account writes):
   - Run against a local or staging environment, never production.
   - Create clearly-named throwaway entities ("Demo Workspace (test)") so any send provably reaches no one; demo accounts only (`*@example.com`).
   - Remove everything you created afterwards, through the app's API or admin tools — **not the UI delete button** if it opens a native `confirm()` (those freeze browser automation).

5. **Capture — Playwright through the kit's `capture-kit.cjs`.** (For `format=clip` / `format=launch`, capture with `hires: true` and directed `cam()` shots, as in steps 6c / 6L.) Write a throwaway script in the app's repo (so `playwright` resolves; delete it after delivery), modeled on `$KIT/examples/walkthrough-capture.example.cjs`:
   ```js
   // demo-<feature>.cjs — run from the app's repo as: KIT="$KIT" node demo-<feature>.cjs
   const kit = require(process.env.KIT + '/scripts/capture-kit.cjs');
   const baseURL = 'http://localhost:3000';
   (async () => {
     const s = await kit.start({ playwright: require('playwright'), baseURL, outDir: '<projects>/<feature>/raw',
       hideText: [/* exact text of the app's dev-only floating buttons */] });
     const me = await s.seat('main');
     await me.page.goto(baseURL + '/settings', { waitUntil: 'networkidle' });
     await s.beat('main', 'name');                       // one beat per chapter start — the polish stage cuts on these
     await me.type(me.page.locator('#display-name'), 'Sam Lee');
     const { videos, marksPath } = await s.finish();     // MP4s (25 fps, libx264) + marks.json; the .webm is deleted for you
     console.log(videos, marksPath);
   })().catch((e) => { console.error(e); process.exit(1); });
   ```
   The kit bakes in: 1440x900 viewport with `deviceScaleFactor: 2` (a 1x capture reads permanently soft), matching `recordVideo.size`, a large arrow cursor (a dot for `mobile: true`), eased 0.45–0.9 s glides, the Next.js dev overlay hidden (add the app's own dev widgets via `hideText` / `hideSelectors`), and dialogs auto-accepted. Video only works on Playwright's bundled Chromium. Drive the flow through `me.glide/click/type` with brief `me.wait(ms)` beats so the video is watchable. If `finish()` reports ffmpeg/libx264 missing, that's a hard blocker for MP4 — report it in the DELIVERY line; **never ship the `.webm`**.
   - **Two seats** (`seats=2`, e.g. owner + guest): call `s.seat()` twice with separate sessions (cookies are per browser context), name the seats in `demo.json`, and beat on whichever seat is acting. The polish stage shows the acting seat large and the other as a live picture-in-picture (`$KIT/examples/two-seat.demo.json`).

6. **Polish (default).** Skip only for `polish=off`. **`format=clip`: follow 6c instead; `format=launch`: follow 6L instead.**
   1. **Detect beats:** `node "$KIT/scripts/detect-beats.mjs" --marks <raw>/marks.json --video main=<raw>/main.mp4 [--video guest=…] --out beats.json`. It fails loudly if flashes and beats don't pair up — fix the capture script, don't hand-edit. **Why beats:** Playwright's video clock drifts ~9% against wall-clock, so script timestamps cannot be used to cut.
   2. **Prepare:** `node "$KIT/scripts/build-demo.mjs" --spec demo.json --beats beats.json --project ~/Documents/Demos/projects/<feature> --stage prepare`. This generates the HyperFrames project, runs `hyperframes check` (0 issues required) and `brand-lint` (brand palette / fonts / logo) and takes chapter snapshots. Exit ≠ 0 means fix the spec or capture, not "ship anyway".
   3. **LOOK before rendering.** Open every `contactSheets` image from the prepare output with the Read tool. Check: footage is the right moment in each chapter (no spinners or blank pages), rail text matches what's on screen, no dev widgets/real emails/tokens visible, the brand logo shows on intro/outro/header. A chapter that shows a page still loading → widen its window or re-record that page as a standalone `file` segment. Fix and re-run prepare.
   4. **Render:** `node "$KIT/scripts/build-demo.mjs" --spec demo.json --project ~/Documents/Demos/projects/<feature> --stage render --out ~/Documents/Demos/features/<feature>-walkthrough.mp4`. Then `ffprobe` it (1920x1080, duration within the standard) and view one frame from the final file.
   5. On any polish failure, print `⚠️ polish=off: <reason>` and deliver the raw MP4: copy `~/Documents/Demos/projects/<feature>/raw/main.mp4` to `~/Documents/Demos/features/<feature>-walkthrough.mp4`. For `seats=2` deliver the primary seat's MP4.
   - `polish=off`: skip 6, copy the raw MP4 straight to the artifact path.
   - **Delete the throwaway capture script** after the artifact is safe and any required PR comment is verified.

6L. **Launch film (`format=launch`)** — capture with `hires: true`. `polish=off` delivers the raw hi-res capture and says it is not a launch film.
   1. **Shot list first** (step 1 still applies). One beat per shot:
      - `screen` — the camera over the floating UI. Opens on a ~1.5 s wide framing, then follows every `me.cam()`/`me.look()` in its range: each move takes 0.8 s and is followed by a hold of ≥ 1 s. Every zoomed framing ≤ 900 CSS px; aim for 560–760 on text.
      - `focus` — ONE panel or input isolated on the bare canvas, at most 1.4x the capture. **Not a lone button**: blown up on its own it reads as a sticker. Press buttons inside a `screen` framing of their toolbar. A phrase-sized element needs `maxFill` 0.8.
      - `title` — one kinetic line (≤ 8 words; `accent` word indexes in the brand accent). `card` — a result callout that restates something the recording showed. `device` — a phone capture in a bezel.
      - **Narrate every footage shot** with `say` (title ≤ 4 words + one line ≤ 90 chars about what's on screen right then); switch mid-shot with `say: [{ at: { beat, plus }, title, sub }]`.
      - Opener `words` = the feature name (≤ 4 words) after the brand logo, plus one `line`; outro `line` + `url`.
      Budget: opener 4.6 s, 4–7 shots, outro 4 s, total ≤ 45 s. Light theme by default (`theme: "dark"` exists).
   2. **Capture** with `kit.start({ ..., hires: true, cursor: 'none' })`, one `s.beat()` at the start of every footage shot (model: `$KIT/examples/launch-capture.example.cjs`). The builder draws the cursor from the recorded mouse path, so move the mouse only with `me.glide/click/press/type` or `me.moveTo(x, y)`. Before each action, `await me.cam({ x, y, w })` from measured boxes (`locator.boundingBox()`), or `me.look(locator)` for a focus shot. For a click that **changes the layout**, use `await me.press(locator)` and, when a wait follows, mark `s.beat('main', 'pressed')` right after and end the shot at `"to": { "beat": "pressed", "plus": 0.6 }`. Hold 1–2 s after each result. Force the app's light theme before the first `goto` if it has one. Rehearse once un-recorded.
   3. **Detect beats** with `--focus <raw>/focus.json --mouse <raw>/mouse.json`, then **prepare**: `node "$KIT/scripts/build-launch.mjs" --spec launch.json --beats beats.json --project ~/Documents/Demos/projects/<feature>-launch/hf --stage prepare` (keep `launch.json` beside `raw/`: paths resolve against the spec's folder). Exit 1 on an unreadable framing, a failed `hyperframes check` or brand-lint; read every warning. **LOOK at every contact sheet**: right element in each focus shot, the arrow's tip on what it clicks, no spinners, no dev widgets, real data only.
   4. **Render:** `--stage render --out ~/Documents/Demos/features/<feature>-launch.mp4` (writes `-poster.jpg`; silent, 1920x1080 @ 30 fps). Extract a frame per second from the FINAL file and look at them.
   5. **Deliver** the `.mp4` + poster from `~/Documents/Demos/features/` (a social asset: no PR comment unless asked). Music is added in the posting app.

6c. **Clip format (`format=clip`)** — a clean, zoomed 1440x900 clip for small embeds. `polish=off` delivers the raw hi-res capture and says it is not a clip. Reference: `$KIT/README.md` "Clip format", `$KIT/examples/clip-capture.example.cjs` + `feature.clip.json`.
   1. **Capture hi-res with a directed camera:** `kit.start({ ..., hires: true })`, and `me.cam({ x, y, w })` (or `me.look(locator)`) **before** each action the viewer must watch. Every shot ≤ 900 CSS px of the app (≤ 500 for narrow embeds).
   2. **Detect beats with focus events:** add `--focus <raw>/focus.json`.
   3. **Prepare:** write `clip.json` and run `node "$KIT/scripts/build-clip.mjs" --spec clip.json --beats beats.json --project <projects>/<name>/hf --stage prepare`. **LOOK at the contact sheets.**
   4. **Render:** same with `--stage render --out <projects>/<name>/<name>.mp4 --poster <projects>/<name>/<name>-poster.jpg`. `ffprobe` it (1440x900 @30 fps) and read the UI text in a frame extracted at retina width (`ffmpeg -ss T -i clip.mp4 -frames:v 1 -vf scale=1290:-1 f.png`).
   5. **Deliver** the `.mp4` + poster (assets for a page, not PR attachments, unless asked).

7. **Delivery**
   - Default: the artifact is in `~/Documents/Demos/features/`; the requester shares it.
   - **For a PR** (only when asked, e.g. `for PR #123`): resolve the target from the argument, or `gh pr view --json number,url` for the current branch; if neither yields one PR, ask — never guess. GitHub's API cannot upload video attachments, so this needs browser automation: open the PR, attach the MP4 to a new comment, wait for the upload to finish, submit the plain **Comment** action, then verify the posted comment shows the video (limit 100 MB). If no browser automation is available, leave the file in place and give the user the exact manual step. Don't claim delivery until the comment is visible, and never commit video files to the repo to work around it.

## Report (exactly this shape, nothing more)

```
FORMAT: <walkthrough|clip|launch> + polish=<on (hyperframes@<pin>, brand: <name>)|off (<reason>)>
RECORDING: <path> (<size>, <duration>, <WxH>)
FLOW: <one line: what the recording shows>
SAFETY: <environment used / side effects proven zero / accounts created and removed — or "read-only flow">
DELIVERY: <where it is, or PR comment URL, or the exact blocker/manual step>
```

## Limits worth knowing
- **Headless bots** with no download budget should run `/record-demo polish=off`; a person can re-polish later from the saved spec.
- Playwright `context.setOffline()` cannot show a "reconnecting" state when the app has a peer-to-peer channel; demonstrate durability with a reload instead.
- Headless browsers have no microphone: live transcription/dictation UI can't be demoed.
- A page that loads right before the context closes may never reach its loaded state in the video; record it as its own short clip and use a `file` segment.

Token discipline: background long runs (the Playwright script, the render) with a log and read only the tail; never dump raw playwright output or base64 frames; report final state only.

Record the following: $ARGUMENTS
