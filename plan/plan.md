# Multi-phase plan: Benchmark separation from harness orchestration

## Phase 0: Shared prerequisites
- Model readiness (local 4-bit copy, external caches)
- Benchmark verification scripts
- Server readiness checks (but deferred to harness)

## Phase 1: Benchmark work
- DFlash2 optimizations, hillclimb, overnight evidence
- HumanEval paired runs, results.json, .audit/

## Phase 2: Harness work
- Pi orchestration layer
- Server hardening (dflash_server.py, launch.sh)
- Tool/agent delegation wiring

## Boundaries
- Phase 0 → Phase 1: After manual benchmark confirmation
- Phase 1 → Phase 2: After Phase 0 + "harness ready" gate
- Shared: HANDOFF.md, README.md, benchmark.sh, server/*.py

Supporting files: HANDOFF.md, README.md, benchmark.sh, server/dflash_server.py, server/launch.sh, results.json, .audit/overnight-2026-08-31/

Contradictions: Historical (19c2fa1, no server work) vs current (uncommitted server, llama.cpp still running, no restart service).