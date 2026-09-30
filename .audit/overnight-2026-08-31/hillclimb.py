#!/usr/bin/env python3
"""Overnight DFlash hillclimb controller — stock DFlash server edition.

Targets the stock dflash_server.py. Default endpoint is 127.0.0.1:8083 with no
/v1 suffix (API paths still use /health, /v1/dflash/stats, /v1/chat/completions).
Hypothesis settings are server restart env/args applied externally; this
controller never starts or stops the server. `iterate` measures only when the
live process config already matches.

Usage:
  hillclimb.py status
  hillclimb.py snapshot                     # write baseline-snapshot.json
  hillclimb.py check                        # exit 0 iff safe to benchmark
  hillclimb.py baseline                     # warmup + 5x256-token fixed coding requests, append TSV row
  hillclimb.py bench [--reps N] [--max-tokens M] [--warmup/--no-warmup]
  hillclimb.py iterate --file cfg.json      # measure after external restart to that config
  hillclimb.py benchloop --suites speed     # BenchLoop gate (wall-time proxy)
  hillclimb.py pi-gate                      # one Pi representative coding prompt
  hillclimb.py freeze                       # freeze workload+config JSON, print checksum
  hillclimb.py matrix [--reps N] [--timeout S] [--warmup/--no-warmup]
  hillclimb.py selftest                     # synthetic fail-closed gate tests (no live model)

Exit codes: 0 ok, 2 refusal (not ready / mismatch / lease held).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import statistics
import subprocess
import shutil
import time
import urllib.error
import sys
import urllib.request
from pathlib import Path

DEFAULT_ENDPOINT = "http://127.0.0.1:8083"
DEFAULT_SUBPROCESS_TIMEOUT = 600
HERE = Path(__file__).resolve().parent
BENCHLOOP = shutil.which("benchloop") or "benchloop"
TSV = HERE / "results.tsv"
MATRIX_TSV = HERE / "matrix-results.tsv"
SNAPSHOT = HERE / "baseline-snapshot.json"
EVIDENCE = HERE / "evidence"
FROZEN = HERE / "frozen-config.json"
MODEL = "qwen3.8-27b"  # server --model-name; single-tenant: any id maps to resident pair
DEFAULT_LEASE_DIR = "/var/tmp/local-model-hardware-lease"
LIVE_CMDS = ("status", "snapshot", "check", "baseline", "bench", "iterate",
             "benchloop", "pi-gate", "freeze", "matrix")
TSV_HEADER = ("id\ttimestamp\tsettings_json\tworkload\trepetitions\t"
              "median_decode_tps\tmedian_wall_s\tacceptance\tpi_smoke\t"
              "verdict\tevidence")
MATRIX_TSV_HEADER = (
    "run_id\ttimestamp\tendpoint\tcase\treps\t"
    "median_generation_tps\tmedian_prefill_tps\tmedian_wall_client_s\t"
    "median_queue_wait_s\tmedian_derived_ttft_s\tmedian_completion_tokens\t"
    "mean_acceptance\ttool_gate\tjson_gate\terrors\tnote\tmatrix_sha")
FREEZE_POLICY = {
    "keep_threshold_pct": 7,
    "stop_target_pct": 20,
    "min_hypotheses": 10,
    "material_swap_rise_mb": 200,
}

# Fixed direct coding workload, frozen ruler primary channel.
PROMPT = ("Write a Python function `word_frequencies(text)` that returns a dict "
          "mapping each lowercase word in `text` to its count, stripping "
          "punctuation. Then write three pytest cases for it. Include only code.")
PI_PROMPT = ("In this directory, create a file `fizzbuzz.py` with a function "
             "`fizzbuzz(n)` returning the FizzBuzz list for 1..n, then run a "
             "quick sanity check importing it. Keep it minimal.")

BASE = DEFAULT_ENDPOINT


def normalize_endpoint(url):
    """Host root only. Strip trailing slash and a trailing /v1 if present."""
    u = (url or DEFAULT_ENDPOINT).strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u or DEFAULT_ENDPOINT


def http(method, path, body=None, timeout=600):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def lease_path():
    return Path(os.environ.get("DFLASH_LEASE_DIR", DEFAULT_LEASE_DIR))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _clear_lease_dir(path, report=False):
    jf = path / "lease.json"
    try:
        jf.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        if report:
            print(f"lease: failed to remove {jf}: {e}", file=sys.stderr)
        return False
    try:
        path.rmdir()
    except OSError as e:
        if report:
            print(f"lease: failed to rmdir {path}: {e}", file=sys.stderr)
        return False
    return True


def acquire_lease():
    path = lease_path()
    takeover = Path(str(path) + ".takeover")
    for _ in range(3):
        try:
            os.mkdir(path)
        except FileExistsError:
            try:
                os.mkdir(takeover)
            except FileExistsError:
                print("lease takeover in progress", file=sys.stderr)
                sys.exit(2)
            try:
                holder = {}
                try:
                    holder = json.loads((path / "lease.json").read_text())
                except (OSError, json.JSONDecodeError):
                    pass
                pid = holder.get("pid")
                if isinstance(pid, int) and not pid_alive(pid):
                    _clear_lease_dir(path, report=False)
                    continue
                print(f"lease held by pid {pid} command {holder.get('argv')}",
                      file=sys.stderr)
                sys.exit(2)
            finally:
                try:
                    takeover.rmdir()
                except OSError as e:
                    print(f"lease: failed to rmdir takeover {takeover}: {e}",
                          file=sys.stderr)
        try:
            (path / "lease.json").write_text(json.dumps({
                "pid": os.getpid(),
                "argv": list(sys.argv),
                "acquired_utc": now(),
            }) + "\n")
        except Exception:
            _clear_lease_dir(path, report=True)
            raise
        return path
    print("lease: acquire retry failed", file=sys.stderr)
    sys.exit(2)


def release_lease(path):
    if path is None:
        return
    _clear_lease_dir(Path(path), report=True)


def http_raw(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            parsed = json.loads(raw) if raw else None
            return r.status, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed, raw


def observe():
    """Health + DFlash stats observation from the stock endpoints."""
    health = http("GET", "/health", timeout=30)
    stats = http("GET", "/v1/dflash/stats", timeout=30)
    status = health.get("status")
    return {
        "timestamp": now(),
        "health": status,
        "ready": status == "ready",
        "process_config": {k: stats.get(k) for k in
                           ("model", "draft", "draft_bits", "block_size")},
        "requests": stats.get("requests"),
        "errors": stats.get("errors"),
        "last_error": stats.get("last_error"),
        "mean_acceptance": stats.get("mean_acceptance"),
        "uptime_seconds": stats.get("uptime_seconds"),
    }


def process_pid():
    out = subprocess.run(
        ["pgrep", "-f", "dflash_server.py"], capture_output=True, text=True,
        timeout=5)
    return out.stdout.split()


def launch_invocation():
    """Observe running process command line; never start or stop the server."""
    pids = process_pid()
    if not pids:
        return None
    out = subprocess.run(["ps", "-p", pids[0], "-o", "command="],
                         capture_output=True, text=True, timeout=5)
    return out.stdout.strip()


def refuse(ob, reason):
    print(json.dumps({"refused": True, "reason": reason, "observation": ob}, indent=2))
    sys.exit(2)


def guard_for_bench():
    ob = observe()
    if not ob["ready"]:
        refuse(ob, f"health={ob['health']}")
    if ob["errors"] and ob["last_error"]:
        print(f"note: stats report {ob['errors']} prior errors: {ob['last_error']}",
              file=sys.stderr)
    return ob


def ensure_tsv():
    if not TSV.exists():
        TSV.write_text(TSV_HEADER + "\n")


def append_row(**c):
    ensure_tsv()
    row = [str(c.get(k, "NA")) for k in TSV_HEADER.split("\t")]
    with TSV.open("a") as f:
        f.write("\t".join(row) + "\n")


def chat(max_tokens, timeout=600):
    t0 = time.monotonic()
    resp = http("POST", "/v1/chat/completions", {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }, timeout=timeout)
    return resp, time.monotonic() - t0


def rep_error(resp, case=None):
    """None if one measured rep is complete; else the fail-closed reason."""
    if not isinstance(resp, dict):
        return "response is not a JSON object"
    choices = resp.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    content = msg.get("content")
    if case == "tool":
        if not tool_gate(resp):
            return "tool_gate failed"
    elif not isinstance(content, str) or not content.strip():
        return "empty content"
    ct = (resp.get("usage") or {}).get("completion_tokens")
    if not isinstance(ct, int) or isinstance(ct, bool) or ct <= 0:
        return f"bad completion_tokens: {ct!r}"
    dflash = resp.get("dflash") or {}
    tps = dflash.get("generation_tps")
    if not isinstance(tps, (int, float)) or isinstance(tps, bool) or not tps > 0:
        return f"bad generation_tps: {tps!r}"
    acc = dflash.get("mean_acceptance")
    if not isinstance(acc, (int, float)) or isinstance(acc, bool) or acc < 0:
        return f"bad mean_acceptance: {acc!r}"
    if not dflash.get("finish_reason"):
        return "missing finish_reason"
    return None


def run_error(reps, expected, warmup, before, after, expected_total):
    """None if the whole run is complete; else the fail-closed reason.

    reps: list of parsed response dicts, one per measured rep (warmup excluded).
    """
    if len(reps) != expected:
        return f"partial run: measured {len(reps)}/{expected} reps"
    for i, resp in enumerate(reps):
        if e := rep_error(resp):
            return f"rep {i}: {e}"
    want = expected + (1 if warmup else 0)
    got = (after.get("requests") or 0) - (before.get("requests") or 0)
    if got != want:
        return f"stats request mismatch: delta {got} != {want} ({expected} measured + {1 if warmup else 0} warmup)"
    if (after.get("errors") or 0) != (before.get("errors") or 0):
        return (f"server error count increased during run: "
                f"{before.get('errors')} -> {after.get('errors')}")
    total = sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in reps)
    if total != expected_total:
        return f"completion token mismatch: responses sum {total} != recorded {expected_total}"
    return None


def run_bench(args, label="coding", timeout=600):
    ob = guard_for_bench()
    EVIDENCE.mkdir(exist_ok=True)
    config = {**ob["process_config"], "launch_invocation": launch_invocation()}
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    before = http("GET", "/v1/dflash/stats", timeout=30)
    if args.warmup:
        _, w = chat(args.max_tokens, timeout)
        print(f"warmup: {w:.1f}s")

    def fail(reason, extra=None):
        ev = {"run_id": run_id, "reason": reason, **(extra or {})}
        (EVIDENCE / f"{run_id}-fail.json").write_text(json.dumps(ev, indent=2) + "\n")
        append_row(id=run_id, timestamp=now(), settings_json=json.dumps(config),
                   workload=label, repetitions=args.reps, verdict="fail",
                   evidence=f"evidence/{run_id}-fail.json; {reason}")
        print(json.dumps({"run_id": run_id, "verdict": "fail", "reason": reason},
                         indent=2))
        sys.exit(1)

    reps, walls = [], []
    for i in range(args.reps):
        try:
            resp, wall = chat(args.max_tokens, timeout)
        except Exception as e:  # fail-closed: any request failure fails the run
            fail(f"rep {i} request error: {type(e).__name__}: {e}")
        if e := rep_error(resp):
            fail(f"rep {i} incomplete: {e}", extra={"rep": i, "response": resp})
        reps.append(resp)
        walls.append(wall)
        dflash = resp.get("dflash") or {}
        print(f"rep {i+1}/{args.reps}: tps={dflash.get('generation_tps')} "
              f"wall={wall:.1f}s acc={dflash.get('mean_acceptance')}")
    after = http("GET", "/v1/dflash/stats", timeout=30)
    total_ct = sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in reps)
    (EVIDENCE / f"{run_id}-stats.json").write_text(json.dumps(
        {"before": {"requests": before.get("requests"),
                    "errors": before.get("errors"),
                    "mean_acceptance": before.get("mean_acceptance")},
         "after": {"requests": after.get("requests"),
                   "errors": after.get("errors"),
                   "mean_acceptance": after.get("mean_acceptance"),
                   "acceptance_histogram": after.get("acceptance_histogram")}},
        indent=2) + "\n")
    if e := run_error(reps, args.reps, args.warmup, before, after, total_ct):
        fail(e)
    tps_list = [float(r["dflash"]["generation_tps"]) for r in reps]
    accepts = [float(r["dflash"]["mean_acceptance"]) for r in reps]
    med_tps = statistics.median(tps_list)
    med_wall = statistics.median(walls)
    med_acc = statistics.median(accepts)
    spread = (f"{min(tps_list):.2f}-{max(tps_list):.2f}"
              if len(tps_list) > 1 else "n/a")
    append_row(id=run_id, timestamp=now(), settings_json=json.dumps(config),
               workload=label, repetitions=args.reps, median_decode_tps=f"{med_tps:.2f}",
               median_wall_s=f"{med_wall:.2f}", acceptance=f"{med_acc:.3f}",
               pi_smoke="skipped", verdict="ok",
               evidence=(f"evidence/{run_id}-rep*.json + {run_id}-stats.json; "
                         f"tps spread {spread}; completion_tokens {total_ct}"))
    print(json.dumps({"run_id": run_id, "median_generation_tps": round(med_tps, 2),
                      "median_wall_s": round(med_wall, 2),
                      "acceptance": round(med_acc, 3), "tps_spread": spread,
                      "completion_tokens": total_ct,
                      "tsv": str(TSV)}, indent=2))
    return run_id


def atlas_longctx():
    topics = [
        "ingress gateway", "event bus", "schema registry", "retry policy",
        "dead-letter queue", "saga coordinator", "outbox publisher",
        "idempotency store", "clock service", "tenant isolator",
        "rate limiter", "authn broker", "authz evaluator", "audit ledger",
        "metrics scraper", "trace collector", "config watcher",
        "feature flag pole", "cache front", "materializer",
        "projection rebuild", "compaction worker", "snapshotter",
        "watermark tracker", "backpressure valve", "shard assigner",
        "lease manager", "health prober", "alert router", "runbook index",
    ]
    paras = []
    for i, name in enumerate(topics, 1):
        paras.append(
            f"{i}. Atlas component {name} belongs to the event-driven control "
            f"plane. It consumes typed envelopes, stamps correlation ids, and "
            f"publishes outcomes without blocking the caller. Operators set its "
            f"batch window, concurrency, and circuit thresholds together. The "
            f"{name} never owns retry policy unless this is the retry policy "
            f"component; recovery stays centralized. Side effects are "
            f"idempotent and ordered by watermark. This numbered paragraph "
            f"describes exactly one component of Atlas."
        )
    body = "\n\n".join(paras)
    q = ("Based only on the document above, which component owns retry policy, "
         "and how many numbered paragraphs are there?")
    return body + "\n\n" + q


TOOL_SPEC = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}]


def matrix_case_defs():
    return [
        {"name": "short", "max_tokens": 16, "prompt":
         "Reply with exactly one word. What is the capital of France?",
         "kind": "generation"},
        {"name": "medium", "max_tokens": 256, "prompt":
         "Explain in three short paragraphs how speculative decoding speeds "
         "up language model inference.",
         "kind": "generation"},
        {"name": "longctx", "max_tokens": 128, "prompt": atlas_longctx(),
         "kind": "generation"},
        {"name": "json", "max_tokens": 256, "prompt":
         'Return only a JSON object with keys "name" and "steps" describing '
         "how to reverse a Python list. No prose, no markdown fences.",
         "kind": "generation"},
        {"name": "tool", "max_tokens": 64, "prompt":
         "What is the weather in Paris? Call the get_weather function.",
         "kind": "generation", "tools": TOOL_SPEC},
        {"name": "stability", "max_tokens": 16, "prompt": "Say stable.",
         "kind": "stability", "extra": {"logprobs": 1}},
    ]


def matrix_definition(reps_default=5, warmup=True):
    cases = matrix_case_defs()
    return {
        "temperature": 0,
        "reps_default": reps_default,
        "warmup": warmup,
        "primary_cases": ["medium", "longctx", "json", "tool"],
        "primary_label": "median_of_four_sustained_cases",
        "cases": [
            {**{k: c[k] for k in ("name", "max_tokens", "prompt", "kind",
                                  "tools", "extra") if k in c},
             **({"role": "sensitivity_only", "primary": False}
                if c["name"] == "short" else {})}
            for c in cases
        ],
    }


def matrix_definition_sha256(defn=None):
    body = defn if defn is not None else matrix_definition()
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def derived_ttft_s(queue_wait_seconds, prompt_tokens, prefill_tps):
    if not prefill_tps:
        return None
    return float(queue_wait_seconds or 0) + float(prompt_tokens) / float(prefill_tps)


def strip_optional_fence(text):
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip(), True
    return s, False


def json_gate(content):
    stripped, needed = strip_optional_fence(content)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return False, needed
    return isinstance(obj, dict), needed


def tool_gate(resp):
    try:
        tc = ((resp.get("choices") or [{}])[0].get("message") or {}).get("tool_calls")
        if not tc:
            return False
        fn = (tc[0].get("function") or {})
        if fn.get("name") != "get_weather":
            return False
        args = json.loads(fn.get("arguments") or "{}")
        city = str(args.get("city") or "")
        return "paris" in city.lower()
    except (json.JSONDecodeError, TypeError, IndexError, AttributeError):
        return False


def stability_error_ok(before_errors, after_errors, stability_http_4xx):
    delta = (after_errors or 0) - (before_errors or 0)
    if delta == 0:
        return True, None
    if delta == 1 and stability_http_4xx:
        return True, "stability"
    return False, delta


def sysctl_str(name):
    r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                       timeout=5)
    if r.returncode != 0:
        return "unknown"
    return r.stdout.strip() or "unknown"


def stability_rejection_ok(status, parsed):
    if not (400 <= (status or 0) < 500):
        return False
    if not isinstance(parsed, dict):
        return False
    err = parsed.get("error")
    if isinstance(err, str) and err.strip():
        blob = err.lower()
    elif isinstance(err, dict) and err:
        blob = json.dumps(err).lower()
    else:
        return False
    return "logprobs" in blob


def tools_unsupported_ok(status, parsed):
    if not (400 <= (status or 0) < 500):
        return False
    if not isinstance(parsed, dict):
        return False
    err = parsed.get("error")
    if isinstance(err, str) and err.strip():
        blob = err.lower()
    elif isinstance(err, dict) and err:
        blob = json.dumps(err).lower()
    else:
        return False
    return any(tok in blob for tok in ("tool", "tools", "tool_call", "function"))


def expected_generation_requests(reps, warmup, tool_unsupported, tool_posts=0):
    n = reps + (1 if warmup else 0)
    four = 4 * n
    if tool_unsupported:
        return four
    return four + n


def matrix_publication_ok(req_delta, expected, err_ok, case_medians,
                          tools_unsupported):
    if tools_unsupported:
        return False, "tool_gate=unsupported"
    if req_delta != expected:
        return False, f"request delta {req_delta} != {expected}"
    if not err_ok:
        return False, "error delta not attributable"
    for name in ("medium", "longctx", "json", "tool"):
        v = case_medians.get(name)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False, f"missing case median {name}"
    return True, None


def freeze_matrix_gate(sha, tsv_path=None):
    path = Path(tsv_path) if tsv_path else MATRIX_TSV
    if not path.exists():
        return "no matrix-results.tsv"
    keys = MATRIX_TSV_HEADER.split("\t")
    try:
        sha_i = keys.index("matrix_sha")
        case_i = keys.index("case")
    except ValueError:
        return "matrix-results.tsv header missing columns"
    for line in path.read_text().splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) <= max(sha_i, case_i):
            continue
        if cols[case_i] == "AGGREGATE" and cols[sha_i] == sha:
            return None
    return "no qualified AGGREGATE row matching current matrix_sha"


def fail_matrix(evdir, run_id, reason, extra=None, exit_code=1):
    payload = {"run_id": run_id, "verdict": "fail", "reason": reason,
               **(extra or {})}
    (evdir / "fail.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    sys.exit(exit_code)



def swap_usage():
    r = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True,
                       text=True, timeout=5)
    if r.returncode != 0:
        return "unknown"
    return r.stdout.strip()


def memory_pressure():
    try:
        r = subprocess.run(
            ["sysctl", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    raw = r.stdout.strip()
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return "unknown"


def memory_pressure_preflight_ok(level):
    return level == 1


def memory_pressure_worsened(before, after):
    if not isinstance(after, int) or isinstance(after, bool):
        return True
    if after > 1:
        return True
    if isinstance(before, int) and not isinstance(before, bool) and after > before:
        return True
    return False

def parse_swap_used_mb(s):
    if not isinstance(s, str):
        return None
    import re
    m = re.search(r"used\s*=\s*([0-9.]+)\s*M", s, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def swap_rise_worsened(before_s, after_s, limit_mb=None):
    lim = FREEZE_POLICY["material_swap_rise_mb"] if limit_mb is None else limit_mb
    b = parse_swap_used_mb(before_s)
    a = parse_swap_used_mb(after_s)
    if b is None or a is None:
        return True
    return (a - b) > lim


def extract_rep_fields(resp, wall):
    dflash = resp.get("dflash") or {} if isinstance(resp, dict) else {}
    usage = resp.get("usage") or {} if isinstance(resp, dict) else {}
    qwait = dflash.get("queue_wait_seconds")
    prefill = dflash.get("prefill_tps")
    pt = usage.get("prompt_tokens")
    return {
        "queue_wait_seconds": qwait,
        "generation_tps": dflash.get("generation_tps"),
        "prefill_tps": prefill,
        "wall_seconds": wall,
        "peak_memory_gb": dflash.get("peak_memory_gb"),
        "mean_acceptance": dflash.get("mean_acceptance"),
        "usage": usage,
        "derived_ttft_s": derived_ttft_s(qwait, pt, prefill),
        "completion_tokens": usage.get("completion_tokens"),
    }


def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return statistics.median(xs) if xs else None


def format_matrix_tsv_row(row):
    keys = MATRIX_TSV_HEADER.split("\t")
    return "\t".join(str(row.get(k, "NA")) for k in keys)


def append_matrix_row(row):
    if not MATRIX_TSV.exists():
        MATRIX_TSV.write_text(MATRIX_TSV_HEADER + "\n")
    with MATRIX_TSV.open("a") as f:
        f.write(format_matrix_tsv_row(row) + "\n")


def chat_body(case, extra=None):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": case["prompt"]}],
        "max_tokens": case["max_tokens"],
        "temperature": 0,
    }
    if case.get("tools"):
        body["tools"] = case["tools"]
    if extra:
        body.update(extra)
    if case.get("extra"):
        body.update(case["extra"])
    return body


def cmd_matrix(args):
    ob = guard_for_bench()
    EVIDENCE.mkdir(exist_ok=True)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evdir = EVIDENCE / f"matrix-{run_id}"
    evdir.mkdir()
    before_hw = {"swap": swap_usage(), "memory_pressure": memory_pressure()}
    if not memory_pressure_preflight_ok(before_hw["memory_pressure"]):
        fail_matrix(evdir, run_id,
                    f"memory pressure preflight refused: {before_hw['memory_pressure']!r}",
                    extra={"before_hw": before_hw}, exit_code=2)
    before = http("GET", "/v1/dflash/stats", timeout=30)
    cases = matrix_case_defs()
    per_case = []
    gen_posts = 0
    stability_http = None
    stability_body = None
    tools_section = {}
    json_section = {}
    timeout = args.timeout

    for case in cases:
        name = case["name"]
        if case["kind"] == "stability":
            t0 = time.monotonic()
            status, parsed, raw = http_raw("POST", "/v1/chat/completions",
                                          chat_body(case), timeout=timeout)
            wall = time.monotonic() - t0
            stability_http = status
            stability_body = raw
            body_hash = hashlib.sha256(raw or b"").hexdigest()
            note = ""
            stab_ok = stability_rejection_ok(status, parsed)
            if not stab_ok:
                fail_matrix(evdir, run_id,
                            f"stability rejection invalid: status={status}",
                            extra={"status": status, "body": parsed})
            body_hash = hashlib.sha256(raw or b"").hexdigest()
            per_case.append({
                "name": name, "reps": 0, "median_generation_tps": None,
                "median_prefill_tps": None, "median_wall_client_s": round(wall, 4),
                "median_queue_wait_s": None, "median_derived_ttft_s": None,
                "median_completion_tokens": None, "mean_acceptance": None,
                "tool_gate": "NA", "json_gate": "NA", "errors": 0,
                "note": f"status={status} body_sha256={body_hash}",
                "status": status, "body_sha256": body_hash,
                "stability_ok": True,
            })
            continue

        measured = []
        tools_unsupported = False
        json_gate_vals = []
        tool_gate_vals = []
        errors = 0
        note = ""
        n_loop = args.reps + (1 if args.warmup else 0)
        for i in range(n_loop):
            t0 = time.monotonic()
            status, parsed, raw = http_raw("POST", "/v1/chat/completions",
                                          chat_body(case), timeout=timeout)
            wall = time.monotonic() - t0
            gen_posts += 1
            if name == "tool" and status is not None and status >= 400:
                if tools_unsupported_ok(status, parsed):
                    tools_unsupported = True
                    tools_section = {
                        "tools_unsupported": True,
                        "status": status,
                        "error_body": (raw or b"").decode(errors="replace"),
                    }
                    note = f"tools_unsupported status={status}"
                    break
                fail_matrix(evdir, run_id,
                            f"tool HTTP {status} is not attributable tools_unsupported",
                            extra={"status": status, "body": parsed})
            resp = parsed if isinstance(parsed, dict) else {}
            is_warmup = args.warmup and i == 0
            if not is_warmup:
                if e := rep_error(resp, case=name):
                    fail_matrix(evdir, run_id,
                                f"{name} measured rep {len(measured)+1}: {e}",
                                extra={"case": name, "response": resp})
            fields = extract_rep_fields(resp, wall)
            if name == "json":
                content = ((resp.get("choices") or [{}])[0].get("message")
                           or {}).get("content")
                ok, fenced = json_gate(content)
                fields["json_gate"] = ok
                fields["json_fence_stripped"] = fenced
                if not is_warmup:
                    json_gate_vals.append(ok)
            if name == "tool":
                ok = tool_gate(resp)
                fields["tool_gate"] = ok
                if not is_warmup:
                    tool_gate_vals.append(ok)
            if not is_warmup:
                measured.append(fields)
                (evdir / f"{name}-rep{len(measured)}.json").write_text(
                    json.dumps({"case": name, "rep": len(measured), **fields,
                                "response": resp}, indent=2) + "\n")
        if not tools_unsupported and len(measured) != args.reps:
            fail_matrix(evdir, run_id,
                        f"{name}: measured {len(measured)}/{args.reps} reps")
        med_tps = _median([m.get("generation_tps") for m in measured]) if measured else None
        med_pre = _median([m.get("prefill_tps") for m in measured]) if measured else None
        med_wall = _median([m.get("wall_seconds") for m in measured]) if measured else None
        med_q = _median([m.get("queue_wait_seconds") for m in measured]) if measured else None
        med_ttft = _median([m.get("derived_ttft_s") for m in measured]) if measured else None
        med_ct = _median([m.get("completion_tokens") for m in measured]) if measured else None
        med_acc = _median([m.get("mean_acceptance") for m in measured]) if measured else None
        jg = "NA"
        tg = "NA"
        if name == "json":
            jg = all(json_gate_vals) if json_gate_vals else False
            json_section = {"pass": jg, "fence_stripped_any": any(
                m.get("json_fence_stripped") for m in measured)}
        if name == "tool":
            tg = "unsupported" if tools_unsupported else (
                all(tool_gate_vals) if tool_gate_vals else False)
            if not tools_unsupported:
                tools_section = {"tools_unsupported": False, "pass": tg}
            else:
                tools_section["tool_gate"] = "unsupported"
        per_case.append({
            "name": name, "reps": len(measured),
            "median_generation_tps": med_tps,
            "median_prefill_tps": med_pre,
            "median_wall_client_s": med_wall,
            "median_queue_wait_s": med_q,
            "median_derived_ttft_s": med_ttft,
            "median_completion_tokens": med_ct,
            "mean_acceptance": med_acc,
            "tool_gate": tg, "json_gate": jg, "errors": errors, "note": note,
        })

    after = http("GET", "/v1/dflash/stats", timeout=30)
    after_hw = {"swap": swap_usage(), "memory_pressure": memory_pressure()}
    matrix_sha = matrix_definition_sha256()
    if memory_pressure_worsened(before_hw.get("memory_pressure"),
                                after_hw.get("memory_pressure")):
        summary = {
            "run_id": run_id, "qualified": False,
            "reason": "MEMORY_PRESSURE_WORSENED",
            "before_hw": before_hw, "after_hw": after_hw,
            "cases": per_case, "primary_metric": None,
            "matrix_sha": matrix_sha,
        }
        (evdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        sys.exit(3)
    if swap_rise_worsened(before_hw.get("swap"), after_hw.get("swap")):
        summary = {
            "run_id": run_id, "qualified": False,
            "reason": "MEMORY_SWAP_WORSENED",
            "before_hw": before_hw, "after_hw": after_hw,
            "cases": per_case, "primary_metric": None,
            "matrix_sha": matrix_sha,
        }
        (evdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        sys.exit(3)
    req_delta = (after.get("requests") or 0) - (before.get("requests") or 0)
    stab_ok = any(c.get("stability_ok") for c in per_case if c["name"] == "stability")
    err_ok, err_attr = stability_error_ok(
        before.get("errors"), after.get("errors"), stab_ok)
    tool_unsup = bool(tools_section.get("tools_unsupported"))
    expected = expected_generation_requests(args.reps, args.warmup, tool_unsup)
    case_medians = {c["name"]: c.get("median_generation_tps") for c in per_case}
    ts = now()
    for c in per_case:
        append_matrix_row({
            "run_id": run_id, "timestamp": ts, "endpoint": BASE,
            "case": c["name"], "reps": c["reps"],
            "median_generation_tps": c["median_generation_tps"],
            "median_prefill_tps": c["median_prefill_tps"],
            "median_wall_client_s": c["median_wall_client_s"],
            "median_queue_wait_s": c["median_queue_wait_s"],
            "median_derived_ttft_s": c["median_derived_ttft_s"],
            "median_completion_tokens": c["median_completion_tokens"],
            "mean_acceptance": c["mean_acceptance"],
            "tool_gate": c["tool_gate"], "json_gate": c["json_gate"],
            "errors": c["errors"], "note": c["note"] or "",
            "matrix_sha": matrix_sha,
        })
    if tool_unsup:
        append_matrix_row({
            "run_id": run_id, "timestamp": ts, "endpoint": BASE,
            "case": "UNQUALIFIED_TOOL_UNSUPPORTED", "reps": args.reps,
            "median_generation_tps": "NA", "median_prefill_tps": "NA",
            "median_wall_client_s": "NA", "median_queue_wait_s": "NA",
            "median_derived_ttft_s": "NA", "median_completion_tokens": "NA",
            "mean_acceptance": "NA", "tool_gate": "unsupported",
            "json_gate": json_section.get("pass", "NA"),
            "errors": (after.get("errors") or 0) - (before.get("errors") or 0),
            "note": "wave not qualified; tool_gate=unsupported; no primary",
            "matrix_sha": matrix_sha,
        })
        summary = {
            "run_id": run_id, "qualified": False,
            "reason": "UNQUALIFIED_TOOL_UNSUPPORTED",
            "tools": tools_section, "cases": per_case,
            "primary_metric": None, "matrix_sha": matrix_sha,
        }
        (evdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        sys.exit(3)
    pub_ok, pub_why = matrix_publication_ok(
        req_delta, expected, err_ok, case_medians, False)
    if not pub_ok:
        fail_matrix(evdir, run_id, pub_why, extra={
            "req_delta": req_delta, "expected": expected, "err_ok": err_ok})
    names = ("medium", "longctx", "json", "tool")
    primary_label = "median_of_four_sustained_cases"
    primary = statistics.median([case_medians[n] for n in names])
    print(f"PRIMARY METRIC median_generation_tps={primary} ({primary_label})")
    append_matrix_row({
        "run_id": run_id, "timestamp": ts, "endpoint": BASE,
        "case": "AGGREGATE", "reps": args.reps,
        "median_generation_tps": primary,
        "median_prefill_tps": "NA", "median_wall_client_s": "NA",
        "median_queue_wait_s": "NA", "median_derived_ttft_s": "NA",
        "median_completion_tokens": "NA", "mean_acceptance": "NA",
        "tool_gate": True, "json_gate": json_section.get("pass", "NA"),
        "errors": (after.get("errors") or 0) - (before.get("errors") or 0),
        "note": f"primary={primary} {primary_label}; req_delta={req_delta}",
        "matrix_sha": matrix_sha,
    })
    summary = {
        "run_id": run_id, "endpoint": BASE, "before": before, "after": after,
        "before_hw": before_hw, "after_hw": after_hw, "qualified": True,
        "request_delta": req_delta, "expected_generation_posts": expected,
        "error_delta_ok": err_ok, "error_attributed_to": err_attr,
        "stability": {"status": stability_http, "ok": stab_ok,
                      "body_sha256": hashlib.sha256(stability_body or b"").hexdigest()
                      if stability_body is not None else None},
        "tools": tools_section, "json": json_section,
        "cases": per_case, "primary_metric": primary,
        "primary_label": primary_label, "matrix_sha": matrix_sha,
        "observation": ob,
    }
    (evdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"run_id": run_id, "primary_metric": primary,
                      "primary_label": primary_label, "qualified": True,
                      "evidence": str(evdir)}, indent=2))


def cmd_status(_):
    ob = observe()
    print(json.dumps(ob, indent=2))
    sys.exit(0 if ob["ready"] else 2)


def cmd_snapshot(_):
    ob = observe()
    snap = {"timestamp": now(), "health": ob["health"],
            "process_config": ob["process_config"],
            "launch_invocation": launch_invocation(),
            "stats": {"requests": ob["requests"], "errors": ob["errors"],
                      "mean_acceptance": ob["mean_acceptance"],
                      "uptime_seconds": ob["uptime_seconds"]}}
    SNAPSHOT.write_text(json.dumps(snap, indent=2) + "\n")
    print(f"wrote {SNAPSHOT}")
    sys.exit(0 if ob["ready"] else 2)


def cmd_check(_):
    ob = guard_for_bench()
    print(json.dumps({"ok": True, "observation": ob}, indent=2))


def cmd_baseline(args):
    args.warmup = True
    args.reps = 5
    args.max_tokens = 256
    run_bench(args, label="baseline")


def cmd_iterate(args):
    """One-change-one-measurement against a proposed restart env/args config."""
    proposed = json.loads(Path(args.file).read_text())
    live_cfg = observe()["process_config"]
    live = {**live_cfg, "launch_invocation": launch_invocation()}
    if live != proposed:
        refuse({"live": live, "proposed": proposed},
               "live process config does not match proposed hypothesis config; "
               "apply the server restart externally, then re-run iterate")
    run_bench(args, label=f"iterate:{Path(args.file).stem}")


def frozen_payload_bytes(frozen):
    body = {k: v for k, v in frozen.items() if k != "sha256"}
    return json.dumps(body, indent=2, sort_keys=True).encode() + b"\n"


def checksum_frozen(frozen):
    return hashlib.sha256(frozen_payload_bytes(frozen)).hexdigest()


def attach_checksum(frozen):
    out = dict(frozen)
    out.pop("sha256", None)
    out["sha256"] = checksum_frozen(out)
    return out


def verify_frozen_checksum(frozen):
    stored = frozen.get("sha256")
    if not stored:
        return "missing sha256"
    got = checksum_frozen(frozen)
    if stored != got:
        return f"sha256 mismatch: stored {stored} != {got}"
    return None


def benchloop_argv(binary, endpoint, model, suites):
    return [str(binary), "run", "--model", model,
            "--endpoint", normalize_endpoint(endpoint),
            "--provider", "openai_compat", "--harness", "pi",
            "--suites", suites]


def evidence_paths(run_id):
    return (EVIDENCE / f"{run_id}.pending.json",
            EVIDENCE / f"{run_id}.json",
            EVIDENCE / f"{run_id}.json.tmp")


def write_pending_evidence(run_id, meta):
    EVIDENCE.mkdir(exist_ok=True)
    pending, _, _ = evidence_paths(run_id)
    payload = {"run_id": run_id, "status": "pending", **meta}
    pending.write_text(json.dumps(payload, indent=2) + "\n")
    return pending


def finalize_evidence(run_id, meta):
    pending, final, tmp = evidence_paths(run_id)
    payload = {"run_id": run_id, **meta}
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, final)
    pending.unlink(missing_ok=True)
    return final


def run_subprocess(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r, None
    except subprocess.TimeoutExpired as e:
        return e, "timeout"


def cmd_benchloop(args):
    binary = Path(BENCHLOOP).expanduser()
    if not binary.is_file():
        refuse({}, f"benchloop CLI not found at {binary}")
    if FROZEN.exists():
        frozen = json.loads(FROZEN.read_text())
        if err := verify_frozen_checksum(frozen):
            refuse({"frozen": str(FROZEN)}, f"frozen-config checksum: {err}")
    endpoint = normalize_endpoint(getattr(args, "endpoint", None) or BASE)
    timeout = getattr(args, "timeout", None) or DEFAULT_SUBPROCESS_TIMEOUT
    cmd = benchloop_argv(binary, endpoint, MODEL, args.suites)
    run_id = f"benchloop-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    started = now()
    write_pending_evidence(run_id, {
        "command": cmd,
        "endpoint": endpoint,
        "started": started,
        "timeout": timeout,
    })
    print("+", shlex.join(cmd))
    result, kind = run_subprocess(cmd, timeout)
    ended = now()
    if kind == "timeout":
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        out = finalize_evidence(run_id, {
            "status": "timeout",
            "command": cmd,
            "endpoint": endpoint,
            "started": started,
            "ended": ended,
            "timeout": timeout,
            "stdout": stdout,
            "stderr": stderr,
            "exit": None,
        })
        print(f"wrote {out}; timeout after {timeout}s")
        sys.exit(1)
    out = finalize_evidence(run_id, {
        "status": "complete",
        "command": cmd,
        "endpoint": endpoint,
        "started": started,
        "ended": ended,
        "timeout": timeout,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit": result.returncode,
    })
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    print(f"wrote {out}; exit={result.returncode}")
    sys.exit(result.returncode)
def cmd_pi_gate(args):
    out = HERE / f"pi-gate-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.md"
    cmd = ["pi", "--provider", "llama-cpp", "--model", MODEL, "-p",
           "--mode", "json", "--no-session", "--thinking", "off", PI_PROMPT]
    print(f"+ pi ... (provider llama-cpp baseUrl {BASE}/v1, "
          "single-tenant server serves qwen3.8-27b for any requested id)")
    r, kind = run_subprocess(cmd, args.timeout)
    if kind == "timeout":
        out.write_text(f"timeout after {args.timeout}s\n")
        print(f"wrote {out}; timeout")
        sys.exit(1)
    out.write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
    print(f"wrote {out}; exit={r.returncode}")
    sys.exit(r.returncode)


def cmd_freeze(_):
    ob = guard_for_bench()
    msha = matrix_definition_sha256()
    if err := freeze_matrix_gate(msha):
        print(json.dumps({"refused": True, "reason": err}, indent=2))
        sys.exit(2)
    frozen = {
        "frozen_at": now(),
        "model": MODEL,
        "endpoint": BASE,
        "process_config": ob["process_config"],
        "launch_invocation": launch_invocation(),
        "server_pids": process_pid(),
        "workload": matrix_definition(),
        "ruler": {
            "primary": (
                "median over the four sustained generation cases "
                "(medium, longctx, json, tool) of each case's median "
                "server dflash.generation_tps; short is latency-only and "
                "excluded from the primary; qualified waves only "
                f"(matrix sha256={msha})"
            ),
            "gate_quality": "one Pi representative coding prompt",
            "gate_wall": "BenchLoop speed suite (smallest coding-agent-speed)",
            "hypothesis_settings": "server restart env/args (no admin API)",
        },
        "benchloop": {"binary": BENCHLOOP, "version": None},
        "matrix": {
            "definition": matrix_definition(),
            "sha256": matrix_definition_sha256(),
        },
        "policy": dict(FREEZE_POLICY),
    }
    binary = Path(BENCHLOOP).expanduser()
    if binary.is_file():
        ver, kind = run_subprocess([str(binary), "--version"], 10)
        frozen["benchloop"]["version"] = None if kind else ver.stdout.strip()
    frozen = attach_checksum(frozen)
    FROZEN.write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FROZEN}\nsha256={frozen['sha256']}")
    sys.exit(0)


def _add_bench_flags(parser):
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=600, help="per-request seconds")
    parser.add_argument("--warmup", action=argparse.BooleanOptionalAction, default=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint",
                   default=os.environ.get("DFLASH_ENDPOINT", DEFAULT_ENDPOINT),
                   help="server root without /v1 (default 127.0.0.1:8083)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print health/stats state; exit 2 unless ready")
    sub.add_parser("snapshot", help="write baseline-snapshot.json from live endpoints")
    sub.add_parser("check", help="exit 0 iff safe to benchmark")
    sub.add_parser("baseline", help="frozen ruler: warmup + 5x256-token coding requests")
    b = sub.add_parser("bench", help="run fixed coding workload and append one TSV row")
    _add_bench_flags(b)
    a = sub.add_parser("iterate", help="measure one restart-env/args change "
                                       "(config must already be live)")
    a.add_argument("--file", required=True)
    _add_bench_flags(a)
    bl = sub.add_parser("benchloop", help="BenchLoop gate via exact wrapper")
    bl.add_argument("--suites", default="speed")
    bl.add_argument("--timeout", type=int, default=DEFAULT_SUBPROCESS_TIMEOUT)
    pg = sub.add_parser("pi-gate", help="one Pi representative coding prompt")
    pg.add_argument("--timeout", type=int, default=900)
    sub.add_parser("freeze", help="freeze workload/config JSON, print sha256")
    mx = sub.add_parser("matrix", help="six-case workload matrix")
    mx.add_argument("--reps", type=int, default=5)
    mx.add_argument("--timeout", type=int, default=600, help="per-request seconds")
    mx.add_argument("--warmup", action=argparse.BooleanOptionalAction, default=True)
    sub.add_parser("selftest", help="synthetic fail-closed gate tests (no live model)")
    args = p.parse_args()
    global BASE
    BASE = normalize_endpoint(args.endpoint)
    dispatch = {"status": cmd_status, "snapshot": cmd_snapshot, "check": cmd_check,
                "bench": run_bench, "baseline": cmd_baseline, "iterate": cmd_iterate,
                "benchloop": cmd_benchloop, "pi-gate": cmd_pi_gate, "freeze": cmd_freeze,
                "matrix": cmd_matrix, "selftest": cmd_selftest}
    if args.cmd in LIVE_CMDS:
        held = None
        try:
            held = acquire_lease()
            dispatch[args.cmd](args)
        finally:
            release_lease(held)
    else:
        dispatch[args.cmd](args)


def cmd_selftest(_):
    """Synthetic fixtures proving the fail-closed gate; no live model calls."""
    def ok_resp(ct=10):
        return {"choices": [{"message": {"content": "def f(): ..."}}],
                "usage": {"completion_tokens": ct},
                "dflash": {"generation_tps": 50.0, "mean_acceptance": 2.0,
                           "finish_reason": "stop"}}

    good = [ok_resp() for _ in range(5)]
    before = {"requests": 100, "errors": 0}
    after = {"requests": 106, "errors": 0}  # 5 measured + 1 warmup
    cases = [
        ("missing tps", [ok_resp() for _ in range(4)] +
         [{**ok_resp(), "dflash": {k: v for k, v in ok_resp()["dflash"].items()
                                   if k != "generation_tps"}}],
         before, after, 50),
        ("empty content", [ok_resp() for _ in range(4)] +
         [{"choices": [{"message": {"content": ""}}],
           "usage": {"completion_tokens": 10},
           "dflash": {"generation_tps": 50.0, "mean_acceptance": 2.0,
                      "finish_reason": "stop"}}],
         before, after, 50),
        ("partial rep count", good[:4], before, after, 40),
        ("error delta", good, before, {"requests": 106, "errors": 2}, 50),
        ("stats request mismatch", good, before, {"requests": 105, "errors": 0}, 50),
        ("token mismatch", good, before, after, 999),
    ]
    failures = []
    for name, reps, b, a, total in cases:
        if run_error(reps, 5, True, b, a, total) is None:
            failures.append(f"{name}: expected fail, got pass")
    if run_error(good, 5, True, before, after, 50) is not None:
        failures.append("complete run: expected pass")
    for i, resp in enumerate(good):
        if rep_error(resp) is not None:
            failures.append(f"rep {i}: expected complete")

    if normalize_endpoint("http://127.0.0.1:8083/v1") != "http://127.0.0.1:8083":
        failures.append("endpoint /v1 strip failed")
    if normalize_endpoint("http://127.0.0.1:8083/") != "http://127.0.0.1:8083":
        failures.append("endpoint slash strip failed")
    argv = benchloop_argv("/bin/echo", "http://127.0.0.1:8083/v1", MODEL, "speed")
    if argv[argv.index("--endpoint") + 1] != "http://127.0.0.1:8083":
        failures.append("benchloop argv leaked /v1 on endpoint")
    if argv[0] != "/bin/echo" or "start" in argv or "stop" in argv:
        failures.append("benchloop argv must not start/stop the server")

    frozen = attach_checksum({"model": MODEL, "endpoint": DEFAULT_ENDPOINT})
    if verify_frozen_checksum(frozen) is not None:
        failures.append("checksum: expected pass on attached digest")
    bad = dict(frozen)
    bad["sha256"] = "0" * 64
    if verify_frozen_checksum(bad) is None:
        failures.append("checksum: expected fail on tampered digest")
    if verify_frozen_checksum({"model": MODEL}) is None:
        failures.append("checksum: expected fail when sha256 missing")

    run_id = "selftest-evidence"
    pending = write_pending_evidence(run_id, {"command": [sys.executable, "-c", "pass"]})
    if not pending.exists() or json.loads(pending.read_text())["status"] != "pending":
        failures.append("evidence: pending not written before launch")
    # inert subprocess: no benchmark, no server
    result, kind = run_subprocess([sys.executable, "-c", "pass"], 5)
    if kind is not None or result.returncode != 0:
        failures.append("inert command should complete")
    final = finalize_evidence(run_id, {"status": "complete", "exit": result.returncode})
    if pending.exists():
        failures.append("evidence: pending survived finalize")
    if not final.exists() or json.loads(final.read_text())["status"] != "complete":
        failures.append("evidence: final missing after complete")
    final.unlink(missing_ok=True)

    timed, kind = run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(30)"], 0.2)
    if kind != "timeout":
        failures.append("timeout: expected TimeoutExpired on short deadline")
    else:
        run_id_t = "selftest-timeout"
        write_pending_evidence(run_id_t, {"command": ["sleep"]})
        final_t = finalize_evidence(run_id_t, {"status": "timeout", "exit": None})
        if json.loads(final_t.read_text())["status"] != "timeout":
            failures.append("evidence: timeout not finalized")
        evidence_paths(run_id_t)[0].unlink(missing_ok=True)
        final_t.unlink(missing_ok=True)

    # lease: acquire/release, refuse when held, stale takeover
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="dflash-lease-st-"))
    os.environ["DFLASH_LEASE_DIR"] = str(tmp / "lease")
    held = acquire_lease()
    if not (held / "lease.json").exists():
        failures.append("lease: acquire did not write lease.json")
    else:
        meta = json.loads((held / "lease.json").read_text())
        if meta.get("pid") != os.getpid() or "argv" not in meta:
            failures.append("lease: json missing pid/argv")
    rc = 0
    _err = sys.stderr
    try:
        sys.stderr = open(os.devnull, "w")
        acquire_lease()
        rc = 0
    except SystemExit as e:
        rc = e.code
    finally:
        try:
            sys.stderr.close()
        except OSError:
            pass
        sys.stderr = _err
    if rc != 2:
        failures.append(f"lease: held acquire expected exit 2, got {rc}")
    release_lease(held)
    if held.exists():
        failures.append("lease: release left directory")
    stale = lease_path()
    os.mkdir(stale)
    dead = None
    for cand in range(50_000, 90_000):
        if not pid_alive(cand):
            dead = cand
            break
    if dead is None:
        failures.append("lease: no dead pid found for stale test")
    else:
        (stale / "lease.json").write_text(json.dumps({
            "pid": dead, "argv": ["dead"], "acquired_utc": now(),
        }) + "\n")
        took = acquire_lease()
        if not (took / "lease.json").exists():
            failures.append("lease: stale takeover failed")
        release_lease(took)

    row = format_matrix_tsv_row({
        "run_id": "x", "timestamp": "t", "endpoint": "e", "case": "short",
        "reps": 5, "median_generation_tps": 1, "median_prefill_tps": 2,
        "median_wall_client_s": 3, "median_queue_wait_s": 4,
        "median_derived_ttft_s": 5, "median_completion_tokens": 6,
        "mean_acceptance": 7, "tool_gate": "NA", "json_gate": "NA",
        "errors": 0, "note": "", "matrix_sha": "abc",
    })
    if row.split("\t")[3] != "short" or len(row.split("\t")) != 17:
        failures.append("matrix TSV row formatting")
    ok, attr = stability_error_ok(0, 1, True)
    if not ok or attr != "stability":
        failures.append("stability error-delta attributable")
    ok, attr = stability_error_ok(0, 2, True)
    if ok:
        failures.append("stability error-delta too large should fail")
    ok, attr = stability_error_ok(0, 0, False)
    if not ok:
        failures.append("stability zero delta should pass")
    words = atlas_longctx().split()
    if not (1800 <= len(words) <= 2200):
        failures.append(f"longctx word count {len(words)} not in 1800-2200")

    defn = matrix_definition()
    need = {"prompt", "kind", "max_tokens", "name"}
    if not all(need <= set(c) for c in defn["cases"]):
        failures.append("matrix_definition missing per-case fields")
    if not any(c.get("tools") for c in defn["cases"]):
        failures.append("matrix_definition omitted tools")
    if not any(c.get("extra", {}).get("logprobs") == 1 for c in defn["cases"]):
        failures.append("matrix_definition omitted stability logprobs")
    digest = matrix_definition_sha256(defn)
    changed = json.loads(json.dumps(defn))
    changed["cases"][0]["prompt"] = "CHANGED PROMPT"
    if matrix_definition_sha256(changed) == digest:
        failures.append("freeze digest unchanged after prompt edit")

    if stability_rejection_ok(400, {"error": {"message": "unknown logprobs",
                                              "type": "invalid_request_error"}}) is not True:
        failures.append("stability: valid logprobs 4xx should pass")
    stock = {"error": "Unsupported parameters for stock DFlash server: logprobs/top_logprobs"}
    if stability_rejection_ok(400, stock) is not True:
        failures.append("stability: stock string error should pass")
    if stability_rejection_ok(400, {"choices": [{"message": {"content": "ok"}}]}):
        failures.append("stability: non-error JSON should fail")
    if stability_rejection_ok(200, {"error": {"message": "logprobs"}}):
        failures.append("stability: 200 should fail")
    if tools_unsupported_ok(400, {"error": "tools not supported"}) is not True:
        failures.append("tools: attributable 4xx should pass")
    if tools_unsupported_ok(400, {"error": {"message": "unknown tool calling"}}):
        pass
    else:
        failures.append("tools: dict error naming tool calling should pass")
    if tools_unsupported_ok(500, {"error": "tools not supported"}):
        failures.append("tools: 5xx should fail")
    if tools_unsupported_ok(401, {"error": "unauthorized"}):
        failures.append("tools: auth 4xx should fail")

    meds4 = {n: 10.0 for n in ("medium", "longctx", "json", "tool")}
    meds4["short"] = 2.0
    ok, why = matrix_publication_ok(30, 30, True, meds4, False)
    if not ok:
        failures.append(f"publication: expected pass, {why}")
    ok, why = matrix_publication_ok(10, 30, True, meds4, False)
    if ok:
        failures.append("publication: bad req_delta should refuse")
    lo = "vm.swapusage: total = 1024.00M used = 100.00M free = 924.00M"
    hi = "vm.swapusage: total = 1024.00M used = 400.00M free = 624.00M"
    mid = "vm.swapusage: total = 1024.00M used = 150.00M free = 874.00M"
    if parse_swap_used_mb(lo) != 100.0:
        failures.append("parse_swap_used_mb")
    if not swap_rise_worsened(lo, hi):
        failures.append("swap 300MB rise should refuse")
    if swap_rise_worsened(lo, mid):
        failures.append("swap 50MB rise should pass")
    if not swap_rise_worsened(lo, "unknown"):
        failures.append("unparseable swap should refuse")
    pol = FREEZE_POLICY
    if (pol["keep_threshold_pct"] != 7 or pol["stop_target_pct"] != 20
            or pol["min_hypotheses"] != 10 or pol["material_swap_rise_mb"] != 200):
        failures.append("FREEZE_POLICY constants")
    ok, why = matrix_publication_ok(30, 30, False, meds4, False)
    if ok:
        failures.append("publication: err_ok false should refuse")
    meds_missing = dict(meds4)
    del meds_missing["json"]
    ok, why = matrix_publication_ok(30, 30, True, meds_missing, False)
    if ok:
        failures.append("publication: missing sustained case median should refuse")
    no_short = {n: 10.0 for n in ("medium", "longctx", "json", "tool")}
    ok, why = matrix_publication_ok(30, 30, True, no_short, False)
    if not ok:
        failures.append(f"publication: short excluded should still pass, {why}")
    mixed = {"short": 1.0, "medium": 10.0, "longctx": 20.0, "json": 30.0, "tool": 40.0}
    if statistics.median([mixed[n] for n in ("medium", "longctx", "json", "tool")]) != 25.0:
        failures.append("four-case median math")
    if expected_generation_requests(5, True, True) != 24:
        failures.append("expected generation requests unsupported should be 24")
    meds_unsup = {n: 10.0 for n in ("short", "medium", "longctx", "json")}
    ok, why = matrix_publication_ok(24, 24, True, meds_unsup, True)
    if ok:
        failures.append("publication: tool unsupported must not qualify")

    mp = memory_pressure()
    if not (isinstance(mp, int) or mp == "unknown"):
        failures.append(f"memory_pressure: expected int or unknown, got {mp!r}")
    if memory_pressure_preflight_ok(1) is not True:
        failures.append("preflight: level 1 should pass")
    if memory_pressure_preflight_ok(2) is not False:
        failures.append("preflight: level 2 should refuse")
    if memory_pressure_preflight_ok("unknown") is not False:
        failures.append("preflight: unknown should refuse")
    if memory_pressure_worsened(1, 1):
        failures.append("worsened: 1->1 should pass")
    if not memory_pressure_worsened(1, 2):
        failures.append("worsened: 1->2 should fail")
    if not memory_pressure_worsened(1, "unknown"):
        failures.append("worsened: unknown after should fail")
    dflash_ok = {"generation_tps": 40.0, "mean_acceptance": 1.0,
                 "finish_reason": "tool_calls"}
    usage_ok = {"completion_tokens": 8}
    tool_ok = {"choices": [{"message": {"content": "", "tool_calls": [
        {"function": {"name": "get_weather",
                      "arguments": '{"city": "Paris"}'}}]}}],
               "usage": usage_ok, "dflash": dflash_ok}
    if rep_error(tool_ok, case="tool") is not None:
        failures.append("tool empty content with tool_calls should pass")
    tool_bad = {"choices": [{"message": {"content": ""}}],
                "usage": usage_ok, "dflash": dflash_ok}
    if rep_error(tool_bad, case="tool") is None:
        failures.append("tool empty content without tool_calls should fail")
    if rep_error({"choices": [{"message": {"content": ""}}],
                  "usage": usage_ok, "dflash": dflash_ok}) is None:
        failures.append("non-tool empty content should fail")
    wl = matrix_definition()
    if "cases" not in wl or "reps_default" not in wl or "warmup" not in wl:
        failures.append("freeze workload matrix_definition missing fields")
    primary = (
        "median over the four sustained generation cases "
        "(medium, longctx, json, tool) of each case's median "
        "server dflash.generation_tps; short is latency-only and "
        "excluded from the primary; qualified waves only "
        f"(matrix sha256={digest})"
    )
    if "four sustained" not in primary or "medium, longctx, json, tool" not in primary:
        failures.append("ruler.primary missing four sustained cases")
    if digest not in primary:
        failures.append("ruler.primary missing matrix sha")
    defn = matrix_definition()
    if defn.get("primary_cases") != ["medium", "longctx", "json", "tool"]:
        failures.append("matrix_definition missing primary_cases")
    if defn.get("primary_label") != "median_of_four_sustained_cases":
        failures.append("matrix_definition missing primary_label")
    short_def = next(c for c in defn["cases"] if c["name"] == "short")
    if short_def.get("role") != "sensitivity_only" or short_def.get("primary") is not False:
        failures.append("short case not tagged sensitivity_only")
    digest = matrix_definition_sha256(defn)
    mutated = json.loads(json.dumps(defn))
    mutated["primary_cases"] = ["short", "medium", "longctx", "json", "tool"]
    new_sha = matrix_definition_sha256(mutated)
    if new_sha == digest:
        failures.append("primary_cases edit did not change matrix sha")
    old_row = format_matrix_tsv_row({
        "run_id": "old", "timestamp": "t", "endpoint": "e",
        "case": "AGGREGATE", "reps": 5, "median_generation_tps": 1,
        "median_prefill_tps": "NA", "median_wall_client_s": "NA",
        "median_queue_wait_s": "NA", "median_derived_ttft_s": "NA",
        "median_completion_tokens": "NA", "mean_acceptance": "NA",
        "tool_gate": True, "json_gate": True, "errors": 0, "note": "",
        "matrix_sha": new_sha,
    })
    old_tsv = tmp / "old-five-case.tsv"
    old_tsv.write_text(MATRIX_TSV_HEADER + "\n" + old_row + "\n")
    if freeze_matrix_gate(digest, old_tsv) is None:
        failures.append("freeze_matrix_gate accepted old five-case sha")

    tsv_gate = tmp / "matrix-results.tsv"
    tsv_gate.write_text(MATRIX_TSV_HEADER + "\n")
    if freeze_matrix_gate("deadbeef", tsv_gate) is None:
        failures.append("freeze gate: empty tsv should refuse")
    append_line = format_matrix_tsv_row({
        "run_id": "r", "timestamp": "t", "endpoint": "e",
        "case": "AGGREGATE", "reps": 5, "median_generation_tps": 1,
        "median_prefill_tps": "NA", "median_wall_client_s": "NA",
        "median_queue_wait_s": "NA", "median_derived_ttft_s": "NA",
        "median_completion_tokens": "NA", "mean_acceptance": "NA",
        "tool_gate": True, "json_gate": True, "errors": 0, "note": "",
        "matrix_sha": digest,
    })
    with tsv_gate.open("a") as f:
        f.write(append_line + "\n")
    if freeze_matrix_gate(digest, tsv_gate) is not None:
        failures.append("freeze gate: matching AGGREGATE should pass")
    if freeze_matrix_gate("other", tsv_gate) is None:
        failures.append("freeze gate: mismatched sha should refuse")

    global MATRIX_TSV
    tsv_orig = MATRIX_TSV
    tsv_tmp = tmp / "abort-matrix-results.tsv"
    MATRIX_TSV = tsv_tmp
    evdir = tmp / "ev-fail"
    evdir.mkdir()
    _out = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        fail_matrix(evdir, "abort-rep", "measured rep error")
        failures.append("fail_matrix: expected SystemExit 1")
    except SystemExit as e:
        if e.code != 1:
            failures.append(f"fail_matrix: expected exit 1, got {e.code}")
    finally:
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.stdout = _out
    MATRIX_TSV = tsv_orig
    if tsv_tmp.exists():
        failures.append("measured-rep abort wrote a TSV row")
    if not (evdir / "fail.json").exists():
        failures.append("fail_matrix: fail.json missing")

    # two concurrent stale takeovers: exactly one winner
    race_dir = tmp / "race-lease"
    os.environ["DFLASH_LEASE_DIR"] = str(race_dir)
    os.mkdir(race_dir)
    (race_dir / "lease.json").write_text(json.dumps({
        "pid": dead if dead is not None else 999999,
        "argv": ["dead"], "acquired_utc": now(),
    }) + "\n")
    src = str(Path(__file__).resolve())
    helper = (
        "import importlib.util, os, sys\n"
        f"os.environ['DFLASH_LEASE_DIR'] = {str(race_dir)!r}\n"
        f"spec = importlib.util.spec_from_file_location('hillclimb', {src!r})\n"
        "h = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(h)\n"
        "try:\n"
        "    h.acquire_lease()\n"
        "except SystemExit as e:\n"
        "    print('LOSE', e.code)\n"
        "    raise SystemExit(e.code or 2)\n"
        "print('WIN')\n"
    )
    env = {**os.environ, "DFLASH_LEASE_DIR": str(race_dir)}
    procs = [
        subprocess.Popen([sys.executable, "-c", helper], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outs = []
    for p in procs:
        so, se = p.communicate(timeout=10)
        outs.append((p.returncode, so, se))
    wins = sum(1 for rc, so, se in outs if "WIN" in so)
    if wins != 1:
        failures.append(f"takeover race: expected 1 winner, got {wins} {outs!r}")
    if race_dir.exists():
        release_lease(race_dir)

    wf = tmp / "lease-writefail"
    os.environ["DFLASH_LEASE_DIR"] = str(wf)
    orig_dumps = json.dumps
    def boom(*a, **k):
        raise OSError("injected lease.json write failure")
    json.dumps = boom
    raised = False
    try:
        acquire_lease()
    except OSError:
        raised = True
    except SystemExit:
        raised = True
    finally:
        json.dumps = orig_dumps
    if not raised:
        failures.append("lease write-fail: acquire did not raise")
    if wf.exists():
        failures.append("lease write-fail: directory not cleared")


    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("selftest OK: fail-closed + lease + matrix gates covered")


if __name__ == "__main__":
    main()
