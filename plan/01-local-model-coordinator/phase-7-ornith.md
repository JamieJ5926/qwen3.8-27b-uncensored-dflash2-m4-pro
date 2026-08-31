# Phase 7. Ornith, optional isolated candidate

## Goal

Keep Ornith available as a candidate without letting it touch anything shared.

## Files (3)

- `plan/01-local-model-coordinator/phase-7-ornith.md` (this spec, merged into the adapter contract)
- `router/ornith_adapter.py` (future implementation target, named for ownership only)
- `router/fixtures/ornith/` (isolated fixtures)

## Research summary

The linked GGUF card confirms the available quant files and sizes. It lists Q3_K_M at 17.3GB and labels it `lower quality`. It does not provide SWE-bench results, coding comparisons, context validation, or RTX 4090 throughput evidence.

The base-model publisher describes Ornith-1.5-35B-A3B as a Qwen3.5 MoE model with around 35B total parameters, around 3B active per token, and a 262K native context. Treat those details and the published coding and agent benchmarks as publisher claims until the local harness verifies them. The reported 158 tokens per second on an RTX 4090 remains an unverified candidate claim and does not predict Apple M4 Pro performance. Llama.cpp has Qwen3.5 MoE architecture naming, but exact Ornith GGUF loading, tool parsing, context behavior, memory use, and throughput remain unverified.

## Constraints

Opt-in only, enabled separately by Jamie. Separate endpoint, process, lease slot, and artifacts. Use the same task corpus, prompt profile, context limits, generation settings, scoring rules, and repeat count as the Qwen and LFM arms. A config kill switch removes Ornith from all default routes. Before download approval, record the selected quant, download size, installed size, cache path, free-space floor, and deletion path. No download happens as part of this plan. Ornith never overlaps Qwen, LFM, Docker-launched model processes, or another GGUF server. Never mix Ornith results into Qwen benchmark numbers or rank Ornith before the matched local comparison completes.

## Static checks

- Config lint rejects Ornith entries in default routes and in any shared artifact path. The storage manifest rejects an unapproved quant, an unknown cache path, or insufficient free disk.

## Runtime checks

- If and only if Jamie enables the lane and approves the quant and storage budget, the exclusive hardware-lane owner downloads and runs it. The owner acquires the host-wide lock, verifies `/Volumes/BBALL_GM_DATA` free space, stops the current lease holder, verifies process and Metal release, starts Ornith in its isolated slot, runs one smoke request, stops it, verifies release, and restores Qwen only if requested. Record transfer and installed sizes, disk delta, load time, tokens per second, memory, swap, tool JSON validity, and pass rate. Otherwise the phase records SKIPPED.

## Exit gate

Isolation proven by config lint and fixture separation. Lane never blocks phases 2 through 6 or 8.
