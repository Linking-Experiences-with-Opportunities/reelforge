"""The recipe: a source video's structure, in an editable JSON form."""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from . import visuals
from .analyze import Analysis

RECIPE_VERSION = 1

# Minimum segment length. Sub-half-second shots are almost always scene-detector
# noise (a flash frame, a whip pan) rather than an editorial cut worth rebuilding.
MIN_SEGMENT = 0.5


def _rhythm(avg: float) -> str:
    if avg <= 1.2:
        return "fast"
    if avg <= 2.5:
        return "medium"
    return "slow"


def _text_density(beats: int, duration: float) -> str:
    if duration <= 0 or beats == 0:
        return "none"
    per_10s = beats / (duration / 10.0)
    if per_10s >= 3:
        return "heavy"
    if per_10s >= 1:
        return "moderate"
    return "light"


def _merge_short_segments(bounds: list[float], duration: float) -> list[tuple[float, float]]:
    points = [0.0] + [b for b in bounds if 0 < b < duration] + [duration]
    segments: list[tuple[float, float]] = []
    start = points[0]
    for end in points[1:]:
        if end - start < MIN_SEGMENT:
            continue  # fold this sliver into the running segment
        segments.append((start, end))
        start = end
    if not segments:
        return [(0.0, duration)] if duration > 0 else []
    # Whatever is left over after the last accepted boundary extends the final shot.
    if duration - segments[-1][1] > 0.01:
        segments[-1] = (segments[-1][0], duration)
    return segments


def _energy_for(analysis: Analysis, start: float, end: float) -> float | None:
    if not analysis.energy:
        return None
    hop = analysis.energy_hop or 0.05
    lo = int(start / hop)
    hi = max(lo + 1, int(end / hop))
    window = analysis.energy[lo:hi]
    if not window:
        return None
    return round(statistics.fmean(window), 3)


def _overlapping_text(beats, start: float, end: float) -> str:
    """The OCR beat that covers the most of this segment."""
    best, best_overlap = "", 0.0
    for beat in beats:
        overlap = min(end, beat.end) - max(start, beat.start)
        if overlap > best_overlap and overlap > 0:
            best, best_overlap = beat.text, overlap
    return best


def _overlapping_speech(segments: list[dict], start: float, end: float) -> str:
    parts = [
        seg["text"]
        for seg in segments
        if min(end, seg["end"]) - max(start, seg["start"]) > 0.05
    ]
    return " ".join(parts).strip()


# A caption is a line, not a paragraph. Anything longer than this was almost
# certainly OCR'd out of a screenshot or a dense graphic, not a real overlay.
MAX_CAPTION_WORDS = 8

# Platform watermarks (TikTok stamps "TikTok @handle" and drifts it around the
# frame; Instagram and YouTube do similar) are on-screen text, so OCR picks
# them up - but they belong to the *source*, not to the video being built.
# Seeding them as captions would burn another creator's handle into your
# render, so they are dropped. They stay in `source_text` for reference.
_WATERMARK_PATTERNS = [
    re.compile(r"\btiktok\b", re.I),
    re.compile(r"\binstagram\b", re.I),
    re.compile(r"\byoutube\b", re.I),
    re.compile(r"^\s*@[\w.]+\s*$"),
]


def _looks_like_watermark(text: str) -> bool:
    """Is this OCR'd text a platform watermark rather than a real caption?"""
    stripped = text.strip()
    if not stripped:
        return False
    # A bare handle, or any short line naming a platform, is a watermark.
    # A longer sentence that merely mentions TikTok is left alone.
    if len(stripped.split()) > MAX_CAPTION_WORDS:
        return False
    return any(pattern.search(stripped) for pattern in _WATERMARK_PATTERNS)


def _starting_text(source_text: str, is_asset: bool, copy_source_text: bool) -> str:
    """The `text` a fresh recipe starts with, before you edit it.

    Asset slots start empty: the diagram or screenshot you supply carries its
    own words, so stamping the source's text over it would double up. Footage
    and title-card slots inherit short overlays so a first render is
    immediately watchable.
    """
    if not copy_source_text or is_asset:
        return ""
    if len(source_text.split()) > MAX_CAPTION_WORDS:
        return ""
    if _looks_like_watermark(source_text):
        return ""
    return source_text


def _reference_frame(analysis: Analysis, start: float, end: float) -> str | None:
    """The sampled still that best represents this segment, as a project-relative path."""
    if not analysis.frames:
        return None
    midpoint = (start + end) / 2
    time, path = min(analysis.frames, key=lambda f: abs(f[0] - midpoint))
    return f"frames/{path.name}"


def _role(index: int, count: int, start: float) -> str:
    if start < 3.0 and index == 0:
        return "hook"
    if index == count - 1 and count > 2:
        return "cta"
    return "body"


def build_recipe(
    analysis: Analysis,
    *,
    name: str,
    origin: str,
    platform: str,
    meta: dict,
    copy_source_text: bool = True,
) -> dict:
    probe = analysis.probe
    duration = float(probe.get("duration") or 0.0)
    spans = _merge_short_segments(analysis.cuts, duration)

    segments = []
    for index, (start, end) in enumerate(spans):
        source_text = _overlapping_text(analysis.ocr_beats, start, end)
        speech = _overlapping_speech(analysis.transcript, start, end)
        verdict = visuals.summarize_segment(analysis.visual_verdicts, start, end)
        # A title card counts as footage: its words come back through the
        # caption system, so it needs no image from the user.
        is_asset = verdict is not None and verdict.kind in visuals.ASSET_KINDS

        segments.append({
            "index": index,
            "role": _role(index, len(spans), start),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "energy": _energy_for(analysis, start, end),
            "source_text": source_text,
            "source_speech": speech,
            # What this slot needs from you: "footage" -> a clip from --clips,
            # "asset" -> a diagram/screenshot/image from --assets.
            "fill": "asset" if is_asset else "footage",
            "visual": {
                "kind": verdict.kind if verdict else "unknown",
                "confidence": round(verdict.confidence, 2) if verdict else 0.0,
                "placement": verdict.placement if verdict else "full",
                "bbox_per_mille": verdict.bbox if verdict else None,
            },
            "reference_frame": _reference_frame(analysis, start, end),
            # `text`, `clip` and `asset` are the fields you edit before rendering.
            "text": _starting_text(source_text, is_asset, copy_source_text),
            "clip": None,
            "asset": None,
            "transition": "cut",
        })

    asset_slots = [
        {
            "segment": s["index"],
            "kind": s["visual"]["kind"],
            "confidence": s["visual"]["confidence"],
            "placement": s["visual"]["placement"],
            "start": s["start"],
            "duration": s["duration"],
            "source_text": s["source_text"],
            "reference_frame": s["reference_frame"],
        }
        for s in segments if s["fill"] == "asset"
    ]

    shot_lengths = [s["duration"] for s in segments] or [duration]
    avg_shot = statistics.fmean(shot_lengths) if shot_lengths else duration

    return {
        "version": RECIPE_VERSION,
        "name": name,
        "source": {
            "origin": origin,
            "platform": platform,
            "meta": meta,
            "probe": probe,
        },
        "target": {
            "width": probe.get("width") or 1080,
            "height": probe.get("height") or 1920,
            "fps": round(float(probe.get("fps") or 30.0)),
            "duration": round(duration, 3),
        },
        "style": {
            "cut_count": len(analysis.cuts),
            "segment_count": len(segments),
            "avg_shot_seconds": round(avg_shot, 2),
            "shortest_shot": round(min(shot_lengths), 2) if shot_lengths else None,
            "longest_shot": round(max(shot_lengths), 2) if shot_lengths else None,
            "cut_rhythm": _rhythm(avg_shot),
            "tempo_bpm": round(analysis.tempo_bpm, 1) if analysis.tempo_bpm else None,
            "text_density": _text_density(len(analysis.ocr_beats), duration),
            "orientation": probe.get("orientation"),
            "has_speech": bool(analysis.transcript),
        },
        "analysis": {
            # Sampling interval used for OCR, visual detection and the contact
            # sheet; needed to map saved frames back to timestamps.
            "frame_step": round(analysis.frame_step, 4),
            "frame_count": len(analysis.frames),
        },
        "assets": {
            # How many diagrams / screenshots / images you need to supply.
            "required": len(asset_slots),
            "footage_slots": sum(1 for s in segments if s["fill"] == "footage"),
            "slots": asset_slots,
        },
        "captions": {
            "enabled": any(s["text"] for s in segments),
            "font_size_ratio": 0.055,
            "position": "center",
            "color": "white",
            "box": True,
            "box_opacity": 0.45,
            "uppercase": False,
        },
        "audio": {
            # "music" = use --music; "source" = keep the clips' own audio;
            # "silent" = drop audio entirely.
            "mode": "music",
            "music": None,
            "music_gain_db": -2.0,
            "fade_out_seconds": 0.75,
        },
        "segments": segments,
        "notes": analysis.notes,
    }


def load_recipe(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read recipe: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"recipe is not valid JSON ({path}): {exc}") from exc

    if not isinstance(data, dict) or "segments" not in data:
        raise ValueError(f"{path} does not look like a reelforge recipe (no 'segments')")
    if data.get("version") != RECIPE_VERSION:
        raise ValueError(
            f"recipe version {data.get('version')!r} is not supported "
            f"(this build expects {RECIPE_VERSION})"
        )
    return data


def save_recipe(recipe: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(_public(recipe), indent=2) + "\n", "utf-8")


def _public(value):
    """Strip internal `_`-prefixed bookkeeping the renderer adds in memory."""
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def describe(recipe: dict) -> str:
    """Human-readable summary of a recipe."""
    style = recipe.get("style", {})
    target = recipe.get("target", {})
    audio = recipe.get("audio", {})
    assets = recipe.get("assets", {})
    segments = recipe.get("segments", [])

    lines = [
        f"{recipe.get('name', 'recipe')}  ({recipe['source'].get('platform', '?')})",
        f"  source     {recipe['source'].get('origin', '?')}",
        f"  target     {target.get('width')}x{target.get('height')} @ {target.get('fps')}fps"
        f"  |  {target.get('duration')}s",
        f"  rhythm     {style.get('cut_rhythm')} - {style.get('segment_count')} segments,"
        f" avg {style.get('avg_shot_seconds')}s"
        f" (range {style.get('shortest_shot')}-{style.get('longest_shot')}s)",
        f"  tempo      {style.get('tempo_bpm') or 'n/a'} BPM",
        f"  text       {style.get('text_density')}"
        f"  |  speech: {'yes' if style.get('has_speech') else 'no'}",
        f"  audio      mode={audio.get('mode')} music={audio.get('music') or 'none'}",
        f"  needs      {assets.get('footage_slots', 0)} footage slot(s)"
        f"  +  {assets.get('required', 0)} image/diagram slot(s)",
        "",
        "  #   role   fill     kind        start    dur   source                text",
    ]
    for seg in segments:
        source = seg.get("asset") if seg.get("fill") == "asset" else seg.get("clip")
        source = Path(source).name if source else "-"
        kind = (seg.get("visual") or {}).get("kind", "?")
        text = (seg.get("text") or "").replace("\n", " ")
        lines.append(
            f"  {seg['index']:<3} {seg['role']:<6} {seg.get('fill', '?'):<8} {kind:<11}"
            f"{seg['start']:>6.2f} {seg['duration']:>6.2f}"
            f"   {source[:20]:<21} {text[:36]}"
        )

    slots = assets.get("slots") or []
    if slots:
        lines.append("")
        lines.append(f"  {len(slots)} image/diagram(s) needed:")
        for position, slot in enumerate(slots, start=1):
            label = slot.get("source_text") or "(no text detected)"
            lines.append(
                f"    {position}. segment {slot['segment']:<3} {slot['kind']:<11}"
                f" @ {slot['start']:>6.2f}s for {slot['duration']:.2f}s"
                f"  conf {slot.get('confidence', 0):.2f}"
            )
            lines.append(f"       looks like: {label[:60]}")
            if slot.get("reference_frame"):
                lines.append(f"       reference:  {slot['reference_frame']}")

    notes = recipe.get("notes") or []
    if notes:
        lines.append("")
        lines.append("  notes:")
        lines += [f"    - {n}" for n in notes]
    return "\n".join(lines)
