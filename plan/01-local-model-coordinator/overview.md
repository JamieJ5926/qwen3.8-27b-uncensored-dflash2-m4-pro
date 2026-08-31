# Local model coordinator plan

## Decision

The base design is Candidate B, a stateless router boundary with a declarative route table. Candidate A, a typed bounded coordinator state machine, was rejected as the base and mined for parts. The deciding factors, in the order the brief set.

- Smallest correct surface. The contract allows one Qwen routing decision plus at most one bounded follow-up. A route table plus a single typed follow-up slot models that exactly. A state machine would add mutable workflow state, restart recovery, and a persistence layer to govern two decisions.
- Replayability. A request replays from its inputs plus the frozen route table. No checkpoint format, no resumption semantics.
- Failure handling. Fallback is a declared field on each route rule. There is no state to corrupt on timeout or malformed results.
- Qwen token minimization. The packet compiler sends Qwen a compact, hashed decision packet, never the conversation.
- Maintainability. The DFlash server stays inference-only. The router never imports from `server/dflash_server.py`. The server's owners, `benchmark.sh`, `results.json`, `hillclimb.py`, and `benchloop`, keep their jobs.

Grafted from Candidate A. Typed request-scoped records for decisions and observations, and the bounded follow-up envelope with `follow_up_used` and `follow_up_budget` fields, which caps the coordinator at one follow-up by construction.

## Current truth

DFlash serves Qwen at 127.0.0.1:8083 with OpenAI-compatible chat endpoints plus `/health`, `/v1/models`, and `/v1/dflash/stats`. Port 8082 is closed. Pi routes to 8083. Jamie is testing manually. `dflash_server.py` owns inference, health, stats, and serving. It has no tool registry and no agent layer. Tool calls are parsed inside the Qwen tokenizer path. LFM2.5-26 does not exist in the repo yet. BenchLoop has no valid completed gate. HANDOFF.md lists its defects, including an undefined `args.install_ok`, evidence written too late, and stale oMLX assumptions.

## Data shapes

Every shape has one producer and one consumer. These are type sketches, not implementations.

- `ConversationEnvelope = {request_id, task_id, messages, profile, budgets, available_tools}`. Produced by the router normalizer. Consumed by the resolver and packet compiler.
- `RouteRule = {id, match, lane, lane_input_budget, lane_output_budget, timeout_s, fallback_lane, enabled}`. Produced by the route table. Consumed by the lane resolver.
- `LaneDecision = {rule_id, lane, reason_codes, rule_version, lane_budget, fallback_lane}`. Produced deterministically by the lane resolver. Consumed by the packet compiler or direct-lane adapter. This selects the execution lane. It never selects a tool or specialist agent.
- `DecisionPacket = {request_id, task_class, goal, relevant_context, constraints, allowed_actions, expected_result_schema, action_budget, trace_id, packet_hash}`. Produced by the packet compiler. Consumed by Qwen and the action validator.
- `ActionDecision = {kind, action_name, arguments, answer, confidence, packet_hash, action_budget, follow_up_budget}`. Produced once by Qwen when the selected lane is `qwen_coordinator`. Consumed by the action validator and dispatcher. This is the only model-made action-selection decision.
- `WorkerJob = {job_id, lane_rule_id, action, packet, timeout_s, max_tool_calls, isolation, idempotency_key}`. Produced by the dispatcher. Consumed by the target adapter.
- `WorkerResult = {job_id, status, answer, artifact_refs, tool_calls, evidence, confidence, error}`. Produced by the target adapter. Consumed by the validator.
- `CompactObservation = {request_id, step, wall_ms, prompt_tokens, completion_tokens, tps, acceptance_summary, artifact_ref}`. Produced by the dispatcher after each execution step. Consumed by telemetry and the action validator. Large payloads never ride in this shape. The `artifact_ref` field points at an append-only artifact on disk. This is how observations stay compact and how replay stays possible.
- `FinalResponse = {request_id, answer, artifact_refs, citations, lane_trace, action_trace, usage, status}`. Produced by the validator. Consumed by the client as an OpenAI-compatible response plus trace headers.

## Module boundaries

- `router/` owns ingress, normalization, route resolution, packet compilation, dispatch, validation, and telemetry. It never imports DFlash internals.
- `server/dflash_server.py` stays the inference owner, unchanged except for contract-level compatibility tests.
- `benchmark.sh` and `results.json` stay the benchmark owners. `.audit/overnight-2026-08-31/hillclimb.py` and its `benchloop` wrapper stay the unattended measurement owners, after repair.
- Pi stays the full control profile.

## Caller flow

1. Client or Pi sends an OpenAI-compatible chat request to the router.
2. The normalizer builds a ConversationEnvelope and applies hard budgets.
3. The lane resolver evaluates ordered RouteRule records deterministically and emits a LaneDecision. The lane is direct LFM chat, Qwen coordinator, a fixed worker, or a visible failure lane.
4. If the lane is Qwen coordinator, the packet compiler builds and hashes a compact DecisionPacket.
5. Qwen emits one ActionDecision. It chooses one allowed tool, one specialist agent, or a direct answer. Qwen does not choose the lane.
6. The dispatcher executes at most one selected action and captures a CompactObservation referencing artifacts.
7. If the action validator rejects the result and the lane rule grants a follow-up budget, Qwen may receive that compact observation once and emit one final ActionDecision. Otherwise the request fails visibly.
8. The validator emits the FinalResponse with separate lane and action traces.

## Principles

- The router makes one deterministic lane decision per request. Qwen makes no lane decision.
- Qwen makes one bounded action-selection decision only inside the Qwen coordinator lane. A lane rule may grant one final follow-up decision after a compact observation. No other Qwen loop exists.
- Execution is idempotent. Every WorkerJob carries an `idempotency_key` derived from the request id, lane rule id, packet hash, and action step. Retries with the same key reuse the recorded artifact instead of re-running a side effect.
- Observations reference artifacts. CompactObservation carries pointers, not payloads. Bench evidence, route traces, and worker outputs are append-only files with unique run ids. One writer per artifact.
- The benchmark is frozen before any harness change. BenchLoop is repaired before any unattended claim.
- Ornith is an optional isolated candidate. No download, no shared state, no production route, no benchmark mixing.

## Mac resource envelope

The target machine is a MacBook Pro M4 Pro with 48GB unified memory. Recorded Qwen evidence reached 19.49GB peak memory. Local model processes share unified memory with macOS, Docker, the KV cache, Metal allocations, and filesystem caches. Large models and Hugging Face caches live on `/Volumes/BBALL_GM_DATA`. The plan therefore assigns every hardware-affecting operation to one exclusive hardware lane.

- One named hardware-lane owner holds a host-wide lock for every model start, stop, restart, live request, benchmark, download, cache mutation, or model deletion. No other worker or subagent may perform those operations.
- Offline code, documents, schemas, and fixtures may run in parallel because they do not acquire the hardware lock or touch model caches.
- Only one local model server or model process may exist while the lock is held. Qwen DFlash, LFM, Ornith, and GGUF sidecars use separate time slots.
- Dockerized Pi may call the leased server during the hardware owner's run. Pi must not start a local model process. Pi and BenchLoop never use the serialized DFlash worker concurrently.
- The host-wide lock records owner, process id, model, port, model path, run id, action, start time, and expected cleanup. Stale-lock recovery verifies that the recorded process is gone before taking ownership.
- Preflight inventories model processes and known ports, reads memory pressure and swap, checks `/Volumes/BBALL_GM_DATA` availability and free space, checks Docker disk use, and verifies the requested model and artifact budget.
- Every model switch is stop, verify process and Metal release, release or update the lock, start, health check, run, stop, and verify release. The next model never starts until the prior process and Metal allocation are gone.
- Downloads and model caches stay on `/Volumes/BBALL_GM_DATA`, outside Docker images. Each candidate records quant filename, expected transfer size, installed size, temporary download overhead, cache path, minimum remaining free space, and deletion path before approval.
- Raw artifacts use per-run directories and retention limits. Summaries and frozen controls are kept. Replaceable logs and failed partial downloads are removable only through a hardware-lane cleanup operation.
- Context size is a measured parameter, not a model-card entitlement. Start with the smallest shared context that covers the corpus. Increase it only after memory, swap, and latency evidence passes.

The initial thresholds are conservative gates, not performance claims. Use the recorded 19.49GB Qwen peak as evidence that Qwen fits alone, not evidence that another model fits beside it. Require at least 8GB of reported free unified memory after model load before a benchmark begins. Abort if memory pressure becomes red, swap grows continuously across samples, the model becomes unresponsive, the external volume disappears, or free space falls below the run budget. Replace these provisional thresholds only with measured evidence from this Mac.

## Alternatives considered

- Candidate A, typed bounded coordinator state machine. Rejected as base. Strongest at modeling retries and longer workflows, which the contract does not ask for. Costs mutable state and replay complexity. Two typed records and a follow-up counter capture the needed rigor.
- B1, declarative rules only. Simplest to freeze and diff. Chosen as the route mechanism, with B3's classifier as a later optional profile, not the base.
- B3, rules plus an LFM2.5-26 classifier. More flexible on nuanced context. Classifier drift costs replay determinism, so it waits for phase 6 evidence.
- B2, full provider-adapter router. More adapter code than the current single Qwen target justifies. Revisit if a second model target lands.

## Scope exclusions

- No implementation in this planning assignment.
- No coordinator state machine, no durable workflow database, no cross-request mutable orchestration state.
- No changes to DFlash inference internals or the active Qwen server configuration. No model downloads, including Ornith and official Qwen GGUFs.
- No unattended tuning until BenchLoop repair and durable evidence gates pass.
- No claim that wall-time BenchLoop numbers equal decode throughput. Server `dflash.generation_tps` stays the primary optimization metric.
- No concurrent Pi and BenchLoop gates against the serialized DFlash worker.

## Throughput checkpoint dimensions

Every checkpoint records these dimensions. A checkpoint row missing any dimension fails the gate.

1. Profile. Full Pi control, slim LFM coordinator, slim plus Qwen packet, hosted capable subagent control.
2. Task. Bounded retrieval, exact extraction, classification, log interpretation, one permitted tool operation, coding escalation control.
3. Route. Direct LFM, worker, Qwen packet decision, Qwen escalation.
4. Context size. Tiny under 1K tokens, Pi-like around 8.5K, bounded long context.
5. Quality. Correctness, tool-call validity, evidence completeness, escalation rate.
6. Performance. TTFT, prefill and decode tokens per second, total wall time, queue wait, prompt and completion tokens, memory and swap.
7. Reliability. Timeouts, malformed packets, worker failures, fallback success, replay determinism.

## Feature throughput checkpoint

- Blocking first steps. Freeze the benchmark contract, define the host-wide lock, record the storage budget, and repair BenchLoop before parallel harness work.
- Independent workstreams. Route-table, HTTP-boundary, packet, worker, documentation, and fixture work may run in parallel because they are offline and touch disjoint files.
- Shared mutable state. One hardware-lane owner serializes all model processes, live requests, benchmarks, downloads, and cache changes. Each evidence artifact has one writer and a unique run id.
- Smallest safe decomposition. One owner integrates the code-coupled router feature. Offline workers may own independent code and fixtures. They hand live-test requests to the hardware lane rather than operating models themselves.

## Phase order

0 freeze contract, 1 BenchLoop repair (hard gate), 2 route table, 3 router boundary, 4 packets, 5 workers, 6 profiles, 7 Ornith (optional, never blocking), 8 replay gates, 9 testing harness. Phases 2 through 5 may proceed in parallel after phase 1 closes, since each touches disjoint files. Phase 6 requires full-Pi control results first. No phase touches more than three files.

## Applicable skills

- `skill://pstack-omp` for role routing and briefs.
- `skill://how` over Pi routing, BenchLoop, and the DFlash API before changing each subsystem.
- `skill://architect` for the typed router contracts and module boundaries before implementation.
- `skill://interrogate` before shipping if route precedence, fallback policy, or the Qwen follow-up budget remains contested.
- `skill://unslop` for every prose surface and `/deslop` over each diff before commit.
- `skill://show-me-your-work` for the benchmark decision trail and accepted tradeoffs.
- The Babysit playbook after opening the pull request.
- `skill://unslop` for any prose written into these documents.
- `skill://blast-radius` before any edit to shared config or server files.
- `skill://find-not-invent` verdicts already recorded. ADOPT-EXISTING for `benchmark.sh`, `results.json`, `hillclimb.py`, `benchloop`, `dflash_server.py`. BUILD-JUSTIFIED only for the new router boundary and the Pi smoke gate.
