# Offline coordinator harness handoff

## Resume contract

Use the Poteto **Session pickup** playbook. Read this file and the latest Git
commit first. Do not reconstruct the design from earlier chat transcripts. The
commit containing this file is the durable resume point. The final report from
the pausing chat also supplies its commit id.

## Goal

Continue the local-model coordinator without changing or contacting any model.
The next offline step is Pi integration. Add a non-default Pi provider for the
router, preserve the direct Qwen control profile, and validate the Pi config
without Docker or endpoint calls.

## Boundaries

The completed work used injected fake transports only. It did not call Qwen,
LFM, Ornith, Docker, the reranker, or another endpoint. It did not acquire the
hardware lease or change `server/dflash_server.py`.

Keep those boundaries until a separate hardware-owner chat starts the live
smoke. Never start, stop, restart, download, benchmark, or reconfigure a model
from the offline harness chat.

## Current product

`python3 -m router serve` is the executable OpenAI-compatible front door.

The route table selects one lane:

- `qwen_coordinator` uses the injected Qwen action transport, validates one
  `ActionDecision`, dispatches one action, and permits at most one bounded
  follow-up.
- `lfm_direct` uses an explicitly configured `LFMTransport` and returns a direct
  answer. Missing LFM configuration returns a typed 503. It never falls back to
  Qwen.
- `fixed_worker` uses an injected adapter keyed by route-rule id. Missing worker
  configuration returns a typed 503.
- `fail_visible` returns `NoRouteError` before any transport call.

The entrypoint accepts `--qwen-url`, `--lfm-url`, `--lfm-model`, and
`--timeout-s`. `build_handle_chat` is the injectable composition seam used by
the fake entrypoint smoke.

Durable replay reads append-only `result.json` before any transport call. It
requires matching packet hash, rule id, and rule version. A missing result may
run. Malformed stored state or an identity mismatch fails visibly.

## Data and ownership

- `router/domain.py` owns frozen request, route, packet, action, worker, and
  response records.
- `router/rules.py` owns deterministic lane selection.
- `router/packet.py` owns canonical packet hashing.
- `router/transport.py` owns injected Qwen and LFM HTTP boundaries.
- `router/coordinator.py` owns lane handlers, bounded follow-up, artifacts, and
  durable replay.
- `router/http_boundary.py` owns OpenAI-compatible WSGI request and response
  mapping.
- `router/__main__.py` owns executable composition. It never manages model
  processes.
- `router/hardware_lane.py` owns bounded command observation and lease evidence.
  Its module contract says it never starts models.

## Harness-contained verification

The final harness-contained command was:

```sh
python3 -m unittest discover -s router/tests -p 'test_*.py'
python3 -m compileall -q router
```

It ran 127 tests and returned `OK`. Compilation returned no error.

Focused evidence:

- Transport suite ran 18 tests and returned `OK`.
- Coordinator suite ran 24 tests and returned `OK`.
- Entrypoint suite ran 10 tests and returned `OK`.
- The fake LFM entrypoint test drove `handle_chat_request` without binding a
  socket and asserted zero Qwen calls.
- Fresh-Coordinator replay tests asserted zero Qwen or LFM calls.
- Confidence configuration accepts 0, 0.5, and 1, and rejects booleans, strings,
  and values outside the unit interval.

## External excluded evidence

The other chat's untracked audit controller was not staged. Its read-only
selftest was run separately:

```sh
python3 .audit/overnight-2026-08-31/hillclimb.py selftest
```

It returned `selftest OK: fail-closed + lease + matrix gates covered`. This is
external evidence only. The harness commit does not contain `.audit/`.

## Harness commit boundary

This handoff is committed with:

- `.gitignore`
- `router/`
- `plan/01-local-model-coordinator/`
- `HANDOFF-HARNESS-OFFLINE.md`

The following paths belong to the other chat and are not part of the harness
commit:

- `.audit/`
- `server/`
- `HANDOFF-QWEN-HILLCLIMB.md`
- `HANDOFF.md`
- `plan/plan.md`

They remained untracked when this handoff was finalized.

## Remaining offline work

The concrete offline gap is Pi integration in the sibling `pi-docker`
repository:

1. Add a non-default provider that points at
   `http://host.docker.internal:8060/v1`.
2. Add a router model alias without changing the current
   `qwen-local/qwen3.8-27b` default.
3. Add offline config validation for provider URLs, model aliases, and selected
   defaults.
4. Add config tests that do not run Docker or contact any endpoint.
5. Preserve full Pi as the control profile.

`plan/01-local-model-coordinator/pi-integration-prep.md` contains the provider
map and future live commands. `hardware-lane-request.md` contains the bounded
live-smoke packet.

## Live work that remains separate

A live chat must prove the router against real Qwen and one selected LFM
endpoint. That chat requires an already-running server or the active server
owner. It must use the exclusive hardware lane, report the selected provider
and model, and keep the reranker untouched.

No model quality or performance claim follows from the fake-only suite.

## First action on resume

1. Read this file.
2. Read the latest Git commit and confirm the harness-owned paths are clean.
3. Open `/Users/jamie/Documents/Projects/active/local/pi-docker/config/models.json`
   and `config/settings.json`.
4. Add the non-default router provider and its offline config tests.
5. Do not start Docker or contact a model endpoint.
