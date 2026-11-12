#!/usr/bin/env bash
# One-step setup for people who will never open a terminal again (macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/Linking-Experiences-with-Opportunities/reelforge/main/install.sh | bash
#
# Installs everything reelforge needs, then starts the local helper that
# marketing.pathion.dev talks to, and keeps it running across restarts.
# Safe to run again: it updates what is there.
#
#   REELFORGE_SRC=/path/to/checkout   use an existing checkout (never pulled)
#   REELFORGE_REF=branch              install a branch other than main
#   REELFORGE_ORIGINS="url url"       extra pages allowed to use the helper
#   REELFORGE_SKIP_WHISPER=1          skip speech transcription (about 2 GB)
set -euo pipefail

REPO="https://github.com/Linking-Experiences-with-Opportunities/reelforge.git"
HOME_DIR="$HOME/.reelforge"
SRC="${REELFORGE_SRC:-$HOME_DIR/src}"
REF="${REELFORGE_REF:-main}"
LABEL="dev.pathion.reelforge-helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=47821

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "This installer is for macOS. A Windows one is on the way."
mkdir -p "$HOME_DIR"

# ---- Homebrew -------------------------------------------------------------
BREW="$(command -v brew || true)"
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -n "$BREW" ] || { [ -x "$candidate" ] && BREW="$candidate"; }
done
if [ -z "$BREW" ]; then
  say "Installing Homebrew (it will ask for your Mac password once)"
  sudo -v </dev/tty || fail "Homebrew needs your Mac password to install."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$candidate" ] && BREW="$candidate"
  done
  [ -n "$BREW" ] || fail "Homebrew did not install. Send this window's text to Max."
fi
eval "$("$BREW" shellenv)"
export PATH="$HOME/.local/bin:$PATH"

# ---- video tools ----------------------------------------------------------
say "Installing the video tools (ffmpeg, yt-dlp, tesseract)"
for formula in ffmpeg yt-dlp tesseract git uv; do
  command -v "$formula" >/dev/null 2>&1 || brew install "$formula"
done

# ---- reelforge ------------------------------------------------------------
if [ -z "${REELFORGE_SRC:-}" ]; then
  say "Getting reelforge"
  if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --quiet origin "$REF"
    git -C "$SRC" checkout --quiet -B "$REF" "origin/$REF"
  else
    git clone --quiet --branch "$REF" "$REPO" "$SRC"
  fi
fi
[ -f "$SRC/pyproject.toml" ] || fail "No reelforge checkout at $SRC"
uv tool install --quiet --force --python 3.12 -e "$SRC"

if [ -z "${REELFORGE_SKIP_WHISPER:-}" ] && ! command -v whisper >/dev/null 2>&1; then
  say "Installing speech transcription (large download, a few minutes)"
  uv tool install --quiet --python 3.12 openai-whisper \
    || echo "Transcription did not install; reelforge works without it."
fi

# ---- the helper -----------------------------------------------------------
ORIGIN_FLAGS="--allow-origin https://marketing.pathion.dev"
for origin in ${REELFORGE_ORIGINS:-}; do ORIGIN_FLAGS="$ORIGIN_FLAGS --allow-origin $origin"; done
PULL=""
if [ -z "${REELFORGE_SRC:-}" ]; then
  # Editable install: a pull is the whole update. Never let it block startup.
  PULL="before=\$(git -C \"$SRC\" rev-parse HEAD 2>/dev/null || true)
( git -C \"$SRC\" fetch --quiet origin \"$REF\" && git -C \"$SRC\" merge --quiet --ff-only \"origin/$REF\" ) >/dev/null 2>&1 || true
[ \"\$before\" = \"\$(git -C \"$SRC\" rev-parse HEAD 2>/dev/null || true)\" ] || uv tool install --quiet --force --python 3.12 -e \"$SRC\" || true"
fi
cat > "$HOME_DIR/run-helper.sh" <<EOF
#!/usr/bin/env bash
# Written by install.sh. Started by launchd at login and whenever it exits.
export PATH="\$HOME/.local/bin:$(dirname "$BREW"):/usr/bin:/bin:/usr/sbin:/sbin"
$PULL
exec reelforge serve --port $PORT $ORIGIN_FLAGS
EOF
chmod +x "$HOME_DIR/run-helper.sh"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HOME_DIR/run-helper.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOME_DIR/helper.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/helper.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "Starting the helper"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    say "Done. Go back to marketing.pathion.dev, open Tools, and click Connect."
    exit 0
  fi
  sleep 1
done
fail "The helper did not start. Send $HOME_DIR/helper.log to Max."
