"""Basic audio cleanup: less background noise, steadier volume.

For a recording with hiss, room tone, a fan or an air conditioner behind the
voice. Each file gets a cleaned copy; the source is never changed.

1. A high-pass at 80 Hz drops rumble that sits under any voice.
2. The quietest half second of the file (nobody talking) is taken as a sample
   of the background noise. ffmpeg's ``afftdn`` learns its spectrum from that
   sample and subtracts it everywhere. The sample is played into the filter
   ahead of the real audio and trimmed off again, so the first second is
   cleaned as well as the last.
3. When no stretch is clearly quieter than the rest (wall-to-wall speech or
   music), there is nothing to learn from; ``afftdn`` then tracks the noise
   floor by itself, which is gentler.
4. Loudness is set to -16 LUFS (two-pass ``loudnorm``, so it is one gain
   change and not a compressor), the level podcasts and social video use.

Video files keep their picture untouched (stream copy); only the audio is
re-encoded.

stdlib only.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .shell import ToolError, ffmpeg, ffmpeg_stderr, ffprobe_json
from .sync import AUDIO_EXTS, ENVELOPE_RATE, VIDEO_EXTS, envelope

# dB of reduction asked of afftdn. 12 is its own default; past ~24 voices
# start to sound watery.
STRENGTHS = {"light": 6, "normal": 12, "strong": 20}
DEFAULT_LOUDNESS = -16.0     # LUFS
TRUE_PEAK = -1.5             # dBTP ceiling after the gain change
HIGHPASS_HZ = 80
SAMPLE_SECONDS = 0.5         # length of the noise sample
QUIET_RATIO = 0.25           # the sample must be 12 dB under the loud parts
SILENCE = 1e-5               # envelope values below this are digital silence
LOSSLESS_EXTS = {".wav", ".flac", ".aif", ".aiff"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS | {".ogg", ".webm"}


@dataclass
class Result:
    source: Path
    out: Path
    learned: bool                 # a noise sample was found and used
    noise_before: float | None    # dB RMS of the noise sample, before
    noise_after: float | None     # the same stretch in the cleaned file
    loudness_before: float | None  # LUFS
    loudness_after: float | None

    def summary(self) -> str:
        parts = []
        if self.noise_before is not None and self.noise_after is not None:
            drop = self.noise_before - self.noise_after
            parts.append(f"background {drop:.0f} dB quieter" if drop >= 1
                         else "background already quiet")
        elif not self.learned:
            parts.append("no quiet moment to learn the noise from, cleaned gently")
        if self.loudness_before is not None and self.loudness_after is not None:
            parts.append(f"volume {self.loudness_before:.0f} -> {self.loudness_after:.0f} LUFS")
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
# pure logic
# --------------------------------------------------------------------------- #

def quiet_stretch(env: list[float], rate: int = ENVELOPE_RATE,
                  seconds: float = SAMPLE_SECONDS) -> tuple[float, float] | None:
    """(start, end) in seconds of the quietest stretch that is still sound.

    None when the file is too short, or when its quietest stretch is not
    clearly quieter than the rest - then it is speech or music, not noise.
    """
    size = max(1, round(seconds * rate))
    if len(env) < size * 4:
        return None
    # the level of the loud parts: the median of speech sits in its many small dips
    typical = sorted(env)[len(env) * 9 // 10]
    if typical <= SILENCE:
        return None
    total = sum(env[:size])
    dead = sum(1 for v in env[:size] if v <= SILENCE)
    best_sum, best_at = None, 0
    for start in range(len(env) - size + 1):
        if start:
            total += env[start + size - 1] - env[start - 1]
            dead += (env[start + size - 1] <= SILENCE) - (env[start - 1] <= SILENCE)
        # digital silence (a cut, a muted mic) holds no noise to learn from
        if not dead and (best_sum is None or total < best_sum):
            best_sum, best_at = total, start
    if best_sum is None or best_sum / size > typical * QUIET_RATIO:
        return None
    return best_at / rate, (best_at + size) / rate


def output_suffix(source: Path, has_video: bool) -> str:
    ext = source.suffix.lower()
    if has_video:
        return ext
    return ".wav" if ext in LOSSLESS_EXTS else ".m4a"


def output_name(source: Path, has_video: bool, suffix: str, taken: set[str]) -> str:
    """Output file name for a source; a name already in ``taken`` gets -2, -3, ..."""
    ext = output_suffix(source, has_video)
    name = f"{source.stem}{suffix}{ext}"
    n = 2
    while name.lower() in taken:
        name = f"{source.stem}{suffix}-{n}{ext}"
        n += 1
    taken.add(name.lower())
    return name


def denoise_graph(strength: str, sample: tuple[float, float] | None) -> str:
    """Filtergraph from [0:a] to [den]: high-pass, then noise reduction."""
    nr = STRENGTHS[strength]
    if sample is None:
        return f"[0:a]highpass=f={HIGHPASS_HZ},afftdn=nr={nr}:nf=-40:tn=1[den]"
    start, end = sample
    length = end - start
    # the noise sample runs first so afftdn has learned it before the real audio starts
    return (
        f"[0:a]atrim={start:.3f}:{end:.3f},asetpts=PTS-STARTPTS[noise];"
        f"[noise][0:a]concat=n=2:v=0:a=1,highpass=f={HIGHPASS_HZ},"
        f"asendcmd=c='0.02 afftdn sn start; {length - 0.02:.3f} afftdn sn stop',"
        f"afftdn=nr={nr}:nf=-40,atrim=start={length:.3f},asetpts=PTS-STARTPTS[den]"
    )


def parse_loudnorm(stderr: str) -> dict | None:
    """The JSON block loudnorm prints at the end of a measuring pass."""
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not found:
        return None
    try:
        stats = {k: float(v) for k, v in json.loads(found[-1]).items()
                 if k != "normalization_type"}
    except (ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in stats.values()):
        return None  # silence measures as -inf; there is nothing to set the level of
    return stats


def loudnorm_filter(stats: dict, target: float) -> str:
    return (
        f"loudnorm=I={target:g}:TP={TRUE_PEAK:g}:LRA=11:linear=true"
        f":measured_I={stats['input_i']:g}:measured_TP={stats['input_tp']:g}"
        f":measured_LRA={stats['input_lra']:g}:measured_thresh={stats['input_thresh']:g}"
        f":offset={stats['target_offset']:g}"
    )


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

def collect(paths: list[str]) -> list[Path]:
    """Files to clean: the files given, plus the media directly inside any folders."""
    out: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            out += sorted(p for p in path.iterdir()
                          if p.is_file() and p.suffix.lower() in MEDIA_EXTS
                          and not p.name.startswith("."))
        elif path.is_file():
            out.append(path)
        else:
            raise ToolError(f"not found: {path}")
    return out


def streams(path: Path) -> tuple[dict | None, bool, float]:
    """(first audio stream, has a real video stream, duration in seconds)."""
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = any(s.get("codec_type") == "video"
                and not (s.get("disposition") or {}).get("attached_pic")
                for s in streams)
    try:
        duration = float((info.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    return audio, video, duration


def _rms_db(path: Path, start: float, end: float) -> float | None:
    text = ffmpeg_stderr([
        "-nostats", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path),
        "-vn", "-af", "astats=measure_perchannel=none:measure_overall=RMS_level",
        "-f", "null", "-",
    ])
    found = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", text)
    if not found or found[-1] == "-inf":
        return None
    return float(found[-1])


def _codec_args(out: Path, audio: dict, has_video: bool, bitrate: str) -> list[str]:
    ext = out.suffix.lower()
    if ext == ".wav":
        deep = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 16) > 16
        return ["-c:a", "pcm_s24le" if deep else "pcm_s16le"]
    if ext == ".webm":
        return ["-c:a", "libopus", "-b:a", bitrate]
    args = ["-c:a", "aac", "-b:a", bitrate]
    if has_video and ext in {".mp4", ".mov", ".m4v"}:
        args += ["-movflags", "+faststart"]
    return args


def clean_file(source: Path, out: Path, *, strength: str = "normal",
               loudness: float | None = DEFAULT_LOUDNESS,
               bitrate: str = "192k") -> Result:
    """Write a cleaned copy of ``source`` to ``out``."""
    if strength not in STRENGTHS:
        raise ToolError(f"strength is one of: {', '.join(STRENGTHS)}")
    audio, has_video, duration = streams(source)
    if audio is None:
        raise ToolError(f"{source.name} has no audio to clean")
    if out.resolve() == source.resolve():
        raise ToolError(f"refusing to write over the source: {source}")

    sample = quiet_stretch(envelope(source))
    graph = denoise_graph(strength, sample)

    stats = None
    if loudness is not None:
        text = ffmpeg_stderr([
            "-nostats", "-i", str(source), "-filter_complex",
            f"{graph};[den]loudnorm=I={loudness:g}:TP={TRUE_PEAK:g}:LRA=11:print_format=json[out]",
            "-map", "[out]", "-f", "null", "-",
        ])
        stats = parse_loudnorm(text)
    tail = loudnorm_filter(stats, loudness) if stats else "anull"

    rate = str(audio.get("sample_rate") or 48000)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(source), "-filter_complex", f"{graph};[den]{tail}[out]"]
    if has_video:
        args += ["-map", "0:v:0", "-c:v", "copy"]
    # loudnorm resamples internally, so the source's rate is set back explicitly
    args += ["-map", "[out]", "-ar", rate, *_codec_args(out, audio, has_video, bitrate), str(out)]
    ffmpeg(args)

    _, _, written = streams(out)
    if duration and abs(written - duration) > max(1.0, duration * 0.01):
        raise ToolError(
            f"{out.name} came out {written:.1f}s long but {source.name} is "
            f"{duration:.1f}s; the copy is not complete (is the disk full?)")

    before = after = None
    if sample:
        before = _rms_db(source, *sample)
        after = _rms_db(out, *sample)
        if before is not None and after is not None and stats:
            # compare like with like: take the volume change back out
            after -= loudness - stats["input_i"] if loudness is not None else 0.0
    return Result(
        source=source, out=out, learned=sample is not None,
        noise_before=before, noise_after=after,
        loudness_before=stats["input_i"] if stats else None,
        loudness_after=loudness if stats else None,
    )
