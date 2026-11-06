"""Match camera videos to separately recorded audio takes, and mux them.

A shoot often has a camera (scratch audio) and a field recorder (clean audio)
running on unsynchronised clocks, so file timestamps are useless for pairing
them. This module pairs them by *sound* instead:

1. Both sides are reduced to a loudness envelope: band-passed to the voice
   range (250-3400 Hz), resampled to 8 kHz, rectified, and smoothed to one
   value per 20 ms. Envelopes survive the very different mics and gain.
2. Several probe snippets are cut from the video's envelope and slid along each
   take with normalised cross-correlation (FFT based).
3. A pairing is only accepted when at least two probes land at the *same*
   offset (within ``tolerance`` seconds). A single strong peak can be
   coincidence; two independent probes agreeing on the spacing is not.

``offset`` throughout means ``take_time = video_time + offset``.

stdlib only; numpy is used for the FFT when it happens to be installed.
"""

from __future__ import annotations

import cmath
import json
import math
from array import array
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .shell import ToolError, ffmpeg, ffprobe_json, require, run

ENVELOPE_RATE = 50          # values per second (20 ms hop)
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts"}

try:  # optional fast path
    import numpy as _np
except Exception:  # pragma: no cover - depends on environment
    _np = None


# --------------------------------------------------------------------------- #
# envelopes
# --------------------------------------------------------------------------- #

def envelope(path: Path, rate: int = ENVELOPE_RATE) -> list[float]:
    """Voice-band loudness envelope of a file's first audio stream."""
    require("ffmpeg")
    chain = (
        "highpass=f=250,lowpass=f=3400,aresample=8000,"
        f"aeval=abs(val(0)),lowpass=f={rate / 2.5:g},aresample={rate}"
    )
    proc = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path), "-vn", "-ac", "1", "-af", chain,
        "-f", "f32le", "-",
    ])
    values = array("f")
    data = proc.stdout or b""
    values.frombytes(data[: len(data) - len(data) % 4])
    return list(values)


# --------------------------------------------------------------------------- #
# correlation
# --------------------------------------------------------------------------- #

def _fft(values: list[complex], invert: bool = False) -> list[complex]:
    """Iterative radix-2 FFT (len must be a power of two)."""
    n = len(values)
    a = list(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    sign = 1 if invert else -1
    while length <= n:
        w_len = cmath.exp(sign * 2j * math.pi / length)
        half = length // 2
        for start in range(0, n, length):
            w = 1 + 0j
            for k in range(start, start + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= w_len
        length <<= 1
    if invert:
        a = [x / n for x in a]
    return a


def _raw_xcorr(snippet: list[float], track: list[float]) -> list[float]:
    """sum(snippet[i] * track[lag + i]) for every valid lag."""
    m, n = len(snippet), len(track)
    lags = n - m + 1
    if _np is not None:
        size = 1 << (n + m - 1).bit_length()
        spec = _np.fft.rfft(track, size) * _np.conj(_np.fft.rfft(snippet, size))
        return _np.fft.irfft(spec, size)[:lags].tolist()
    size = 1 << (n + m - 1).bit_length()
    ta = _fft([complex(x) for x in track] + [0j] * (size - n))
    sa = _fft([complex(x) for x in snippet] + [0j] * (size - m))
    out = _fft([t * s.conjugate() for t, s in zip(ta, sa)], invert=True)
    return [out[k].real for k in range(lags)]


def best_lag(snippet: list[float], track: list[float]) -> tuple[int, float]:
    """Normalised cross-correlation peak: (lag in samples, score in [-1, 1])."""
    m = len(snippet)
    if m < 2 or len(track) < m:
        return 0, 0.0
    mean = sum(snippet) / m
    s0 = [x - mean for x in snippet]
    s_norm = math.sqrt(sum(x * x for x in s0))
    if s_norm == 0:
        return 0, 0.0
    corr = _raw_xcorr(s0, track)
    # sliding window energy of the track via prefix sums
    p1 = [0.0]
    p2 = [0.0]
    for x in track:
        p1.append(p1[-1] + x)
        p2.append(p2[-1] + x * x)
    best, best_score = 0, -2.0
    for lag, c in enumerate(corr):
        s1 = p1[lag + m] - p1[lag]
        s2 = p2[lag + m] - p2[lag]
        var = s2 - s1 * s1 / m
        if var <= 1e-12:
            continue
        score = c / (s_norm * math.sqrt(var))
        if score > best_score:
            best, best_score = lag, score
    return best, max(best_score, 0.0) if best_score > -2 else 0.0


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

@dataclass
class Probe:
    video_time: float
    offset: float
    score: float


@dataclass
class Match:
    video: str
    take: str | None
    offset: float | None = None          # take_time = video_time + offset
    score: float = 0.0
    agreeing: int = 0
    coverage: tuple[float, float] | None = None   # clean-audio span, video time
    video_duration: float = 0.0
    probes: list[Probe] = field(default_factory=list)


def probe_positions(duration: float, snippet: float, count: int) -> list[float]:
    """Evenly spread snippet start times, avoiding the very edges."""
    if duration <= snippet:
        return [0.0]
    usable = duration - snippet
    if count == 1:
        return [usable / 2]
    lo, hi = usable * 0.1, usable * 0.9
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


def match_envelopes(video_env: list[float], take_env: list[float], *,
                    snippet: float = 30.0, probes: int = 3,
                    tolerance: float = 1.0, min_score: float = 0.3,
                    rate: int = ENVELOPE_RATE) -> tuple[float | None, float, int, list[Probe]]:
    """Return (offset, mean score, agreeing probes, all probes)."""
    duration = len(video_env) / rate
    snippet = min(snippet, duration)
    width = int(snippet * rate)
    found: list[Probe] = []
    for start in probe_positions(duration, snippet, probes):
        i = int(start * rate)
        lag, score = best_lag(video_env[i:i + width], take_env)
        found.append(Probe(round(start, 3), round(lag / rate - start, 3), round(score, 3)))

    good = [p for p in found if p.score >= min_score]
    best_group: list[Probe] = []
    for anchor in good:
        group = [p for p in good if abs(p.offset - anchor.offset) <= tolerance]
        if (len(group), sum(p.score for p in group)) > (
                len(best_group), sum(p.score for p in best_group)):
            best_group = group
    if len(best_group) < 2:
        return None, 0.0, len(best_group), found
    offset = sum(p.offset for p in best_group) / len(best_group)
    score = sum(p.score for p in best_group) / len(best_group)
    return round(offset, 3), round(score, 3), len(best_group), found


def coverage(offset: float, video_duration: float,
             take_duration: float) -> tuple[float, float] | None:
    start = max(0.0, -offset)
    end = min(video_duration, take_duration - offset)
    return (round(start, 3), round(end, 3)) if end > start else None


def expand(paths: list[str], exts: set[str]) -> list[Path]:
    """Files as given; directories expanded (non-recursively) by extension."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out += sorted(c for c in p.iterdir()
                          if c.is_file() and c.suffix.lower() in exts
                          and not c.name.startswith("."))
        elif p.is_file():
            out.append(p)
        else:
            raise ToolError(f"not found: {p}")
    return out


def duration_of(path: Path) -> float:
    return float(ffprobe_json(path).get("format", {}).get("duration") or 0.0)


def match_all(videos: list[Path], takes: list[Path], *, log=print,
              **kwargs) -> list[Match]:
    take_envs = {}
    for t in takes:
        log(f"   envelope  {t.name}")
        take_envs[t] = envelope(t)
    results: list[Match] = []
    for v in videos:
        log(f"   matching  {v.name}")
        v_env = envelope(v)
        v_dur = len(v_env) / ENVELOPE_RATE
        best = Match(video=str(v), take=None, video_duration=round(v_dur, 3))
        for t, t_env in take_envs.items():
            offset, score, agreeing, probes = match_envelopes(v_env, t_env, **kwargs)
            if offset is None:
                if not best.take and len(probes) > len(best.probes):
                    best.probes = probes
                continue
            if (agreeing, score) > (best.agreeing, best.score):
                best = Match(
                    video=str(v), take=str(t), offset=offset, score=score,
                    agreeing=agreeing, video_duration=round(v_dur, 3),
                    coverage=coverage(offset, v_dur, len(t_env) / ENVELOPE_RATE),
                    probes=probes,
                )
        results.append(best)
    return results


# --------------------------------------------------------------------------- #
# muxing
# --------------------------------------------------------------------------- #

def mux(match: Match, out_path: Path, *, bitrate: str = "192k",
        keep_camera_gaps: bool = True) -> Path:
    """Copy the video stream; replace audio with the take where it covers.

    Outside the take's coverage (recorder started late / stopped early) the
    camera's own audio is used so the file never goes silent.
    """
    if match.take is None or match.offset is None or match.coverage is None:
        raise ToolError(f"{Path(match.video).name}: no matched take to mux")
    dur = match.video_duration
    off = match.offset
    c0, c1 = match.coverage
    if off >= 0:
        clean = f"[1:a]atrim=start={off:.3f},asetpts=PTS-STARTPTS"
    else:
        ms = int(round(-off * 1000))
        clean = f"[1:a]adelay={ms}:all=1"
    clean += f",apad,atrim=end={dur:.3f},aformat=channel_layouts=stereo[clean]"
    if keep_camera_gaps and (c0 > 0.05 or c1 < dur - 0.05):
        graph = (
            f"{clean};"
            f"[0:a]aformat=channel_layouts=stereo,"
            f"volume=0:enable='between(t,{c0:.3f},{c1:.3f})'[cam];"
            f"[clean][cam]amix=inputs=2:duration=first:normalize=0[a]"
        )
    else:
        graph = clean.replace("[clean]", "[a]")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg([
        "-i", match.video, "-i", match.take,
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", bitrate,
        "-movflags", "+faststart", "-shortest",
        str(out_path),
    ])
    got = duration_of(out_path)
    if abs(got - dur) > 1.0:
        raise ToolError(
            f"{out_path.name}: output is {got:.1f}s but the video is {dur:.1f}s "
            "- likely truncated (disk full?). Not trusting it."
        )
    return out_path


def write_report(results: list[Match], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n", "utf-8")
