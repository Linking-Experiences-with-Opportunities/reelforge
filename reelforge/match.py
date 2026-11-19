"""Camera matching: make footage from different cameras cut together.

The rule the code implements: correct per CAMERA, not per clip.

Forcing every clip to the same numbers wrecks footage. A white tee at a white
wall and a red hoodie measure very different saturation, and both are right:
that is the scene, not a camera fault. Scene-to-scene variation is signal;
camera-to-camera bias is the artifact. So:

  1. Group clips by the camera that shot them.
  2. Take each camera's MEDIAN across its clips. That averages the scenes out
     and leaves the camera's own bias (exposure, colour science, HDR-to-SDR).
  3. Pick one camera as the reference and correct the others to it. Every clip
     from a camera gets the same correction, so the look inside a camera holds.
  4. Optionally smooth what is left: nudge each clip's brightness a FRACTION
     of the way toward the sequence median. Partial on purpose.

Brightness is matched hardest because that is what the eye catches at a cut.
Saturation and white balance are corrected per camera only, never per clip.
"""

from __future__ import annotations

import json
import platform
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .shell import ToolError, ffprobe_json, require, run

# Beyond these a clip is a different exposure, not a camera bias.
MAX_GAIN, MIN_GAIN = 1.9, 0.55
MAX_SAT, MIN_SAT = 1.45, 0.70
MAX_WB = 0.22

SAMPLE_SECONDS = 2.0
SAMPLE_POINTS = (0.15, 0.5, 0.85)
STAT_FIELDS = ("ylow", "yavg", "yhigh", "sat", "u", "v")

VT_HLG = ["-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt"]
VT_HLG_VF = ("format=p010le,hwupload,scale_vt=w={w}:h={h}:color_matrix=bt709:"
             "color_primaries=bt709:color_transfer=bt709,hwdownload,format=p010le,format=yuv420p")
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


@dataclass
class Stats:
    ylow: float = 0.0
    yavg: float = 0.0
    yhigh: float = 0.0
    sat: float = 0.0
    u: float = 128.0
    v: float = 128.0

    def __str__(self) -> str:
        return (f"Y {self.ylow:5.1f}-{self.yhigh:5.1f} (avg {self.yavg:5.1f})  "
                f"sat {self.sat:5.1f}  U {self.u:5.1f} V {self.v:5.1f}")


@dataclass
class Clip:
    cam: str
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    hdr: bool = False
    audio_codec: str | None = None
    stats: Stats | None = field(default=None, repr=False)

    def pre(self) -> list[str]:
        return VT_HLG if self.hdr else []

    def base_vf(self) -> str:
        """HDR (iPhone HLG) footage has to go through VideoToolbox to reach SDR;
        a plain decode misreads BT.2020 and shifts the colours."""
        return VT_HLG_VF.format(w=self.width, h=self.height) if self.hdr else ""


def median_stats(group: list[Stats]) -> Stats:
    return Stats(*[statistics.median(getattr(s, f) for s in group) for f in STAT_FIELDS])


def parse_signalstats(text: str) -> Stats | None:
    def avg(key: str) -> float | None:
        values = [float(m) for m in re.findall(rf"signalstats\.{key}=([\d.]+)", text)]
        return sum(values) / len(values) if values else None

    ylow, yavg, yhigh, sat = avg("YLOW"), avg("YAVG"), avg("YHIGH"), avg("SATAVG")
    if yavg is None:
        return None
    u, v = avg("UAVG"), avg("VAVG")
    return Stats(ylow or 0.0, yavg, yhigh or 0.0, sat or 0.0,
                 128.0 if u is None else u, 128.0 if v is None else v)


def probe_clip(cam: str, path: Path) -> Clip:
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video:
        raise ToolError(f"no video in {path.name}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"])
    if abs(rotation) in (90, 270):
        width, height = height, width
    hdr = (video.get("color_transfer") or "") in HDR_TRANSFERS
    if hdr and platform.system() != "Darwin":
        raise ToolError(f"{path.name} is HDR; converting it is only built for macOS so far")
    return Clip(cam, path, float(info.get("format", {}).get("duration") or 0.0),
                width, height, hdr, audio.get("codec_name") if audio else None)


def measure(clip: Clip) -> Stats:
    """The clip's levels: the median of a few short samples across it.

    -ss goes BEFORE -i. Input seeking is instant; output seeking decodes the
    whole file first, which turns a minute of work into a quarter of an hour.
    """
    require("ffmpeg")
    samples: list[Stats] = []
    window = min(SAMPLE_SECONDS, clip.duration) if clip.duration else SAMPLE_SECONDS
    for point in SAMPLE_POINTS:
        start = max(0.0, clip.duration * point - window / 2) if clip.duration else 0.0
        vf = ",".join(p for p in (clip.base_vf(), "signalstats", "metadata=print:file=-") if p)
        proc = run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *clip.pre(),
                    "-ss", f"{start:.2f}", "-t", f"{window:.2f}", "-i", str(clip.path),
                    "-vf", vf, "-an", "-f", "null", "-"], check=False)
        found = parse_signalstats((proc.stdout or b"").decode("utf-8", "replace"))
        if found:
            samples.append(found)
        if not clip.duration:
            break
    if not samples:
        raise ToolError(f"could not read any frames from {clip.path.name}")
    clip.stats = median_stats(samples)
    return clip.stats


@dataclass
class Plan:
    cams: dict[str, Stats]
    ref: str
    smooth: float
    seq_yavg: float

    def filter_for(self, clip: Clip) -> str:
        """The ffmpeg colour filter that brings this clip to the reference camera."""
        src, tgt = self.cams[clip.cam], self.cams[self.ref]
        st = clip.stats or src
        parts: list[str] = []

        # Brightness: the camera's bias, then an optional partial nudge toward the sequence median.
        span = src.yhigh - src.ylow
        gain, lift = 1.0, 0.0
        if clip.cam != self.ref and span > 1:
            gain = max(MIN_GAIN, min(MAX_GAIN, (tgt.yhigh - tgt.ylow) / span))
            lift = tgt.ylow - gain * src.ylow
        if self.smooth > 0:
            lift += (self.seq_yavg - (st.yavg * gain + lift)) * self.smooth
        if abs(gain - 1.0) > 0.02 or abs(lift) > 2.0:
            # eq, not colorlevels: colorlevels cannot darken (its input range is clamped to 0-1).
            #   eq: out = (in - 0.5) * contrast + 0.5 + brightness      (normalised)
            bright = max(-1.0, min(1.0, lift / 255.0 + 0.5 * gain - 0.5))
            parts.append(f"eq=contrast={gain:.4f}:brightness={bright:.4f}")

        if clip.cam == self.ref:
            return ",".join(parts)

        # Saturation and white balance: per camera ONLY, never per clip.
        if src.sat > 1.0:
            sat = max(MIN_SAT, min(MAX_SAT, tgt.sat / src.sat))
            if abs(sat - 1.0) > 0.03:
                if parts and parts[-1].startswith("eq="):
                    parts[-1] += f":saturation={sat:.3f}"
                else:
                    parts.append(f"eq=saturation={sat:.3f}")
        du, dv = (tgt.u - src.u) / 255.0, (tgt.v - src.v) / 255.0
        blue = max(-MAX_WB, min(MAX_WB, du * 1.6))
        red = max(-MAX_WB, min(MAX_WB, dv * 1.6))
        if abs(blue) > 0.012 or abs(red) > 0.012:
            parts.append(f"colorbalance=rm={red:.3f}:bm={blue:.3f}")
        return ",".join(parts)


def make_plan(clips: list[Clip], ref: str, smooth: float = 0.0) -> Plan:
    cams: dict[str, Stats] = {}
    for cam in {c.cam for c in clips}:
        cams[cam] = median_stats([c.stats for c in clips if c.cam == cam and c.stats])
    if ref not in cams:
        raise ToolError(f"reference camera {ref!r} is not one of: {', '.join(sorted(cams))}")
    return Plan(cams, ref, smooth, statistics.median(c.stats.yavg for c in clips if c.stats))


def describe(vf: str) -> str:
    """A filter in plain words, for the report."""
    if not vf:
        return "no change"
    words: list[str] = []
    eq = re.search(r"eq=([^,]+)", vf)
    if eq:
        fields = dict(part.split("=") for part in eq.group(1).split(":"))
        if "brightness" in fields:
            gain, bright = float(fields.get("contrast", 1)), float(fields["brightness"])
            # `brightness` is how far mid-grey moves.
            words.append("brighter" if bright > 0.004 else "darker" if bright < -0.004
                         else ("more contrast" if gain > 1 else "less contrast"))
        if "saturation" in fields:
            words.append("more colour" if float(fields["saturation"]) > 1 else "less colour")
    balance = re.search(r"colorbalance=rm=([-\d.]+):bm=([-\d.]+)", vf)
    if balance:
        red, blue = float(balance.group(1)), float(balance.group(2))
        words.append("warmer" if red - blue > 0 else "cooler")
    return ", ".join(words) or "small correction"


def encode(clip: Clip, vf: str, out_path: Path) -> None:
    """Writes the corrected copy: H.264 at a quality that survives another edit."""
    require("ffmpeg")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    chain = ",".join(p for p in (clip.base_vf(), vf, "format=yuv420p") if p)
    audio = ["-an"] if clip.audio_codec is None else (
        ["-c:a", "copy"] if clip.audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k"])
    run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *clip.pre(),
         "-i", str(clip.path), "-map", "0:v:0", "-map", "0:a:0?", "-vf", chain,
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         *audio, "-movflags", "+faststart", str(out_path)])


def write_report(plan: Plan, clips: list[Clip], filters: dict[str, str],
                 outputs: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "reference": plan.ref,
        "smooth": plan.smooth,
        "sequence_median_luma": round(plan.seq_yavg, 2),
        "cameras": {cam: asdict(stats) for cam, stats in sorted(plan.cams.items())},
        "clips": [{
            "camera": c.cam, "path": str(c.path), "hdr": c.hdr,
            "measured": asdict(c.stats) if c.stats else None,
            "filter": filters.get(str(c.path), ""),
            "out": outputs.get(str(c.path)),
        } for c in clips],
    }, indent=2) + "\n", "utf-8")
