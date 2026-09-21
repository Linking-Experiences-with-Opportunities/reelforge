"""Thin wrappers around the external binaries reelforge depends on."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    pass


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def require(tool: str, hint: str = "") -> str:
    path = shutil.which(tool)
    if not path:
        msg = f"required tool '{tool}' not found on PATH"
        if hint:
            msg += f"\n  {hint}"
        raise ToolError(msg)
    return path


class TimeoutError_(ToolError):
    """A subprocess exceeded its time budget."""


def run(cmd: list[str], *, capture: bool = True, check: bool = True,
        stdin_bytes: bytes | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_bytes,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError_(
            f"'{cmd[0]}' did not finish within {timeout:.0f}s and was stopped."
        ) from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-2000:]
        raise ToolError(f"command failed ({proc.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return proc


def ffprobe_json(path: Path) -> dict:
    require("ffprobe")
    proc = run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ])
    return json.loads(proc.stdout.decode("utf-8", "replace"))


def ffmpeg(args: list[str], *, quiet: bool = True) -> subprocess.CompletedProcess:
    require("ffmpeg")
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    if quiet:
        base += ["-loglevel", "error"]
    return run(base + args)


def ffmpeg_stderr(args: list[str]) -> str:
    """Run ffmpeg and return stderr text (for filters that report via logging)."""
    require("ffmpeg")
    proc = run(["ffmpeg", "-hide_banner", "-nostdin", "-y"] + args, check=False)
    return (proc.stderr or b"").decode("utf-8", "replace")
