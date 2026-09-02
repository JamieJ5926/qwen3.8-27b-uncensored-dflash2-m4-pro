# Qwen performance and model-testing handoff

## Goal

Resume the pure-Qwen performance work. Use the existing BenchLoop controller to measure and improve the current Qwen3.8 27B DFlash setup on the M4 Pro. Maximize useful generation speed without breaking correctness, tool calling, reliability, or the one-local-generation-model rule.

Ornith is a later comparison candidate. Do not make it the first task.

## Hard boundary

Every live model call, benchmark, model start or stop, cache mutation, download, move, or deletion belongs to one exclusive hardware lane.

Never:

- Run Qwen and LFM, Ornith, or another local generation model concurrently.
- Touch the reranker. Jamie is moving it to the server.
- Run Pi and BenchLoop concurrently.
- Alter the active Qwen server without Jamie's explicit approval.
- Download official Qwen GGUFs or run the official Qwen A/B.
- Download Ornith without Jamie's explicit approval.
- Create a second benchmark controller beside BenchLoop.
- Change the frozen workload during a hillclimb and compare the new numbers to the old baseline.
- Keep an unmeasured tweak.
- Run Git mutations without Jamie's explicit authorization.

Read-only model-path and configuration discovery may occur outside the hardware slot. Starting or calling a model may not.

## Repository and existing owner

Repository:

`/Users/jamie/Documents/Projects/active/local/qwen3.8-27b-uncensored-dflash2-m4-pro`

Adopt the existing benchmark owner:

- `.audit/overnight-2026-08-31/hillclimb.py`
- `.audit/overnight-2026-08-31/benchloop`
- `.audit/overnight-2026-08-31/evidence/`

Do not build another harness.

## Current model state (recorded launch config, not live state)

> CORRECTED 2026-09-02: this section records the launch configuration and past verification results, not current runtime state. Qwen is currently STOPPED and port 8083 is free (verified 2026-09-02; see the 2026-09-02 findings in `HANDOFF.md`). Before any launch or wave: acquire the hardware lease, take a live process/listener inventory, and only then start the server — never trust these values as proof a server is running.

Qwen:

- Endpoint `http://127.0.0.1:8083/v1`
- Model `/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit`
- Recorded model peak memory `19.49 GB`
- Prior locked smoke returned exact `QWEN_ROUTE_READY`
- Prior smoke usage was 19 prompt tokens and 6 completion tokens

The recorded peak is not current whole-system memory use. Before each benchmark wave, inspect current memory pressure and swap. Run one request at a time initially. Stop if Memory Pressure turns yellow or red, swap rises materially, or server errors increase.

The current process inventory previously showed Qwen as the only generation model. Embedding and reranking processes are separate, but the reranker is off-limits and must not become part of the experiment.

## Benchmark discipline

Use the Hillclimb playbook.

One iteration means:

1. Read the decision log.
2. State one mechanism-based hypothesis.
3. Capture the before result with the frozen harness.
4. Make one bounded change through the authorized owner.
5. Capture the after result with the same harness.
6. Run the correctness and tool-calling gates.
7. Keep the change only if the improvement exceeds noise and every gate remains green.
8. Otherwise revert the change in full.
9. Append the result to `decision.tsv`.

Never stack two unmeasured changes.

## Phase 1. Freeze the ruler

Before tuning, inspect the existing BenchLoop configuration and evidence. Resolve any stale `8082` reference to the configured `8083` endpoint before accepting a new baseline. `8083` is the configured endpoint, not proof of a live server — the server is currently stopped; verify with a live listener inventory at launch time.

Define a workload matrix that represents chat use:

- Short direct answer.
- Medium conversational response.
- Long-context prompt.
- Structured JSON response.
- Tool-selection request using the supported tool schema.
- One error or refusal case that must remain behaviorally stable.

Use deterministic prompts and fixed output limits. Record warmup policy, repetitions, temperature, seed when supported, context size, concurrency, and timeout.

Prove sensitivity before freezing the harness. The short, medium, and long cases must separate in expected latency or throughput. If they do not, fix the workload before tuning.

## Metrics

Choose one primary metric and keep it fixed.

Recommended primary metric:

- Median decode tokens per second across the representative chat matrix.

Required secondary metrics:

- Time to first token.
- End-to-end latency.
- Prompt processing tokens per second.
- Decode tokens per second.
- Peak memory or memory-pressure observation.
- Swap before and after each wave.
- Error rate.
- Exact tool-call validity rate.
- Response-quality gate result.

Report each measurement mode honestly:

- `live_end_to_end` means the full live path ran in one model slot.
- `integration_replay` means prerecorded intents or artifacts drove the integration.
- `staged_local` means separate local model stages were measured independently, including shutdown and switch time.

Do not compare replay or staged timing as if it were live end-to-end timing.

## Stop predicate

Before the first attempt, freeze a numeric target and a minimum attempt count.

Default if Jamie has not supplied a stronger target:

- Run at least 10 measured hypotheses.
- Seek at least a 20 percent improvement in the primary metric over the frozen baseline.
- Preserve all correctness, tool-calling, error-rate, and memory gates.
- Continue past the first plateau while cheap, mechanism-based hypotheses remain.

Do not lower the target after seeing the results.

## Decision log

Keep a gitignored decision log outside revertable source changes. Use one row per attempt:

```text
id	hypothesis	change	before	after	delta	tests	verdict	note
```

Verdict is `kept` or `reverted`.

The log is the run memory. Read it before proposing the next hypothesis.

## Lanes

Use at most four lanes.

### Lane 1. Architecture and hypothesis research

Read-only. Study the current DFlash server, recorded metrics, and upstream capabilities. Distinguish custom LoopSpec or DFlash behavior from upstream llama.cpp or MLX behavior. Preserve tool calling. Return mechanism-based hypotheses, not generic tuning lists.

### Lane 2. BenchLoop and evidence integrity

Offline. Own the frozen workload, config checksum, result parsing, evidence completeness, and regression gates. It may repair a proven BenchLoop defect, but changing the ruler invalidates the old baseline and requires a fresh baseline.

### Lane 3. Quality and tool-call evaluation

Offline except when the hardware lane supplies completed artifacts. Score exact JSON and tool-call validity, response correctness, and regression cases from stored outputs. It must not call the model.

### Lane 4. Exclusive hardware lane

Own every live request and process action. It runs one experiment at a time under the atomic lease. BenchLoop, Pi, Qwen, LFM, and Ornith live activity all serialize here.

## Hardware-lane sequence

For each live wave:

1. Freeze the run ID, command, model, port, model path, cleanup action, measurement mode, and artifact paths.
2. Acquire `/var/tmp/local-model-hardware-lease`.
3. Inventory processes and listeners.
4. Confirm Qwen is the only local generation model.
5. Check Memory Pressure and swap.
6. Confirm Qwen health and `/v1/models` on 8083.
7. Run the frozen benchmark wave.
8. Persist completed or failed evidence atomically.
9. Recheck memory pressure, swap, and server errors.
10. Release the lease.

Stop the wave if process discovery fails, another generation model appears, Memory Pressure turns yellow or red, swap rises materially, the command times out, or correctness gates fail.

## Qwen optimization order

Start with low-risk, measurable categories. Do not assume any is a win.

1. Confirm request serialization and concurrency are not hiding queue time.
2. Separate prompt processing from decode throughput.
3. Test output-token limits and stopping behavior.
4. Test context-size effects with fixed prompt content.
5. Test supported batch, cache, and DFlash settings one at a time.
6. Measure warm versus cold behavior separately.
7. Inspect whether tool schemas materially change prompt-processing cost.
8. Test server parameters only after an explicit change packet and rollback command exist.
9. Combine accepted independent wins only after each has passed alone.

Correctness and simplicity outrank a small throughput gain.

## Ornith gate

Ornith is not authorized merely because this handoff mentions it.

First perform read-only discovery to determine whether an approved Ornith model already exists locally. If it does not, stop and obtain Jamie's explicit download approval.

Before any approved download, record:

- Exact model and quant.
- Transfer size.
- Installed size.
- Temporary download and conversion overhead.
- Destination under `/Volumes/BBALL_GM_DATA`.
- Required free-space floor.
- Exact deletion path.

Before an Ornith run:

1. Acquire the hardware lease.
2. Stop Qwen only within the authorized switch window.
3. Verify the Qwen PID and listeners are gone.
4. Verify Metal memory has released.
5. Start exactly one Ornith process.
6. Record its PID, port, model path, and configuration.
7. Run the same frozen workload and quality gates.
8. Stop Ornith and verify process and Metal release.
9. Restore Qwen if requested.
10. Recheck Qwen health.

Never rank Ornith against Qwen using different workloads or measurement modes.

## Verification gates

A performance result is acceptable only when:

- The frozen-config checksum matches.
- Every required workload dimension completed.
- Median and sample count are recorded.
- The exact model, endpoint, and server settings are recorded.
- Tool calls remain valid.
- Response quality remains within the frozen floor.
- Error rate does not regress.
- Memory pressure and swap remain inside the safety gate.
- The evidence artifact reached a terminal state.

## Done predicate

The Qwen hillclimb is complete when:

- The workload and primary metric are frozen and sensitivity-proven.
- A baseline exists on the current Qwen 8083 setup.
- At least the minimum number of measured hypotheses ran.
- Every attempt is recorded as kept or reverted.
- The final accepted configuration beats baseline by the frozen target or the remaining ideas are documented as marginal.
- Tool calling, correctness, reliability, and memory gates remain green.
- Qwen is restored and healthy at the end.
- Any Ornith comparison, if separately authorized, uses the identical frozen workload and has complete switch and cleanup evidence.

## First action

Read `.audit/overnight-2026-08-31/hillclimb.py`, its frozen configuration, existing `results.tsv`, and completed evidence artifacts. Confirm which endpoint the active frozen config uses. Then define the workload, primary metric, noise threshold, minimum attempts, and stop target before running another model request.
