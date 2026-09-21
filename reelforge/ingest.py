"""Resolve a source (local file or social URL) into a project workspace."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .shell import TimeoutError_, ToolError, ffprobe_json, have, require, run

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

# Platforms we explicitly know how to name. Anything else still goes through
# yt-dlp; this map only drives the project slug and the cookie hint.
_PLATFORM_PATTERNS = [
    ("instagram", re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")),
    ("tiktok", re.compile(r"tiktok\.com/.*?/video/(\d+)")),
    ("tiktok", re.compile(r"vm\.tiktok\.com/([A-Za-z0-9]+)")),
    ("youtube", re.compile(r"(?:youtube\.com/shorts/|youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{6,})")),
]

COOKIE_HINT = (
    "Instagram/TikTok usually refuse logged-out downloads. Re-run with\n"
    "  --cookies-from-browser chrome      (or firefox, safari, edge, brave)\n"
    "so yt-dlp can reuse the session you already have in that browser."
)

# On macOS, reading Chrome/Edge/Brave cookies needs the "Chrome Safe Storage"
# key from the login Keychain, which raises a GUI prompt. In a non-interactive
# shell nothing answers it and yt-dlp blocks indefinitely, so downloads are
# capped by a timeout that explains this rather than hanging forever.
KEYCHAIN_HINT = (
    "This usually means yt-dlp is waiting on a macOS Keychain prompt.\n"
    "Reading Chrome/Edge/Brave cookies needs the 'Chrome Safe Storage' key, and\n"
    "the prompt cannot be answered from a non-interactive shell.\n"
    "\n"
    "Any of these work:\n"
    "  - run the same command yourself in a terminal and click Allow once\n"
    "  - use Firefox instead: --cookies-from-browser firefox (no Keychain)\n"
    "  - export a cookies.txt with a browser extension and pass --cookies FILE\n"
    "  - download the mp4 by hand and ingest the local file"
)

DEFAULT_DOWNLOAD_TIMEOUT = 180.0


@dataclass
class Source:
    """A resolved source video plus where its project lives."""

    project_dir: Path
    video: Path
    origin: str
    platform: str = "local"
    meta: dict = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return self.project_dir.name


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def classify_url(url: str) -> tuple[str, str | None]:
    """Return (platform, post_id) for a URL, best effort."""
    for platform, pattern in _PLATFORM_PATTERNS:
        match = pattern.search(url)
        if match:
            return platform, match.group(1)
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "web", None


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return (slug or "source")[:60]


def _unique_dir(root: Path, slug: str) -> Path:
    candidate = root / slug
    counter = 2
    while candidate.exists() and any(candidate.iterdir()):
        candidate = root / f"{slug}-{counter}"
        counter += 1
    return candidate


def ingest(
    source: str,
    *,
    projects_root: Path,
    cookies_from_browser: str | None = None,
    cookies_file: Path | None = None,
    name: str | None = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
) -> Source:
    """Download or copy `source` into a fresh project directory."""
    projects_root.mkdir(parents=True, exist_ok=True)

    if is_url(source):
        platform, post_id = classify_url(source)
        slug = slugify(name or f"{platform}-{post_id or 'post'}")
        project_dir = _unique_dir(projects_root, slug)
        project_dir.mkdir(parents=True, exist_ok=True)
        video, meta = _download(
            source,
            project_dir,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            timeout=timeout,
        )
        return Source(project_dir, video, source, platform, meta)

    src_path = Path(source).expanduser().resolve()
    if not src_path.exists():
        raise ToolError(f"no such file: {src_path}")
    if src_path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ToolError(
            f"unsupported file type '{src_path.suffix}'. "
            f"Expected one of: {', '.join(sorted(VIDEO_SUFFIXES))}"
        )

    slug = slugify(name or src_path.stem)
    project_dir = _unique_dir(projects_root, slug)
    project_dir.mkdir(parents=True, exist_ok=True)
    dest = project_dir / f"source{src_path.suffix.lower()}"
    shutil.copy2(src_path, dest)
    return Source(project_dir, dest, str(src_path), "local", {})


def _download(
    url: str,
    project_dir: Path,
    *,
    cookies_from_browser: str | None,
    cookies_file: Path | None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
) -> tuple[Path, dict]:
    require("yt-dlp", "install with: brew install yt-dlp")

    out_template = str(project_dir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--write-info-json",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", out_template,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    cmd.append(url)

    try:
        proc = run(cmd, check=False, timeout=timeout)
    except TimeoutError_ as exc:
        message = f"{exc}"
        if cookies_from_browser in {"chrome", "edge", "brave", "chromium"}:
            message += f"\n\n{KEYCHAIN_HINT}"
        else:
            message += ("\n\nThe download may just be slow; raise the budget with "
                        "--download-timeout SECONDS.")
        raise ToolError(message) from exc

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        needs_auth = any(
            token in err
            for token in ("empty media response", "login required", "rate-limit",
                          "Requested content is not available", "cookies")
        )
        message = f"download failed for {url}\n{err[-1500:]}"
        if needs_auth and not (cookies_from_browser or cookies_file):
            message += f"\n\n{COOKIE_HINT}"
        raise ToolError(message)

    videos = [
        p for p in sorted(project_dir.iterdir())
        if p.suffix.lower() in VIDEO_SUFFIXES and p.stem == "source"
    ]
    if not videos:
        raise ToolError(f"yt-dlp reported success but produced no video in {project_dir}")
    video = videos[0]

    meta = {}
    info_files = list(project_dir.glob("source.info.json"))
    if info_files:
        try:
            raw = json.loads(info_files[0].read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        meta = {
            key: raw.get(key)
            for key in ("id", "title", "description", "uploader", "channel",
                        "duration", "view_count", "like_count", "upload_date",
                        "webpage_url", "extractor")
            if raw.get(key) is not None
        }
    return video, meta


def probe(video: Path) -> dict:
    """Normalized probe of a video file."""
    info = ffprobe_json(video)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise ToolError(f"{video} has no video stream")

    fps = 30.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        num, _, den = rate.partition("/")
        if den and float(den) != 0:
            fps = float(num) / float(den)
    except ValueError:
        pass

    duration = 0.0
    for candidate in (info.get("format", {}).get("duration"), v.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 4),
        "duration": round(duration, 3),
        "aspect": f"{width}x{height}",
        "orientation": _orientation(width, height),
        "video_codec": v.get("codec_name"),
        "audio_codec": (a or {}).get("codec_name"),
        "has_audio": a is not None,
        "sample_rate": int((a or {}).get("sample_rate") or 0) or None,
    }


def _orientation(width: int, height: int) -> str:
    if not width or not height:
        return "unknown"
    ratio = width / height
    if ratio < 0.85:
        return "vertical"
    if ratio > 1.2:
        return "horizontal"
    return "square"


def yt_dlp_available() -> bool:
    return have("yt-dlp")
