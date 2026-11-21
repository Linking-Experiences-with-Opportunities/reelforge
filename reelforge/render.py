"""Rebuild a new video from a recipe plus your own clips."""

from __future__ import annotations

import re
import shlex
import shutil
import tempfile
from pathlib import Path

from . import captions as captions_mod
from . import effects as effects_mod
from .shell import ToolError, ffmpeg, ffprobe_json, run

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def find_font(explicit: str | None = None) -> str | None:
    if explicit:
        if not Path(explicit).exists():
            raise ToolError(f"font file not found: {explicit}")
        return explicit
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def collect_clips(paths: list[str]) -> list[Path]:
    """Expand a mix of files and directories into a sorted media list."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found += sorted(
                p for p in path.iterdir()
                if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
            )
        elif path.is_file():
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                raise ToolError(f"unsupported clip type: {path}")
            found.append(path)
        else:
            raise ToolError(f"no such clip or directory: {path}")
    if not found:
        raise ToolError(
            "no usable clips found. Pass a directory of videos/images or "
            "individual files via --clips."
        )
    return found


def _media_info(path: Path) -> dict:
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in (info.get("format", {}).get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    return {
        "duration": duration,
        "has_audio": audio is not None,
        "is_image": path.suffix.lower() in IMAGE_SUFFIXES,
    }


def segment_source(segment: dict) -> str | None:
    """The media path a segment will use, whichever slot kind it is."""
    if segment.get("fill") == "asset":
        return segment.get("asset") or segment.get("clip")
    return segment.get("clip")


def asset_segments(recipe: dict) -> list[dict]:
    return [s for s in recipe.get("segments", []) if s.get("fill") == "asset"]


def footage_segments(recipe: dict) -> list[dict]:
    return [s for s in recipe.get("segments", []) if s.get("fill") != "asset"]


def assign_sources(recipe: dict, clips: list[Path], assets: list[Path]) -> dict:
    """Fill each segment's `clip` / `asset` field from the supplied media.

    Footage slots cycle round-robin over `clips`; image/diagram slots are filled
    in order from `assets`. Assignments already present in the recipe are kept,
    so you can pin a specific diagram to a specific segment by hand and let the
    rest fall into place. When one clip covers several segments, each reuse
    starts further into it so the output does not repeat the same frames.
    """
    segments = recipe.get("segments", [])
    if not segments:
        raise ToolError("recipe has no segments to render")

    if clips:
        pending = [s for s in footage_segments(recipe) if not s.get("clip")]
        for offset, segment in enumerate(pending):
            segment["clip"] = str(clips[offset % len(clips)].resolve())

    if assets:
        pending = [s for s in asset_segments(recipe) if not s.get("asset")]
        for offset, segment in enumerate(pending):
            # Never wrap around: a short asset list is reported as an error
            # rather than quietly repeating the same diagram.
            if offset < len(assets):
                segment["asset"] = str(assets[offset].resolve())

    seen: dict[str, int] = {}
    for segment in segments:
        source = segment_source(segment)
        if not source:
            continue
        segment["_reuse_index"] = seen.get(source, 0)
        seen[source] = segment["_reuse_index"] + 1
    return recipe


_TIMECODE_RE = re.compile(r"^\d+(?::\d+){0,2}(?:\.\d+)?$")
_SPEED_RE = re.compile(r"^\d+(?:\.\d+)?x$", re.I)


def parse_shot_list(text: str, base_dir: Path,
                    search_dirs: list[Path] | None = None) -> list[dict]:
    """Parse a shot list: one line per footage segment, in timeline order.

        # clip           in-point   effects       speed
        gojo-01.mp4      0:03.2     zoom flash
        sukuna.mov       12.5       shake         0.5x
        toji.mp4                    -                     <- no effects
        ...                                               <- keep segment as-is

    Only the clip is required. A bare number or MM:SS is the in-point, `Nx`
    is the speed, `-` clears effects, and any effect name is added. A line of
    just `...` leaves that segment's existing settings alone. Clip names are
    looked up next to the shot list, then in each `search_dirs` folder.
    """
    from . import effects as fx_mod

    shots: list[dict] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise ToolError(f"shot list line {number}: {exc}") from None
        if tokens == ["..."]:
            shots.append({})
            continue

        name, rest = tokens[0], tokens[1:]
        candidates = [Path(name).expanduser()]
        if not candidates[0].is_absolute():
            candidates = [base_dir / name] + [d / name for d in (search_dirs or [])]
        clip = next((c for c in candidates if c.is_file()), None)
        if clip is None:
            raise ToolError(f"shot list line {number}: clip not found: {name}")

        shot: dict = {"clip": str(clip.resolve())}
        effects: list[str] = []
        for token in rest:
            if _TIMECODE_RE.match(token):
                if "clip_start" in shot:
                    raise ToolError(f"shot list line {number}: two in-points ({token})")
                from .ingest import parse_timecode
                shot["clip_start"] = round(parse_timecode(token), 3)
            elif _SPEED_RE.match(token):
                shot["speed"] = fx_mod.parse_speed(token)
            elif token == "-":
                shot["effects"] = []
            else:
                effects += fx_mod.parse_effects(token)
        if effects:
            shot["effects"] = list(dict.fromkeys(shot.get("effects", []) + effects))
        shots.append(shot)
    if not shots:
        raise ToolError("the shot list is empty")
    return shots


def apply_shot_list(recipe: dict, shots: list[dict]) -> list[str]:
    """Pin shots onto the recipe's footage segments in order. Returns warnings."""
    footage = footage_segments(recipe)
    warnings = []
    if len(shots) > len(footage):
        warnings.append(
            f"shot list has {len(shots)} shots but the recipe only has {len(footage)} "
            f"footage segments; the last {len(shots) - len(footage)} are unused"
        )
    elif len(shots) < len(footage):
        warnings.append(
            f"shot list covers {len(shots)} of {len(footage)} footage segments; "
            f"the rest are filled from --clips"
        )
    for segment, shot in zip(footage, shots):
        segment.update(shot)
    return warnings


def snap_to_frames(segments: list[dict], fps: int) -> None:
    """Give each segment a whole-frame length measured from the timeline.

    Rendering each segment at its own rounded length drifts: 0.469s at 30fps
    is 14.06 frames, encoded as 14, so every cut lands a little earlier than
    the beat it was placed on and a 60-cut edit ends ~0.1s out of sync.
    Rounding the *boundaries* instead keeps every cut within half a frame.
    """
    if fps <= 0:
        return
    position = 0.0
    for segment in segments:
        # Trust the recipe's own `start` when it is consistent with the
        # running position; hand-edited recipes may not keep it up to date.
        start = position
        stated = segment.get("start")
        if isinstance(stated, (int, float)) and abs(stated - position) < 0.01:
            start = float(stated)
        end = start + float(segment["duration"])
        frames = round(end * fps) - round(start * fps)
        segment["_render_duration"] = max(1, frames) / fps
        position = end


def clip_start_seconds(segment: dict) -> float | None:
    """The segment's pinned in-point, from seconds or an 'MM:SS.ss' string."""
    value = segment.get("clip_start")
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            raise ToolError(f"segment {segment.get('index')}: negative clip_start {value}")
        return float(value)
    from .ingest import parse_timecode
    return parse_timecode(str(value))


def missing_sources(recipe: dict) -> tuple[list[dict], list[dict]]:
    """(footage segments without a clip, asset segments without an image)."""
    return (
        [s for s in footage_segments(recipe) if not s.get("clip")],
        [s for s in asset_segments(recipe) if not s.get("asset")],
    )


def _caption_png(segment: dict, recipe: dict, font: str | None, tmp_dir: Path) -> Path | None:
    """Rasterize this segment's caption to a transparent overlay, if it has one."""
    captions = recipe.get("captions", {})
    if not captions.get("enabled", True):
        return None
    text = (segment.get("text") or "").strip()
    if not text or font is None:
        return None
    if captions.get("uppercase"):
        text = text.upper()

    style = captions_mod.style_from_recipe(recipe, font)
    out = tmp_dir / f"caption-{int(segment['index']):04d}.png"
    drawn = captions_mod.render_caption_png(
        text,
        int(recipe["target"]["width"]),
        int(recipe["target"]["height"]),
        style,
        out,
    )
    return out if drawn else None


def parse_crop(value: str) -> tuple[str, ...]:
    """Parse 'x,y,w,h' into ffmpeg crop terms.

    Values are pixels by default, or a percentage of the source when suffixed
    with '%' - useful for a screen capture whose facecam sits at a fixed
    fraction of the frame regardless of resolution.
    """
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 4:
        raise ToolError(
            f"--crop expects 'x,y,w,h' (got {value!r}). "
            f"Pixels, or percentages like '2%,69%,7%,29%'."
        )

    terms = []
    for part, (dimension, origin) in zip(parts, (("iw", "x"), ("ih", "y"),
                                                 ("iw", "w"), ("ih", "h"))):
        if part.endswith("%"):
            try:
                fraction = float(part[:-1]) / 100.0
            except ValueError:
                raise ToolError(f"--crop: bad percentage {part!r}") from None
            terms.append(f"{dimension}*{fraction:.6f}")
        else:
            try:
                pixels = float(part)
            except ValueError:
                raise ToolError(f"--crop: bad number {part!r}") from None
            if pixels < 0:
                raise ToolError(f"--crop: negative value {part!r}")
            terms.append(f"{pixels:.0f}")
    x, y, w, h = terms
    return (w, h, x, y)


def _crop_filter(crop: str | None) -> str:
    if not crop:
        return ""
    w, h, x, y = parse_crop(crop)
    # Keep even dimensions; yuv420p cannot encode odd sizes.
    return f"crop=w=floor(({w})/2)*2:h=floor(({h})/2)*2:x={x}:y={y},"


def _fit_filter(width: int, height: int, fit: str) -> str:
    if fit == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if fit == "blur":
        # Handled separately; needs a split, so it cannot be a single chain.
        raise AssertionError("blur fit is built inline")
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )


def _render_segment(segment: dict, recipe: dict, out_path: Path, tmp_dir: Path,
                    font: str | None, fit: str, use_clip_audio: bool,
                    crop: str | None = None,
                    default_effects: list[str] | None = None) -> None:
    clip = Path(segment_source(segment))
    info = _media_info(clip)
    duration = float(segment.get("_render_duration") or segment["duration"])
    width = int(recipe["target"]["width"])
    height = int(recipe["target"]["height"])
    fps = int(recipe["target"]["fps"])

    # A segment's own `effects` (even an empty list) wins over the global set.
    if "effects" in segment and segment["effects"] is not None:
        fx = effects_mod.parse_effects(segment["effects"])
    else:
        fx = list(default_effects or [])
    speed = 1.0 if info["is_image"] else effects_mod.parse_speed(segment.get("speed"))
    # At 0.5x a 1s slot eats 0.5s of source; at 2x it eats 2s.
    source_span = duration * speed

    args: list[str] = []
    clip_has_audio = info["has_audio"] and use_clip_audio and not info["is_image"]
    next_input = 1  # input 0 is always the clip itself

    if info["is_image"]:
        args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(clip)]
    else:
        clip_duration = info["duration"] or 0.0
        pinned = clip_start_seconds(segment)
        if pinned is not None:
            # The moment you picked. Past the end of the clip is a typo, not
            # something to paper over with a silent loop.
            if clip_duration and pinned >= clip_duration:
                raise ToolError(
                    f"segment {segment['index']}: clip_start {pinned:.2f}s is past the end "
                    f"of {clip.name} ({clip_duration:.2f}s)"
                )
            offset = pinned
            if clip_duration and pinned + source_span > clip_duration:
                # Not enough clip after the chosen moment: hold on it by
                # backing the in-point up instead of looping back to frame 0.
                offset = max(0.0, clip_duration - source_span)
        else:
            offset = 0.0
            if clip_duration > source_span:
                # Stagger reuses across the clip, but never past its end.
                stride = max(0.0, clip_duration - source_span)
                reuse = segment.get("_reuse_index", 0)
                offset = min(stride, reuse * source_span)
        if clip_duration and clip_duration - offset < source_span:
            args += ["-stream_loop", "-1"]
        args += ["-ss", f"{offset:.3f}", "-t", f"{source_span:.3f}", "-i", str(clip)]

    caption_png = _caption_png(segment, recipe, font, tmp_dir)
    caption_index = None
    if caption_png is not None:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}",
                 "-i", str(caption_png)]
        caption_index = next_input
        next_input += 1

    audio_input_index = None
    if not clip_has_audio:
        args += ["-f", "lavfi", "-t", f"{duration:.3f}",
                 "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        audio_input_index = next_input
        next_input += 1

    crop_chain = _crop_filter(crop)
    if speed != 1.0:
        crop_chain = f"setpts=(PTS-STARTPTS)/{speed:.6f}," + crop_chain

    if fit == "blur":
        chain = (
            f"[0:v]{crop_chain}split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=40:luma_power=2[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[fit];"
        )
        tail = f"[fit]fps={fps},setsar=1"
    else:
        chain = ""
        tail = (f"[0:v]{crop_chain}{_fit_filter(width, height, fit)},"
                f"fps={fps},setsar=1")

    fx_chain = effects_mod.filter_chain(fx, width, height, duration)
    if fx_chain:
        tail += "," + fx_chain

    if caption_index is not None:
        # Composite the caption before flattening to yuv420p so the PNG's alpha
        # is respected.
        tail += "[base];"
        tail += (f"[base][{caption_index}:v]overlay=0:0:format=auto,"
                 f"format=yuv420p[v]")
    else:
        tail += ",format=yuv420p[v]"

    filter_complex = chain + tail
    audio_map = "0:a:0" if clip_has_audio else f"{audio_input_index}:a:0"
    if clip_has_audio and speed != 1.0:
        filter_complex += f";[0:a:0]{effects_mod.atempo_chain(speed)}[a]"
        audio_map = "[a]"

    ffmpeg(args + [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ])


def _concat(parts: list[Path], out_path: Path, tmp_dir: Path) -> None:
    listing = tmp_dir / "concat.txt"
    listing.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in parts), "utf-8"
    )
    concat_args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    try:
        ffmpeg(concat_args + ["-c", "copy", "-movflags", "+faststart", str(out_path)])
    except ToolError:
        # Stream copy is picky; fall back to a re-encode of the joined timeline.
        ffmpeg(concat_args + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out_path),
        ])


def _apply_audio(video: Path, recipe: dict, out_path: Path,
                 music_override: Path | None) -> None:
    audio_cfg = recipe.get("audio", {})
    mode = audio_cfg.get("mode", "music")
    music = music_override or (Path(audio_cfg["music"]) if audio_cfg.get("music") else None)
    duration = float(recipe["target"]["duration"])
    fade = float(audio_cfg.get("fade_out_seconds", 0.75) or 0.0)

    if mode == "source" or (mode == "music" and music is None):
        shutil.move(str(video), str(out_path))
        return

    if mode == "silent":
        ffmpeg([
            "-i", str(video),
            "-c:v", "copy", "-an",
            "-movflags", "+faststart", str(out_path),
        ])
        return

    if not music.exists():
        raise ToolError(f"music file not found: {music}")

    gain = float(audio_cfg.get("music_gain_db", 0.0) or 0.0)
    fade_start = max(0.0, duration - fade)
    afilter = f"volume={gain}dB"
    if fade > 0:
        afilter += f",afade=t=out:st={fade_start:.3f}:d={fade:.3f}"

    ffmpeg([
        "-i", str(video),
        # `music_start` lets a recipe built from the middle of a song (the
        # drop, the chorus) line its beats up with the same spot in the track.
        "-ss", f"{float(audio_cfg.get('music_start') or 0.0):.3f}",
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex", f"[1:a]{afilter},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ])


def render(
    recipe: dict,
    out_path: Path,
    *,
    clips: list[Path] | None = None,
    assets: list[Path] | None = None,
    music: Path | None = None,
    fit: str = "cover",
    asset_fit: str = "blur",
    crop: str | None = None,
    font: str | None = None,
    effects: list[str] | None = None,
    keep_temp: bool = False,
    on_progress=None,
) -> Path:
    """Render `recipe` to `out_path`, returning the written file.

    `effects` applies to every segment that does not list its own.
    """
    default_effects = effects_mod.parse_effects(effects)
    assign_sources(recipe, clips or [], assets or [])

    segments = recipe.get("segments", [])
    no_clip, no_asset = missing_sources(recipe)

    if no_asset:
        needed = len(asset_segments(recipe))
        supplied = len(assets or [])
        details = "\n".join(
            f"    segment {s['index']:<3} {(s.get('visual') or {}).get('kind', 'image'):<11}"
            f" @ {s['start']:>6.2f}s for {s['duration']:.2f}s"
            f"   looks like: {(s.get('source_text') or '(no text detected)')[:44]}"
            for s in no_asset
        )
        raise ToolError(
            f"this recipe needs {needed} image/diagram(s); you supplied {supplied}.\n"
            f"  {len(no_asset)} slot(s) still unfilled:\n{details}\n"
            f"  Pass them with --assets img1.png img2.png ... (in timeline order),\n"
            f"  or run 'reelforge assets --recipe <recipe>' to see reference stills."
        )

    if no_clip:
        raise ToolError(
            f"segments {[s['index'] for s in no_clip]} have no clip assigned. "
            f"Pass --clips, or set 'clip' on each segment in the recipe."
        )

    for segment in segments:
        source = segment_source(segment)
        if not Path(source).exists():
            raise ToolError(
                f"segment {segment['index']} references a missing file: {source}"
            )

    for name, value in (("--fit", fit), ("--asset-fit", asset_fit)):
        if value not in {"cover", "contain", "blur"}:
            raise ToolError(f"unknown {name} '{value}' (expected cover, contain, or blur)")

    captions_on = recipe.get("captions", {}).get("enabled", True)
    wants_text = captions_on and any(s.get("text") for s in segments)
    font_path = find_font(font)
    if wants_text and font_path is None:
        raise ToolError(
            "no usable font found for captions. Pass --font /path/to/font.ttf, "
            "set captions.enabled=false in the recipe, or render with --no-captions."
        )
    if wants_text and captions_mod.backend() == "none":
        raise ToolError(
            "this ffmpeg has no drawtext filter and Pillow is not installed, "
            "so captions cannot be rendered.\n"
            "  fix: pip3 install Pillow   (or render with --no-captions)"
        )

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    snap_to_frames(segments, int(recipe["target"]["fps"]))

    tmp_root = Path(tempfile.mkdtemp(prefix="reelforge-render-"))
    try:
        parts: list[Path] = []
        for position, segment in enumerate(segments):
            part = tmp_root / f"seg-{segment['index']:04d}.mp4"
            if on_progress:
                on_progress(position + 1, len(segments), segment)
            # A diagram must stay fully readable, so asset slots default to a
            # non-cropping fit even when footage is set to cover.
            segment_fit = segment.get("fit") or (
                asset_fit if segment.get("fill") == "asset" else fit
            )
            # A per-segment crop in the recipe wins over the global --crop,
            # so one facecam region can differ from another shot's framing.
            segment_crop = segment.get("crop") or (
                None if segment.get("fill") == "asset" else crop
            )
            _render_segment(
                segment, recipe, part, tmp_root, font_path, segment_fit,
                use_clip_audio=recipe.get("audio", {}).get("mode") == "source",
                crop=segment_crop,
                default_effects=default_effects,
            )
            parts.append(part)

        joined = tmp_root / "joined.mp4"
        _concat(parts, joined, tmp_root)
        _apply_audio(joined, recipe, out_path, music)
    finally:
        if keep_temp:
            print(f"  temp files kept in {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    return out_path
