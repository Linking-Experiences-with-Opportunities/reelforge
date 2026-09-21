"""Classify sampled frames as live footage vs. a graphic (diagram / screenshot / photo).

Everything here is heuristic and deliberately dependency-light: Pillow only, no
OpenCV, no model weights. The signals that separate a diagram or app screenshot
from camera footage are mostly about *flatness* --- graphics have large regions
of constant color and a small dominant palette, while real footage has texture
almost everywhere. The classifier reports a confidence alongside every verdict
so a wrong call is visible in the recipe and easy to override by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # pragma: no cover - import guard
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    PILLOW_AVAILABLE = False


ANALYSIS_WIDTH = 480

# Tuning constants. Named so a bad classification can be traced to one number.
FLAT_EDGE_THRESHOLD = 14      # gradient below this counts as "flat"
FLAT_RATIO_GRAPHIC = 0.55     # flat share above which a frame looks like a graphic
PALETTE_DOMINANCE_GRAPHIC = 0.45  # top-8 colors covering this much = flat palette
RULED_LINE_RATIO = 0.06       # share of near-uniform rows/cols (tables, charts, UI)
BORDER_UNIFORM_TOLERANCE = 12 # per-channel spread allowed in a "uniform" border

# A "title card" is text over a solid background. It scores like a graphic on
# every flatness metric, but it needs no asset from the user - the renderer
# reproduces it with a caption. These bounds separate it from a real diagram.
TITLE_CARD_MAX_REGIONS = 2
TITLE_CARD_MIN_FLAT = 0.80
TITLE_CARD_MAX_WORDS = 8

# Kinds that require the user to supply an image; everything else is either
# footage or something reelforge can draw itself.
ASSET_KINDS = {"diagram", "screenshot", "photo"}


@dataclass
class FrameVerdict:
    time: float
    kind: str                  # footage | diagram | screenshot | photo
    confidence: float
    placement: str = "full"    # full | card
    bbox: list[int] | None = None
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "time": round(self.time, 2),
            "kind": self.kind,
            "confidence": round(self.confidence, 2),
            "placement": self.placement,
            "bbox": self.bbox,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
        }


def _metrics(path: Path) -> dict:
    image = Image.open(path).convert("RGB")
    image.thumbnail((ANALYSIS_WIDTH, ANALYSIS_WIDTH))
    width, height = image.size
    total = width * height
    if total == 0:
        return {}

    gray = image.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    flat_pixels = sum(histogram[:FLAT_EDGE_THRESHOLD])
    strong_pixels = sum(histogram[60:])

    # Dominant-palette share: quantize hard, then see how much of the frame the
    # top few buckets cover. Graphics concentrate; photographs spread out.
    quantized = image.quantize(colors=64, method=Image.Quantize.FASTOCTREE)
    counts = sorted(quantized.histogram()[:64], reverse=True)
    palette_dominance = sum(counts[:8]) / total if total else 0.0
    # How many genuinely distinct color regions the frame contains. A title card
    # over a solid background has one or two; a flowchart or chart has several.
    color_regions = sum(1 for c in counts if c / total >= 0.04)

    saturation = ImageStat.Stat(image.convert("HSV")).mean[1] / 255.0

    luma = gray.histogram()
    near_white = sum(luma[235:]) / total
    near_black = sum(luma[:20]) / total

    ruled = _ruled_line_ratio(gray)

    return {
        "flat_ratio": flat_pixels / total,
        "strong_edge_ratio": strong_pixels / total,
        "palette_dominance": palette_dominance,
        "color_regions": float(color_regions),
        "saturation": saturation,
        "near_white": near_white,
        "near_black": near_black,
        "ruled_ratio": ruled,
    }


def _ruled_line_ratio(gray: "Image.Image") -> float:
    """Share of rows/columns that are near-uniform - tables, charts, UI chrome."""
    width, height = gray.size
    if width < 8 or height < 8:
        return 0.0
    pixels = gray.load()

    uniform = 0
    step_y = max(1, height // 80)
    for y in range(0, height, step_y):
        row_min, row_max = 255, 0
        for x in range(0, width, max(1, width // 60)):
            value = pixels[x, y]
            row_min = min(row_min, value)
            row_max = max(row_max, value)
        if row_max - row_min < 10:
            uniform += 1
    rows_checked = len(range(0, height, step_y)) or 1

    step_x = max(1, width // 80)
    uniform_cols = 0
    for x in range(0, width, step_x):
        col_min, col_max = 255, 0
        for y in range(0, height, max(1, height // 60)):
            value = pixels[x, y]
            col_min = min(col_min, value)
            col_max = max(col_max, value)
        if col_max - col_min < 10:
            uniform_cols += 1
    cols_checked = len(range(0, width, step_x)) or 1

    return (uniform / rows_checked + uniform_cols / cols_checked) / 2


def _placement(path: Path) -> tuple[str, list[int] | None]:
    """Is the graphic full-bleed, or a card sitting on a plain background?"""
    image = Image.open(path).convert("RGB")
    image.thumbnail((ANALYSIS_WIDTH, ANALYSIS_WIDTH))
    width, height = image.size
    if width < 20 or height < 20:
        return "full", None

    margin_x = max(2, int(width * 0.04))
    margin_y = max(2, int(height * 0.04))
    border_samples = []
    pixels = image.load()
    for x in range(0, width, max(1, width // 40)):
        border_samples.append(pixels[x, margin_y])
        border_samples.append(pixels[x, height - margin_y - 1])
    for y in range(0, height, max(1, height // 40)):
        border_samples.append(pixels[margin_x, y])
        border_samples.append(pixels[width - margin_x - 1, y])

    if not border_samples:
        return "full", None

    channels = list(zip(*border_samples))
    spread = max(max(c) - min(c) for c in channels)
    if spread > BORDER_UNIFORM_TOLERANCE:
        return "full", None

    background = tuple(sum(c) // len(c) for c in channels)
    flat = Image.new("RGB", image.size, background)
    diff = ImageChops.difference(image, flat).convert("L")
    mask = diff.point(lambda v: 255 if v > 24 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return "full", None

    covered = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / (width * height)
    if covered > 0.92:
        return "full", None

    # Scale the bbox back to a 0-1000 coordinate space so it survives resizing.
    scaled = [
        int(bbox[0] / width * 1000), int(bbox[1] / height * 1000),
        int(bbox[2] / width * 1000), int(bbox[3] / height * 1000),
    ]
    return "card", scaled


def classify_frame(path: Path, ocr_text: str = "") -> FrameVerdict:
    """Decide what a single sampled frame is showing."""
    if not PILLOW_AVAILABLE:
        return FrameVerdict(0.0, "footage", 0.0)

    metrics = _metrics(path)
    if not metrics:
        return FrameVerdict(0.0, "footage", 0.0)

    flat = metrics["flat_ratio"]
    dominance = metrics["palette_dominance"]
    ruled = metrics["ruled_ratio"]
    saturation = metrics["saturation"]
    word_count = len(ocr_text.split())

    # Graphic score: flat areas and a concentrated palette are the strong
    # signals; ruled lines and dense text reinforce them; heavy saturation and
    # texture pull back toward footage.
    score = 0.0
    score += 1.6 * max(0.0, flat - FLAT_RATIO_GRAPHIC) / (1 - FLAT_RATIO_GRAPHIC)
    score += 1.2 * max(0.0, dominance - PALETTE_DOMINANCE_GRAPHIC) / (1 - PALETTE_DOMINANCE_GRAPHIC)
    score += 0.8 * min(1.0, ruled / max(RULED_LINE_RATIO, 1e-6)) if ruled > RULED_LINE_RATIO else 0.0
    score += 0.5 * min(1.0, word_count / 12.0)
    score -= 0.6 * max(0.0, metrics["strong_edge_ratio"] - 0.10) / 0.30

    confidence = max(0.0, min(1.0, score / 2.2))

    if confidence < 0.45:
        return FrameVerdict(0.0, "footage", 1.0 - confidence, metrics=metrics)

    placement, bbox = _placement(path)
    regions = metrics["color_regions"]

    # A title card - a few words over a solid background - is *not* an asset the
    # user has to supply: reelforge reproduces it from the caption system. It
    # looks like a graphic on every flatness metric, so it is separated here by
    # having almost no internal structure: one or two color regions, no ruled
    # lines, and a short line of text.
    # Note: ruled_ratio is deliberately *not* part of this test - a solid
    # background makes most rows uniform, so a title card scores as high on it
    # as a real table does.
    if (regions <= TITLE_CARD_MAX_REGIONS
            and flat >= TITLE_CARD_MIN_FLAT
            and word_count <= TITLE_CARD_MAX_WORDS):
        return FrameVerdict(0.0, "title_card", confidence, "full", None, metrics)

    # Screenshot vs diagram vs photo, in rough order of how much text they carry.
    if word_count >= 10 and metrics["near_white"] > 0.12:
        kind = "screenshot"
    elif ruled > RULED_LINE_RATIO or regions >= 3 or word_count >= 3:
        kind = "diagram"
    elif saturation > 0.35 and flat < 0.75:
        kind = "photo"
    else:
        kind = "diagram"

    return FrameVerdict(0.0, kind, confidence, placement, bbox, metrics)


def classify_frames(frames: list[tuple[float, Path]],
                    ocr_by_time: dict[float, str] | None = None) -> list[FrameVerdict]:
    ocr_by_time = ocr_by_time or {}
    verdicts = []
    for time, path in frames:
        verdict = classify_frame(path, ocr_by_time.get(time, ""))
        verdict.time = time
        verdicts.append(verdict)
    return verdicts


def summarize_segment(verdicts: list[FrameVerdict], start: float,
                      end: float) -> FrameVerdict | None:
    """Majority verdict across the frames falling inside one segment."""
    inside = [v for v in verdicts if start - 1e-6 <= v.time < end]
    if not inside:
        # A short segment may fall between sampled frames; use the nearest one.
        if not verdicts:
            return None
        midpoint = (start + end) / 2
        inside = [min(verdicts, key=lambda v: abs(v.time - midpoint))]

    assets = [v for v in inside if v.kind in ASSET_KINDS]
    if len(assets) * 2 >= len(inside):
        # Majority of the segment shows a real graphic; the highest-confidence
        # frame stands in for the whole span.
        best = max(assets, key=lambda v: v.confidence)
        return FrameVerdict(start, best.kind, best.confidence, best.placement,
                            best.bbox, best.metrics)

    # Otherwise it is footage or a title card, whichever dominates.
    remainder = [v for v in inside if v.kind not in ASSET_KINDS]
    cards = [v for v in remainder if v.kind == "title_card"]
    if cards and len(cards) * 2 >= len(remainder):
        best = max(cards, key=lambda v: v.confidence)
        return FrameVerdict(start, "title_card", best.confidence, "full", None,
                            best.metrics)

    best = max(inside, key=lambda v: v.confidence)
    return FrameVerdict(start, "footage", best.confidence)
