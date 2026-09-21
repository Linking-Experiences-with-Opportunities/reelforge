"""Pull structure out of a source video: cuts, audio energy, speech, on-screen text."""

from __future__ import annotations

import array
import difflib
import json
import math
import re
import shutil
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import visuals
from .shell import ToolError, ffmpeg, ffmpeg_stderr, have, run

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")

# Audio is decoded at this rate; 8 kHz mono is plenty for an energy envelope
# and keeps a 60 s clip under half a megabyte.
AUDIO_RATE = 8000
HOP_SECONDS = 0.05


@dataclass
class TextBeat:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "text": self.text}


@dataclass
class Analysis:
    probe: dict
    cuts: list[float] = field(default_factory=list)
    shot_lengths: list[float] = field(default_factory=list)
    energy: list[float] = field(default_factory=list)
    energy_hop: float = HOP_SECONDS
    onsets: list[float] = field(default_factory=list)
    tempo_bpm: float | None = None
    transcript: list[dict] = field(default_factory=list)
    transcript_text: str = ""
    ocr_beats: list[TextBeat] = field(default_factory=list)
    overlay_beats: list[TextBeat] = field(default_factory=list)
    frames: list[tuple[float, Path]] = field(default_factory=list)
    frame_step: float = 1.0
    visual_verdicts: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "frame_step": self.frame_step,
            "visual_verdicts": [v.as_dict() for v in self.visual_verdicts],
            "probe": self.probe,
            "cuts": [round(c, 3) for c in self.cuts],
            "shot_lengths": [round(s, 3) for s in self.shot_lengths],
            "energy": [round(e, 4) for e in self.energy],
            "energy_hop": self.energy_hop,
            "onsets": [round(o, 3) for o in self.onsets],
            "tempo_bpm": round(self.tempo_bpm, 1) if self.tempo_bpm else None,
            "transcript": self.transcript,
            "transcript_text": self.transcript_text,
            "ocr_beats": [b.as_dict() for b in self.ocr_beats],
            "overlay_beats": [b.as_dict() for b in self.overlay_beats],
            "notes": self.notes,
        }


def detect_cuts(video: Path, duration: float, threshold: float = 0.28) -> list[float]:
    """Scene-change timestamps via ffmpeg's scene score, in seconds."""
    stderr = ffmpeg_stderr([
        "-i", str(video),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ])
    times = sorted({float(m) for m in _PTS_RE.findall(stderr)})
    # A cut in the first fraction of a second is the opening frame, not a cut.
    cuts = [t for t in times if 0.25 < t < max(duration - 0.15, 0.0)]

    if not cuts and duration > 0:
        return []
    return cuts


def shot_lengths_from_cuts(cuts: list[float], duration: float) -> list[float]:
    boundaries = [0.0] + cuts + [duration]
    return [
        round(boundaries[i + 1] - boundaries[i], 3)
        for i in range(len(boundaries) - 1)
        if boundaries[i + 1] - boundaries[i] > 0.01
    ]


def audio_envelope(video: Path, has_audio: bool) -> tuple[list[float], list[float], float | None]:
    """Return (rms_envelope, onset_times, tempo_bpm)."""
    if not has_audio:
        return [], [], None

    proc = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(video),
        "-map", "0:a:0", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-f", "s16le", "-",
    ], check=False)
    raw = proc.stdout or b""
    if len(raw) < 2:
        return [], [], None
    if len(raw) % 2:
        raw = raw[:-1]

    samples = array.array("h")
    samples.frombytes(raw)

    hop = int(AUDIO_RATE * HOP_SECONDS)
    envelope: list[float] = []
    for start in range(0, len(samples) - hop + 1, hop):
        window = samples[start:start + hop]
        total = 0.0
        for value in window:
            total += float(value) * float(value)
        envelope.append(math.sqrt(total / len(window)) / 32768.0)

    if not envelope:
        return [], [], None

    peak = max(envelope) or 1.0
    envelope = [round(v / peak, 5) for v in envelope]

    onsets = _detect_onsets(envelope, HOP_SECONDS)
    tempo = _estimate_tempo(onsets)
    return envelope, onsets, tempo


def _detect_onsets(envelope: list[float], hop: float) -> list[float]:
    """Peaks in positive energy flux — a cheap stand-in for beat tracking."""
    if len(envelope) < 4:
        return []
    flux = [max(0.0, envelope[i] - envelope[i - 1]) for i in range(1, len(envelope))]
    if not any(flux):
        return []

    mean = statistics.fmean(flux)
    spread = statistics.pstdev(flux) if len(flux) > 1 else 0.0
    threshold = mean + spread

    onsets: list[float] = []
    min_gap = 0.18  # ~333 BPM ceiling; avoids double-triggering one hit
    for i in range(1, len(flux) - 1):
        if flux[i] <= threshold:
            continue
        if flux[i] < flux[i - 1] or flux[i] < flux[i + 1]:
            continue
        time = (i + 1) * hop
        if onsets and time - onsets[-1] < min_gap:
            continue
        onsets.append(round(time, 3))
    return onsets


def _estimate_tempo(onsets: list[float]) -> float | None:
    if len(onsets) < 4:
        return None
    intervals = [b - a for a, b in zip(onsets, onsets[1:]) if 0.15 < (b - a) < 2.0]
    if len(intervals) < 3:
        return None
    median = statistics.median(intervals)
    if median <= 0:
        return None
    bpm = 60.0 / median
    # Fold into a musically plausible 60-180 range.
    while bpm < 60:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return bpm


def transcribe(video: Path, project_dir: Path, model: str = "base") -> tuple[list[dict], str, str | None]:
    """Speech-to-text via the openai-whisper CLI. Returns (segments, text, note)."""
    if not have("whisper"):
        return [], "", "whisper not installed - skipping transcript (pip install -U openai-whisper)"

    audio = project_dir / "audio.wav"
    try:
        ffmpeg([
            "-i", str(video),
            "-map", "0:a:0", "-ac", "1", "-ar", "16000",
            str(audio),
        ])
    except ToolError:
        return [], "", "no audio track - skipping transcript"

    out_dir = project_dir / "whisper"
    out_dir.mkdir(exist_ok=True)
    proc = run([
        "whisper", str(audio),
        "--model", model,
        "--output_format", "json",
        "--output_dir", str(out_dir),
        "--verbose", "False",
        "--fp16", "False",
    ], check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        return [], "", f"whisper failed - skipping transcript ({tail})"

    results = list(out_dir.glob("*.json"))
    if not results:
        return [], "", "whisper produced no output - skipping transcript"

    try:
        data = json.loads(results[0].read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], "", f"could not read whisper output: {exc}"

    segments = [
        {
            "start": round(float(seg.get("start", 0.0)), 2),
            "end": round(float(seg.get("end", 0.0)), 2),
            "text": (seg.get("text") or "").strip(),
        }
        for seg in data.get("segments", [])
        if (seg.get("text") or "").strip()
    ]
    return segments, (data.get("text") or "").strip(), None


def sample_frames(video: Path, project_dir: Path, fps: float = 1.0) -> list[tuple[float, Path]]:
    """Extract frames at `fps` into the project, returning (timestamp, path) pairs.

    Frames are kept rather than thrown away: OCR, the diagram/image classifier
    and the reference stills the CLI shows all read from this one sampling pass.
    """
    frames_dir = project_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg([
        "-i", str(video),
        "-vf", f"fps={fps}",
        "-q:v", "3",
        str(frames_dir / "frame-%05d.jpg"),
    ])
    files = sorted(frames_dir.glob("frame-*.jpg"))
    # ffmpeg's fps filter emits the first frame at t=0, then every 1/fps after.
    return [(index / fps, path) for index, path in enumerate(files)]


def ocr_frames(frames: list[tuple[float, Path]]
               ) -> tuple[dict[float, str], dict[float, str], str | None]:
    """OCR each sampled frame.

    Returns (all_text_by_time, overlay_text_by_time, note). The second map
    holds only text large enough to be a deliberate caption overlay; see
    `_overlay_text` for why the two are kept apart.
    """
    if not have("tesseract"):
        return {}, {}, ("tesseract not installed - skipping on-screen text "
                        "(brew install tesseract)")
    if not frames:
        return {}, {}, "no frames extracted for OCR"

    frame_height = _frame_height(frames[0][1])

    text_by_time: dict[float, str] = {}
    overlay_by_time: dict[float, str] = {}
    for time, frame in frames:
        proc = run(["tesseract", str(frame), "stdout", "--psm", "11", "tsv"], check=False)
        if proc.returncode != 0:
            continue
        raw = (proc.stdout or b"").decode("utf-8", "replace")
        lines = _ocr_lines(raw)
        text_by_time[time] = _join_lines(lines)
        overlay_by_time[time] = _overlay_text(lines, frame_height)
    return text_by_time, overlay_by_time, None


def _frame_height(frame: Path) -> int:
    """Pixel height of a sampled frame; 0 when it cannot be read."""
    try:
        from PIL import Image
        with Image.open(frame) as image:
            return image.size[1]
    except Exception:
        return 0


def text_beats_from_ocr(text_by_time: dict[float, str], step: float,
                        duration: float) -> list[TextBeat]:
    observations = sorted(text_by_time.items())
    return _group_text_beats(observations, step, duration)


MIN_WORD_CONFIDENCE = 62.0

# A caption overlay is set large on purpose. Measured on a real reel, the
# burned-in lyric captions ran 2.7-3.9% of frame height while text belonging to
# the scene itself (a wall panel behind the subject) was 0.5-0.7%. Anything
# below this is treated as part of the picture, not as a caption.
CAPTION_MIN_HEIGHT_RATIO = 0.015


def _ocr_lines(raw: str) -> list[dict]:
    """Parse tesseract TSV into lines that keep their geometry.

    Only words tesseract is actually confident about survive: textured footage
    makes it hallucinate words with confidence in the teens, and those would
    otherwise become invented captions.
    """
    rows = raw.splitlines()
    if not rows:
        return []

    header = rows[0].split("\t")
    try:
        at = {name: header.index(name) for name in
              ("conf", "text", "line_num", "block_num", "top", "height",
               "left", "width")}
    except ValueError:
        return []

    grouped: dict[tuple[str, str], dict] = {}
    for row in rows[1:]:
        columns = row.split("\t")
        if len(columns) <= max(at.values()):
            continue
        word = columns[at["text"]].strip()
        if not word:
            continue
        try:
            confidence = float(columns[at["conf"]])
        except ValueError:
            continue
        if confidence < MIN_WORD_CONFIDENCE:
            continue
        # Drop fragments that are mostly punctuation or stray marks.
        alnum = sum(c.isalnum() for c in word)
        if alnum == 0 or (len(word) > 1 and alnum / len(word) < 0.6):
            continue

        try:
            top = int(columns[at["top"]])
            height = int(columns[at["height"]])
            left = int(columns[at["left"]])
            width = int(columns[at["width"]])
        except ValueError:
            continue

        key = (columns[at["block_num"]], columns[at["line_num"]])
        line = grouped.setdefault(key, {
            "words": [], "height": 0, "top": top, "left": left, "right": 0,
        })
        line["words"].append(word)
        line["height"] = max(line["height"], height)
        line["top"] = min(line["top"], top)
        line["left"] = min(line["left"], left)
        line["right"] = max(line["right"], left + width)

    lines = []
    for line in grouped.values():
        text = " ".join(line["words"])
        if len(text) < 3:
            continue
        lines.append({**line, "text": text})
    return lines


def _join_lines(lines: list[dict]) -> str:
    return " ".join(line["text"] for line in lines).strip()


def _overlay_text(lines: list[dict], frame_height: int) -> str:
    """Just the lines big enough to be a deliberate caption overlay.

    Seeding captions from *all* on-screen text goes wrong the moment the scene
    itself contains writing - a screen, a poster, a wall panel. That text gets
    merged with the real overlay into one long blob, which then trips the
    caption-length guard and the recipe ends up with no captions at all, even
    though the source clearly had them. Sorting by size recovers the overlay.
    """
    if not lines or frame_height <= 0:
        return ""
    minimum = frame_height * CAPTION_MIN_HEIGHT_RATIO
    big = [line for line in lines if line["height"] >= minimum]
    if not big:
        return ""
    # Preserve reading order: captions stack top to bottom.
    big.sort(key=lambda line: line["top"])
    return " ".join(line["text"] for line in big).strip()


def _clean_ocr_tsv(raw: str) -> str:
    """All confident text in a frame, as one string."""
    return _join_lines(_ocr_lines(raw))


def _similar(a: str, b: str) -> bool:
    """Loose match so OCR jitter across frames does not split one caption.

    Two signals, either of which is enough. Token overlap handles a caption
    that gains or loses a word between frames; character similarity handles the
    common case of one glyph being misread (SCROLLING -> SCROLLlNG), which on a
    short caption drags token overlap well below any useful threshold.
    """
    if not a or not b:
        return False
    if a == b:
        return True

    a_lower, b_lower = a.lower(), b.lower()
    a_tokens = set(a_lower.split())
    b_tokens = set(b_lower.split())
    if a_tokens and b_tokens:
        overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
        if overlap >= 0.6:
            return True

    return difflib.SequenceMatcher(None, a_lower, b_lower).ratio() >= 0.8


def _group_text_beats(observations: list[tuple[float, str]], step: float,
                      duration: float) -> list[TextBeat]:
    beats: list[TextBeat] = []
    current: TextBeat | None = None

    for time, text in observations:
        if not text:
            if current:
                current.end = time
                beats.append(current)
                current = None
            continue
        if current and _similar(current.text, text):
            # Keep the longer reading; OCR often clips the first frame of a fade.
            if len(text) > len(current.text):
                current.text = text
            current.end = time + step
            continue
        if current:
            current.end = time
            beats.append(current)
        current = TextBeat(start=time, end=time + step, text=text)

    if current:
        current.end = min(current.end, duration)
        beats.append(current)

    return [b for b in beats if b.end - b.start >= step * 0.9 and len(b.text) >= 3]


def analyze(
    video: Path,
    probe_info: dict,
    project_dir: Path,
    *,
    do_transcribe: bool = True,
    do_ocr: bool = True,
    do_visuals: bool = True,
    whisper_model: str = "base",
    scene_threshold: float = 0.28,
    frame_fps: float = 2.0,
) -> Analysis:
    duration = float(probe_info.get("duration") or 0.0)
    result = Analysis(probe=probe_info, frame_step=1.0 / frame_fps)

    result.cuts = detect_cuts(video, duration, scene_threshold)
    result.shot_lengths = shot_lengths_from_cuts(result.cuts, duration)
    if not result.cuts:
        result.notes.append(
            "no hard cuts detected - source may be a single continuous shot "
            "(lower --scene-threshold to catch softer transitions)"
        )

    result.energy, result.onsets, result.tempo_bpm = audio_envelope(
        video, bool(probe_info.get("has_audio"))
    )
    if not probe_info.get("has_audio"):
        result.notes.append("source has no audio track")

    if do_transcribe:
        segments, text, note = transcribe(video, project_dir, whisper_model)
        result.transcript = segments
        result.transcript_text = text
        if note:
            result.notes.append(note)

    if do_ocr or do_visuals:
        result.frames = sample_frames(video, project_dir, frame_fps)
        if not result.frames:
            result.notes.append("no frames could be sampled from the video")

    text_by_time: dict[float, str] = {}
    if do_ocr and result.frames:
        text_by_time, overlay_by_time, note = ocr_frames(result.frames)
        if note:
            result.notes.append(note)
        result.ocr_beats = text_beats_from_ocr(text_by_time, result.frame_step, duration)
        result.overlay_beats = text_beats_from_ocr(
            overlay_by_time, result.frame_step, duration)

    if do_visuals and result.frames:
        if not visuals.PILLOW_AVAILABLE:
            result.notes.append(
                "Pillow not installed - skipping diagram/image detection "
                "(pip3 install Pillow)"
            )
        else:
            result.visual_verdicts = visuals.classify_frames(result.frames, text_by_time)

    return result
