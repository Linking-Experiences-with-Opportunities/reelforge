#!/usr/bin/env python3
"""Build a synthetic vertical 'reel' fixture for exercising the pipeline.

Deliberately mixes scene types so the diagram/image detector has something
real to separate: textured pseudo-footage, a flat diagram, a light UI
screenshot, and a caption card.
"""

from __future__ import annotations

import math
import random
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FPS = 30
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def font(size: int):
    return ImageFont.truetype(FONT, size)


def centered(draw, text, y, f, fill=(255, 255, 255)):
    width = draw.textlength(text, font=f)
    draw.text(((W - width) / 2, y), text, font=f, fill=fill)


def scene_footage(seed: int, tint: tuple[int, int, int]) -> Image.Image:
    """Noisy, textured, saturated - should read as footage, not a graphic."""
    random.seed(seed)
    image = Image.new("RGB", (W, H), tint)
    pixels = image.load()
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            jitter = random.randint(-55, 55)
            wave = int(40 * math.sin((x + y + seed * 40) / 70.0))
            value = (
                max(0, min(255, tint[0] + jitter + wave)),
                max(0, min(255, tint[1] + jitter - wave)),
                max(0, min(255, tint[2] + jitter + wave // 2)),
            )
            for dy in range(2):
                for dx in range(2):
                    if x + dx < W and y + dy < H:
                        pixels[x + dx, y + dy] = value
    return image


def scene_diagram() -> Image.Image:
    """Flat background, boxes, arrows, few colors - a classic diagram."""
    image = Image.new("RGB", (W, H), (248, 249, 251))
    draw = ImageDraw.Draw(image)
    centered(draw, "HOW IT WORKS", 320, font(78), (24, 28, 38))

    boxes = [
        (140, 620, 940, 860, (66, 133, 244), "INGEST"),
        (140, 980, 940, 1220, (52, 168, 83), "ANALYZE"),
        (140, 1340, 940, 1580, (234, 67, 53), "RENDER"),
    ]
    label = font(62)
    for x0, y0, x1, y1, color, text in boxes:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=color)
        width = draw.textlength(text, font=label)
        draw.text((x0 + (x1 - x0 - width) / 2, y0 + 82), text, font=label, fill="white")

    for y in (900, 1260):
        draw.line([(540, y), (540, y + 40)], fill=(90, 98, 112), width=9)
        draw.polygon([(540, y + 70), (512, y + 30), (568, y + 30)], fill=(90, 98, 112))
    return image


def scene_screenshot() -> Image.Image:
    """Light UI chrome with dense text - should read as a screenshot."""
    image = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, W, 210], fill=(242, 243, 246))
    draw.rounded_rectangle([60, 70, W - 60, 160], radius=18, fill=(255, 255, 255),
                           outline=(206, 210, 218), width=3)
    draw.text((100, 96), "dashboard.internal/metrics", font=font(44), fill=(90, 98, 112))

    rows = [
        "Daily active users        14,208",
        "Median session            6m 42s",
        "Retention (D7)            38.4 %",
        "Crash free sessions       99.81 %",
        "Net new signups           1,043",
        "Churned accounts          212",
    ]
    body = font(46)
    y = 420
    for index, row in enumerate(rows):
        if index % 2 == 0:
            draw.rectangle([60, y - 18, W - 60, y + 66], fill=(248, 249, 251))
        draw.text((100, y), row, font=body, fill=(32, 37, 48))
        draw.line([(60, y + 76), (W - 60, y + 76)], fill=(226, 230, 238), width=2)
        y += 120
    return image


def scene_card(text: str, bg: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(image)
    centered(draw, text, H // 2 - 60, font(96))
    return image


def encode(image: Image.Image, seconds: float, out: Path, tmp: Path) -> None:
    still = tmp / f"{out.stem}.png"
    image.save(still)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", str(FPS), "-t", f"{seconds}", "-i", str(still),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS), str(out),
    ], check=True)


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tmp"
    tmp.mkdir(exist_ok=True)

    scenes = [
        (scene_card("STOP SCROLLING", (18, 24, 42)), 2.0),
        (scene_footage(1, (150, 90, 70)), 2.0),
        (scene_diagram(), 3.0),
        (scene_footage(2, (60, 110, 150)), 2.0),
        (scene_screenshot(), 2.5),
        (scene_card("FOLLOW FOR MORE", (120, 40, 70)), 2.0),
    ]

    parts = []
    for index, (image, seconds) in enumerate(scenes, start=1):
        part = tmp / f"scene-{index}.mp4"
        encode(image, seconds, part, tmp)
        parts.append(part)

    listing = tmp / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    silent = tmp / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent),
    ], check=True)

    total = sum(seconds for _, seconds in scenes)
    beat = tmp / "beat.m4a"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"aevalsrc='0.7*sin(2*PI*180*t)*exp(-9*mod(t,0.5))':d={total}:s=48000",
        "-c:a", "aac", "-b:a", "128k", str(beat),
    ], check=True)

    final = out_dir / "fixture-reel.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(silent), "-i", str(beat),
        "-c:v", "copy", "-c:a", "aac", "-shortest", str(final),
    ], check=True)

    print(f"{final}  ({total}s, {len(scenes)} scenes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
