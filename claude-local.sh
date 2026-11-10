#!/usr/bin/env sh
# Run MoneyPrinterTurbo locally on your Claude subscription (macOS / Linux).
#
#   ./claude-local.sh                      # set up (first run) + start WebUI and API
#   ./claude-local.sh --setup-only         # only prepare config and check Claude
#   ./claude-local.sh --local-voice        # also run an offline Kokoro voice server
#   PEXELS_API_KEY=xxx ./claude-local.sh   # also store a Pexels key
#   CLAUDE_MODEL=claude-sonnet-5-5 ./claude-local.sh
#
# Needs: uv (https://docs.astral.sh/uv/), ffmpeg is bundled via imageio-ffmpeg,
# and the `claude` CLI signed in once (`npm i -g @anthropic-ai/claude-code`, then `claude`).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> Installing Python dependencies (uv sync --frozen)"
uv sync --frozen

echo "==> Preparing config.toml for Claude"
SETUP_ONLY=0
LOCAL_VOICE=0
for arg in "$@"; do
  case "$arg" in
    --setup-only) SETUP_ONLY=1 ;;
    --local-voice) LOCAL_VOICE=1 ;;
  esac
done
if [ -n "${CLAUDE_MODEL:-}" ]; then
  uv run python scripts/claude_local_setup.py --model "$CLAUDE_MODEL"
else
  uv run python scripts/claude_local_setup.py
fi

if [ "$SETUP_ONLY" = "1" ]; then
  exit 0
fi

PIDS=""
trap 'kill $PIDS 2>/dev/null || true' EXIT INT TERM

if [ "$LOCAL_VOICE" = "1" ]; then
  echo "==> Starting offline Kokoro voice on http://127.0.0.1:8880 (first run downloads ~350 MB)"
  uv run --with kokoro-onnx==0.6.1 --with soundfile==0.14.0 python scripts/kokoro_local_server.py &
  PIDS="$PIDS $!"
  echo "    In the WebUI, pick a voice that starts with kokoro:, e.g. kokoro:af_heart"
fi

echo "==> Starting API on http://127.0.0.1:8080/docs"
uv run python main.py &
PIDS="$PIDS $!"

echo "==> Starting WebUI"
sh "$ROOT/webui.sh"
