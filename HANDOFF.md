# Qwen3.8 27B DFlash2 and Pi integration handoff

## Purpose

This document captures the local benchmark, server integration, failures, verified results, and remaining work for running the uncensored Qwen3.8 27B MLX model with DFlash2 on the M4 Pro and using it through the Dockerized Pi coding agent.

## Current state

- The OMP-managed stock DFlash server is stopped.
- The oMLX DFlash server is stopped.
- The separate llama.cpp server that formerly ran on port `8082` is no longer listening (verified 2026-09-02).
- No overnight optimization loop is running.
- No durable automatic-restart service was installed for the stock DFlash server.
- Findings through 2026-09-02 were committed (handoff docs); server/audit code remains uncommitted.

## Findings update 2026-09-02

- Qwen stock DFlash server (pid 7592, port 8083) was stopped on Jamie's order; process gone, listener free, no generation process above 2 GB RSS, model memory released.
- Overnight hillclimb kept nothing; policy unchanged. H02 (+7.29%) cleared the paired line but was left unaccepted because D03 marked that regime confounded and the frozen threshold was missed; only H08 (+5.37%) and H08b (+5.32%) were rejected for being below the current paired keep line. Same-regime controls ran ~23.0 tps vs the 20.07 frozen baseline; the shift is unattributed, so cross-regime comparisons stay invalid.
- Implementation attribution: installed `dflash 0.1.0` is the official Z Lab project (z-lab/dflash) and is current on PyPI — the only implementation proven to serve Qwen3.8 + incoai/Qwen3.8-27B-DFlash2. bstnxbt/dflash-mlx v0.1.10 is a separate implementation with no Qwen3.8/DFlash2 support in its README; its KV-quant and long-context draft levers require a compatibility probe first. H11 (no KV-quant path in Z Lab dflash 0.1.0) stands.
- oMLX: the installed 0.6.2 fork Metal-timeouts on Qwen3.8 DFlash prefill capture (section below). Releases 0.6.3 and 0.6.4 name no fix for that path; a retry is gated on a code-level diff of the fork's prefill/capture path, not release notes.
- Session-level details: `handoff-2026-09-02.md`.

## Hardware

- MacBook Pro with Apple M4 Pro.
- 48 GB unified memory.
- Large models and Hugging Face caches are kept on `/Volumes/BBALL_GM_DATA`.

## Models

### Target

```text
/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit
```

Source repository:

```text
orcarouter/Qwen3.8-27B-Uncensored-MLX
```

The repository root also contains a copy of the 4-bit model. The root and `/4-bit` configurations and indexes describe the same 4-bit affine model with group size 64. The explicit `/4-bit` path was used by the final stock DFlash server.

### DFlash2 draft

```text
incoai/Qwen3.8-27B-DFlash2
```

External cache:

```text
/Volumes/BBALL_GM_DATA/models/models--incoai--Qwen3.8-27B-DFlash2
```

The draft was runtime-quantized to 4-bit with group size 64.

## Published benchmark

Repository:

```text
https://github.com/JamieJ5926/qwen3.8-27b-uncensored-dflash2-m4-pro
```

Hugging Face dataset:

```text
https://huggingface.co/datasets/hamiejuice/qwen3.8-27b-uncensored-dflash2-m4-pro-benchmark
```

Local repository:

```text
/Users/jamie/Documents/Projects/active/local/qwen3.8-27b-uncensored-dflash2-m4-pro
```

Latest published commit before this session's uncommitted server work:

```text
19c2fa1 Update benchmark with 20-sample results
```

## Verified command-line benchmark

Environment:

```text
/Users/jamie/models/mlx-venv
```

Installed versions:

- `dflash==0.1.0`
- `mlx==0.32.0`
- `mlx-lm==0.31.3`
- `mlx-metal==0.32.0`

Verified benchmark command:

```bash
env -u HF_HUB_OFFLINE \
  HF_HUB_CACHE=/Volumes/BBALL_GM_DATA/models \
  HF_DATASETS_CACHE=/Volumes/BBALL_GM_DATA/hf-datasets \
  PIP_CACHE_DIR=/Volumes/BBALL_GM_DATA/pip-cache \
  /Users/jamie/models/mlx-venv/bin/dflash benchmark mlx \
    --model /Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit \
    --draft incoai/Qwen3.8-27B-DFlash2 \
    --draft-bits 4 \
    --block-size 5 \
    --max-new-tokens 256 \
    --temperature 0 \
    --reasoning off \
    --dataset humaneval \
    --max-samples 20
```

Twenty-sample paired HumanEval result:

| Metric | Result |
|---|---:|
| Plain MLX | 12.35 tok/s |
| DFlash2 | 25.48 tok/s |
| Speedup | 2.06x |
| Mean acceptance | 4.87 of 5 |
| Fully accepted five-token blocks | 94.9% |
| Paired progress time | 9m54s |
| Full wall time including loading | 825.84s |

This remains the strongest reproducible benchmark result.

## Other measured runtimes

### Plain uncensored MLX 4-bit

| Metric | Result |
|---|---:|
| Prompt processing | 40.067 tok/s |
| Generation | 12.993 tok/s |
| Peak memory | 15.541 GB |

### llama.cpp GGUF with MTP

The Q3_K_XL GGUF model with `--spec-type draft-mtp` produced about `7.4–7.9 tok/s`.

Historical target-only llama.cpp performance for the same model was about `10.2–10.4 tok/s`.

Conclusion: MTP is a slowdown on the tested llama.cpp build. Do not use it for this model on this Mac unless a newer implementation is independently measured.

## oMLX investigation

Installed application:

```text
/Applications/oMLX.app
```

Version:

```text
0.6.2-zlab-dflash2
```

The app has a valid Developer ID signature. Gatekeeper rejects the inner application because the release does not receive matching notarization acceptance. The bundled CLI works without launching the GUI.

Bundled CLI:

```text
/Applications/oMLX.app/Contents/MacOS/omlx-cli
```

The server was successfully started on port `8082`, discovered the target and draft, and exposed OpenAI-compatible endpoints.

### oMLX configuration corrections

Initial DFlash configuration used the route-safe discovered model ID:

```text
incoai--Qwen3.8-27B-DFlash2
```

This failed because the DFlash loader treated it as a Hugging Face repository ID, and Hugging Face rejects repository IDs containing `--`.

Correct value:

```text
incoai/Qwen3.8-27B-DFlash2
```

The next load failed because draft activation quantization was set to 4 bits. The bundled runtime accepts activation widths 16 or 32 for this path.

Corrected quantization:

```text
w4a16:gs64
```

The oMLX DFlash engine then loaded successfully.

### oMLX Metal timeout

The first real DFlash completion failed before emitting a token.

Failure location:

```text
dflash_mlx/engine/spec_epoch.py
_run_prefill_events
sampling.py: mx.eval(logits, *captured.values())
```

Error:

```text
kIOGPUCommandBufferCallbackErrorTimeout
```

The request had only 28 prompt tokens and a 128-token output cap. Memory pressure was not the cause. Peak process footprint was below the Metal working-set limit, and system memory remained available.

Controlled probes:

- `DFLASH_PREFILL_STEP_SIZE=1` still timed out.
- Draft activation width 32 still timed out.
- The same model, draft, block size, prompt class, and output cap succeeded with stock `dflash==0.1.0` after oMLX was stopped.

Conclusion: the timeout is specific to the forked oMLX `dflash-mlx` target-prefill and hidden-state-capture path. It is not a general model, Metal, or stock DFlash failure.

After a Metal timeout, later submissions in the same process fail with `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored`. A process restart is required to reset the Metal context.

## Architecture decision

Three approaches were considered:

1. Build a small persistent OpenAI-compatible server around stock DFlash.
2. Patch the signed oMLX bundle and its forked capture path.
3. Put the one-shot `dflash generate` CLI behind a request-per-process proxy.

The selected design was the stock DFlash server.

Reasons:

- Stock DFlash was already proven with the exact target, draft, and hardware.
- Models could remain resident between requests.
- The implementation could reuse `mlx_lm.server` and the standard library.
- It avoided patching a signed app bundle and maintaining a fork-specific overlay.
- Request-per-process loading would cost several minutes per request and was unsuitable for Pi.

## Stock DFlash server

Files:

```text
qwen3.8-27b-uncensored-dflash2-m4-pro/server/dflash_server.py
qwen3.8-27b-uncensored-dflash2-m4-pro/server/launch.sh
```

The first implementation attempt was an invalid stub with hard-coded metrics and incorrect `mlx_lm.server` calls. It was discarded and replaced.

The final server:

- Loads the target and draft once.
- Uses stock `dflash.model_mlx.stream_generate`.
- Serializes generation through one worker.
- Supports non-streaming and streaming OpenAI-compatible chat responses.
- Exposes real DFlash metrics from `GenerationResponse` values.
- Exposes health, model listing, and DFlash statistics.
- Fails closed on Metal timeout and submissions-ignored errors.
- Calls `os._exit(1)` after a poisoned Metal error so an external supervisor can restart it.

Endpoints:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
POST /v1/completions
GET  /v1/dflash/stats
```

Model ID:

```text
qwen3.8-27b
```

Default launch:

```bash
cd /Users/jamie/Documents/Projects/active/local/qwen3.8-27b-uncensored-dflash2-m4-pro/server
TARGET_PATH=/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit \
CACHE_ROOT="$HOME/.cache/huggingface" \
./launch.sh --host 127.0.0.1 --port 8082
```

The server is currently stopped.

## Stock server verification

Independent verification passed the following checks before the server was stopped:

- Health returned ready.
- `/v1/models` listed `qwen3.8-27b`.
- Two consecutive requests ran under the same PID with no reload.
- Malformed JSON returned HTTP 400.
- A successful request immediately followed malformed input.
- Concurrent requests serialized. The second request waited about one complete first-request interval.
- Real DFlash statistics matched accumulated response metrics.
- Dockerized Pi listed and used the local model.

Example consecutive requests:

| Request | Generation | Acceptance | Wall |
|---|---:|---:|---:|
| First short request | 19.82 tok/s | 2.0 | 1.094s |
| Second short request | 19.85 tok/s | 2.0 | 1.078s |

High-acceptance serialized workload:

| Metric | Result |
|---|---:|
| Completion | 160 tokens |
| Generation | 33.36 tok/s |
| Mean acceptance | 4.969 |
| Wall | 5.687s |

Representative Pi coding request:

| Metric | Result |
|---|---:|
| Prompt | 413 tokens |
| Completion | 47 tokens |
| Generation | 30.56 tok/s |
| Prefill | 97.15 tok/s |
| Mean acceptance | 4.6 |
| Wall | 6.127s |
| Peak memory | 19.49 GB |

The generated answer correctly implemented a clamp function with inclusive bounds and a `ValueError` for `low > high`.

## Frozen server baseline

Audit directory:

```text
qwen3.8-27b-uncensored-dflash2-m4-pro/.audit/overnight-2026-08-31
```

Measured results file:

```text
qwen3.8-27b-uncensored-dflash2-m4-pro/.audit/overnight-2026-08-31/results.tsv
```

The synthetic gate row was removed. `results.tsv` contains only the measured baseline.

Frozen workload:

- One warmup request.
- Five identical coding requests.
- Temperature 0.
- Maximum 256 output tokens.
- Actual deterministic completion length was 180 tokens.

Baseline:

| Metric | Result |
|---|---:|
| Median generation | 25.85 tok/s |
| Range | 24.34–27.71 tok/s |
| Median wall | 8.06s |
| Mean acceptance | 4.163 |
| Prompt | 53 tokens |
| Completion | 180 tokens |

The output was token-identical across measured repetitions. The performance spread therefore represents runtime noise, not output variation.

## Pi integration

Pi repository:

```text
/Users/jamie/Documents/Projects/active/local/pi-docker
```

Configured route:

```text
provider: qwen-local
model: qwen3.8-27b
base URL: http://host.docker.internal:8082/v1
API: openai-completions
```

Model-list command:

```bash
cd /Users/jamie/Documents/Projects/active/local/pi-docker
docker compose run --rm pi --list-models
```

Explicit local model command:

```bash
docker compose run --rm pi \
  --model qwen-local/qwen3.8-27b \
  -p \
  --mode json \
  --no-session \
  --thinking off \
  'Return only LOCAL_DFLASH_OK.'
```

This explicit route returned:

```text
LOCAL_DFLASH_OK
```

The corresponding `artifact://594` pointer is session-only evidence. It is not a durable audit file.

Pi adds a large system prompt. The observed prompt was 8,496 tokens even for a tiny user request. An 8,192-token request cap therefore rejected fresh Pi sessions.

The server default prompt cap was raised to:

```text
16384
```

The output request cap was set to:

```text
1024
```

The generation watchdog was set to:

```text
600 seconds
```

These cap changes were syntax-checked and the Pi local route passed after the 16K prompt-cap restart.

## Important Pi caveat

Some Pi smoke attempts accidentally used the default `cli-proxy/openrouter.glm-5.3-flash` provider rather than the local model. They are not evidence for the DFlash route.

Only runs that explicitly report these values count:

```text
provider=qwen-local
model=qwen3.8-27b
```

## BenchLoop

Installed CLI:

```text
/Users/jamie/.local/bin/benchloop
```

Version:

```text
bench-loop 0.2.3
```

Correct OpenAI-compatible command shape:

```bash
/Users/jamie/.local/bin/benchloop run \
  --model qwen3.8-27b \
  --endpoint http://127.0.0.1:8082 \
  --provider openai_compat \
  --harness pi \
  --suites speed
```

The endpoint must not end in `/v1`; the provider appends the OpenAI paths.

BenchLoop local mode uses non-streaming wall-time-based throughput. It is not a decode-only measurement. Server-reported `dflash.generation_tps` remains the primary optimization metric.

### BenchLoop gate result

The speed gate did not complete successfully.

The initial Pi and BenchLoop gates were launched concurrently against a single serialized worker. Pi submitted 25K to 26K-token prompts that took about five minutes each. BenchLoop's short probes waited behind them for roughly 302 to 349 seconds before generating normally in about 1.5 to 1.8 seconds.

Later isolated BenchLoop execution was still running and was cancelled. No valid completed BenchLoop speed artifact was produced.

Conclusion: BenchLoop remains an unmet quality and wall-time gate. Its launch alone is not a pass.

## Request cancellation and unattended safety

The generation worker checks the inherited generation context between emitted tokens and closes the DFlash generator when the context reports a stop. This is useful for streaming disconnects.

A non-streaming client disconnect cannot be reliably detected until the server writes the final response. A client-side timeout may therefore leave generation running until completion.

The reliable safeguards are:

- Prompt cap.
- Output cap.
- Generation watchdog.
- Process exit on poisoned Metal state.
- External process restart.

The final server has the first four. A durable external restart service was not created or tested.

The server was OMP-managed during verification. That was not a durable supervisor. After OMP shutdown or a process crash, no installed service guarantees recovery.

Do not call the server unattended-safe until a LaunchAgent or another external supervisor has passed a forced-watchdog restart test and a successful post-restart completion.

## Current port 8082 ownership
> SUPERSEDED 2026-09-02: port 8082 was verified free (no listener); the llama.cpp service is gone. See "Findings update 2026-09-02" at the top.

At the end of the work:

- `dflash-stock-server` was stopped.
- `omlx-dflash` was stopped.
- A separate llama.cpp server remained running on port `8082`.

The llama.cpp server was not stopped.

Before restarting the stock DFlash server, identify and stop or move the existing llama.cpp service so both servers do not contend for the same port.

## Best optimization hypothesis
> SUPERSEDED 2026-09-02: the overnight hillclimb measured block sizes 2-6. Block 4 was the best (+5.37%/+5.32% vs same-wave paired controls, below the +7% keep line); block 6 lost 13.63%; block 8 was never tested. The standing plan is to re-freeze the baseline on the current regime, then rerun the block-4 pair. See "Findings update 2026-09-02" at the top and `handoff-2026-09-02.md`.

The draft checkpoint declares a native block size of 8. The verified server baseline used block size 5.

First experiment:

```bash
./launch.sh --host 127.0.0.1 --port 8082 --block-size 8
```

Expected mechanism:

- A target verification step may verify more draft tokens at nearly the same target-forward cost.
- The baseline workload already fully accepted five tokens in many cycles.
- Extending the block could increase tokens per target step.

Estimated result, not yet measured:

```text
31–34 tok/s
```

Keep criteria:

- Median generation at least `27.4 tok/s`.
- Deterministic completion remains exactly 180 tokens and 614 characters.
- Finish reason remains `stop`.
- No server error increase.
- Pi explicit local route passes.
- BenchLoop gate passes or is replaced by a bounded equivalent agreed in advance.

Revert if any criterion fails.

## Other optimization hypotheses
> SUPERSEDED 2026-09-02: item 1 — block 6 was measured and lost 13.63% (H07); block 7 was ineligible and never run (H10). Item 2 — only draft bits 3 was measured (H09, -4.26%); bits 5/6 were never run. One-change-at-a-time policy unchanged. See "Findings update 2026-09-02" at the top.

Potential one-change experiments after block size 8:

1. Test block sizes 6 and 7 if 8 improves speed but increases runtime variance.
2. Test draft weight quantization bits 3, 5, and 6.
3. Compare unquantized draft support if the server parser and loader are extended cleanly.
4. Measure a higher-acceptance coding workload separately from the fixed baseline.
5. Measure prompt-length bands because Pi's real system prompt is about 8.5K tokens.
6. Reduce per-request HTTP and streaming overhead only after model decode is optimized.

Do not stack hypotheses. Apply one change, measure, keep or revert, then continue.

## Why overnight tuning did not run
> HISTORICAL / SUPERSEDED 2026-09-02: this section records the EARLIER blocked overnight launch from the pre-hillclimb session. The overnight hillclimb subsequently ran (Aug 31 - Sep 1) under the authorized stop predicate with `.audit/overnight-2026-08-31/hillclimb.py` as the frozen harness: it completed multiple paired trials (some attempts produced no treatment wave, one hypothesis was ineligible, and one pivot was disposed offline), and no performance change was accepted. See "Findings update 2026-09-02" at the top.

The planned unattended predicate required:

- At least 10 controlled iterations.
- A frozen, fail-closed measurement harness.
- Pi success through Docker.
- DFlash proven per request.
- BenchLoop or an agreed quality and wall-time gate.
- Automatic recovery from a watchdog or Metal failure.

The following were achieved:

- Frozen server baseline.
- Real DFlash metrics.
- Explicit local Pi route.
- Bounded prompt and output settings.
- Queue-drain verification.
- Grounded optimization hypotheses.

The following remained incomplete:

- Valid completed BenchLoop gate.
- Durable external supervisor.
- Forced-watchdog restart proof.
- Ten controlled optimization iterations.

The overnight run was therefore not launched. The acceptance predicate was not relaxed.

## Files created or changed during this work

Uncommitted server files:

```text
qwen3.8-27b-uncensored-dflash2-m4-pro/server/dflash_server.py
qwen3.8-27b-uncensored-dflash2-m4-pro/server/launch.sh
```

Uncommitted audit directory:

```text
qwen3.8-27b-uncensored-dflash2-m4-pro/.audit/overnight-2026-08-31/
```

Important audit files:

```text
results.tsv
frozen-config.json
baseline-snapshot.json
evidence/
```

The audit harness went through several overlapping edits. Before relying on `hillclimb.py`, inspect and repair it. Known historical defects included:

- Undefined `args.install_ok` in the BenchLoop command.
- Evidence paths referenced before files were written.
- Wrapper artifacts written only after subprocess completion, losing evidence on timeout.
- Incorrect or stale assumptions about oMLX model-status entries.

The current stock server should prove DFlash per request through response metrics and `/v1/dflash/stats`, not through oMLX helper-model residency.

## Recommended next session
> SUPERSEDED 2026-09-02: items 1-2 (port 8082 ownership) are moot — 8082 is free. Item 10 (block size 8) is superseded by the overnight block-size results. Current next steps live in `handoff-2026-09-02.md`.

1. Confirm which process owns port `8082`.
2. Stop or move the llama.cpp server if the stock DFlash server will use `8082`.
3. Review `server/dflash_server.py` and the audit harness from the current working tree.
4. Install a LaunchAgent or another external supervisor for the stock server.
5. Force a short watchdog expiry.
6. Verify automatic restart, model reload, and a successful next completion.
7. Repair the BenchLoop wrapper and run the speed gate alone with no Pi or other clients.
8. Record a durable Pi explicit-local artifact.
9. Re-run the five-request baseline after all harness repairs.
10. Start with the block-size-8 experiment.
11. Continue one hypothesis at a time until the stop predicate is met.

## Workstreams agreed for today
> SUPERSEDED 2026-09-02: "starting with block size 8" is superseded by the overnight block-size results; the tuning plan now runs through the re-frozen baseline and the block-4 pair. See "Findings update 2026-09-02" at the top.

The next work session has three connected workstreams. They must use one shared benchmark contract so model tuning, prompt changes, and subagent routing can be compared without confounding variables.

1. Benchmark and improve the Qwen3.8 27B MLX plus DFlash2 configuration. Freeze a fresh full-Pi, block-size-5 control before changing anything. Then test one performance variable at a time, starting with block size 8.
2. Continue building the local-model harness on Pi. Preserve the current full Pi profile as the control, then create a separate slim profile that replaces or bypasses Pi's large built-in system prompt while retaining the minimum tool and correctness contract needed for local coding work.
3. Explore subagents for bounded retrieval, extraction, classification, log analysis, and simple tool operations. LFM2.5 is one local candidate, but early harness and ability testing does not have to use LFM or remain fully local. OpenAI or another capable hosted model may be used as the control subagent so the harness behavior can be separated from LFM model quality.

Shared measurements should include the same task corpus, correctness scoring, repetitions, warmup policy, timeouts, prompt and completion tokens, time to first token, prefill and decode rates, total wall time, memory and swap, tool-call validity, and raw evidence. Change only one variable between adjacent comparisons.

## Deferred subagent evaluation

Do not run this evaluation until work resumes.

- Start by exercising Pi's normal subagent mechanism with a capable hosted model such as OpenAI. This establishes whether the delegation flow, task contract, tools, result collection, and escalation behavior work independently of LFM.
- Add only a small extension if the existing `pi-subagents` mechanism cannot expose the measurements or routing controls needed for the evaluation. Extend the existing owner rather than building a second delegation framework.
- Use organic bounded tasks covering file discovery, exact extraction, classification, log interpretation, and one permitted tool operation. Keep coding, architecture, difficult debugging, and final review with Qwen or another strong model.
- Once the normal-subagent control works, run the identical corpus through the existing LFM providers on ports `8090` through `8094`. Compare correctness, tool-call validity, wall time, and escalation rate against the hosted control.
- Route failures, malformed tool arguments, missing evidence, unsupported task classes, and low-confidence results to the stronger subagent or Qwen. Do not depend on the small model to initiate its own escalation.
- The likely target design is a slim Pi coordinator, bounded subagent jobs, mechanical result validation, and Qwen escalation. LFM is optional in the first harness tests and becomes eligible only for task categories where it meets the shared correctness threshold.

## Discussion questions

- Is the goal maximum raw decode throughput, maximum end-to-end Pi task throughput, or the best balance of speed and agent reliability?
- Should BenchLoop remain a mandatory gate if its local speed number is wall-time-based rather than decode-only?
- Should Pi use a smaller system prompt for the local model to reduce the 8.5K-token prefill cost?
- Is single-request serialization acceptable, or should the design optimize queue throughput?
- Should the stock server become a maintained project artifact, or remain a local experiment?
- Is patching oMLX still valuable after the stock server proved the model pair works?
- Should block size 8 become the first measured optimization, or should the supervisor and harness receive a separate hardening pass first?
