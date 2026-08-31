# Phase 1. Repair BenchLoop evidence lifecycle

## Goal

Make unattended benchmark evidence trustworthy. This is a hard gate. No router or harness work may publish performance claims until it closes.

## Files (3)

- `.audit/overnight-2026-08-31/hillclimb.py`
- `.audit/overnight-2026-08-31/benchloop`
- `.audit/overnight-2026-08-31/evidence/README.md`

## Known defects (from HANDOFF.md, confirmed by BenchmarkMap)

- Undefined `args.install_ok` referenced in command construction against bench-loop 0.2.3.
- Evidence files written after the subprocess completes, so timeouts and crashes leave nothing.
- Stale oMLX assumptions in the launch path.
- `results.tsv` has no schema guard, so malformed rows accumulate silently.
- No checksum on `frozen-config.json`, so config drift is undetectable.

## Work

Fix command construction. Create the run and failure evidence files before launching any subprocess. Persist timeout and partial output. Add a TSV schema guard. Add a checksum check on `frozen-config.json`. Fail closed when a completed artifact is missing. Document the evidence lifecycle in the README.

## Static checks

- `python -m py_compile` passes for hillclimb.py.
- hillclimb.py selftest passes.
- Inspect constructed argv and evidence paths in a dry run.

## Runtime checks

- The benchmark owner submits the run specification to the exclusive hardware-lane owner. Only the hardware owner may call BenchLoop or the Qwen server.
- The hardware owner acquires the host-wide lock, passes the resource preflight, runs BenchLoop speed alone with no Pi concurrency against the leased Qwen server, and records the lock and resource evidence.
- Require a durable completed artifact in `evidence/`, not a successful launch message, before the hardware owner releases the lock.

## Exit gate

One clean BenchLoop run produces a schema-valid TSV row and a completed evidence JSON. Only then may later phases cite benchmark numbers.
