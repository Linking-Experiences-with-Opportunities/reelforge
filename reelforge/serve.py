"""Local helper: lets a web page drive reelforge on this machine.

`reelforge serve` listens on 127.0.0.1 only. A page (marketing.pathion.dev)
talks to it with fetch(); the footage never leaves the computer. Each job is
the ordinary CLI run as a subprocess, so the page and the terminal behave the
same.

Who may talk to it:
  * the Host header must be this loopback address (stops DNS rebinding);
  * a browser's Origin header must be on the allow list;
  * every route except /health and /pair needs the pairing token, which the
    page gets only after the person clicks Allow in a native dialog.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from . import recipe as recipe_mod
from .render import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from .shell import have

DEFAULT_PORT = 47821
DEFAULT_ORIGINS = ("https://marketing.pathion.dev",)
STATE_DIR = Path.home() / ".reelforge"
OUTPUT_DIR = Path.home() / "Movies" / "Reelforge"
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
BROWSERS = {"safari", "chrome", "firefox", "edge", "brave"}
FITS = {"cover", "contain", "blur"}
AUDIO_MODES = {"music", "source", "silent"}
MAX_BODY = 4 * 1024 * 1024
MAX_LOG_LINES = 4000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_PROJECT_LINE = re.compile(r"^-> project (.+)$")
_OUT_LINE = re.compile(r"^-> (.+\.mp4)\s+\(")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def host_allowed(host: str | None, port: int) -> bool:
    return (host or "").lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    """No Origin (curl, the CLI) is fine: such a caller still needs the token."""
    return origin is None or origin in allowed


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and ".." not in name


def safe_child(root: Path, relative: str) -> Path:
    """`relative` resolved under `root`, refusing anything that escapes it."""
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise ApiError(400, "that path is outside the project")
    return target


def _paths(value, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ApiError(400, f"{label} must be a list of paths")
    return [str(Path(v).expanduser()) for v in value]


def ingest_args(body: dict, projects: Path) -> list[str]:
    source = str(body.get("source") or "").strip()
    if not source:
        raise ApiError(400, "give a link or pick a video file")
    if source.startswith("-"):
        raise ApiError(400, "that is not a link or a file")
    args = ["ingest", source, "--projects", str(projects)]
    browser = str(body.get("browser") or "").lower()
    if browser:
        if browser not in BROWSERS:
            raise ApiError(400, f"unknown browser: {browser}")
        args += ["--cookies-from-browser", browser]
    for key in ("start", "duration"):
        value = str(body.get(key) or "").strip()
        if value:
            if not re.fullmatch(r"[0-9:.]{1,12}", value):
                raise ApiError(400, f"{key} must look like 1:12:30 or 45")
            args += [f"--{key}", value]
    name = str(body.get("name") or "").strip()
    if name:
        if not valid_name(name):
            raise ApiError(400, "project names use letters, numbers, dots and dashes")
        args += ["--name", name]
    return args


def render_args(body: dict, projects: Path, out_dir: Path, stamp: str) -> tuple[list[str], Path]:
    name = str(body.get("project") or "")
    if not valid_name(name):
        raise ApiError(400, "unknown project")
    recipe_path = projects / name / "recipe.json"
    if not recipe_path.is_file():
        raise ApiError(404, f"project {name} has no recipe")
    clips = _paths(body.get("clips"), "clips")
    assets = _paths(body.get("assets"), "assets")
    if not clips and not assets:
        raise ApiError(400, "pick the footage to build the video from")
    out = out_dir / f"{name}-{stamp}.mp4"
    args = ["render", "--recipe", str(recipe_path), "--out", str(out)]
    if clips:
        args += ["--clips", *clips]
    if assets:
        args += ["--assets", *assets]
    music = str(body.get("music") or "").strip()
    if music:
        args += ["--music", str(Path(music).expanduser())]
    fit = str(body.get("fit") or "")
    if fit:
        if fit not in FITS:
            raise ApiError(400, f"unknown fit: {fit}")
        args += ["--fit", fit]
    mode = str(body.get("audio_mode") or "")
    if mode:
        if mode not in AUDIO_MODES:
            raise ApiError(400, f"unknown audio mode: {mode}")
        args += ["--audio-mode", mode]
    if body.get("captions") is False:
        args.append("--no-captions")
    return args, out


def tool_status() -> dict:
    return {tool: have(tool) for tool in ("ffmpeg", "ffprobe", "yt-dlp", "whisper", "tesseract")}


def project_summary(folder: Path) -> dict | None:
    recipe_path = folder / "recipe.json"
    if not recipe_path.is_file():
        return None
    try:
        data = recipe_mod.load_recipe(recipe_path)
    except ValueError:
        return None
    source = data.get("source") or {}
    target = data.get("target") or {}
    return {
        "name": folder.name,
        "modified": recipe_path.stat().st_mtime,
        "platform": source.get("platform"),
        "origin": source.get("origin"),
        "duration": target.get("duration"),
        "width": target.get("width"),
        "height": target.get("height"),
        "segments": len(data.get("segments") or []),
        "assets_required": (data.get("assets") or {}).get("required", 0),
        "has_sheet": (folder / "contact-sheet.jpg").is_file(),
    }


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

class Job:
    def __init__(self, kind: str, args: list[str], result: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.args = args
        self.status = "running"
        self.log: list[str] = []
        self.result: dict = dict(result or {})
        self.started = time.time()
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "reelforge", *self.args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, text=True, errors="replace",
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            with self.lock:
                if len(self.log) < MAX_LOG_LINES:
                    self.log.append(line)
                found = _PROJECT_LINE.match(line)
                if found and self.kind == "ingest":
                    self.result["project"] = Path(found.group(1).strip()).name
        code = self.proc.wait()
        with self.lock:
            if self.status == "running":
                self.status = "done" if code == 0 else "failed"
            self.result["exit_code"] = code

    def cancel(self) -> None:
        with self.lock:
            if self.status != "running":
                return
            self.status = "cancelled"
        if self.proc:
            self.proc.terminate()

    def view(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "log": self.log[since:], "next": len(self.log),
                "result": dict(self.result), "seconds": round(time.time() - self.started, 1),
            }


# --------------------------------------------------------------------------- #
# native dialogs (macOS)
# --------------------------------------------------------------------------- #

def _osascript(script: str, timeout: float = 600) -> tuple[int, str]:
    if platform.system() != "Darwin":
        raise ApiError(501, "file pickers are only built for macOS so far")
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip()


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


PICK_KINDS = {
    "folder": (None, False),
    "video": (VIDEO_SUFFIXES, False),
    "media": (VIDEO_SUFFIXES | IMAGE_SUFFIXES, True),
    "images": (IMAGE_SUFFIXES | VIDEO_SUFFIXES, True),
    "audio": (AUDIO_SUFFIXES, False),
}


def pick(kind: str, prompt: str) -> list[str]:
    if kind not in PICK_KINDS:
        raise ApiError(400, f"unknown picker: {kind}")
    suffixes, multiple = PICK_KINDS[kind]
    prompt = _quote(prompt[:120] or "Choose")
    if suffixes is None:
        script = f'tell me to activate\nreturn POSIX path of (choose folder with prompt {prompt})'
    else:
        types = "{" + ", ".join(_quote(s.lstrip(".")) for s in sorted(suffixes)) + "}"
        many = " with multiple selections allowed" if multiple else ""
        script = (
            "tell me to activate\n"
            f"set picked to choose file with prompt {prompt} of type {types}{many}\n"
            "if class of picked is not list then set picked to {picked}\n"
            'set out to ""\n'
            "repeat with f in picked\n"
            "set out to out & POSIX path of f & linefeed\n"
            "end repeat\n"
            "return out"
        )
    code, out = _osascript(script)
    if code != 0:
        return []  # the person pressed Cancel
    return [line for line in out.splitlines() if line.strip()]


def ask_to_pair(origin: str) -> bool:
    text = (f"Allow {origin} to use Reelforge on this Mac?\\n\\n"
            "It will be able to read the videos you pick and save new ones to Movies/Reelforge.")
    script = (
        "tell me to activate\n"
        f'set answer to display dialog "{text}" with title "Reelforge" '
        'buttons {"Don\'t Allow", "Allow"} default button "Allow" giving up after 120\n'
        "return button returned of answer"
    )
    code, out = _osascript(script, timeout=150)
    return code == 0 and out == "Allow"


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #

def load_token(state_dir: Path) -> str:
    path = state_dir / "helper.json"
    try:
        token = json.loads(path.read_text("utf-8")).get("token")
        if token:
            return token
    except (OSError, ValueError):
        pass
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(json.dumps({"token": token}) + "\n", "utf-8")
    path.chmod(0o600)
    return token


class Helper:
    def __init__(self, projects: Path, *, port: int = DEFAULT_PORT,
                 origins: tuple[str, ...] = DEFAULT_ORIGINS, token: str | None = None,
                 out_dir: Path = OUTPUT_DIR, state_dir: Path = STATE_DIR):
        self.projects = projects
        self.port = port
        self.origins = tuple(origins)
        self.token = token or load_token(state_dir)
        self.out_dir = out_dir
        self.jobs: dict[str, Job] = {}
        self.pair_lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None

    # --- routes ----------------------------------------------------------- #

    def health(self) -> dict:
        tools = tool_status()
        return {
            "ok": True, "version": __version__, "platform": platform.system(),
            "tools": tools, "ready": tools["ffmpeg"] and tools["ffprobe"],
            "projects_dir": str(self.projects), "output_dir": str(self.out_dir),
        }

    def list_projects(self) -> list[dict]:
        if not self.projects.is_dir():
            return []
        found = [project_summary(p) for p in self.projects.iterdir() if p.is_dir()]
        return sorted((f for f in found if f), key=lambda f: -f["modified"])

    def _project_dir(self, name: str) -> Path:
        if not valid_name(name) or not (self.projects / name / "recipe.json").is_file():
            raise ApiError(404, "no such project")
        return self.projects / name

    def get_project(self, name: str) -> dict:
        folder = self._project_dir(name)
        try:
            data = recipe_mod.load_recipe(folder / "recipe.json")
        except ValueError as exc:
            raise ApiError(422, str(exc)) from exc
        try:
            summary = recipe_mod.describe(data)
        except (KeyError, TypeError, ValueError):
            summary = ""  # a hand-edited recipe can lack the parts the summary reads
        return {"name": name, "dir": str(folder), "recipe": data, "summary": summary}

    def put_recipe(self, name: str, body: dict) -> dict:
        folder = self._project_dir(name)
        data = body.get("recipe")
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise ApiError(400, "that is not a recipe (no segments)")
        if data.get("version") != recipe_mod.RECIPE_VERSION:
            raise ApiError(400, "recipe version does not match this build")
        path = folder / "recipe.json"
        backup = folder / "recipe.before-edit.json"
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        recipe_mod.save_recipe(data, path)
        return self.get_project(name)

    def start_job(self, kind: str, args: list[str], result: dict | None = None) -> dict:
        job = Job(kind, args, result)
        self.jobs[job.id] = job
        job.start()
        return job.view()

    def reveal(self, body: dict, *, open_it: bool) -> dict:
        path = Path(str(body.get("path") or "")).expanduser().resolve()
        allowed = (self.out_dir.resolve(), self.projects.resolve())
        if not path.exists() or not any(root == path or root in path.parents for root in allowed):
            raise ApiError(400, "only files reelforge made can be opened from the page")
        if platform.system() != "Darwin":
            raise ApiError(501, "opening files is only built for macOS so far")
        subprocess.run(["open", str(path)] if open_it else ["open", "-R", str(path)], check=False)
        return {"ok": True}

    def pair(self, origin: str | None) -> dict:
        if not origin:
            raise ApiError(400, "pairing is started from the web page")
        if not self.pair_lock.acquire(blocking=False):
            raise ApiError(409, "a pairing request is already on screen")
        try:
            if not ask_to_pair(origin):
                raise ApiError(403, "pairing was not allowed")
            return {"token": self.token}
        finally:
            self.pair_lock.release()

    def restart(self) -> dict:
        """Exit so launchd starts the helper again; its wrapper pulls updates first."""
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True}

    # --- dispatch --------------------------------------------------------- #

    def handle(self, method: str, path: str, query: dict, body: dict, origin: str | None):
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        if method == "GET" and parts == ["health"]:
            return self.health()
        if method == "POST" and parts == ["pair"]:
            return self.pair(origin)
        if method == "GET" and parts == ["projects"]:
            return {"projects": self.list_projects()}
        if len(parts) == 2 and parts[0] == "projects" and method == "GET":
            return self.get_project(parts[1])
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "recipe" and method == "PUT":
            return self.put_recipe(parts[1], body)
        if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "file" and method == "GET":
            target = safe_child(self._project_dir(parts[1]), "/".join(parts[3:]))
            if target.suffix.lower() not in {".jpg", ".jpeg", ".png"} or not target.is_file():
                raise ApiError(404, "no such image")
            return target
        if method == "POST" and parts == ["pick"]:
            return {"paths": pick(str(body.get("kind") or ""), str(body.get("prompt") or ""))}
        if method == "POST" and parts == ["ingest"]:
            return self.start_job("ingest", ingest_args(body, self.projects))
        if method == "POST" and parts == ["render"]:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            args, out = render_args(body, self.projects, self.out_dir, time.strftime("%Y%m%d-%H%M%S"))
            return self.start_job("render", args, {"out": str(out), "project": body.get("project")})
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            job = self.jobs.get(parts[1])
            if not job:
                raise ApiError(404, "no such job")
            try:
                since = max(0, int((query.get("since") or ["0"])[0]))
            except ValueError:
                since = 0
            return job.view(since)
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            job = self.jobs.get(parts[1])
            if not job:
                raise ApiError(404, "no such job")
            job.cancel()
            return job.view()
        if method == "POST" and parts == ["reveal"]:
            return self.reveal(body, open_it=False)
        if method == "POST" and parts == ["open"]:
            return self.reveal(body, open_it=True)
        if method == "POST" and parts == ["restart"]:
            return self.restart()
        raise ApiError(404, "not found")

    def make_server(self) -> ThreadingHTTPServer:
        helper = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # keep the launchd log quiet
                pass

            def _cors(self, origin: str | None) -> None:
                if origin and origin in helper.origins:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                    self.send_header("Access-Control-Max-Age", "600")

            def _send(self, status: int, payload, origin: str | None) -> None:
                if isinstance(payload, Path):
                    data = payload.read_bytes()
                    kind = "image/png" if payload.suffix.lower() == ".png" else "image/jpeg"
                else:
                    data = json.dumps(payload).encode("utf-8")
                    kind = "application/json"
                self.send_response(status)
                self._cors(origin)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _serve(self, method: str) -> None:
                origin = self.headers.get("Origin")
                try:
                    if not host_allowed(self.headers.get("Host"), helper.port):
                        raise ApiError(403, "wrong host")
                    if not origin_allowed(origin, helper.origins):
                        raise ApiError(403, "this site may not use the helper")
                    if method == "OPTIONS":
                        self.send_response(204)
                        self._cors(origin)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    url = urlparse(self.path)
                    open_route = (method, url.path.rstrip("/")) in {("GET", "/health"), ("POST", "/pair")}
                    if not open_route:
                        given = self.headers.get("Authorization", "")
                        if not secrets.compare_digest(given, f"Bearer {helper.token}"):
                            raise ApiError(401, "not paired")
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "request too large")
                    body = {}
                    if length:
                        try:
                            body = json.loads(self.rfile.read(length).decode("utf-8"))
                        except ValueError as exc:
                            raise ApiError(400, "body is not JSON") from exc
                        if not isinstance(body, dict):
                            raise ApiError(400, "body must be an object")
                    self._send(200, helper.handle(method, url.path, parse_qs(url.query), body, origin), origin)
                except ApiError as exc:
                    self._send(exc.status, {"error": exc.message}, origin)
                except subprocess.TimeoutExpired:
                    self._send(408, {"error": "nobody answered the dialog"}, origin)
                except Exception as exc:  # noqa: BLE001 - the page needs an answer either way
                    self._send(500, {"error": f"{type(exc).__name__}: {exc}"}, origin)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_PUT(self):
                self._serve("PUT")

            def do_OPTIONS(self):
                self._serve("OPTIONS")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        return self.server


def serve(projects: Path, port: int, origins: list[str] | None) -> int:
    helper = Helper(projects, port=port, origins=tuple(origins or DEFAULT_ORIGINS))
    server = helper.make_server()
    print(f"reelforge helper {__version__} on http://127.0.0.1:{port}", flush=True)
    print(f"  pages allowed: {', '.join(helper.origins)}", flush=True)
    print(f"  projects: {projects}   renders: {helper.out_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
