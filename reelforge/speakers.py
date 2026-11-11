"""Label a transcript by speaker using a recorder's per-mic tracks.

Multitrack recorders (Zoom PodTrak, Rodecaster, ...) write one file per mic
next to the stereo mix, all on the take's timeline. For each transcript
segment, whoever's mic is loudest is speaking. That is exact for one person
per mic and needs nothing beyond ffmpeg.

When two people share a mic, the mic alone cannot tell them apart. Pass the
mic as ``"David/Papa=MIC3.WAV"`` plus one ``--anchor NAME=SECONDS`` per voice
(a moment, in take time, where that person is clearly talking, e.g. their
self-introduction). The shared mic's segments are then embedded with
resemblyzer and clustered with k-means seeded from the anchors, so every
cluster comes out already named. That path needs the optional
``speakers`` extras (numpy + resemblyzer).

Segments are whatever the transcriber produced; a segment where two people
talk is given to whoever is loudest overall. Transcribe with short segments
(whisper-cli ``-ml 90 -sow``) for cleaner turns.
"""

from __future__ import annotations

import json
from array import array
from dataclasses import dataclass
from pathlib import Path

from .shell import ToolError, require, run

ENERGY_RATE = 100           # energy values per second
MIN_EMBED_SECONDS = 0.8     # shorter shared-mic segments inherit a neighbour


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = ""
    mic: str = ""


@dataclass
class Mic:
    path: Path
    names: list[str]        # one name, or several when the mic is shared

    @property
    def label(self) -> str:
        return "/".join(self.names)


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #

def parse_mic(spec: str) -> Mic:
    """``"Max=MIC1.WAV"`` or ``"David/Papa=MIC3.WAV"``."""
    names, sep, path = spec.partition("=")
    if not sep or not names.strip() or not path.strip():
        raise ValueError(f"--mic expects NAME=PATH (or A/B=PATH), got {spec!r}")
    parts = [n.strip() for n in names.split("/") if n.strip()]
    if len(set(parts)) != len(parts):
        raise ValueError(f"--mic {spec!r}: repeated name")
    return Mic(Path(path.strip()).expanduser(), parts)


def parse_anchor(spec: str) -> tuple[str, float]:
    """``"Papa=28.5"`` or ``"Papa=0:28.5"`` (take time)."""
    name, sep, when = spec.partition("=")
    if not sep or not name.strip() or not when.strip():
        raise ValueError(f"--anchor expects NAME=SECONDS, got {spec!r}")
    seconds = 0.0
    for part in when.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return name.strip(), seconds


def load_transcript(path: Path) -> list[Segment]:
    """Segments from whisper-cli ``-oj`` JSON or openai-whisper JSON."""
    return parse_transcript(json.loads(Path(path).read_text("utf-8")))


def parse_transcript(data: dict) -> list[Segment]:
    out: list[Segment] = []
    if "transcription" in data:                 # whisper.cpp / whisper-cli
        for s in data["transcription"]:
            off = s.get("offsets") or {}
            out.append(Segment(off.get("from", 0) / 1000, off.get("to", 0) / 1000,
                               (s.get("text") or "").strip()))
    elif "segments" in data:                    # openai-whisper
        for s in data["segments"]:
            out.append(Segment(float(s["start"]), float(s["end"]),
                               (s.get("text") or "").strip()))
    else:
        raise ValueError("transcript JSON has neither 'transcription' "
                         "(whisper-cli) nor 'segments' (openai-whisper)")
    return [s for s in out if s.text and s.end > s.start]


# --------------------------------------------------------------------------- #
# loudest mic
# --------------------------------------------------------------------------- #

def mic_energy(path: Path, rate: int = ENERGY_RATE) -> list[float]:
    """Mean-square energy of a mic track, ``rate`` values per second."""
    require("ffmpeg")
    chain = ("highpass=f=80,aresample=8000,aeval=val(0)*val(0),"
             f"lowpass=f={rate / 2.5:g},aresample={rate}")
    proc = run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", str(path), "-vn", "-ac", "1", "-af", chain,
                "-f", "f32le", "-"])
    values = array("f")
    data = proc.stdout or b""
    values.frombytes(data[: len(data) - len(data) % 4])
    return list(values)


def window_mean(energy: list[float], start: float, end: float,
                rate: int = ENERGY_RATE) -> float:
    i = int(start * rate)
    window = energy[i:max(int(end * rate), i + 1)]
    return sum(window) / len(window) if window else 0.0


def assign_loudest(segments: list[Segment], energies: dict[str, list[float]],
                   rate: int = ENERGY_RATE) -> None:
    """Set ``Segment.mic`` to the label of the loudest mic (in place)."""
    for s in segments:
        s.mic = max(energies, key=lambda m: window_mean(energies[m], s.start, s.end, rate))


# --------------------------------------------------------------------------- #
# shared mic: split voices
# --------------------------------------------------------------------------- #

def anchored_kmeans(vectors, seeds, iterations: int = 30) -> list[int]:
    """Cosine k-means whose centroid ``k`` starts at ``seeds[k]``.

    Seeding from labelled examples means cluster ``k`` *is* speaker ``k``; no
    guessing which cluster is whom afterwards. Returns one label per vector.
    """
    import numpy as np
    X = np.asarray(vectors, dtype=float)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
    C = np.asarray(seeds, dtype=float)
    C = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-12)
    labels = np.full(len(X), -1)
    for _ in range(iterations):
        new = np.argmax(X @ C.T, axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for k in range(len(C)):
            members = X[labels == k]
            if len(members):
                c = members.mean(axis=0)
                C[k] = c / (np.linalg.norm(c) + 1e-12)
    return [int(x) for x in labels]


def _load_16k(path: Path):
    import numpy as np
    proc = run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                "-f", "f32le", "-"])
    return np.frombuffer(proc.stdout or b"", dtype=np.float32)


def nearest_segment(segments: list[Segment], t: float) -> Segment | None:
    """The segment containing ``t``, else the closest one."""
    return min(segments, key=lambda s: 0.0 if s.start <= t <= s.end
               else min(abs(s.start - t), abs(s.end - t)), default=None)


def seed_indices(embedded: list[Segment], mic: Mic,
                 anchors: dict[str, float]) -> list[int]:
    """Index into ``embedded`` of each name's anchor segment, in ``mic.names`` order.

    Two anchors landing on the same segment would start both voices from one
    example, and the names could come out swapped, so that is an error.
    """
    picked: dict[int, str] = {}
    out = []
    for name in mic.names:
        t = anchors[name]
        hit = nearest_segment(embedded, t)
        if hit is None or not (hit.start - 5 <= t <= hit.end + 5):
            raise ToolError(f"--anchor {name}={t}: no {mic.label} "
                            "speech near that time (is it in take time?)")
        j = embedded.index(hit)
        if j in picked:
            other = picked[j]
            raise ToolError(
                f"--anchor {other}={anchors[other]} and {name}={t} both land on "
                f"the segment {hit.start:.1f}-{hit.end:.1f}s ({hit.text[:40]!r}); "
                "pick, for each person, a moment where only they are talking")
        picked[j] = name
        out.append(j)
    return out


def split_shared(segments: list[Segment], mic: Mic,
                 anchors: dict[str, float], *, log=print) -> None:
    """Give each segment on a shared mic one of ``mic.names`` (in place)."""
    missing = [n for n in mic.names if n not in anchors]
    if missing:
        raise ToolError(f"mic {mic.label} is shared: pass --anchor for "
                        + ", ".join(f"{n}=SECONDS" for n in missing))
    try:
        import numpy  # noqa: F401
        from resemblyzer import VoiceEncoder
    except Exception as exc:  # pragma: no cover - depends on environment
        raise ToolError(
            "splitting a shared mic needs numpy + resemblyzer. Install with:\n"
            "  uv tool install -e '.[speakers]' --python 3.11") from exc

    mine = [s for s in segments if s.mic == mic.label]
    if not mine:
        return
    log(f"   voices    {mic.label}: embedding {len(mine)} segments")
    audio = _load_16k(mic.path)
    enc = VoiceEncoder(verbose=False)
    embedded, vectors = [], []
    for s in mine:
        clip = audio[int(s.start * 16000):int(s.end * 16000)]
        if len(clip) >= MIN_EMBED_SECONDS * 16000:
            vectors.append(enc.embed_utterance(clip))
            embedded.append(s)
    if not embedded:
        raise ToolError(f"no {mic.label} segment is long enough to tell voices apart")

    seeds = [vectors[j] for j in seed_indices(embedded, mic, anchors)]
    for s, k in zip(embedded, anchored_kmeans(vectors, seeds)):
        s.speaker = mic.names[k]
    # too short to embed: same voice as the nearest earlier segment on this mic
    last = mic.names[0]
    for s in mine:
        if s.speaker:
            last = s.speaker
        else:
            s.speaker = last


def label(segments: list[Segment], mics: list[Mic],
          anchors: dict[str, float] | None = None, *, log=print) -> list[Segment]:
    """Full pipeline: loudest mic, then a voice split for any shared mic."""
    energies = {}
    for m in mics:
        log(f"   energy    {m.label} ({m.path.name})")
        energies[m.label] = mic_energy(m.path)
    assign_loudest(segments, energies)
    for m in mics:
        if len(m.names) == 1:
            for s in segments:
                if s.mic == m.label:
                    s.speaker = m.names[0]
        else:
            split_shared(segments, m, anchors or {}, log=log)
    return segments


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #

def _clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    return (f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:"
            f"{ms % 60000 // 1000:02d},{ms % 1000:03d}")


def to_text(segments: list[Segment], header: list[str] | None = None) -> str:
    """One paragraph per speaker turn: ``[0:01:07] Max: ...``."""
    turns: list[tuple[float, str, list[str]]] = []
    for s in segments:
        if turns and turns[-1][1] == s.speaker:
            turns[-1][2].append(s.text)
        else:
            turns.append((s.start, s.speaker, [s.text]))
    body = "\n\n".join(f"[{_clock(t)}] {who}: {' '.join(parts)}" for t, who, parts in turns)
    head = "\n".join(header or [])
    return (head + "\n\n" if head else "") + body + "\n"


def to_srt(segments: list[Segment], shift: float = 0.0,
           duration: float | None = None) -> str:
    """SRT cues ``Name: text``; ``shift`` is subtracted from every time.

    For a camera file matched by ``sync``, ``shift = offset`` and
    ``duration = video_duration`` puts the cues in that video's own time.
    """
    cues, n = [], 0
    for s in segments:
        a, b = s.start - shift, s.end - shift
        if b <= 0 or (duration is not None and a >= duration):
            continue
        a = max(a, 0.0)
        if duration is not None:
            b = min(b, duration)
        n += 1
        cues.append(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{s.speaker}: {s.text}\n")
    return "\n".join(cues)


def to_json(segments: list[Segment]) -> str:
    return json.dumps([{"start": round(s.start, 3), "end": round(s.end, 3),
                        "speaker": s.speaker, "mic": s.mic, "text": s.text}
                       for s in segments], indent=1) + "\n"


def matches_for_take(report: list[dict], take: Path) -> list[dict]:
    """Rows of a ``sync --report`` whose take is this file.

    An exact path match wins. Otherwise the file name stem is compared, which
    lets you pass the original WAV when sync was run on a resampled copy, but
    only if exactly one take in the report has that stem: ``takeA/MIC1.WAV``
    and ``takeB/MIC1.WAV`` are different recordings and must not be mixed.
    """
    rows = [r for r in report if r.get("take") and r.get("offset") is not None]

    def resolved(p) -> Path:
        return Path(p).expanduser().resolve()

    want = resolved(take)
    exact = [r for r in rows if resolved(r["take"]) == want]
    if exact:
        return exact
    stem = take.stem.lower()
    by_stem = [r for r in rows if Path(r["take"]).stem.lower() == stem]
    distinct = sorted({str(resolved(r["take"])) for r in by_stem})
    if len(distinct) > 1:
        raise ToolError(f"{take.name} is ambiguous in the sync report: "
                        + ", ".join(distinct) + ". Pass --take as the exact path "
                        "the report uses.")
    return by_stem


def camera_srt_names(videos: list[Path], reserved: set[str] = frozenset()) -> list[str]:
    """One distinct SRT base name per matched camera file.

    Normally the video's stem (``C0001``). Two cameras that both number from
    ``C0001`` get their folder prefixed (``Max Camera-C0001``) so neither
    overwrites the other; a name that still collides is an error.
    """
    stems = [v.stem for v in videos]
    names = [f"{v.parent.name}-{v.stem}" if stems.count(v.stem) > 1 or v.stem in reserved
             else v.stem for v in videos]
    clash = sorted({n for n in names if names.count(n) > 1 or n in reserved})
    if clash:
        raise ToolError("camera files would overwrite each other's subtitles: "
                        + ", ".join(clash) + ". Rename one of them or pass a different --name.")
    return names
