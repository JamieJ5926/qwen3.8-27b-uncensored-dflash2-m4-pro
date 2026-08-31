# Phase 3. OpenAI-compatible router boundary

## Goal

Give Pi and clients a stable OpenAI-compatible front door that never loads a model.

## Files (3)

- `plan/01-local-model-coordinator/phase-3-boundary.md` (this spec, merged into the boundary contract)
- `router/http_boundary.py` (future implementation target, named for ownership only)
- `router/normalize.py` (future implementation target, named for ownership only)

## Work

Document `/v1/chat/completions` ingress and egress, ConversationEnvelope normalization, streaming and non-streaming behavior, error mapping to OpenAI-style error bodies, trace headers carrying `route_trace` and `trace_id`, and timeout semantics. Hard request limits live here, independent of any model config. The boundary performs no model loading and imports nothing from `server/`.

## Static checks

- Contract example validation against the OpenAI chat completion shape.
- Grep gate. `router/` must not import `dflash`, `mlx_lm`, or anything under `server/`.

## Runtime checks

- Proxy an offline echo fixture through the boundary and verify request and response compatibility.
- If a live health probe is required, the boundary owner submits the probe specification to the exclusive hardware-lane owner. Only the hardware owner may call 127.0.0.1:8083. No generation call occurs in this phase.

## Exit gate

Boundary serves an OpenAI-shaped response with a trace header. Server untouched.
