# Qwen3.8-27B (MLX 4-bit) + DFlash2 speculative decoding on M4 Pro — benchmark recipe

This is a **benchmark recipe**, not redistributed weights. It records the exact
hardware, software, and commands used to measure a 2.12x generation-throughput
speedup with DFlash speculative decoding, and how to rerun it.

## Result

| Configuration | Throughput (tok/s) | Speedup |
|---|---|---|
| Baseline (target only) | 13.76 | 1.00x |
| DFlash (target + draft) | 29.11 | 2.12x |

Mean acceptance length: 4.95 tokens. Acceptance histogram (bucket 0–5):
`[0.0%, 0.0%, 1.7%, 0.0%, 0.0%, 98.3%]`.

## Models

- Target: [`orcarouter/Qwen3.8-27B-Uncensored-MLX`](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX), subdir `4-bit` (affine 4-bit, group size 64)
- Draft: [`incoai/Qwen3.8-27B-DFlash2`](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2), quantized 4-bit at runtime
- Runtime: [`dflash`](https://pypi.org/project/dflash/) 0.1.0 on MLX

## Hardware & software

- MacBook Pro, Apple M4 Pro, 48GB unified memory
- macOS, MLX 0.32.0, mlx-metal 0.32.0, mlx-lm 0.31.3, dflash 0.1.0
- Target model weights and Hugging Face caches on an external APFS HDD
- All other model servers stopped during the run

## Rerun

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# TARGET_PATH: local path to orcarouter/Qwen3.8-27B-Uncensored-MLX subdir 4-bit
# CACHE_ROOT: where HF caches go (defaults to ~/.cache/huggingface; set to an
# external drive path to match the measured configuration)
TARGET_PATH=/path/to/Qwen3.8-27B-Uncensored-MLX/4-bit ./benchmark.sh
```

The script verifies required commands, exact pinned package versions, and the
target path, exports `HF_HOME` / `HF_HUB_CACHE` / `HF_DATASETS_CACHE` under
`CACHE_ROOT`, then runs the paired benchmark: HumanEval, 3 samples, max 256 new
tokens, temperature 0 (greedy), reasoning off, block size 5 — baseline and
DFlash under identical settings.

## Interpretation

The speedup is generation throughput on HumanEval with greedy decoding under
the exact configuration above: one target model, one draft, one hardware
setup, one task. Acceptance length 4.95 with a 5-token block means the draft
proposals were accepted end-to-end on ~98.3% of blocks in this sample.

## Limitations

- Small sample count (3) and single task (HumanEval); treat the numbers as a
  configuration record, not a robust estimate. No statistical significance is
  claimed.
- Measured on M4 Pro with weights and caches on an external HDD; other
  hardware, storage, or settings will differ.
- The warm one-shot wall time of 218.13s is end-to-end timing and is **not**
  generation throughput.
- No claim is made that the abliterated target matches the base model's
  quality, or that this pairing is a first of its kind.
- A model compatibility warning about unrecognized `mrope_section` /
  `mrope_interleaved` keys appeared at load; it is recorded here as observed
  and no fallback behavior is implied.
