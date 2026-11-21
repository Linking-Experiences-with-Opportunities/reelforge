"""Per-shot edit effects: the beat-hit moves that make an AMV read as an edit.

Every effect is a plain ffmpeg filter applied to one segment after it has been
fitted to the target frame, so `t` inside an expression is seconds since the
segment's first frame - i.e. since the cut. That is what lets a flash or a zoom
punch land exactly on the beat the cut sits on.

    flash   white flash that fades out over ~0.12s
    zoom    punch-in on the cut that eases back to normal over ~0.3s
    shake   short camera shake on the cut, settling over ~0.35s
    push    slow push-in across the whole shot
"""

from __future__ import annotations

from .shell import ToolError

EFFECTS = ("flash", "zoom", "shake", "push")

FLASH_SECONDS = 0.12
ZOOM_SECONDS = 0.30
ZOOM_AMOUNT = 0.18
SHAKE_SECONDS = 0.35
SHAKE_MARGIN = 0.08
PUSH_AMOUNT = 0.10

MIN_SPEED = 0.1
MAX_SPEED = 8.0


def parse_effects(value) -> list[str]:
    """Accept 'zoom,flash', ['zoom', 'flash'], or None; reject unknown names."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        names = [part.strip().lower() for part in value.replace(" ", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        names = [str(part).strip().lower() for part in value]
    else:
        raise ToolError(f"effects must be a list or comma-separated string, got {value!r}")
    names = [n for n in names if n and n != "none"]
    unknown = [n for n in names if n not in EFFECTS]
    if unknown:
        raise ToolError(
            f"unknown effect(s) {', '.join(unknown)} (available: {', '.join(EFFECTS)})"
        )
    # Keep the order given but drop repeats; applying a zoom twice is not a feature.
    return list(dict.fromkeys(names))


def parse_speed(value) -> float:
    if value is None or value == "":
        return 1.0
    text = str(value).strip().lower().rstrip("x")
    try:
        speed = float(text)
    except ValueError:
        raise ToolError(f"bad speed {value!r} (use e.g. 0.5 or 2x)") from None
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ToolError(f"speed {speed} is outside {MIN_SPEED}-{MAX_SPEED}")
    return speed


def _even(expr: str) -> str:
    return f"trunc(({expr})/2)*2"


def filter_chain(effects: list[str], width: int, height: int, duration: float) -> str:
    """The ffmpeg filters (comma-joined, no leading comma) for `effects`.

    Input and output are both `width`x`height`, so the chain slots in after the
    fit and before captions without changing anything around it.
    """
    parts: list[str] = []
    # Geometry first (zoom / push / shake), flash last so it covers the frame.
    scale_terms = []
    if "zoom" in effects:
        scale_terms.append(f"{ZOOM_AMOUNT}*max(0\\,1-t/{ZOOM_SECONDS})")
    if "push" in effects and duration > 0:
        scale_terms.append(f"{PUSH_AMOUNT}*min(1\\,t/{duration:.3f})")
    if scale_terms:
        factor = "(1+" + "+".join(scale_terms) + ")"
        parts.append(
            f"scale=w='{_even(f'{width}*{factor}')}':h='{_even(f'{height}*{factor}')}'"
            f":eval=frame,crop={width}:{height}"
        )

    if "shake" in effects:
        big_w = int(width * (1 + SHAKE_MARGIN)) // 2 * 2
        big_h = int(height * (1 + SHAKE_MARGIN)) // 2 * 2
        decay = f"max(0\\,1-t/{SHAKE_SECONDS})"
        parts.append(
            f"scale={big_w}:{big_h},crop={width}:{height}"
            f":x='(iw-ow)/2*(1+sin(t*83)*{decay})'"
            f":y='(ih-oh)/2*(1+cos(t*61)*{decay})'"
        )

    if "flash" in effects:
        parts.append(f"fade=t=in:st=0:d={FLASH_SECONDS}:color=white")

    return ",".join(parts)


def atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5-2.0 per instance, so chain it for anything else."""
    steps = []
    remaining = speed
    while remaining > 2.0:
        steps.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        steps.append(0.5)
        remaining /= 0.5
    steps.append(remaining)
    return ",".join(f"atempo={s:.6f}" for s in steps)
