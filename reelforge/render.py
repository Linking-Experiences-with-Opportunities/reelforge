"""Rebuild a new video from a recipe plus your own clips."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import captions as captions_mod
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
                    font: str | None, fit: str, use_clip_audio: bool) -> None:
    clip = Path(segment_source(segment))
    info = _media_info(clip)
    duration = float(segment["duration"])
    width = int(recipe["target"]["width"])
    height = int(recipe["target"]["height"])
    fps = int(recipe["target"]["fps"])

    args: list[str] = []
    clip_has_audio = info["has_audio"] and use_clip_audio and not info["is_image"]
    next_input = 1  # input 0 is always the clip itself

    if info["is_image"]:
        args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(clip)]
    else:
        clip_duration = info["duration"] or 0.0
        offset = 0.0
        if clip_duration > duration:
            # Stagger reuses across the clip, but never past its end.
            stride = max(0.0, clip_duration - duration)
            reuse = segment.get("_reuse_index", 0)
            offset = min(stride, reuse * duration)
        if clip_duration and clip_duration < duration:
            args += ["-stream_loop", "-1"]
        args += ["-ss", f"{offset:.3f}", "-t", f"{duration:.3f}", "-i", str(clip)]

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

    if fit == "blur":
        chain = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=40:luma_power=2[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[fit];"
        )
        tail = f"[fit]fps={fps},setsar=1"
    else:
        chain = ""
        tail = f"[0:v]{_fit_filter(width, height, fit)},fps={fps},setsar=1"

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
    font: str | None = None,
    keep_temp: bool = False,
    on_progress=None,
) -> Path:
    """Render `recipe` to `out_path`, returning the written file."""
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
            _render_segment(
                segment, recipe, part, tmp_root, font_path, segment_fit,
                use_clip_audio=recipe.get("audio", {}).get("mode") == "source",
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
