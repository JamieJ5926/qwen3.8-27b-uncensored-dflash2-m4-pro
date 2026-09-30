#!/usr/bin/env bash
# Paired DFlash HumanEval benchmark for Qwen3.8-27B-Uncensored-MLX on Apple M4 Pro.
# Usage: TARGET_PATH=/path/to/Qwen3.8-27B-Uncensored-MLX/4-bit ./benchmark.sh
#
# Reproduces the measured configuration:
#   baseline (target-only) vs DFlash speculative decoding (draft: incoai/Qwen3.8-27B-DFlash2, 4-bit runtime quant)
#   HumanEval, 20 samples, max_new_tokens=256, temperature=0 (greedy), reasoning off, block_size=5
set -euo pipefail

TARGET_PATH="${TARGET_PATH:?Set TARGET_PATH to the local path of orcarouter/Qwen3.8-27B-Uncensored-MLX subdir 4-bit}"
VENV_ROOT="${VENV_ROOT:-/Users/jamie/models/mlx-venv}"
CACHE_ROOT="${CACHE_ROOT:-/Volumes/BBALL_GM_DATA/models/huggingface-cache}"
BLOCK_SIZE="${BLOCK_SIZE:-5}"
DRAFT_BITS="${DRAFT_BITS:-4}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-256}"
MAX_SAMPLES="${MAX_SAMPLES:-20}"

[ -x "$VENV_ROOT/bin/python3" ] || { echo "missing venv python: $VENV_ROOT/bin/python3" >&2; exit 1; }
[ -x "$VENV_ROOT/bin/dflash" ] || { echo "missing venv dflash: $VENV_ROOT/bin/dflash" >&2; exit 1; }
export PATH="$VENV_ROOT/bin:$PATH"
for cmd in python3 dflash; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "missing required command: $cmd" >&2; exit 1; }
done
[ -d "$TARGET_PATH" ] || { echo "TARGET_PATH is not a directory: $TARGET_PATH" >&2; exit 1; }

mkdir -p "$CACHE_ROOT"
export HF_HOME="$CACHE_ROOT"
export HF_DATASETS_CACHE="$CACHE_ROOT/datasets"
export HF_HUB_CACHE="$CACHE_ROOT/hub"

python3 - <<'PY'
import sys
import mlx.core
import importlib.metadata as md

want = {"dflash": "0.1.0", "mlx": "0.32.0", "mlx-lm": "0.31.3", "mlx-metal": "0.32.0"}
for pkg, ver in want.items():
    got = md.version(pkg)
    if got != ver:
        sys.exit(f"version mismatch: {pkg}=={got}, expected {ver}")
PY

dflash benchmark mlx \
  --model "$TARGET_PATH" \
  --draft incoai/Qwen3.8-27B-DFlash2 \
  --draft-bits "$DRAFT_BITS" \
  --block-size "$BLOCK_SIZE" \
  --max-new-tokens "$MAX_NEW_TOKENS" \
  --temperature 0 \
  --reasoning off \
  --dataset humaneval \
  --max-samples "$MAX_SAMPLES"
