# frame.md — design spec for demo videos

Colors, fonts and logos come from the active brand pack (see README "Brand packs"). Never retype a color: `scripts/brand-lint.mjs` fails the build on any hex, rgba triple or font that is not in the pack's `tokens.css` / font variables. This file describes how the builders USE the pack.

## Look
- Walkthroughs and clips use the dark theme: `<html data-theme="dark">`. Page `--bg-page`; surfaces `--bg-surface` / `--bg-surface-2`; hairlines `--border-default`.
- Text `--text-primary`; secondary `--text-secondary`; tertiary only for footnotes.
- One accent: `--brand-accent` for rings and CTAs, `--text-accent` for accent-colored text, `--accent-active` for the current chapter tick. `--status-success` only for done/verified.
- No gradients; glows limited to a soft accent ring. Radii from `--radius-lg`, full pill (`--radius-full`) for chips.

## Brand
- Logo: `logo.dark` from `brand.json` on dark compositions, `logo.light` on light ones. Present on the intro, the outro and the header. Never recolor, stretch or add effects.
- Voice: plain and specific. Say only what the recording shows.

## Type
- `--font-sans` for everything spoken; `--font-mono` for eyebrows, labels and footnotes: uppercase, letter-spacing `.14em`. Titles 700–800, tracking `-0.03em`.
- Chapter title ≤ 4 words, sub ≤ 90 characters.

## Motion
- Ease-out `expo.out`; fast 150ms / base 260ms / slow 500ms. Enter = fade + 8–24px rise. Nothing bounces.
- Speed-ups ≤ 1.5x, and only for typing or waiting; never for a result the viewer needs to read.

## Layout (1920×1080)
- Real product footage at 1:1 in a 1440px frame (1px `--border-default`, radius 14). Right rail 320px: progress ticks, `NN / NN` counter, chapter title + sub. Second seat (if any) as a 320×200 picture-in-picture at the bottom of the rail, labelled.
- Intro ≤ 3.6s and outro ≤ 4.2s: logo, mono eyebrow (ticket / PR), title, one-line subtitle. No music by default.

## Launch films (`format=launch`) — the one place the rules above bend
- **Light theme** (`<html data-theme="light">`, the pack's `:root`): `--brand-paper`-ish canvas `--bg-page`, light surfaces, `--brand-ink` text, `--brand-accent` accent. Logo: `logo.light`; brand-lint rejects the dark-background logo there. `theme: "dark"` keeps the dark rules.
- **One gradient:** a faint wash in two canvas corners (`--brand-accent` at 7%, `--brand-alt-2` at 6%). Nothing else gets a gradient.
- **Brand stripes as the motion signature:** the four `stripes` colors from `brand.json` streak through the frame between opener → product → outro, and settle as a row under the outro logo.
- **Shadows:** the floating UI card and focus cards cast one soft `--brand-ink` shadow (16% / 14%) plus a 1px `--border-default` hairline. That is the depth cue that replaces browser chrome.
- **Type:** opener lockup = logo × feature name (700, accent word in `--brand-accent`); kinetic lines 700 at ~7% of the frame height; card titles ~7.5%. Words blur in (14px → 0, 18px rise, 0.6s `expo.out`, staggered); shots blur out in 0.35s.
- **Camera:** moves 0.8s `power3.inOut`, holds ≥ 1s, zoomed framings ≤ 900 CSS px of the app. Never blow the capture up past 1.4× (a 2x capture on the 1500px narrated stage: framings ≥ 536 px wide, focus zoom ≤ 2.8); beyond that UI text goes soft.
- **Cursor:** one oversized macOS-style arrow, 5.2% of the canvas width (100px at 1920), `--brand-ink` body with a 1.4-unit `--brand-paper` edge and a soft 30% shadow; the light-bodied variant only on a dark film. Constant size at every zoom; the tip, not the box, is the hot-spot. It enters from below the stage and leaves through the bottom (never a fade in place), squeezes to 0.84 on the tip for a click (0.1s in, 0.22s out), and is drawn by the builder, never baked into the capture.
- **Focus shots** are for panels and inputs. A lone button blown up reads as a sticker, not the product: press it inside a screen framing of its toolbar or header.
- **Narration rail** (default): the walkthrough rail on the launch look — 420px on the right, small logo at the top, progress ticks (`--border-default` idle, `--brand-accent` current, `--status-success` done), mono `NN / NN` in `--text-accent`, title 700 42px, one line 23px `--text-secondary`. The stage's footage fades to the canvas over its last 72px instead of stopping against the rail.
