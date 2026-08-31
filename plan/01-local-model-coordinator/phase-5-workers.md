# Phase 5. Worker jobs and result validation

## Goal

Run heavy work outside the model server and validate everything that comes back.

## Files (3)

- `plan/01-local-model-coordinator/phase-5-workers.md` (this spec, merged into the worker contract)
- `router/validate.py` (future implementation target, named for ownership only)
- `router/targets.py` (future implementation target, named for ownership only)

## Work

Specify WorkerJob dispatch with isolation and `idempotency_key`. Retries with a seen key reuse the recorded artifact. Define evidence requirements, action-argument validation against the packet's `allowed_actions`, confidence and status handling, and the optional follow-up path within the existing Qwen coordinator lane. A malformed or low-confidence result triggers the single bounded Qwen follow-up if the LaneDecision grants one and it remains unused. Otherwise the request fails visibly. Worker results can never change lanes.

## Compact observations

After each execution step the dispatcher writes a CompactObservation. Numbers only in the record. Answers, logs, and raw outputs go to append-only artifact files under unique run ids. The observation carries `artifact_ref` pointers. One writer per artifact.

## Static checks

- Malformed-result and bad-tool-argument fixture suite. No result may bypass the validator.

## Runtime checks

- Run bounded non-coding worker fixtures and one permitted tool operation. Verify both the accepted path and a rejected path.

## Exit gate

Validator rejects every malformed fixture. Idempotent retry reuses the artifact. Observation payloads stay under the compact size cap.
