# Phase 6. Full Pi and slim profile comparison

## Goal

Evaluate slim-coordinator correctness and mode-specific performance against the full-Pi control. Reserve direct proof and ranking for continuously executed live arms. Treat replay and staged-local arms as integration and component measurements.

## Files (3)

- `plan/01-local-model-coordinator/phase-6-profiles.md` (this spec, merged into the profile contract)
- `plan/01-local-model-coordinator/corpus.md`
- `router/profiles.py` (future implementation target, named for ownership only)

## Work

Keep full Pi as the control. Define the slim LFM2.5-26 chat profile with a minimal system prompt and tool contract. A hosted LFM endpoint may support a true live LFM-to-Qwen comparison while Qwen remains local. Prerecorded or locally generated LFM intent artifacts prove contract compatibility and integration only. They do not prove live end-to-end latency. When both LFM and Qwen are local, run them in separate hardware slots and report two measured stages plus the model-switch or queue interval. Do not present their sum as a paired live pipeline result. The hosted capable subagent profile is the quality control arm. The route table selects the profile explicitly. No silent default.

## Static checks

- Profile diff review. Prompt budgets documented. Corpus identical across arms.

## Runtime checks

- The profile owner prepares the corpus and run specifications offline. The exclusive hardware-lane owner performs every live arm sequentially.
- Full Pi and hosted-LFM-to-local-Qwen arms may report genuine end-to-end wall time when the full request path runs continuously.
- The prerecorded-intent arm reports integration correctness, tool validity, Qwen-stage timing, and artifact compatibility. It carries no LFM latency claim.
- The all-local staged arm reports LFM generation metrics, shutdown and model-switch or queue time, Qwen processing metrics, and an explicitly labeled staged total. It must not be labeled live end-to-end throughput or latency.

## Exit gate

Full-Pi control results exist before slim results are compared. Every result identifies its measurement mode as live end-to-end, integration replay, or staged local. No regression on quality without an explicit accepted tradeoff. No staged local number is ranked against a live pipeline number as if they measured the same thing.
