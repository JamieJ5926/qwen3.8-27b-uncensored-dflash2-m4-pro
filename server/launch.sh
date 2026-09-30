#!/usr/bin/env bash
# Launch the stock DFlash OpenAI-compatible server (mirrors benchmark.sh env).
# Usage: ./launch.sh [--port 8082] (extra args forwarded to dflash_server.py)
set -euo pipefail

TARGET_PATH="${TARGET_PATH:-/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit}"
CACHE_ROOT="${CACHE_ROOT:-$HOME/.cache/huggingface}"
[ -d "$TARGET_PATH" ] || { echo "TARGET_PATH is not a directory: $TARGET_PATH" >&2; exit 1; }
mkdir -p "$CACHE_ROOT"
export HF_HOME="$CACHE_ROOT"
export HF_DATASETS_CACHE="$CACHE_ROOT/datasets"
export HF_HUB_CACHE="$CACHE_ROOT/hub"

exec /Users/jamie/models/mlx-venv/bin/python "$(dirname "$0")/dflash_server.py" \
  --model "$TARGET_PATH" "$@"
