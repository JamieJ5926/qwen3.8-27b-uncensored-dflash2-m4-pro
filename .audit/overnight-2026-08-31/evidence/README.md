# Evidence

Hillclimb writes one evidence record per BenchLoop gate run.

Lifecycle:

1. Before any subprocess launch, write `evidence/<run_id>.pending.json` with command, endpoint, start time, and timeout.
2. After the subprocess completes, times out, or fails, write `evidence/<run_id>.json.tmp` then `os.replace` it onto `evidence/<run_id>.json`.
3. Delete the pending file only after that replace succeeds.

`run_id` looks like `benchloop-YYYYMMDDTHHMMSSZ`. TSV rows stay append-only; this directory is the per-run payload, not a rewrite of `results.tsv`.

Endpoint is the server root without `/v1` (default `http://127.0.0.1:8083`). Frozen config checksum (`sha256` over the JSON body excluding that field) is verified before launch when `frozen-config.json` exists.

This controller never starts or stops the DFlash server.
