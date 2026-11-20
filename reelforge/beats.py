"""Build a recipe from a song alone: a cut on every beat (or every N beats).

`ingest` copies the cut timing of an edit that already exists. This is for
when you have the song but no reference edit: it finds the beat grid in the
track and lays one segment per step of that grid, so every cut lands on the
music. The result is an ordinary recipe - edit it and `render` it like any
other.

The beat tracker is stdlib-only and follows the usual recipe: an onset-
strength curve (rises in loudness, full-band plus a high-passed band so hats
and snares count as much as kicks), its autocorrelation for the tempo with a
preference for ~120 BPM, then a comb search over tempo and phase for the grid
that lines up with the strongest onsets. It is good on music with a clear
pulse, which is most of what people edit to. When it guesses the wrong octave
(half or double time), pass `bpm` explicitly; `beat-check.wav` next to the
recipe lets you hear the grid before rendering anything.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

from . import analyze as analyze_mod
from . import effects as effects_mod
from . import recipe as recipe_mod
from .shell import ToolError, ffmpeg, ffprobe_json, run

BEATS_PER_BAR = 4
# Onset-strength resolution. 10 ms keeps a 140 BPM beat within ~1% per lag
# step before refinement; the shot-level 50 ms envelope is far too coarse.
BEAT_HOP = 0.01
AUDIO_RATE = 8000
MIN_BPM, MAX_BPM = 50.0, 200.0
# Tempo prior: log-normal around 120 BPM, one octave wide. Breaks the
# half/double-time tie the way a listener usually would.
PRIOR_BPM = 120.0


def audio_duration(path: Path) -> float:
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise ToolError(f"{path} has no audio stream")
    for candidate in (info.get("format", {}).get("duration"),
                      *(s.get("duration") for s in streams)):
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    raise ToolError(f"could not read the length of {path}")


def onset_strength(path: Path, hop: float = BEAT_HOP) -> list[float]:
    """Per-hop onset strength: how sharply the sound got louder at that moment."""
    return onset_strengths(path, hop)[0]


def onset_strengths(path: Path, hop: float = BEAT_HOP) -> tuple[list[float], list[float]]:
    """(onset strength, low-band onset strength). The low band is mostly kick
    drum, which is what marks the first beat of a bar in most edit music."""
    proc = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-f", "s16le", "-",
    ], check=False)
    raw = proc.stdout or b""
    raw = raw[: len(raw) // 2 * 2]
    samples = array.array("h")
    samples.frombytes(raw)
    size = max(1, int(AUDIO_RATE * hop))

    full: list[float] = []
    high: list[float] = []
    low: list[float] = []
    previous = 0
    smooth = 0.0
    for start in range(0, len(samples) - size + 1, size):
        window = samples[start:start + size]
        total = 0.0
        total_high = 0.0
        total_low = 0.0
        last = previous
        for value in window:
            total += value * value
            diff = value - last  # first difference = a crude high-pass
            total_high += diff * diff
            smooth += (value - smooth) * 0.08  # one-pole low-pass, ~100 Hz at 8 kHz
            total_low += smooth * smooth
            last = value
        previous = window[-1]
        full.append(math.log1p(total / size / 1e4))
        high.append(math.log1p(total_high / size / 1e4))
        low.append(math.log1p(total_low / size / 1e4))

    if len(full) < 4:
        return [], []
    flux = [0.0] + [
        max(0.0, full[i] - full[i - 1]) + max(0.0, high[i] - high[i - 1])
        for i in range(1, len(full))
    ]
    low_flux = [0.0] + [max(0.0, low[i] - low[i - 1]) for i in range(1, len(low))]
    return _detrend(flux, hop), _detrend(low_flux, hop)


def _detrend(flux: list[float], hop: float) -> list[float]:
    """Subtract a ~1 s moving average so quiet and loud passages both peak."""
    prefix = [0.0]
    for value in flux:
        prefix.append(prefix[-1] + value)
    half = max(1, int(0.5 / hop))
    out = []
    for i in range(len(flux)):
        lo, hi = max(0, i - half), min(len(flux), i + half)
        out.append(max(0.0, flux[i] - (prefix[hi] - prefix[lo]) / (hi - lo)))
    return out


def _value_at(strength: list[float], hop: float, t: float) -> float:
    """Peak strength within one hop either side of `t` (onsets jitter a little)."""
    i = int(round(t / hop))
    window = strength[max(0, i - 2):i + 3]
    return max(window) if window else 0.0


def guess_bar_offset(low: list[float], beats: list[float], hop: float = BEAT_HOP) -> int:
    """Which of the first four beats starts a bar: the one with the most kick.

    Returns the offset to add to a beat index so that (index + offset) % 4 == 0
    on bar starts. 0 when there is too little music to tell.
    """
    if len(beats) < BEATS_PER_BAR * 2 or not any(low):
        return 0
    scores = []
    for first in range(BEATS_PER_BAR):
        picked = [_value_at(low, hop, t) for t in beats[first::BEATS_PER_BAR]]
        scores.append(sum(picked) / len(picked) if picked else 0.0)
    first = max(range(BEATS_PER_BAR), key=lambda k: scores[k])
    return (BEATS_PER_BAR - first) % BEATS_PER_BAR


def estimate_tempo(strength: list[float], hop: float = BEAT_HOP) -> float | None:
    """Tempo from the autocorrelation of the onset-strength curve."""
    n = len(strength)
    lo = max(1, int(round(60.0 / MAX_BPM / hop)))
    hi = int(round(60.0 / MIN_BPM / hop))
    if n < hi * 4 or not any(strength):
        return None
    best_score, best_lag = 0.0, None
    for lag in range(lo, hi + 1):
        ac = sum(strength[i] * strength[i + lag] for i in range(n - lag)) / (n - lag)
        bpm = 60.0 / (lag * hop)
        weight = math.exp(-0.5 * math.log2(bpm / PRIOR_BPM) ** 2)
        if ac * weight > best_score:
            best_score, best_lag = ac * weight, lag
    return 60.0 / (best_lag * hop) if best_lag else None


def _comb(strength: list[float], hop: float, period: float, phase: float) -> float:
    """Mean onset strength sampled on the grid phase + k*period."""
    total, count = 0.0, 0
    t = phase
    limit = (len(strength) - 1) * hop
    while t <= limit:
        position = t / hop
        i = int(position)
        frac = position - i
        nxt = strength[i + 1] if i + 1 < len(strength) else strength[i]
        total += strength[i] * (1 - frac) + nxt * frac
        count += 1
        t += period
    return total / count if count else 0.0


def fit_grid(strength: list[float], bpm: float, hop: float = BEAT_HOP,
             *, refine: bool = True) -> tuple[float, float]:
    """(bpm, phase) of the beat grid that lands on the strongest onsets.

    `phase` is the time of a beat in [0, period). With `refine` the tempo is
    searched within +-4% in 0.1% steps, since the autocorrelation lag is
    quantized to the hop and a 1% error drifts a full beat in under a minute.
    """
    if bpm <= 0:
        raise ToolError(f"bad tempo {bpm}")
    if not strength:
        return bpm, 0.0
    candidates = [bpm]
    if refine:
        candidates = [bpm * (1 + step / 1000.0) for step in range(-40, 41)]

    best = (-1.0, bpm, 0.0)
    for tempo in candidates:
        period = 60.0 / tempo
        steps = max(8, int(period / hop))
        for k in range(steps):
            phase = period * k / steps
            score = _comb(strength, hop, period, phase)
            if score > best[0]:
                best = (score, tempo, phase)
    return best[1], best[2]


def grid_times(phase: float, period: float, start: float, end: float) -> list[float]:
    """Every beat time in [start, end)."""
    first = math.ceil((start - phase) / period - 1e-9)
    times = []
    k = first
    while True:
        t = phase + k * period
        if t >= end - 1e-6:
            break
        if t >= start - 1e-6:
            times.append(t)
        k += 1
    return times


def build_beat_recipe(
    song: Path,
    *,
    every: float = 1.0,
    start: float = 0.0,
    duration: float | None = None,
    bpm: float | None = None,
    effects: list[str] | None = None,
    accent_effects: list[str] | None = None,
    bar_offset: int | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    name: str | None = None,
) -> dict:
    """A recipe with one footage segment per `every` beats of `song`.

    Times in the recipe are relative to `start`, and `audio.music_start`
    records where that is in the track so the render plays the same section.
    Segments that open a bar get `accent_effects`; the rest get `effects`.
    """
    song = Path(song).expanduser().resolve()
    if not song.exists():
        raise ToolError(f"song not found: {song}")
    if every <= 0:
        raise ToolError("--every must be positive (e.g. 1, 2, 0.5)")

    total = audio_duration(song)
    if start >= total:
        raise ToolError(f"--start {start:.2f}s is past the end of the song ({total:.2f}s)")
    end = total if duration is None else min(total, start + duration)
    length = end - start
    if length <= 0.5:
        raise ToolError("the selected section is too short to edit")

    strength, low = onset_strengths(song)
    hop = BEAT_HOP
    # Fit on the section being edited plus a little either side, so a tempo
    # change elsewhere in the song does not pull the grid off.
    lo = max(0, int((start - 2.0) / hop))
    hi = min(len(strength), int((end + 2.0) / hop))
    window = strength[lo:hi]
    offset = lo * hop

    tempo_guess = bpm or estimate_tempo(window) or estimate_tempo(strength)
    notes: list[str] = []
    if not tempo_guess:
        raise ToolError(
            "could not find a steady beat in this track. Pass --bpm (look it up, "
            "or tap it out) and try again."
        )
    tempo, phase = fit_grid(window, tempo_guess, hop, refine=bpm is None)
    phase += offset
    period = 60.0 / tempo
    beats = grid_times(phase, period, start, end)
    if bar_offset is None:
        bar_offset = guess_bar_offset(low, beats)
    if not beats:
        raise ToolError("no beats landed inside the selected section")

    # The video starts exactly on the first beat of the section, so the
    # opening frame is already on the music. Cut points then step every
    # `every` beats (fractions subdivide the grid: 0.5 = every half beat).
    start = beats[0]
    length = end - start
    step = period * every
    whole = int(math.floor(length / step + 1e-6))
    if whole >= 1:
        # End on a beat too, so the video loops cleanly on TikTok instead of
        # finishing on a sliver of a shot.
        length = whole * step
    count = max(1, whole)
    rel_cuts = [round(i * step, 4) for i in range(1, count)]

    probe = {
        "duration": round(length, 3),
        "width": width,
        "height": height,
        "fps": fps,
        "orientation": "portrait" if height >= width else "landscape",
    }
    analysis = analyze_mod.Analysis(
        probe=probe,
        cuts=rel_cuts,
        tempo_bpm=tempo,
        notes=notes,
    )
    recipe = recipe_mod.build_recipe(
        analysis,
        name=name or song.stem,
        origin=str(song),
        platform="song",
        meta={"bpm": round(tempo, 2), "beat_phase": round(phase, 4), "every": every,
              "bar_offset": bar_offset},
        copy_source_text=False,
        min_segment=min(0.05, step / 2),
    )

    fx = effects_mod.parse_effects(effects)
    accent = effects_mod.parse_effects(accent_effects)
    for segment in recipe["segments"]:
        # Which beat this cut sits on, counted from the first beat of the section.
        beat_index = int(round(segment["start"] / period))
        segment["beat"] = beat_index
        on_bar = (beat_index + bar_offset) % BEATS_PER_BAR == 0
        segment["effects"] = list(accent if on_bar and accent else fx)
        segment["clip_start"] = None
        segment["speed"] = 1.0

    recipe["audio"].update({
        "mode": "music",
        "music": str(song),
        "music_start": round(start, 3),
        "music_gain_db": 0.0,
    })
    recipe["captions"]["enabled"] = False
    recipe["style"]["tempo_bpm"] = round(tempo, 1)
    recipe["notes"] = notes + [
        f"cut every {every:g} beat(s) at {tempo:.1f} BPM; "
        f"bar accents: {', '.join(accent) or 'none'}; other cuts: {', '.join(fx) or 'none'}",
    ]
    return recipe


def write_beat_check(recipe: dict, out_path: Path) -> Path:
    """The song section with a click on every cut, to check the grid by ear.

    Bar accents get a higher click. If the clicks drift or sit between the
    drums, the tempo or phase is off: re-run with --bpm (or --bar-offset if
    only the accents are on the wrong beat).
    """
    rate = 22050
    duration = float(recipe["target"]["duration"])
    samples = [0.0] * (int(duration * rate) + 1)
    for segment in recipe["segments"]:
        offset = int((recipe.get("source", {}).get("meta") or {}).get("bar_offset") or 0)
        on_bar = (segment.get("beat", 0) + offset) % BEATS_PER_BAR == 0
        pitch = 1760.0 if on_bar else 1100.0
        first = int(float(segment["start"]) * rate)
        for i in range(int(0.03 * rate)):
            if first + i >= len(samples):
                break
            t = i / rate
            samples[first + i] += 0.6 * math.sin(2 * math.pi * pitch * t) * math.exp(-t * 120)

    clicks = Path(out_path).with_suffix(".clicks.wav")
    with wave.open(str(clicks), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array.array(
            "h", (int(max(-1.0, min(1.0, v)) * 32000) for v in samples)).tobytes())

    audio = recipe.get("audio", {})
    try:
        ffmpeg([
            "-ss", f"{float(audio.get('music_start') or 0.0):.3f}",
            "-t", f"{duration:.3f}", "-i", str(audio["music"]),
            "-i", str(clicks),
            "-filter_complex",
            "[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=0.7[m];"
            "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[c];"
            "[m][c]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "[a]", str(out_path),
        ])
    finally:
        clicks.unlink(missing_ok=True)
    return Path(out_path)
