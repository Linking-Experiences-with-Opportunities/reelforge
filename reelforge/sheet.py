"""Contact sheet: every sampled frame tiled into one image.

This is the artifact you scan to understand a reel at a glance - the shot
order, where the graphics land, how long the piece dwells on each beat. It
doubles as the review surface for diagram detection: with `annotate=True`
each tile is labelled with its timestamp and, for slots the classifier flagged
as an image/diagram, the slot number you will supply with `--assets`.
"""

from __future__ import annotations

from pathlib import Path

try:  # pragma: no cover - import guard
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    PILLOW_AVAILABLE = False

from .shell import ToolError

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

ACCENT = {
    "diagram": (255, 196, 0),
    "screenshot": (74, 158, 255),
    "photo": (61, 214, 132),
    # Not an asset slot - reproduced from the caption system - so it is marked
    # in a muted color and never carries a slot number.
    "title_card": (150, 155, 168),
}

BADGE_LABEL = {
    "diagram": "DIAG",
    "screenshot": "SHOT",
    "photo": "PHOTO",
    "title_card": "TEXT",
}


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _tile_size(frame: Path, columns: int, sheet_width: int) -> tuple[int, int]:
    with Image.open(frame) as probe:
        width, height = probe.size
    tile_width = max(1, sheet_width // columns)
    tile_height = max(1, round(tile_width * height / width))
    return tile_width, tile_height


def build_contact_sheet(
    frames: list[tuple[float, Path]],
    out_path: Path,
    *,
    columns: int = 5,
    sheet_width: int = 1500,
    annotate: bool = False,
    slot_by_time: dict[float, int] | None = None,
    kind_by_time: dict[float, str] | None = None,
    gap: int = 0,
) -> Path:
    """Tile `frames` into a grid image and write it to `out_path`."""
    if not PILLOW_AVAILABLE:
        raise ToolError("Pillow is required to build a contact sheet (pip3 install Pillow)")
    if not frames:
        raise ToolError("no sampled frames available - run 'reelforge ingest' first")
    if columns < 1:
        raise ToolError("--columns must be at least 1")

    slot_by_time = slot_by_time or {}
    kind_by_time = kind_by_time or {}

    tile_w, tile_h = _tile_size(frames[0][1], columns, sheet_width)
    rows = (len(frames) + columns - 1) // columns

    sheet = Image.new(
        "RGB",
        (columns * tile_w + (columns - 1) * gap, rows * tile_h + (rows - 1) * gap),
        (10, 10, 12),
    )

    label_font = _font(max(13, tile_w // 18))
    badge_font = _font(max(15, tile_w // 14))

    for index, (time, path) in enumerate(frames):
        column = index % columns
        row = index // columns
        x = column * (tile_w + gap)
        y = row * (tile_h + gap)

        try:
            with Image.open(path) as source:
                tile = source.convert("RGB").resize((tile_w, tile_h), Image.LANCZOS)
        except OSError:
            continue

        if annotate:
            tile = _annotate(
                tile, time, slot_by_time.get(time), kind_by_time.get(time),
                label_font, badge_font,
            )
        sheet.paste(tile, (x, y))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in {".jpg", ".jpeg"}:
        sheet.save(out_path, quality=88, optimize=True)
    else:
        sheet.save(out_path)
    return out_path


def _annotate(tile: "Image.Image", time: float, slot: int | None, kind: str | None,
              label_font, badge_font) -> "Image.Image":
    draw = ImageDraw.Draw(tile, "RGBA")
    width, height = tile.size

    # Timestamp, bottom-left, on a scrim so it survives any background.
    label = f"{time:6.2f}s".strip()
    text_width = draw.textlength(label, font=label_font)
    pad = max(4, width // 60)
    draw.rectangle(
        [pad, height - pad - label_font.size - 2 * pad,
         pad + text_width + 2 * pad, height - pad],
        fill=(0, 0, 0, 165),
    )
    draw.text((pad * 2, height - pad - label_font.size - pad), label,
              font=label_font, fill=(255, 255, 255))

    if kind and kind in ACCENT:
        accent = ACCENT[kind]
        border = max(3, width // 90) if slot else max(2, width // 150)
        draw.rectangle([0, 0, width - 1, height - 1], outline=accent, width=border)
        badge = f"#{slot}" if slot else BADGE_LABEL.get(kind, kind[:4].upper())
        badge_width = draw.textlength(badge, font=badge_font)
        draw.rectangle(
            [pad, pad, pad + badge_width + 2 * pad, pad + badge_font.size + 2 * pad],
            fill=(*accent, 235),
        )
        draw.text((pad * 2, pad + pad // 2), badge, font=badge_font, fill=(12, 12, 14))

    return tile


def expand_maps(recipe: dict, frames: list[tuple[float, Path]]
                ) -> tuple[dict[float, int], dict[float, str]]:
    """Per-frame slot/kind maps, resolved by which segment each frame falls in.

    A segment covers a span of time, so every frame inside that span inherits
    the segment's verdict - that is what makes the annotated sheet line up with
    the `--assets` ordering.
    """
    slot_number = {
        slot["segment"]: position
        for position, slot in enumerate((recipe.get("assets") or {}).get("slots") or [],
                                        start=1)
    }
    slot_by_time: dict[float, int] = {}
    kind_by_time: dict[float, str] = {}

    for time, _ in frames:
        for segment in recipe.get("segments", []):
            if float(segment["start"]) - 1e-6 <= time < float(segment["end"]):
                kind_by_time[time] = (segment.get("visual") or {}).get("kind", "footage")
                if segment.get("fill") == "asset" and segment["index"] in slot_number:
                    slot_by_time[time] = slot_number[segment["index"]]
                break
    return slot_by_time, kind_by_time


def frames_from_project(project_dir: Path, frame_step: float = 0.5
                        ) -> list[tuple[float, Path]]:
    """Rebuild the (timestamp, path) list from a project's saved frames."""
    frames_dir = Path(project_dir) / "frames"
    if not frames_dir.is_dir():
        raise ToolError(
            f"no sampled frames in {frames_dir}. Re-run 'reelforge ingest' "
            f"without --no-ocr/--no-visuals."
        )
    files = sorted(frames_dir.glob("frame-*.jpg"))
    if not files:
        raise ToolError(f"no frames found in {frames_dir}")
    return [(index * frame_step, path) for index, path in enumerate(files)]
