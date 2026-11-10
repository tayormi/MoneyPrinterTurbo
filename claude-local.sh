#!/usr/bin/env sh
# Run MoneyPrinterTurbo locally on your Claude subscription (macOS / Linux).
#
#   ./claude-local.sh                      # set up (first run) + start WebUI and API
#   ./claude-local.sh --setup-only         # only prepare config and check Claude
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
for arg in "$@"; do
  case "$arg" in
    --setup-only) SETUP_ONLY=1 ;;
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

echo "==> Starting API on http://127.0.0.1:8080/docs"
uv run python main.py &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT INT TERM

echo "==> Starting WebUI"
sh "$ROOT/webui.sh"
