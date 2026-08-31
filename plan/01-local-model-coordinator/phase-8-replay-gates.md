# Phase 8. End-to-end replay and gates

## Goal

Make the whole coordinator reproducible and gated before any harness change.

## Files (3)

- `plan/01-local-model-coordinator/phase-8-gates.md` (this spec, merged into the gates contract)
- `router/replay.py` (future implementation target, named for ownership only)
- `plan/01-local-model-coordinator/gates.md`

## Work

Define deterministic replay. A stored request id plus the frozen route table version reproduces the same LaneDecision, packet hash, ActionDecision, and response, pulling recorded decisions and artifacts by `packet_hash` and `idempotency_key` instead of re-invoking models. Define failure injection for timeouts, malformed packets, and worker crashes. Define the gate ladder. BenchLoop repaired gate. Full-Pi explicit-local gate. Quality gate. Server-error regression gate. Resource-lease gate. Define stop and revert criteria, tested one hypothesis at a time.

## Static checks

- Replay fixture suite. Artifact completeness check. Every CompactObservation resolves to a real artifact.

## Runtime checks

- The replay owner prepares fixtures offline. The exclusive hardware-lane owner performs the full-Pi control and each adjacent live change sequentially. Require the host-wide lock, one model process, Qwen health, a completed BenchLoop artifact, a quality pass, acceptable memory and external-volume disk state, and no increase in server errors.

## Exit gate

Replay is bit-stable for fixtures. All gates defined with pass and fail criteria. Revert criteria written.
