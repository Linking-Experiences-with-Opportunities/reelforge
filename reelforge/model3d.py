"""Prop models: real objects described once, in millimetres, and checked.

A *prop spec* is a small JSON file that says what an object is made of: boxes,
cylinders and spheres, each with a position, a size, a rotation and a colour,
all in millimetres with the origin on the surface the object stands on. The
measurements come from the real thing (a spec sheet or a ruler), so every prop
built this way is at the same scale as every other one.

This module does four things with a spec:

* `check`   - proves it is sound: the parts add up to the size it claims, it
              rests on the ground, and nothing floats free of the rest.
* `sheet`   - draws a turnaround (front, side, top, three-quarter) beside the
              reference photos, so the model can be compared with the object.
* `lineup`  - draws several props side by side at one scale with a ruler.
* `export`  - writes the parts out for a renderer: a TypeScript module, or OBJ.

Axes: x to the right, y up, z toward the viewer. A part's rotation is given in
degrees and applied about x, then y, then z.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

try:  # pragma: no cover - import guard
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    PILLOW_AVAILABLE = False

try:  # pragma: no cover - import guard
    import numpy as np
except ImportError:  # pragma: no cover - import guard
    np = None

from .shell import ToolError

SHAPES = ("box", "cylinder", "sphere")
FINISHES = ("matte", "satin", "gloss", "fabric", "metal", "glow")

Vec = tuple[float, float, float]


@dataclass
class Part:
    name: str
    shape: str
    at: Vec
    size: Vec
    rot: Vec = (0.0, 0.0, 0.0)
    color: str = "#888888"
    finish: str = "satin"
    paint: str | None = None


@dataclass
class Prop:
    name: str
    title: str
    size_mm: Vec
    parts: list[Part]
    source: str = ""
    refs: list[Path] = field(default_factory=list)
    path: Path | None = None


@dataclass
class Issue:
    level: str  # "error" or "warning"
    message: str


# --- loading ----------------------------------------------------------------

def _vec(value, what: str) -> Vec:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ToolError(f"{what} must be three numbers, got {value!r}")
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be three numbers, got {value!r}") from None


def parse_color(text: str) -> tuple[int, int, int]:
    raw = text.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raise ToolError(f"colour must look like #rrggbb, got {text!r}")
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        raise ToolError(f"colour must look like #rrggbb, got {text!r}") from None


def _expand(raw: dict, index: int) -> list[Part]:
    """One spec entry becomes one part, or a grid of them (`grid`: count and step along x and z)."""
    name = str(raw.get("name") or f"part-{index + 1}")
    shape = raw.get("shape")
    if shape not in SHAPES:
        raise ToolError(f"{name}: shape must be one of {', '.join(SHAPES)}, got {shape!r}")
    finish = raw.get("finish", "satin")
    if finish not in FINISHES:
        raise ToolError(f"{name}: finish must be one of {', '.join(FINISHES)}, got {finish!r}")
    color = str(raw.get("color", "#888888"))
    parse_color(color)
    at = _vec(raw.get("at"), f"{name}: at")
    size = _vec(raw.get("size"), f"{name}: size")
    if min(size) <= 0:
        raise ToolError(f"{name}: every size must be above zero, got {list(size)}")
    rot = _vec(raw.get("rot", [0, 0, 0]), f"{name}: rot")
    base = dict(shape=shape, size=size, rot=rot, color=color, finish=finish, paint=raw.get("paint"))

    grid = raw.get("grid")
    if not grid:
        return [Part(name=name, at=at, **base)]
    cols, rows = (int(n) for n in grid.get("count", [1, 1]))
    step_x, step_z = (float(n) for n in grid.get("step", [0, 0]))
    skip = {tuple(cell) for cell in grid.get("skip", [])}
    out = []
    for row in range(rows):
        for col in range(cols):
            if (col, row) in skip:
                continue
            spot = (at[0] + col * step_x, at[1], at[2] + row * step_z)
            out.append(Part(name=f"{name}-{col}-{row}", at=spot, **base))
    return out


def load_prop(path: Path) -> Prop:
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ToolError(f"no prop spec at {path}") from None
    except json.JSONDecodeError as err:
        raise ToolError(f"{path.name} is not valid JSON: {err}") from None
    return prop_from_dict(data, path)


def prop_from_dict(data: dict, path: Path | None = None) -> Prop:
    name = str(data.get("name") or (path.stem if path else "prop"))
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ToolError(f"{name}: a prop needs a non-empty 'parts' list")
    parts: list[Part] = []
    for index, raw in enumerate(raw_parts):
        parts.extend(_expand(raw, index))
    refs = [((path.parent / ref) if path else Path(ref)) for ref in data.get("refs", [])]
    return Prop(
        name=name,
        title=str(data.get("title") or name),
        size_mm=_vec(data.get("size_mm"), f"{name}: size_mm"),
        parts=parts,
        source=str(data.get("source", "")),
        refs=refs,
        path=path,
    )


# --- geometry ---------------------------------------------------------------

def rotation(rot_deg: Vec) -> list[list[float]]:
    """Rz * Ry * Rx as a 3x3 matrix (rows)."""
    ax, ay, az = (math.radians(a) for a in rot_deg)
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]


def _apply(matrix: list[list[float]], p: Vec) -> Vec:
    return (
        matrix[0][0] * p[0] + matrix[0][1] * p[1] + matrix[0][2] * p[2],
        matrix[1][0] * p[0] + matrix[1][1] * p[1] + matrix[1][2] * p[2],
        matrix[2][0] * p[0] + matrix[2][1] * p[1] + matrix[2][2] * p[2],
    )


def _unit_triangles(shape: str) -> list[tuple[Vec, Vec, Vec]]:
    """The shape inside a 1 x 1 x 1 cube centred on the origin, as triangles wound outward."""
    tris: list[tuple[Vec, Vec, Vec]] = []
    if shape == "box":
        h = 0.5
        quads = [
            [(-h, -h, h), (h, -h, h), (h, h, h), (-h, h, h)],
            [(h, -h, -h), (-h, -h, -h), (-h, h, -h), (h, h, -h)],
            [(h, -h, h), (h, -h, -h), (h, h, -h), (h, h, h)],
            [(-h, -h, -h), (-h, -h, h), (-h, h, h), (-h, h, -h)],
            [(-h, h, h), (h, h, h), (h, h, -h), (-h, h, -h)],
            [(-h, -h, -h), (h, -h, -h), (h, -h, h), (-h, -h, h)],
        ]
        for a, b, c, d in quads:
            tris += [(a, b, c), (a, c, d)]
    elif shape == "cylinder":
        seg = 28
        ring = [(math.sin(2 * math.pi * i / seg) * 0.5, math.cos(2 * math.pi * i / seg) * 0.5) for i in range(seg)]
        for i in range(seg):
            (x0, z0), (x1, z1) = ring[i], ring[(i + 1) % seg]
            tris += [((x0, -0.5, z0), (x1, -0.5, z1), (x1, 0.5, z1)), ((x0, -0.5, z0), (x1, 0.5, z1), (x0, 0.5, z0))]
            tris.append(((0, 0.5, 0), (x0, 0.5, z0), (x1, 0.5, z1)))
            tris.append(((0, -0.5, 0), (x1, -0.5, z1), (x0, -0.5, z0)))
    else:  # sphere
        stacks, slices = 12, 20

        def at(i: int, j: int) -> Vec:
            phi = math.pi * i / stacks
            th = 2 * math.pi * j / slices
            return (math.sin(phi) * math.sin(th) * 0.5, math.cos(phi) * 0.5, math.sin(phi) * math.cos(th) * 0.5)

        for i in range(stacks):
            for j in range(slices):
                a, b, c, d = at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)
                if i > 0:
                    tris.append((a, b, d))
                if i < stacks - 1:
                    tris.append((b, c, d))
    return tris


_UNIT = {shape: _unit_triangles(shape) for shape in SHAPES}


def part_triangles(part: Part) -> list[tuple[Vec, Vec, Vec]]:
    matrix = rotation(part.rot)

    def place(p: Vec) -> Vec:
        q = _apply(matrix, (p[0] * part.size[0], p[1] * part.size[1], p[2] * part.size[2]))
        return (q[0] + part.at[0], q[1] + part.at[1], q[2] + part.at[2])

    return [(place(a), place(b), place(c)) for a, b, c in _UNIT[part.shape]]


def bounds(triangles) -> tuple[Vec, Vec]:
    xs = [p[0] for tri in triangles for p in tri]
    ys = [p[1] for tri in triangles for p in tri]
    zs = [p[2] for tri in triangles for p in tri]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def prop_bounds(prop: Prop) -> tuple[Vec, Vec]:
    return bounds([tri for part in prop.parts for tri in part_triangles(part)])


# --- checks -----------------------------------------------------------------

def check_prop(prop: Prop) -> list[Issue]:
    """Everything wrong with a prop. Errors mean the model is not the object it claims to be."""
    issues: list[Issue] = []
    boxes = [bounds(part_triangles(part)) for part in prop.parts]
    lo = tuple(min(box[0][axis] for box in boxes) for axis in range(3))
    hi = tuple(max(box[1][axis] for box in boxes) for axis in range(3))

    # 1. The parts must add up to the size the spec states (the measured size of the real object).
    for axis, label in enumerate(("width", "height", "depth")):
        got = hi[axis] - lo[axis]
        want = prop.size_mm[axis]
        tolerance = max(1.5, want * 0.02)
        if abs(got - want) > tolerance:
            issues.append(Issue("error", f"{label} is {got:.1f} mm but the spec says {want:g} mm"))

    # 2. It stands on the ground, centred, so it can be placed by its footprint.
    if abs(lo[1]) > 0.5:
        issues.append(Issue("error", f"lowest point is at y = {lo[1]:.1f} mm; it should rest on y = 0"))
    for axis, label in ((0, "x"), (2, "z")):
        centre = (lo[axis] + hi[axis]) / 2
        if abs(centre) > max(1.0, prop.size_mm[axis] * 0.02):
            issues.append(Issue("warning", f"footprint is off centre in {label} by {centre:.1f} mm"))

    # 3. Nothing floats: every part touches a chain of parts that reaches the ground.
    gap = 0.6
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if all(a[0][k] - gap <= b[1][k] and b[0][k] - gap <= a[1][k] for k in range(3)):
                parent[find(i)] = find(j)
    grounded = {find(i) for i, box in enumerate(boxes) if box[0][1] <= gap}
    floating = [prop.parts[i].name for i in range(len(boxes)) if find(i) not in grounded]
    if floating:
        shown = ", ".join(floating[:5]) + (f" and {len(floating) - 5} more" if len(floating) > 5 else "")
        issues.append(Issue("error", f"floating parts (touching nothing that reaches the ground): {shown}"))

    if not prop.source:
        issues.append(Issue("warning", "no 'source' given for the measurements"))
    for ref in prop.refs:
        if not ref.exists():
            issues.append(Issue("warning", f"reference photo not found: {ref}"))
    return issues


# --- drawing ----------------------------------------------------------------

VIEWS = {
    # name: (yaw, pitch) in degrees; the camera looks down -z after the turn
    "front": (0.0, 0.0),
    "side": (-90.0, 0.0),
    "top": (0.0, 90.0),
    "three-quarter": (-35.0, 28.0),
}
LIGHT = (0.35, 0.8, 0.5)
PAPER = (244, 241, 233)
INK = (40, 34, 28)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _require_pillow() -> None:
    if not PILLOW_AVAILABLE:
        raise ToolError("drawing props needs Pillow: pip3 install Pillow")


def _view_matrix(view: str) -> list[list[float]]:
    yaw, pitch = VIEWS[view]
    turn = rotation((0.0, yaw, 0.0))
    tilt = rotation((pitch, 0.0, 0.0))
    return [[sum(tilt[r][k] * turn[k][c] for k in range(3)) for c in range(3)] for r in range(3)]


def _shaded(prop: Prop, view: str):
    """Triangles in view space with their flat-shaded colour, far to near."""
    matrix = _view_matrix(view)
    light_len = math.sqrt(sum(c * c for c in LIGHT))
    light = _apply(matrix, tuple(c / light_len for c in LIGHT))
    out = []
    for part in prop.parts:
        base = parse_color(part.color)
        for tri in part_triangles(part):
            a, b, c = (_apply(matrix, p) for p in tri)
            u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
            if length == 0 or n[2] <= 0:  # degenerate, or facing away from the camera
                continue
            if part.finish == "glow":
                shade = 1.0
            else:
                lambert = max(0.0, (n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / length)
                shade = 0.55 + 0.45 * lambert
            color = tuple(min(255, round(ch * shade)) for ch in base)
            out.append(((a[2] + b[2] + c[2]) / 3, (a, b, c), color))
    out.sort(key=lambda item: item[0])
    return out


def _depth_buffered(shaded, to_px, width: int, height: int) -> "Image.Image":
    """Per-pixel depth test, so parts that pass through or sit inside one another draw exactly."""
    color = np.zeros((height, width, 4), dtype=np.uint8)
    depth = np.full((height, width), -np.inf)
    for _, tri, rgb in shaded:
        (x0, y0), (x1, y1), (x2, y2) = (to_px(p) for p in tri)
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue
        left, right = max(0, math.floor(min(x0, x1, x2))), min(width - 1, math.ceil(max(x0, x1, x2)))
        top, bottom = max(0, math.floor(min(y0, y1, y2))), min(height - 1, math.ceil(max(y0, y1, y2)))
        if left > right or top > bottom:
            continue
        xs, ys = np.meshgrid(np.arange(left, right + 1) + 0.5, np.arange(top, bottom + 1) + 0.5)
        w0 = ((x1 - xs) * (y2 - ys) - (x2 - xs) * (y1 - ys)) / area
        w1 = ((x2 - xs) * (y0 - ys) - (x0 - xs) * (y2 - ys)) / area
        w2 = 1.0 - w0 - w1
        z = w0 * tri[0][2] + w1 * tri[1][2] + w2 * tri[2][2]
        window = depth[top:bottom + 1, left:right + 1]
        hit = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (z > window)
        window[hit] = z[hit]
        color[top:bottom + 1, left:right + 1][hit] = rgb + (255,)
    return Image.fromarray(color, "RGBA")


def draw_view(prop: Prop, view: str, px_per_mm: float, pad: int = 16) -> "Image.Image":
    """One orthographic view of the prop, sized by the scale, on a transparent ground."""
    _require_pillow()
    shaded = _shaded(prop, view)
    xs = [p[0] for _, tri, _ in shaded for p in tri]
    ys = [p[1] for _, tri, _ in shaded for p in tri]
    over = 3  # supersample, then shrink, for clean edges
    width = max(1, math.ceil((max(xs) - min(xs)) * px_per_mm)) + pad * 2
    height = max(1, math.ceil((max(ys) - min(ys)) * px_per_mm)) + pad * 2
    left, top = min(xs), max(ys)

    def to_px(p: Vec) -> tuple[float, float]:
        return ((p[0] - left) * px_per_mm * over + pad * over, (top - p[1]) * px_per_mm * over + pad * over)

    if np is not None:
        image = _depth_buffered(shaded, to_px, width * over, height * over)
    else:  # no numpy: far-to-near painting, which can misorder parts that interpenetrate
        image = Image.new("RGBA", (width * over, height * over), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for _, tri, color in shaded:
            draw.polygon([to_px(p) for p in tri], fill=color + (255,))
    return image.resize((width, height), Image.LANCZOS)


def _ruler(draw, x: int, y: int, px_per_mm: float, font) -> None:
    length = next(mm for mm in (10, 50, 100, 500, 1000) if mm * px_per_mm >= 60 or mm == 1000)
    end = x + round(length * px_per_mm)
    draw.line([(x, y), (end, y)], fill=INK, width=3)
    for tick in (x, end):
        draw.line([(tick, y - 6), (tick, y + 6)], fill=INK, width=3)
    draw.text((x, y + 10), f"{length} mm", fill=INK, font=font)


def build_sheet(prop: Prop, out_path: Path, tile: int = 420) -> Path:
    """The turnaround: four views at one scale, the stated size, and the reference photos beneath."""
    _require_pillow()
    lo, hi = prop_bounds(prop)
    extent = max(hi[axis] - lo[axis] for axis in range(3)) * 1.25
    px_per_mm = (tile - 40) / extent
    views = [(name, draw_view(prop, name, px_per_mm)) for name in VIEWS]
    refs = []
    for ref in prop.refs:
        if ref.exists():
            with Image.open(ref) as photo:
                photo = photo.convert("RGB")
                photo.thumbnail((tile, tile))
                refs.append((ref.name, photo.copy()))

    title_font, label_font = _font(26), _font(15)
    head = 70
    columns = max(len(views), len(refs))
    width = columns * tile
    height = head + tile + 30 + ((tile + 30) if refs else 0)
    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)
    w, h, d = prop.size_mm
    draw.text((20, 14), prop.title, fill=INK, font=title_font)
    draw.text((20, 46), f"{w:g} x {h:g} x {d:g} mm (w x h x d)  -  {len(prop.parts)} parts  -  {prop.source}", fill=INK, font=label_font)
    for index, (name, image) in enumerate(views):
        x = index * tile + (tile - image.width) // 2
        y = head + (tile - image.height) // 2
        sheet.paste(image, (x, y), image)
        draw.text((index * tile + 14, head + tile + 4), name, fill=INK, font=label_font)
    _ruler(draw, width - 20 - round(100 * px_per_mm) - 60, 30, px_per_mm, label_font)
    for index, (name, photo) in enumerate(refs):
        x = index * tile + (tile - photo.width) // 2
        y = head + tile + 30 + (tile - photo.height) // 2
        sheet.paste(photo, (x, y))
        draw.text((index * tile + 14, height - 22), f"reference: {name}", fill=INK, font=label_font)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    return out_path


def build_lineup(props: list[Prop], out_path: Path, width: int = 2200, view: str = "three-quarter") -> Path:
    """Every prop on one ground line at one scale. If they look right together here, they are consistent."""
    _require_pillow()
    gap_mm = 60.0
    total = sum(prop.size_mm[0] * 1.3 for prop in props) + gap_mm * (len(props) + 1)
    px_per_mm = (width - 40) / total
    images = [draw_view(prop, view, px_per_mm, pad=4) for prop in props]
    tallest = max(image.height for image in images)
    label_font = _font(15)
    height = tallest + 150
    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)
    ground = tallest + 50
    draw.line([(20, ground), (width - 20, ground)], fill=(150, 140, 125), width=2)
    slots = [max(image.width, round(draw.textlength(prop.title, font=label_font)) + 12) for prop, image in zip(props, images)]
    spare = max(0, width - 40 - sum(slots)) // (len(props) + 1)
    x = 20 + spare
    for prop, image, slot in zip(props, images, slots):
        sheet.paste(image, (x + (slot - image.width) // 2, ground - image.height + 4), image)
        w, h, d = prop.size_mm
        draw.text((x, ground + 10), prop.title, fill=INK, font=label_font)
        draw.text((x, ground + 30), f"{w:g} x {h:g} x {d:g} mm", fill=(110, 100, 90), font=label_font)
        x += slot + spare
    _ruler(draw, 24, 26, px_per_mm, label_font)
    draw.text((24, height - 26), f"one scale for every prop: {px_per_mm:.3f} px per mm", fill=(110, 100, 90), font=label_font)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    return out_path


# --- export -----------------------------------------------------------------

def _round(vec: Vec) -> list[float]:
    return [round(v, 3) + 0.0 for v in vec]


def prop_payload(prop: Prop) -> dict:
    parts = []
    for part in prop.parts:
        entry = {"name": part.name, "shape": part.shape, "at": _round(part.at), "size": _round(part.size),
                 "rot": _round(part.rot), "color": part.color, "finish": part.finish}
        if part.paint:
            entry["paint"] = part.paint
        parts.append(entry)
    return {"title": prop.title, "size": _round(prop.size_mm), "parts": parts}


TS_HEADER = """/**
 * Generated by `reelforge model export --format ts`. Do not edit by hand: change the prop specs and export again.
 * Every measurement is in millimetres. Origin: the centre of the footprint, on the surface the prop stands on.
 * Axes: x right, y up, z toward the viewer. `rot` is degrees about x, then y, then z.
 */
export type PropShape = 'box' | 'cylinder' | 'sphere';
export type PropFinish = 'matte' | 'satin' | 'gloss' | 'fabric' | 'metal' | 'glow';
export type PropPart = {
  name: string;
  shape: PropShape;
  at: number[];
  size: number[];
  rot: number[];
  color: string;
  finish: PropFinish;
  /** The name of a painted surface the renderer supplies for this part. */
  paint?: string;
};
export type PropModel = { title: string; size: number[]; parts: PropPart[] };

"""


def export_ts(props: list[Prop], out_path: Path, const: str = "PROPS") -> Path:
    body = {prop.name: prop_payload(prop) for prop in props}
    names = " | ".join(f"'{prop.name}'" for prop in props)
    text = (
        TS_HEADER
        + f"export type PropName = {names};\n\n"
        + f"export const {const}: Record<PropName, PropModel> = "
        + json.dumps(body, indent=2)
        + ";\n"
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return out_path


def export_obj(prop: Prop, out_path: Path) -> Path:
    """Wavefront OBJ (millimetres) with one material per colour, plus its .mtl beside it."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {prop.title} - reelforge model export, millimetres", f"mtllib {out_path.stem}.mtl"]
    materials: dict[str, str] = {}
    count = 0
    for part in prop.parts:
        material = materials.setdefault(part.color.lower(), f"c{len(materials)}")
        lines += [f"o {part.name}", f"usemtl {material}"]
        for tri in part_triangles(part):
            for p in tri:
                lines.append(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}")
            lines.append(f"f {count + 1} {count + 2} {count + 3}")
            count += 3
    out_path.write_text("\n".join(lines) + "\n")
    mtl = []
    for color, material in materials.items():
        r, g, b = (ch / 255 for ch in parse_color(color))
        mtl += [f"newmtl {material}", f"Kd {r:.4f} {g:.4f} {b:.4f}", ""]
    out_path.with_suffix(".mtl").write_text("\n".join(mtl))
    return out_path
