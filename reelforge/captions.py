"""Caption rendering.

Homebrew's ffmpeg is frequently built without libfreetype, which removes the
`drawtext`, `subtitles` and `ass` filters entirely. Rather than depend on that
build flag, reelforge rasterizes caption text to a transparent PNG with Pillow
and composites it with `overlay`, which every ffmpeg build supports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .shell import ToolError, run

try:  # pragma: no cover - import guard
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    PILLOW_AVAILABLE = False


_COLORS = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "yellow": (255, 214, 0),
    "red": (233, 61, 61),
    "green": (61, 214, 132),
    "blue": (74, 158, 255),
}


def drawtext_available() -> bool:
    """True when this ffmpeg build ships the drawtext filter."""
    proc = run(["ffmpeg", "-hide_banner", "-filters"], check=False)
    return b"drawtext" in (proc.stdout or b"")


def backend() -> str:
    if PILLOW_AVAILABLE:
        return "pillow"
    if drawtext_available():
        return "drawtext"
    return "none"


def parse_color(value: str) -> tuple[int, int, int]:
    if not value:
        return (255, 255, 255)
    value = value.strip().lower()
    if value in _COLORS:
        return _COLORS[value]
    hexed = value.lstrip("#").lstrip("0x")
    if len(hexed) == 6:
        try:
            return (int(hexed[0:2], 16), int(hexed[2:4], 16), int(hexed[4:6], 16))
        except ValueError:
            pass
    return (255, 255, 255)


@dataclass
class CaptionStyle:
    font_path: str
    font_size: int
    color: tuple[int, int, int] = (255, 255, 255)
    stroke_width: int = 0
    stroke_color: tuple[int, int, int] = (0, 0, 0)
    box: bool = True
    box_opacity: float = 0.45
    box_padding: int = 28
    box_radius: int = 18
    line_spacing: float = 1.18
    position: str = "center"
    margin_ratio: float = 0.08


def style_from_recipe(recipe: dict, font_path: str) -> CaptionStyle:
    cfg = recipe.get("captions", {})
    height = int(recipe["target"]["height"])
    size = max(18, int(height * float(cfg.get("font_size_ratio", 0.055))))
    return CaptionStyle(
        font_path=font_path,
        font_size=size,
        color=parse_color(cfg.get("color", "white")),
        stroke_width=max(0, int(size * 0.055)),
        box=bool(cfg.get("box", True)),
        box_opacity=float(cfg.get("box_opacity", 0.45)),
        box_padding=max(12, int(size * 0.45)),
        box_radius=max(8, int(size * 0.28)),
        position=cfg.get("position", "center"),
    )


def _wrap(text: str, font, max_width: int, draw) -> list[str]:
    """Greedy wrap using real glyph metrics."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render_caption_png(text: str, width: int, height: int, style: CaptionStyle,
                       out_path: Path) -> bool:
    """Draw `text` onto a transparent WxH PNG. Returns False if nothing drawn."""
    if not PILLOW_AVAILABLE:
        raise ToolError(
            "Pillow is required to render captions with this ffmpeg build "
            "(no drawtext filter). Install it with: pip3 install Pillow"
        )
    text = text.strip()
    if not text:
        return False

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype(style.font_path, style.font_size)
    except OSError as exc:
        raise ToolError(f"could not load font {style.font_path}: {exc}") from exc

    margin = int(width * style.margin_ratio)
    max_text_width = width - 2 * margin - 2 * style.box_padding

    lines = _wrap(text, font, max_text_width, draw)
    if not lines:
        return False

    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * style.line_spacing)
    block_width = int(max(draw.textlength(line, font=font) for line in lines))
    block_height = line_height * len(lines)

    if style.position == "top":
        block_top = int(height * 0.12)
    elif style.position == "bottom":
        block_top = int(height * 0.80) - block_height
    else:
        block_top = (height - block_height) // 2
    block_top = max(margin, min(block_top, height - block_height - margin))
    block_left = (width - block_width) // 2

    if style.box:
        pad = style.box_padding
        alpha = max(0, min(255, int(style.box_opacity * 255)))
        draw.rounded_rectangle(
            [
                block_left - pad,
                block_top - int(pad * 0.6),
                block_left + block_width + pad,
                block_top + block_height + int(pad * 0.6),
            ],
            radius=style.box_radius,
            fill=(0, 0, 0, alpha),
        )

    y = block_top
    for line in lines:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(*style.color, 255),
            stroke_width=style.stroke_width,
            stroke_fill=(*style.stroke_color, 255),
        )
        y += line_height

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return True
