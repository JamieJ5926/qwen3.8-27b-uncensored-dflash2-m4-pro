# Phase 4. Compact Qwen decision packets

## Goal

Minimize Qwen tokens inside the `qwen_coordinator` lane. Qwen sees a hashed, bounded packet, never the conversation.

## Files (3)

- `plan/01-local-model-coordinator/phase-4-packets.md` (this spec, merged into the packet contract)
- `router/packet.py` (future implementation target, named for ownership only)
- `router/fixtures/packets/` (golden fixtures)

## Work

Define context selection, which turns, files, and action contracts earn a place in the packet. Enforce byte and token caps from the LaneDecision action budget. Compute `packet_hash` over the canonical serialization. Define `allowed_actions`, `expected_result_schema`, and the optional follow-up budget per packet. Qwen returns one structured ActionDecision. It chooses one allowed tool, one specialist agent, or a direct answer. It never selects the execution lane.

## Idempotency

The same envelope, LaneDecision, and rule version produce the same packet hash. Replay uses the hash to fetch the recorded ActionDecision instead of re-asking Qwen.

## Static checks

- Golden packet fixtures with byte and token budget assertions.
- Deterministic hash check. Same input, same hash, every time.

## Runtime checks

- With Jamie's explicit approval, the packet owner hands one compact request specification to the exclusive hardware-lane owner. Only the hardware owner may call Qwen on 8083. Validate one structured ActionDecision and record separate LaneDecision, ActionDecision, lock, memory, swap, and artifact traces.

## Exit gate

Packet compiler passes the fixture suite. One live packet is validated. Qwen token count per ActionDecision is recorded and under budget. No Qwen response can alter the selected lane.
