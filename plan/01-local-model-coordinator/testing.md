# Testing plan

## Order of trust

The benchmark freezes before any harness change. BenchLoop repairs before any unattended measurement is cited. Full-Pi control runs before any slim-profile comparison. Ornith never mixes into Qwen numbers. The Mac model lease serializes every local-model run. Only one local model server or model process may exist at a time.

## Benchmark matrix

Same matrix as the throughput checkpoint in overview.md. Profile, task, route, context size, quality, performance, reliability. Every recorded row fills all seven dimensions.

## Metrics and owners

- Speed. Median generation tps from the DFlash response. Owner `benchmark.sh`, adopted.
- Coordinator overhead. Wall time per rep plus request count delta. Owner `hillclimb.py observe`, adopted.
- Prompt cost. Prompt and completion tokens from usage. Owner `hillclimb.py chat`, adopted.
- Delegation quality. Mean acceptance plus histogram. Owner `results.json` schema, adopted.
- End-to-end latency. Full wall time including load. Owner `hillclimb.py` snapshot, adopted.

## Fixture suites

- Route fixtures. Deterministic resolution, precedence, ambiguity rejection. Phase 2.
- Boundary fixtures. OpenAI shape compatibility, trace headers, error mapping. Phase 3.
- Packet fixtures. Budget caps, deterministic hash, golden packets. Phase 4.
- Worker fixtures. Malformed results, invalid tool arguments, idempotent retries, artifact references. Phase 5.
- Replay fixtures. Bit-stable reruns by packet hash and idempotency key. Failure injection for timeouts and crashes. Phase 8.
- Config lint. Ornith excluded from defaults, router imports clean of `server/`. Phases 3 and 7.

## Runtime commands

- Offline suite. Route, boundary, packet, worker, replay, and lint fixtures. No model calls or cache changes. Offline workers may run this in parallel.
- Hardware preflight. The exclusive hardware-lane owner acquires the host-wide lock, inventories known model ports and processes, reads memory pressure and swap, verifies `/Volumes/BBALL_GM_DATA`, checks external-volume free space and model-cache sizes, and checks Docker disk use. Fail if another actor holds the lock, a second model process exists, the volume is unavailable, or a floor is not met.
- Live packet check. An offline worker submits the request specification and expected artifact schema to the hardware-lane owner. Only that owner may call the leased Qwen server. The owner does not start a server automatically.
- BenchLoop gate. Only the hardware-lane owner runs `benchloop check` and the speed run against the leased Qwen server. No Pi concurrency. A completed evidence artifact is required before releasing the lock.
- Paired profile runs. The hardware-lane owner runs the full-Pi control first, then one adjacent change. Only continuously executed request paths count as paired live end-to-end runs.
- Staged local LFM-to-Qwen run. The hardware owner measures LFM, model shutdown and switch or queue time, and Qwen as separate stages. Prerecorded intents prove integration only. Report a labeled staged total, never live end-to-end latency or throughput.
- Model comparison. The hardware-lane owner stops the current lease holder, verifies process and Metal release, updates the lock, then starts the next approved model in its own slot. Never overlap Qwen, LFM, Ornith, or another GGUF process.
- Downloads and cleanup. Only the hardware-lane owner may download, move, or delete model files or caches. The owner verifies the approved storage budget under `/Volumes/BBALL_GM_DATA` before mutation and records the resulting disk delta.
- Ornith smoke. Only when Jamie enables the isolated lane, approves the quant and download, and the storage preflight passes. Otherwise SKIPPED.

## Artifact schema

- `results.tsv` rows are schema-guarded with a checksum-verified `frozen-config.json`.
- Evidence JSON files are created before the subprocess they describe and finalized after.
- CompactObservation records carry `artifact_ref` pointers into append-only files under unique run ids. One writer per artifact. Payloads stay under the compact cap.

## Pass and fail criteria

- A phase passes when its static checks are green and its runtime checks produce the named durable artifacts.
- Any benchmark claim without a completed BenchLoop artifact fails review.
- A slim profile that loses quality without a documented accepted tradeoff fails the gate.
- Every profile result declares `live_end_to_end`, `integration_replay`, or `staged_local`. Missing or mixed measurement modes fail review.
- Any increase in DFlash server errors during a gated run fails the run.
- A run fails if the hardware lock is absent or has another owner, multiple local model processes exist, memory pressure turns red, swap grows continuously, the server becomes unresponsive, `/Volumes/BBALL_GM_DATA` disappears, or free disk crosses the recorded floor.
- Replay mismatch on any fixture fails the release gate.

## What this plan never does

No implementation beyond the named future targets. No subagent or offline worker starts, stops, calls, benchmarks, downloads, moves, or deletes local model resources. No automatic server starts, restarts, or config changes. No concurrent local model servers. No concurrent Pi and BenchLoop runs. No wall-time numbers passed off as decode throughput.
