# reelforge

Learn a video's structure, then rebuild it with your own footage.

You point it at a reel you like — a local `.mp4`, or an Instagram / TikTok /
YouTube link — and it works out how that video is *built*: where the cuts land,
how fast it moves, what the music is doing, what text is on screen, and which
shots are live footage versus a diagram or screenshot. That becomes a **recipe**:
an editable JSON file. Then you hand it your own clips and images and it renders
a new video with the same skeleton.

The source is a template, not the content.

---

## Install

Nothing to build. You need these on `PATH`:

| tool | required | for |
|---|---|---|
| `ffmpeg` / `ffprobe` | yes | everything |
| Pillow (`pip3 install Pillow`) | yes | captions, diagram detection, contact sheet |
| `yt-dlp` | for URLs | downloading from Instagram/TikTok/YouTube |
| `whisper` | optional | speech transcript |
| `tesseract` | optional | reading on-screen text |

```bash
brew install ffmpeg yt-dlp tesseract
pip3 install Pillow openai-whisper
```

Check everything at once:

```bash
./bin/reelforge doctor
```

> **Note on captions:** Homebrew's ffmpeg is often built *without* libfreetype,
> which removes the `drawtext`, `subtitles` and `ass` filters entirely. reelforge
> does not depend on them — it rasterizes captions with Pillow and composites
> them with `overlay`, which every ffmpeg build supports. `doctor` tells you
> which backend is in use.

---

## Use it

### 1. Ingest a source

```bash
reelforge ingest ./some-reel.mp4
reelforge ingest "https://www.instagram.com/reel/XXXXXXXX/" --cookies-from-browser chrome
```

Instagram and TikTok refuse logged-out downloads, so `--cookies-from-browser`
(`chrome`, `firefox`, `safari`, `edge`, `brave`) reuses the session you already
have open. Without it you'll get an "empty media response" and a reminder.

> **macOS + Chrome gotcha.** Reading Chrome/Edge/Brave cookies needs the
> "Chrome Safe Storage" key from your login Keychain, which raises a **GUI
> prompt**. Nothing can answer that prompt from a non-interactive shell (a
> script, CI, or an agent), so yt-dlp blocks forever. reelforge caps the
> download at `--download-timeout` (180s default) and tells you what happened.
> Either run it in a real terminal and click Allow once, use
> `--cookies-from-browser firefox` (no Keychain), export a `cookies.txt` and
> pass `--cookies FILE`, or just save the mp4 and ingest the local file.

This writes a project directory:

```
~/reelforge-projects/<name>/
  source.mp4              the downloaded / copied video
  recipe.json             the editable structure  <- the thing you care about
  analysis.json           raw measurements
  frames/                 sampled stills
  contact-sheet.jpg       annotated frame grid
  contact-sheet-plain.jpg unlabelled frame grid
```

and prints a summary:

```
  rhythm     medium - 6 segments, avg 2.25s (range 2.0-3.0s)
  tempo      120.0 BPM
  needs      4 footage slot(s)  +  2 image/diagram slot(s)

  #   role   fill     kind        start    dur   source        text
  0   hook   footage  title_card   0.00   2.00   -             STOP SCROLLING
  1   body   footage  footage      2.00   2.00   -
  2   body   asset    diagram      4.00   3.00   -
  3   body   footage  footage      7.00   2.00   -
  4   body   asset    screenshot   9.00   2.50   -
  5   cta    footage  title_card  11.50   2.00   -             FOLLOW FOR MORE
```

### 2. See which images it wants

reelforge detects when the source cuts to a **diagram, screenshot or photo**
rather than live footage, and asks you for exactly that many:

```bash
reelforge assets --recipe ~/reelforge-projects/some-reel/recipe.json
```

```
This recipe needs 2 image/diagram(s), in this order:

  1. segment 2  (diagram, confidence 1.00)
     on screen  4.00s for 3.00s
     source had HOW IT WORKS
     reference  ~/reelforge-projects/some-reel/frames/frame-00012.jpg
```

The `reference` still shows what the original had in that slot, so you know what
to supply. The annotated contact sheet marks the same slots with `#1`, `#2`, …

### 3. Render with your own material

```bash
reelforge render \
  --recipe ~/reelforge-projects/some-reel/recipe.json \
  --clips ./my-footage \
  --assets ./diagram.png ./dashboard.png \
  --music ./track.mp3 \
  --out out.mp4
```

Footage slots cycle through `--clips` (a directory and/or individual files);
image slots are filled from `--assets` **in timeline order**. Supply too few and
it refuses to render, naming each unfilled slot:

```
error: this recipe needs 2 image/diagram(s); you supplied 1.
  1 slot(s) still unfilled:
    segment 4   screenshot  @   9.00s for 2.50s   looks like: dashboard.internal/metrics ...
```

### One-shot

```bash
reelforge make ./some-reel.mp4 --clips ./my-footage --assets a.png b.png --out out.mp4
```

---

## The recipe

`recipe.json` is the whole interface. Edit it by hand and re-render — nothing is
recomputed behind your back.

```jsonc
{
  "target": { "width": 1080, "height": 1920, "fps": 30, "duration": 13.5 },
  "style":  { "cut_rhythm": "medium", "avg_shot_seconds": 2.25, "tempo_bpm": 120.0 },
  "assets": { "required": 2, "footage_slots": 4, "slots": [ /* ... */ ] },
  "captions": { "enabled": true, "font_size_ratio": 0.055, "position": "center" },
  "audio":  { "mode": "music", "music": null, "fade_out_seconds": 0.75 },
  "segments": [
    {
      "index": 2,
      "role": "body",              // hook | body | cta
      "start": 4.0, "end": 7.0, "duration": 3.0,
      "fill": "asset",             // asset -> needs an image; footage -> needs a clip
      "visual": { "kind": "diagram", "confidence": 1.0, "placement": "full" },
      "reference_frame": "frames/frame-00012.jpg",
      "source_text":  "HOW IT WORKS",   // what the original said (read-only reference)
      "text": "",                       // what YOUR render says  <- edit this
      "clip": null,                     // pin a specific video here
      "asset": null,                    // pin a specific image here
      "fit": null,                      // per-segment override: cover | contain | blur
      "transition": "cut"               // only "cut" is implemented today
    }
  ]
}
```

Useful edits:

- **`text`** — the caption burned over that segment. Asset slots start empty on
  purpose: the image you supply already carries its words.
- **`clip` / `asset`** — pin specific media to a specific segment. Anything you
  pin is kept; the rest is auto-assigned around it.
- **`fill`** — override a misclassification. Flip `asset` to `footage` and the
  slot stops asking for an image.
- **`audio.mode`** — `music` (use `--music`), `source` (keep your clips' own
  audio), or `silent`.

---

## How the detection works

All heuristics, no model weights — so it's fast, offline, and inspectable.

**Cuts** come from ffmpeg's scene score (`--scene-threshold`, lower finds more).
Shots under 0.5s are folded into their neighbour as detector noise.

**Tempo** comes from peaks in the audio's energy flux; the median inter-onset
interval is folded into a musical 60–180 BPM range.

**On-screen text** is tesseract in TSV mode, keeping only words scored above
~62 confidence — textured footage otherwise invents captions out of noise.

**Footage vs. graphic** is the interesting one. A diagram or screenshot is
*flat*: large regions of constant color, a small dominant palette. Camera
footage has texture nearly everywhere. The classifier scores flatness, palette
concentration, ruled lines and text density, then splits the graphics by how
much structure they carry:

- **`title_card`** — 1–2 color regions, a few words. *Not* an asset slot: it's
  text on a solid background, which the caption system reproduces for free.
- **`diagram`** — several distinct color regions, or ruled lines.
- **`screenshot`** — dense text on a mostly-white frame.
- **`photo`** — saturated, graphic-flat, little text.

Every verdict carries a `confidence`, and the annotated contact sheet shows
them all at a glance, so a wrong call is visible and one edit to `fill` fixes
it. Tunables live at the top of `reelforge/visuals.py`.

---

## Commands

| command | what it does |
|---|---|
| `ingest <file\|url>` | analyze a source into a project + recipe |
| `assets --recipe R` | list the images the recipe needs, with reference stills |
| `render --recipe R` | build a new video from the recipe + your media |
| `make <file\|url>` | ingest and render in one step |
| `sheet --recipe R` | rebuild the contact sheet (`--columns`, `--plain`) |
| `show --recipe R` | print a recipe (`--json` for raw) |
| `doctor` | check tools and caption backend |

Useful flags: `--fit`/`--asset-fit` (`cover`, `contain`, `blur`), `--no-captions`,
`--audio-mode`, `--blank-text`, `--no-transcribe`, `--no-ocr`, `--no-visuals`,
`--frame-fps`, `--scene-threshold`, `--download-timeout`, `--save-assignments`,
`--keep-temp`.

## Not built yet

- Transitions other than a hard cut (`transition` is parsed, not applied).
- Per-segment speed ramps / zoom moves.
- Anything that picks *which* of your clips suits *which* slot — assignment is
  round-robin in order; pin `clip`/`asset` by hand when it matters.

---

## Layout

```
reelforge/
  shell.py      subprocess wrappers for ffmpeg / ffprobe / yt-dlp
  ingest.py     URL + file resolution into a project
  analyze.py    cuts, audio envelope, transcript, OCR, frame sampling
  visuals.py    footage vs diagram/screenshot/photo/title-card classifier
  recipe.py     analysis -> editable recipe JSON
  captions.py   Pillow caption rasterizer (no libfreetype needed)
  sheet.py      contact sheet artifact
  render.py     recipe + media -> mp4
  cli.py        argparse front end
tools/
  make_fixture.py   synthetic test reel generator
tests/
  test_reelforge.py
```

Every command is a thin wrapper over a library function with JSON in and files
out, so a web front end can sit on top of the same calls later.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
