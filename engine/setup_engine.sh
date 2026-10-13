#!/bin/bash
# GLASS engine setup for macOS 13+ — safe to re-run. No account, key or payment needed.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Library/Application Support/GLASS"
VENV="$APP/venv"
[ -x "$HERE/.venv/bin/python3" ] && VENV="$HERE/.venv"   # developer checkout
mkdir -p "$APP/bin"
step() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }

step "Python 3.11"
PY=""
for c in python3.11 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3.11; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 is not installed."
  echo "Install it from https://www.python.org/downloads/release/python-3119/ (macOS 64-bit universal2 installer),"
  echo "then run this script again."
  open "https://www.python.org/downloads/release/python-3119/"
  exit 1
fi
echo "Using $PY"

step "Engine packages -> $VENV"
[ -x "$VENV/bin/python3" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python3" -m pip install --upgrade pip --quiet
"$VENV/bin/python3" -m pip install -r "$HERE/requirements-mac.txt"

step "System-audio helper (ScreenCaptureKit)"
if ! xcode-select -p >/dev/null 2>&1; then
  echo "The Xcode Command Line Tools are needed to build the small audio helper."
  xcode-select --install || true
  echo "Finish that installation, then run this script again."
  exit 1
fi
swiftc -O -parse-as-library "$HERE/macos/glass-audio.swift" -o "$APP/bin/glass-audio"
echo "Built $APP/bin/glass-audio"

step "Ollama (local AI)"
if ! command -v ollama >/dev/null 2>&1 && [ ! -d "/Applications/Ollama.app" ]; then
  echo "Download Ollama for macOS, drag it to Applications and open it once."
  open "https://ollama.com/download/mac"
  read -r -p "Press Enter when Ollama is running..."
fi
export PATH="$PATH:/Applications/Ollama.app/Contents/Resources:/usr/local/bin"
until ollama list >/dev/null 2>&1; do echo "Waiting for Ollama to start..."; open -a Ollama 2>/dev/null || true; sleep 3; done
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then MODEL="qwen3:4b-instruct"; else MODEL="qwen3:1.7b"; fi
echo "Downloading the answer model $MODEL ..."
ollama pull "$MODEL"

step "Speech model (Whisper small, ~0.5 GB) and a speed check"
"$VENV/bin/python3" - "$HERE" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1])
import numpy as np
from glass.audio.asr import ASRWorker
a = ASRWorker(lambda s: None, lambda s, i: None)
a.load("small", "int8", "cpu", 6)
t = time.perf_counter(); a.transcribe_array(np.zeros(16000 * 5, np.float32)); dt = time.perf_counter() - t
print(f"Whisper small: {dt:.1f}s for 5s of audio on this Mac")
PY

step "Permissions"
echo "When GLASS first listens, macOS will ask to allow 'Screen & System Audio Recording'."
echo "Allow it in System Settings -> Privacy & Security, then restart GLASS."
echo "If you turn on the microphone in GLASS, allow Microphone access too."
printf "\n\033[1;32mGLASS engine is ready.\033[0m\n"
