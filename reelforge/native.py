"""The parts of the helper that talk to the operating system: dialogs, file
pickers, opening files, and stopping a job with everything it started.

macOS uses AppleScript (osascript). Windows uses PowerShell with Windows Forms,
which every Windows 10 and 11 install has. Text that came from the page never
becomes script source: on Windows it travels in environment variables, on
macOS it is quoted.
"""

from __future__ import annotations

import base64
import os
import platform
import re
import signal
import subprocess
from pathlib import Path

SYSTEM = platform.system()
IS_MAC = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"
# How messages name the computer the helper runs on.
MACHINE = "this Mac" if IS_MAC else "this PC" if IS_WINDOWS else "this computer"
TERMINAL = "PowerShell" if IS_WINDOWS else "Terminal"
ENTER_KEY = "Enter" if IS_WINDOWS else "Return"
# Windows: keep child consoles hidden when the helper itself runs without a window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DIALOG_SECONDS = 120


class NativeError(RuntimeError):
    """Something this computer cannot do (yet); the message is shown on the page."""


def default_output_dir() -> Path:
    """Where renders go: Movies on a Mac, Videos on Windows."""
    return Path.home() / ("Videos" if IS_WINDOWS else "Movies") / "Reelforge"


def clean_folder(path: str) -> str:
    """A picked folder without its trailing slash, except a root: "C:" alone means
    the current folder on drive C, not the drive, so "C:\\" stays as it is."""
    stripped = path.rstrip("/\\")
    if not stripped:
        return path[:1]
    if re.fullmatch(r"[A-Za-z]:", stripped):
        return stripped + "\\"
    return stripped


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #

def group_kwargs() -> dict:
    """Popen arguments that put a job in its own group, so cancel reaches what it started."""
    if IS_WINDOWS:
        return {"creationflags": NEW_GROUP | NO_WINDOW}
    return {"start_new_session": True}


def hidden_kwargs() -> dict:
    """Popen/run arguments for a short helper command (no console window on Windows)."""
    return {"creationflags": NO_WINDOW} if IS_WINDOWS else {}


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a job started with group_kwargs() and everything under it."""
    if IS_WINDOWS:
        # taskkill /T walks the child tree (agent -> shell -> browser -> ffmpeg).
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, **hidden_kwargs())
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #

def _osascript(script: str, timeout: float = 600) -> tuple[int, str]:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip()


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mac_pick(suffixes: set[str] | None, multiple: bool, prompt: str) -> list[str]:
    prompt = _quote(prompt)
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


def _mac_confirm(title: str, text: str, default_allow: bool) -> bool:
    default = '"Allow"' if default_allow else '"Don\'t Allow"'
    script = (
        "tell me to activate\n"
        f"set answer to display dialog {_quote(text)} with title {_quote(title)} "
        f'buttons {{"Don\'t Allow", "Allow"}} default button {default} giving up after {DIALOG_SECONDS}\n'
        "return button returned of answer"
    )
    code, out = _osascript(script, timeout=DIALOG_SECONDS + 30)
    return code == 0 and out == "Allow"


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

# Every script reads its inputs from RF_* environment variables, never from its own source.
_PS_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
[Windows.Forms.Application]::EnableVisualStyles()
# An invisible, always-on-top owner so the window comes up in front of the browser.
$owner = New-Object Windows.Forms.Form
$owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'; $owner.Size = New-Object Drawing.Size(1, 1)
$owner.Show(); $owner.Activate()
"""

_PS_PICK = _PS_PRELUDE + r"""
if ($env:RF_FOLDER -eq '1') {
  $d = New-Object Windows.Forms.FolderBrowserDialog
  $d.Description = $env:RF_PROMPT; $d.ShowNewFolderButton = $true
  if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }
} else {
  $d = New-Object Windows.Forms.OpenFileDialog
  $d.Title = $env:RF_PROMPT; $d.Filter = $env:RF_FILTER
  $d.Multiselect = ($env:RF_MULTIPLE -eq '1'); $d.CheckFileExists = $true
  if ($d.ShowDialog($owner) -eq 'OK') { $d.FileNames }
}
"""

_PS_CONFIRM = _PS_PRELUDE + r"""
$form = New-Object Windows.Forms.Form
$form.Text = $env:RF_TITLE; $form.TopMost = $true; $form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'; $form.MaximizeBox = $false; $form.MinimizeBox = $false
$form.AutoSize = $true; $form.AutoSizeMode = 'GrowAndShrink'; $form.Padding = New-Object Windows.Forms.Padding(16)
$form.Font = New-Object Drawing.Font('Segoe UI', 10)
$layout = New-Object Windows.Forms.FlowLayoutPanel
$layout.FlowDirection = 'TopDown'; $layout.AutoSize = $true; $layout.WrapContents = $false
$text = New-Object Windows.Forms.Label
$text.Text = $env:RF_TEXT; $text.AutoSize = $true; $text.MaximumSize = New-Object Drawing.Size(480, 0)
$text.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 16)
$buttons = New-Object Windows.Forms.FlowLayoutPanel
$buttons.FlowDirection = 'RightToLeft'; $buttons.AutoSize = $true; $buttons.Anchor = 'Right'
$allow = New-Object Windows.Forms.Button
$allow.Text = 'Allow'; $allow.DialogResult = 'OK'; $allow.AutoSize = $true; $allow.MinimumSize = New-Object Drawing.Size(110, 34)
$deny = New-Object Windows.Forms.Button
$deny.Text = "Don't Allow"; $deny.DialogResult = 'Cancel'; $deny.AutoSize = $true; $deny.MinimumSize = New-Object Drawing.Size(110, 34)
$buttons.Controls.AddRange(@($allow, $deny))
$layout.Controls.AddRange(@($text, $buttons))
$form.Controls.Add($layout)
$form.CancelButton = $deny
if ($env:RF_DEFAULT_ALLOW -eq '1') { $form.AcceptButton = $allow; $form.ActiveControl = $allow }
else { $form.AcceptButton = $deny; $form.ActiveControl = $deny }
# Nobody answered: the same as Don't Allow, like the Mac dialog's "giving up after".
$timer = New-Object Windows.Forms.Timer
$timer.Interval = [int]$env:RF_SECONDS * 1000
$timer.Add_Tick({ $timer.Stop(); $form.DialogResult = 'Cancel'; $form.Close() })
$form.Add_Shown({ $form.Activate(); $timer.Start() })
if ($form.ShowDialog($owner) -eq 'OK') { 'Allow' } else { 'Deny' }
"""


def _powershell(script: str, env: dict[str, str], timeout: float) -> tuple[int, str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout, env={**os.environ, **env},
            stdin=subprocess.DEVNULL, **hidden_kwargs())
    except FileNotFoundError as exc:
        raise NativeError("PowerShell is missing on this PC") from exc
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def _filter(suffixes: set[str]) -> str:
    patterns = ";".join(f"*{s}" for s in sorted(suffixes))
    return f"Supported files|{patterns}|All files|*.*"


def _windows_pick(suffixes: set[str] | None, multiple: bool, prompt: str) -> list[str]:
    env = {"RF_PROMPT": prompt, "RF_FOLDER": "1" if suffixes is None else "0",
           "RF_FILTER": _filter(suffixes) if suffixes else "", "RF_MULTIPLE": "1" if multiple else "0"}
    code, out = _powershell(_PS_PICK, env, timeout=600)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _windows_confirm(title: str, text: str, default_allow: bool) -> bool:
    env = {"RF_TITLE": title, "RF_TEXT": text, "RF_DEFAULT_ALLOW": "1" if default_allow else "0",
           "RF_SECONDS": str(DIALOG_SECONDS)}
    try:
        code, out = _powershell(_PS_CONFIRM, env, timeout=DIALOG_SECONDS + 30)
    except subprocess.TimeoutExpired:
        return False
    return code == 0 and out.splitlines()[-1:] == ["Allow"]


# --------------------------------------------------------------------------- #
# what the helper calls
# --------------------------------------------------------------------------- #

def pick(suffixes: set[str] | None, multiple: bool, prompt: str) -> list[str]:
    """A native file (or folder, when suffixes is None) picker. [] when the person cancels."""
    prompt = prompt[:120] or "Choose"
    if IS_MAC:
        return _mac_pick(suffixes, multiple, prompt)
    if IS_WINDOWS:
        return _windows_pick(suffixes, multiple, prompt)
    raise NativeError("file pickers are built for macOS and Windows only")


def confirm(title: str, text: str, *, default_allow: bool) -> bool:
    """An Allow / Don't Allow window. Gives up (as Don't Allow) after two minutes."""
    if IS_MAC:
        return _mac_confirm(title, text, default_allow)
    if IS_WINDOWS:
        return _windows_confirm(title, text, default_allow)
    raise NativeError("permission windows are built for macOS and Windows only")


def reveal(path: Path, *, open_it: bool) -> None:
    """Open a file, or show it selected in Finder / File Explorer."""
    if IS_MAC:
        subprocess.run(["open", str(path)] if open_it else ["open", "-R", str(path)], check=False)
    elif IS_WINDOWS:
        if open_it:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
        else:
            # explorer wants "/select,<path>" as one argument, and exits 1 even when it worked.
            subprocess.run(f'explorer.exe /select,"{path}"', check=False)
    else:
        raise NativeError("opening files is built for macOS and Windows only")
