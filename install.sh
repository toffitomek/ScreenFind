#!/bin/bash
# ScreenFind installer / updater for macOS.
#
#   ./install.sh               install or update, and run in the background at login
#   ./install.sh --no-service  install the command only (start it yourself with: screenshot-search --serve)
#
# Safe to re-run: after `git pull`, run it again to update.
set -euo pipefail

LABEL="com.screenfind.server"
DATA_DIR="$HOME/.screenshot-search"
VENV="$DATA_DIR/venv"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_SERVICE=1
[[ "${1:-}" == "--no-service" ]] && WITH_SERVICE=0

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

bold "ScreenFind installer"
[[ "$(uname)" == "Darwin" ]] || die "ScreenFind needs macOS (it uses Apple's Vision OCR)."

# ── 1. Python 3.11+ ──────────────────────────────────────────────────────────
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

PY=""
# Re-use the Python of an existing install so a granted Full Disk Access permission keeps working.
if [[ -x "$VENV/bin/python" ]] && py_ok "$VENV/bin/python"; then
  PY="$("$VENV/bin/python" -c 'import os,sys; print(os.path.realpath(sys._base_executable))')"
fi
if [[ -z "$PY" ]]; then
  for cand in /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 /opt/homebrew/bin/python3.12 \
              /usr/local/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 \
              /opt/homebrew/bin/python3 /usr/local/bin/python3 \
              /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if [[ -x "$cand" ]] && py_ok "$cand"; then PY="$cand"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  if command -v brew >/dev/null; then
    warn "No Python 3.11+ found — installing python@3.13 with Homebrew…"
    brew install python@3.13
    PY="$(brew --prefix)/bin/python3.13"
  else
    die "Python 3.11+ is required. Install it from https://www.python.org/downloads/macos/ (or install Homebrew, then re-run)."
  fi
fi
ok "Python: $("$PY" --version 2>&1) ($PY)"

# ── 2. Virtual environment + package ─────────────────────────────────────────
mkdir -p "$DATA_DIR/logs"
if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c 'import sys' 2>/dev/null; then
  rm -rf "$VENV"
  "$PY" -m venv "$VENV"
  ok "Created virtual environment in $VENV"
fi

# Synced folders (OneDrive, iCloud, Dropbox…) lock files and break Python imports, so
# only link to the source in place when the repo lives somewhere safe.
case "$REPO" in
  *"/Library/CloudStorage/"*|*"/Mobile Documents/"*|*"/Dropbox/"*)
    warn "This folder is synced to the cloud; installing a copy instead of linking to it."
    PIP_TARGET=("$REPO") ;;
  *)
    PIP_TARGET=(-e "$REPO") ;;
esac
PIP_LOG="$DATA_DIR/logs/install.log"
if ! { "$VENV/bin/python" -m pip install --upgrade pip && \
       "$VENV/bin/python" -m pip install --upgrade "${PIP_TARGET[@]}"; } >"$PIP_LOG" 2>&1; then
  tail -20 "$PIP_LOG"
  die "Installing Python packages failed (full log: $PIP_LOG)"
fi
ok "Installed ScreenFind $("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("screenshot-search"))')"

BIN="$VENV/bin/screenshot-search"
"$BIN" --stats >/dev/null   # creates ~/.screenshot-search/config.json on first run
PORT="$("$VENV/bin/python" -c 'from screenshot_search.config import load_config; print(load_config().get("port", 5123))')"
FDA_PROGRAM="$("$VENV/bin/python" -c 'from screenshot_search.config import python_app_path; print(python_app_path())')"

if [[ $WITH_SERVICE -eq 0 ]]; then
  bold "Done."
  echo "  Start it with:  $BIN --serve"
  echo "  Tip: alias screenfind=\"$BIN\""
  exit 0
fi

# ── 3. Background service (launchd) ──────────────────────────────────────────
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Wait for any previous server to exit so the new one gets its usual port.
for _ in $(seq 1 15); do
  pgrep -f "screenshot-search --serve" >/dev/null || break
  sleep 1
done
pkill -9 -f "screenshot-search --serve" 2>/dev/null || true

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>--serve</string>
    <string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$DATA_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DATA_DIR/logs/server.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/logs/server.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
ok "Background service installed (starts at login, restarts if it stops)"

for _ in $(seq 1 30); do
  curl -s -m 2 -o /dev/null "http://localhost:$PORT/api/stats" && break
  sleep 1
done
curl -s -m 2 -o /dev/null "http://localhost:$PORT/api/stats" \
  && ok "Running at http://localhost:$PORT" \
  || warn "Service did not answer yet — check $DATA_DIR/logs/server.log"

# ── 4. macOS privacy permission ──────────────────────────────────────────────
cat <<EOF

$(bold "One manual step: allow file access")
  macOS doesn't let background services read Desktop, Documents, Downloads or
  OneDrive/iCloud folders until you allow it:

    1. In System Settings → Privacy & Security → Full Disk Access,
       drag this file onto the list (Finder will show it) and switch it on:
         $FDA_PROGRAM
       (Don't use the + button — its file picker greys this file out.)
    2. Restart ScreenFind: open ⚙ Settings → Restart ScreenFind, or run
         launchctl kickstart -k gui/\$(id -u)/$LABEL

  If your screenshots live somewhere unprotected, you can skip this.
  ScreenFind shows a warning in its status bar if access is blocked.

EOF
if [[ -t 0 ]]; then
  read -r -p "Open Full Disk Access settings and show the file now? [Y/n] " ans
  if [[ ! "$ans" =~ ^[Nn] ]]; then
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    open -R "$FDA_PROGRAM"
  fi
fi

open "http://localhost:$PORT/#settings"
bold "Done. ScreenFind is open in your browser — add your screenshot folders in ⚙ Settings."
echo "  Optional terminal command:  alias screenfind=\"$BIN\""
