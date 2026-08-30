# Qwen3.8-27B (MLX 4-bit) + DFlash2 speculative decoding on M4 Pro — benchmark recipe

This is a **benchmark recipe**, not redistributed weights. It records the exact
hardware, software, and commands used to measure a 2.06x generation-throughput
speedup with DFlash speculative decoding, and how to rerun it.

## Result

HumanEval, 20 samples, max 256 new tokens, temperature 0 (greedy), reasoning
off, block size 5, paired baseline and DFlash under identical settings. Other
model services stopped; 91% of memory free before launch.

| Configuration | Throughput (tok/s) | Speedup |
|---|---|---|
| Baseline (target only) | 12.35 | 1.00x |
| DFlash (target + draft) | 25.48 | 2.06x |

Mean acceptance length: 4.87 tokens. Acceptance histogram (bucket 0–5):
`[0.0%, 1.2%, 1.5%, 0.6%, 1.7%, 94.9%]`.

Paired progress time: 9m54s. Full process wall time: 825.84s including model
loading.

### Initial smoke test (3 samples)

The first run used 3 samples: baseline 13.76 tok/s, DFlash 29.11 tok/s
(2.12x), mean acceptance length 4.95, 98.3% of blocks accepted in full, warm
one-shot wall time 218.13s. Kept only as an initial smoke-test record; the
20-sample numbers above are the primary result.

## Models

- Target: [`orcarouter/Qwen3.8-27B-Uncensored-MLX`](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX), subdir `4-bit` (affine 4-bit, group size 64)
- Draft: [`incoai/Qwen3.8-27B-DFlash2`](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2), quantized 4-bit at runtime
- Runtime: [`dflash`](https://pypi.org/project/dflash/) 0.1.0 on MLX

## Hardware & software

- MacBook Pro, Apple M4 Pro, 48GB unified memory
- macOS, MLX 0.32.0, mlx-metal 0.32.0, mlx-lm 0.31.3, dflash 0.1.0
- Target model weights and Hugging Face caches on an external APFS HDD
- All other model servers stopped during the run

## Quick setup from GitHub

```bash
git clone https://github.com/JamieJ5926/qwen3.8-27b-uncensored-dflash2-m4-pro
cd qwen3.8-27b-uncensored-dflash2-m4-pro
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Download the target model from
[`orcarouter/Qwen3.8-27B-Uncensored-MLX`](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX)
(subdir `4-bit`), then point the benchmark at it and run:

```bash
TARGET_PATH=/path/to/Qwen3.8-27B-Uncensored-MLX/4-bit ./benchmark.sh
```


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
`CACHE_ROOT`, then runs the paired benchmark: HumanEval, 20 samples, max 256
new tokens, temperature 0 (greedy), reasoning off, block size 5 — baseline and
DFlash under identical settings.

## Interpretation

The speedup is generation throughput on HumanEval with greedy decoding under
the exact configuration above: one target model, one draft, one hardware
setup, one task. The stock DFlash2 draft works against the separately
abliterated target and roughly doubles throughput. Mean acceptance length 4.87
with a 5-token block means 94.9% of blocks were accepted end-to-end in this
run.

## Limitations

- 20 samples on a single task (HumanEval); treat the numbers as a
  configuration record, not a robust estimate. No statistical significance is
  claimed.
- Measured on M4 Pro with weights and caches on an external HDD; other
  hardware, storage, or settings will differ.
- The full process wall time of 825.84s includes model loading and is
  end-to-end timing, **not** generation throughput.
- No claim is made that the abliterated target matches the base model's
  quality, or that this pairing is a first of its kind.
- A model compatibility warning about unrecognized `mrope_section` /
  `mrope_interleaved` keys appeared at load; it is recorded here as observed
  and no fallback behavior is implied.
