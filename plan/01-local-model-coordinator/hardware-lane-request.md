# Hardware-lane request: one live Qwen smoke through the router

Status: requested 2026-08-31 by the offline harness chat. This is a complete
command packet. The executing chat makes no architectural decisions; it runs
these steps, records observations, and reports.

## Hard precondition

Qwen must already be serving at `http://127.0.0.1:8083/v1` (server root
`http://127.0.0.1:8083`). The hardware-lane runner never starts models
(`router/hardware_lane.py:1`), and its `subprocess.run(..., timeout=...)`
executes bounded commands only (`router/hardware_lane.py:329-335`), so this
packet contains no start or restart step. If the server is absent or
unhealthy, stop and hand off to the active server owner. Do not start it.

Repo: `/Users/jamie/Documents/Projects/active/local/qwen3.8-27b-uncensored-dflash2-m4-pro`.

## Step 1. Router serves against the live endpoint

```sh
cd /Users/jamie/Documents/Projects/active/local/qwen3.8-27b-uncensored-dflash2-m4-pro
python3 -m router serve \
  --host 127.0.0.1 --port 8060 \
  --qwen-url http://127.0.0.1:8083/v1 \
  --artifacts router-artifacts-smoke
```

Run it in the background of the executing chat and stop it after Step 2. It
starts no model process, so it needs no lease.

## Step 2. One chat completion through the coordinator

```sh
curl -s http://127.0.0.1:8060/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.8-27b",
    "messages": [{"role": "user", "content": "In one sentence, say the router smoke works."}],
    "max_tokens": 128,
    "tools": [{"type": "function", "function": {"name": "lookup_log"}}],
    "metadata": {"intent": "coding", "risk_class": "high", "needs_qwen_judgment": true}
  }'
```

## Acceptance

1. HTTP 200; headers carry `X-Lane: qwen_coordinator` and `X-Route-Trace: coding-qwen`.
2. Body is OpenAI-shaped with one choice, non-empty content, and integer usage fields.
3. `router-artifacts-smoke/<request-id>/` contains route.json, packet.json,
   action-1.json, step-1.txt, timing.json, usage.json, result.json. The
   request id is the `X-Trace-Id` header value.
4. At most one follow-up occurred, either one step only or exactly two with
   action-2.json present.
5. No artifact file contains an authorization, bearer, api_key, or lease_token
   string.
6. Replay determinism. `python3 -m router replay --artifacts router-artifacts-smoke
   --request-id <id>` reproduces the same answer with zero additional model calls.

## Step 3. Optional Pi local-route cross-check

The command and acceptance in `pi-integration-prep.md` under "Prepared live
validation". Evidence must name provider `qwen-local` and model `qwen3.8-27b`.

## On failure

Record the exact command, verbatim error, artifacts directory, and stop. Do not
patch the router, reconfigure the server, start or stop any model process, or
touch the reranker from the smoke chat. A timeout maps to a limit error, a
refused connection to a service-unavailable error, and a malformed model reply
to a parse error. Those mappings are already tested offline, so a live failure
is evidence about the server or the transport URL, not about the router.
