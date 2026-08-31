# Pi integration preparation (offline, nothing executed)

Status: prepared 2026-08-31 by the offline harness chat. No Pi command was run,
no Docker build or compose invocation was executed, and no model endpoint was
contacted. This document is the wiring map the future live chat executes from.

## Pi configuration map

Repository: `/Users/jamie/Documents/Projects/active/local/pi-docker`.

- Provider registry: `config/models.json`.
- Local Qwen provider: `qwen-local`, baseUrl `http://host.docker.internal:8083/v1`,
  api `openai-completions`, apiKey `local`, compat flags disable store,
  developer role, reasoning effort, streaming usage, strict mode, and use
  `max_tokens` (`config/models.json:3-26`).
- Local Qwen model alias: `qwen3.8-27b`, context window 262144, maxTokens 8192,
  reasoning true (`config/models.json:16-25`).
- Interactive default: provider `qwen-local`, model `qwen3.8-27b`
  (`config/settings.json:3-6`). Subagent default remains
  `cli-proxy/openrouter.glm-5.3-flash` (`config/settings.json:9-12`).
- LFM providers: `lfm-swarm-1` through `lfm-swarm-5` expose
  `lfm-heretic` through OpenAI-compatible endpoints on
  `host.docker.internal:8090/v1` through `:8094/v1`
  (`config/models.json:169-289`).
- Docker host transport: `host.docker.internal`, no extra host-gateway mapping
  (`docker-compose.yml:1-24`).
- Full Pi control profile: the normal image copies the whole `config/` tree into
  `/home/bun/.pi/agent/`, installs `pi-subagents`, mounts sessions, workspace,
  and the brain vault, and launches with `--no-skills`, `--no-prompt-templates`,
  `--no-approve`, and the persistent brain prompt (`Dockerfile:1-22`,
  `docker-compose.yml:3-18`). The RunPod variant is the slimmed profile and is
  not part of this route.

## Port truth

Active local Qwen truth is port 8083 everywhere that is live configuration:
Pi `config/models.json`, router tests, the router default
`router/transport.py DEFAULT_QWEN_BASE_URL`, and the hillclimb controller base
(`DEFAULT_ENDPOINT`). Historical 8082 strings remain in frozen audit records
(`.audit/overnight-2026-08-31/baseline-snapshot.json`, `frozen-config.json`) and
in Pi-repo benchmark/redteam scripts (`bench-local.py`, `redteam/*`). Those are
records, not configuration; they stay untouched so the audit trail stays
honest. The frozen-config endpoint field is inert: the hillclimb controller
reads its endpoint from `BASE`/`DFLASH_ENDPOINT`, not from the frozen file.

## Wiring delta required in Pi

None for the existing direct Qwen control profile. `config/models.json` already
routes `qwen-local` to `host.docker.internal:8083/v1` with alias
`qwen3.8-27b`.

The router now has an offline-complete LFM mouth. `lfm_direct` calls an
explicitly configured LFM transport, `qwen_coordinator` keeps the Qwen action
path, `fixed_worker` uses an injected adapter keyed by route-rule id, and
`fail_visible` returns an error. Missing LFM or worker configuration fails
visibly. It never falls back to Qwen.

The future host-side router command uses one selected LFM endpoint:

```sh
LFM_BASE_URL=http://127.0.0.1:8090/v1 \
LFM_MODEL=lfm-heretic \
python3 -m router serve \
  --qwen-url http://127.0.0.1:8083/v1 \
  --lfm-url http://127.0.0.1:8090/v1 \
  --lfm-model lfm-heretic
```

Do not run that command until the hardware owner assigns the LFM slot. The five
Pi LFM entries are alternatives, not concurrent local backends.

## Prepared live validation (do not run in the offline chat)

One-shot against the explicit local provider and model:

```sh
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" \
docker compose -f /Users/jamie/Documents/Projects/active/local/pi-docker/docker-compose.yml \
  run --rm pi -p --no-tools --no-session --mode json \
  --model qwen-local/qwen3.8-27b \
  "Reply with the exact string LOCAL_QWEN_OK"
```

Command shape grounded in `redteam/report-smoke.md:38-40` (recorded
explicit-provider invocation) and `redteam/driver.py:194-199` (compose run
shape). Acceptance requires all of:

1. The command completes without provider fallback.
2. The response reports provider `qwen-local` and model `qwen3.8-27b`.
3. A default `cli-proxy` run is not accepted as evidence for the local route.
4. If the smoke times out reaching session events, retry once; a second timeout
   is a finding, not a pass.

## Offline configuration validation available today

The Pi repo has no config-lint command and no package.json scripts. The router
side is fully offline. `python3 -m unittest discover -s router/tests -p
'test_*.py'` runs 127 fake-only tests. They cover Qwen action dispatch, direct
LFM routing, fixed-worker routing, fail-visible behavior, durable replay, and
entrypoint composition without binding a socket. `python3 -m router replay`
rebuilds stored results without a model call.
