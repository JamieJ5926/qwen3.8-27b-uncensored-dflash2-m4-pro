# Phase 2. Declarative route table

## Goal

Define the ordered rules that select an execution lane deterministically. This phase does not select a tool or specialist agent.

## Files (3)

- `plan/01-local-model-coordinator/phase-2-route-table.md` (this spec, merged into the route table fixture)
- `router/rules.py` (future implementation target, named for ownership only)
- `router/rules.schema.json`

## Work

Define RouteRule match predicates over intent, risk class, context token count, and whether the request needs Qwen judgment. Each rule selects a lane such as `lfm_direct`, `qwen_coordinator`, `fixed_worker`, or `fail_visible`. Define lane input, output, and time budgets, fallback lanes, and deterministic tie-breaking. Rule ids are unique. Precedence is list order. Ambiguity between two matching rules is a config error, not a runtime choice.

## Idempotency and follow-up

Each LaneDecision records the rule id, rule version, lane, and budgets. It contains no tool or agent choice. The rule may grant the Qwen coordinator lane one bounded follow-up budget, but only the action validator may consume it.

## Static checks

- Schema fixture validation. Duplicate rule ids, missing fallbacks, and ambiguous precedence fail.

## Runtime checks

- Replay route fixtures and assert identical LaneDecision output plus identical lane and follow-up budgets across runs.

## Exit gate

Fixture set replays deterministically. No rule references the DFlash server as a worker target. 8083 appears only as the Qwen inference endpoint. Every fixture proves that lane selection and Qwen action selection are separate decisions.
