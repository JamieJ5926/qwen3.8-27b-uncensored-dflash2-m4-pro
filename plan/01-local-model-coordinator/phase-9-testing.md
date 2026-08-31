# Phase 9. Testing harness

## Goal

Give every phase's static and runtime checks a home, wired to the frozen benchmark.

## Files (3)

- `plan/01-local-model-coordinator/testing.md`
- `router/tests/` (test package, named for ownership only)
- `router/fixtures/` (shared fixtures)

## Work

Consolidate the fixture suites, the checkpoint dimension matrix, the runtime commands, and the pass and fail criteria from testing.md into the test package. The harness runs BenchLoop through the repaired wrapper only. It never invokes the direct `dflash` CLI and never touches the active Qwen server configuration.

## Static checks

- Test package collects and runs the fixture suites from phases 2 through 5 and 8.

## Runtime checks

- One command runs the full fixture suite offline. One command runs the gated live suite, requiring the BenchLoop gate to be green first.

## Exit gate

Offline suite green in CI-like local runs. Live suite documented with its prerequisites.
