#!/usr/bin/env bash
# Sets up offline speech-to-text for Deck Agent (no sudo): uv, a Python 3.12 virtualenv with
# faster-whisper, and the Whisper "base.en" model (~150 MB, downloaded once).
set -euo pipefail

DEST="$HOME/.local/share/deck-agent"
VENV="$DEST/stt-venv"

if [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
elif command -v uv >/dev/null; then
  UV="$(command -v uv)"
else
  echo "==> Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi

echo "==> Creating the speech-to-text environment"
"$UV" venv --python 3.12 --allow-existing "$VENV"
"$UV" pip install --python "$VENV/bin/python" "faster-whisper>=1.1,<2"

echo "==> Downloading the Whisper model (one time)"
"$VENV/bin/python" -c 'from faster_whisper import WhisperModel; WhisperModel("base.en", device="cpu", compute_type="int8")'

systemctl --user restart deck-agent.service
echo "Voice is ready."
