#!/usr/bin/env python3
"""Frozen native Ornith private-speed baseline. No downloads. Serial only."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8083/v1/chat/completions"
HOST, PORT = "127.0.0.1", 8083
MODEL = "/Volumes/BBALL_GM_DATA/models/Ornith-1.5-35B-A3B-Abliterated-MLX-4bit"
RUN_ID = "ornith-mlx-pi-20260901"
TIMEOUT_S = 120
REPS = 3
CASES = ("exact", "summary", "coding", "json", "tool")


def check_ownership() -> dict[str, Any]:
    cmd = ["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"]
    lsof = subprocess.run(cmd, capture_output=True, text=True)
    ps = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,rss=,command="],
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in ps.stdout.splitlines() if ln.strip()]
    owner_ok = False
    listener_pid = None
    lane_pid = None
    details: list[str] = []
    for ln in lines:
        if f"--port {PORT}" in ln or f"--port={PORT}" in ln or ":8083" in ln:
            details.append(ln[:500])
        if RUN_ID in ln and MODEL in ln and str(PORT) in ln:
            owner_ok = True
            parts = ln.split(None, 3)
            try:
                pid = int(parts[0])
            except (ValueError, IndexError):
                continue
            if "mlx_vlm.server" in ln and "-m router.hardware_lane" not in ln:
                listener_pid = pid
            if "-m router.hardware_lane" in ln:
                lane_pid = pid
    listen_ok = f":{PORT}" in lsof.stdout or f".{PORT}" in lsof.stdout
    if not owner_ok or not listen_ok:
        snap = {
            "status": "OWNERSHIP_CHANGED",
            "lsof": lsof.stdout,
            "matching_ps": details,
            "utc": datetime.now(timezone.utc).isoformat(),
        }
        (HERE / "ownership-fail.json").write_text(json.dumps(snap, indent=2) + "\n")
        print("OWNERSHIP_CHANGED", file=sys.stderr)
        sys.exit(2)
    return {
        "ok": True,
        "run_id": RUN_ID,
        "model": MODEL,
        "listener_pid": listener_pid,
        "lane_pid": lane_pid,
        "lsof": lsof.stdout,
        "utc": datetime.now(timezone.utc).isoformat(),
    }


def memory_snapshot(pid: int | None) -> dict[str, Any]:
    snap: dict[str, Any] = {"pid": pid, "utc": datetime.now(timezone.utc).isoformat()}
    if pid:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "rss=,vsz="],
            capture_output=True,
            text=True,
        )
        parts = r.stdout.split()
        if len(parts) >= 2:
            snap["rss_kb"] = int(parts[0])
            snap["vsz_kb"] = int(parts[1])
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True)
    snap["vm_stat"] = vm.stdout
    swap_pages = None
    pressure = None
    for line in vm.stdout.splitlines():
        low = line.lower()
        if "swapins" in low.replace(" ", "") or "swapins" in low:
            try:
                swap_pages = int(line.split(":")[-1].strip().rstrip("."))
            except ValueError:
                pass
        if "pageouts" in low and "swap" not in low:
            pass
    mem = subprocess.run(
        ["sysctl", "-n", "vm.swapusage", "kern.memorystatus_vm_pressure_level"],
        capture_output=True,
        text=True,
    )
    # sysctl -n with two names may fail; query separately
    su = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True)
    snap["swapusage"] = su.stdout.strip()
    pr = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True,
        text=True,
    )
    snap["memory_pressure_level"] = pr.stdout.strip() or None
    snap["swapins_pages"] = swap_pages
    return snap


def synthetic_passage() -> str:
    # Deterministic ~900-word engineering passage. No network.
    sentences = [
        "A native inference service on Apple silicon must treat the memory hierarchy as the first-order constraint.",
        "Weights live in unified memory; every extra copy between host buffers and the Metal command stream is paid twice.",
        "Prefill cost grows with prompt tokens while decode cost grows with generated tokens, so mixed workloads hide the slower path.",
        "A frozen request set is the only way to compare two nights of work without confounding the prompt distribution.",
        "Serial execution avoids queueing artifacts that look like model slowdowns.",
        "Temperature zero removes sampling noise so finish reasons and token counts stay comparable.",
        "Tool-calling validity is orthogonal to HTTP success and must be scored separately.",
        "Exact-output cases are cheap probes for first-byte latency and tokenizer round-trips.",
        "Longer summaries exercise prefill more than decode and should not be averaged blindly with short replies.",
        "Coding prompts tend to emit denser tokens and hit max_tokens more often than chat summaries.",
        "JSON-only prompts test whether the server honors structured constraints without a grammar backend.",
        "Host RSS and swap snapshots catch silent paging that wall clocks miss.",
        "A 120-second timeout is a safety rail, not a performance target.",
        "Identity of the listener process must be re-checked after the run because leases can rotate.",
        "This passage exists only to pin a stable prefill payload for the summary case.",
    ]
    words: list[str] = []
    i = 0
    while len(words) < 900:
        sent = sentences[i % len(sentences)]
        extra = f" Cycle {i} records that the workload remains synthetic and locally generated."
        chunk = (sent + extra).split()
        words.extend(chunk)
        i += 1
    return " ".join(words[:900])


def build_requests() -> dict[str, Any]:
    passage = synthetic_passage()
    assert len(passage.split()) == 900
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                        "units": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                        },
                    },
                    "required": ["city"],
                },
            },
        }
    ]
    reqs = {
        "exact": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 32,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with the exact text ORNITH_SPEED_OK and nothing else.",
                }
            ],
        },
        "summary": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 180,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Summarize the following passage in exactly five bullet points. "
                        "Do not add a title.\n\n" + passage
                    ),
                }
            ],
        },
        "coding": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Write a Python function merge(defaults, file_values, env) that returns a new dict. "
                        "Do not mutate any input. Later mappings override earlier ones with env taking precedence "
                        "over file_values over defaults. Output code only, no markdown fences, no explanation."
                    ),
                }
            ],
        },
        "json": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Return one JSON object and nothing else with these exact keys and values: "
                        '{"ok": true, "suite": "private-speed", "n": 15}.'
                    ),
                }
            ],
        },
        "tool": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 128,
            "tools": tools,
            "tool_choice": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "What is the weather in Paris? Use the get_weather tool.",
                }
            ],
        },
    }
    return reqs


def canonical_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p
    f = int(k)
    c = min(f + 1, len(ys) - 1)
    if f == c:
        return ys[f]
    return ys[f] + (ys[c] - ys[f]) * (k - f)


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return statistics.median(xs)


def post_chat(body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = Request(
        ENDPOINT,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    t0 = time.perf_counter()
    first_byte = None
    raw = b""
    status = None
    err = None
    try:
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if first_byte is None:
                    first_byte = time.perf_counter()
                raw += chunk
    except HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:
            raw = b""
        if first_byte is None:
            first_byte = time.perf_counter()
        err = str(e)
    except (URLError, TimeoutError, socket.timeout) as e:
        err = str(e)
        status = None
    except Exception as e:
        err = repr(e)
        status = None
    t1 = time.perf_counter()
    total_s = t1 - t0
    ttfb_s = (first_byte - t0) if first_byte is not None else None
    text = None
    try:
        text = raw.decode("utf-8")
    except Exception:
        text = raw.decode("utf-8", "replace")
    parsed = None
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return {
        "http_status": status,
        "error": err,
        "ttfb_s": ttfb_s,
        "total_s": total_s,
        "raw_body": text,
        "parsed": parsed,
    }


def extract_usage(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    u = parsed.get("usage") or {}
    return {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "total_tokens": u.get("total_tokens"),
    }


def extract_message(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    choices = parsed.get("choices") or []
    if not choices:
        return {"finish_reason": None, "content": None, "tool_calls": None}
    ch = choices[0]
    msg = ch.get("message") or {}
    return {
        "finish_reason": ch.get("finish_reason"),
        "content": msg.get("content"),
        "tool_calls": msg.get("tool_calls"),
    }


def exact_valid(case: str, content: Any) -> bool | None:
    if case != "exact":
        return None
    if not isinstance(content, str):
        return False
    return content.strip() == "ORNITH_SPEED_OK"


def json_valid(case: str, content: Any) -> bool | None:
    if case != "json":
        return None
    if not isinstance(content, str):
        return False
    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return False
    return obj == {"ok": True, "suite": "private-speed", "n": 15}


def tool_valid(case: str, tool_calls: Any) -> bool | None:
    if case != "tool":
        return None
    if not tool_calls:
        return False
    try:
        tc = tool_calls[0]
        fn = tc.get("function") or {}
        if fn.get("name") != "get_weather":
            return False
        args = fn.get("arguments")
        if isinstance(args, str):
            args = json.loads(args)
        city = str(args.get("city", "")).lower()
        return "paris" in city
    except Exception:
        return False


def tps(completion_tokens: Any, total_s: float | None) -> float | None:
    if completion_tokens is None or not total_s or total_s <= 0:
        return None
    try:
        return float(completion_tokens) / float(total_s)
    except (TypeError, ValueError):
        return None


def main() -> int:
    own_before = check_ownership()
    mem_before = memory_snapshot(own_before.get("listener_pid"))
    (HERE / "ownership-before.json").write_text(json.dumps(own_before, indent=2) + "\n")
    (HERE / "memory-before.json").write_text(json.dumps(mem_before, indent=2) + "\n")

    reqs = build_requests()
    (HERE / "requests.json").write_text(json.dumps(reqs, indent=2) + "\n")
    workload_hash = canonical_hash(reqs)
    manifest = {
        "workload_hash_sha256": workload_hash,
        "endpoint": ENDPOINT,
        "model": MODEL,
        "run_id_expected": RUN_ID,
        "temperature": 0,
        "cases": list(CASES),
        "reps": REPS,
        "warmup": "exact",
        "timeout_s": TIMEOUT_S,
        "comparison_scope": "new_private_ornith_baseline",
        "frozen_utc": datetime.now(timezone.utc).isoformat(),
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    warmup_dir = HERE / "warmup"
    warmup_dir.mkdir(exist_ok=True)
    w = post_chat(reqs["exact"])
    (warmup_dir / "exact.json").write_text(json.dumps(w, indent=2) + "\n")

    records: list[dict[str, Any]] = []
    for case in CASES:
        for rep in range(1, REPS + 1):
            result = post_chat(reqs[case])
            usage = extract_usage(result["parsed"])
            msg = extract_message(result["parsed"])
            rec = {
                "case": case,
                "rep": rep,
                "http_status": result["http_status"],
                "error": result["error"],
                "ttfb_s": result["ttfb_s"],
                "total_s": result["total_s"],
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "completion_tps": tps(usage.get("completion_tokens"), result["total_s"]),
                "finish_reason": msg.get("finish_reason"),
                "exact_output_valid": exact_valid(case, msg.get("content")),
                "json_output_valid": json_valid(case, msg.get("content")),
                "tool_call_valid": tool_valid(case, msg.get("tool_calls")),
                "content": msg.get("content"),
                "tool_calls": msg.get("tool_calls"),
            }
            raw_path = HERE / f"{case}-rep{rep}.json"
            payload = dict(rec)
            payload["raw_body"] = result["raw_body"]
            payload["parsed"] = result["parsed"]
            raw_path.write_text(json.dumps(payload, indent=2) + "\n")
            records.append(rec)
            print(
                f"{case} rep{rep} status={rec['http_status']} total_s={rec['total_s']:.3f} "
                f"tok={rec['completion_tokens']} tps={rec['completion_tps']}",
                flush=True,
            )

    own_after = check_ownership()
    mem_after = memory_snapshot(own_after.get("listener_pid"))
    (HERE / "ownership-after.json").write_text(json.dumps(own_after, indent=2) + "\n")
    (HERE / "memory-after.json").write_text(json.dumps(mem_after, indent=2) + "\n")

    ok = [r for r in records if r["http_status"] == 200]
    err = [r for r in records if r["http_status"] != 200]
    lat = [float(r["total_s"]) for r in records if r["total_s"] is not None]
    all_tps = [float(r["completion_tps"]) for r in records if r["completion_tps"] is not None]
    per_case: dict[str, Any] = {}
    for case in CASES:
        cr = [r for r in records if r["case"] == case]
        ctps = [float(r["completion_tps"]) for r in cr if r["completion_tps"] is not None]
        clat = [float(r["total_s"]) for r in cr if r["total_s"] is not None]
        per_case[case] = {
            "n": len(cr),
            "http_200": sum(1 for r in cr if r["http_status"] == 200),
            "median_completion_tps": median(ctps),
            "p50_total_s": percentile(clat, 0.50),
            "p95_total_s": percentile(clat, 0.95),
            "completion_tokens": sum(int(r["completion_tokens"] or 0) for r in cr),
        }
    summary = {
        "comparison_scope": "new_private_ornith_baseline",
        "workload_hash_sha256": workload_hash,
        "endpoint": ENDPOINT,
        "model": MODEL,
        "package": "mlx_vlm.server",
        "run_id": RUN_ID,
        "listener_pid_before": own_before.get("listener_pid"),
        "listener_pid_after": own_after.get("listener_pid"),
        "requests": len(records),
        "http_200_count": len(ok),
        "error_count": len(err),
        "total_completion_tokens": sum(int(r["completion_tokens"] or 0) for r in records),
        "overall_median_completion_tps": median(all_tps),
        "p50_total_s": percentile(lat, 0.50),
        "p95_total_s": percentile(lat, 0.95),
        "exact_output_valid_count": sum(1 for r in records if r["exact_output_valid"] is True),
        "json_output_valid_count": sum(1 for r in records if r["json_output_valid"] is True),
        "tool_call_valid_count": sum(1 for r in records if r["tool_call_valid"] is True),
        "tool_call_invalid_count": sum(1 for r in records if r["tool_call_valid"] is False),
        "per_case": per_case,
        "memory_before": {
            "rss_kb": mem_before.get("rss_kb"),
            "swapusage": mem_before.get("swapusage"),
            "memory_pressure_level": mem_before.get("memory_pressure_level"),
        },
        "memory_after": {
            "rss_kb": mem_after.get("rss_kb"),
            "swapusage": mem_after.get("swapusage"),
            "memory_pressure_level": mem_after.get("memory_pressure_level"),
        },
        "utc": datetime.now(timezone.utc).isoformat(),
    }
    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (HERE / "records.json").write_text(json.dumps(records, indent=2) + "\n")
    verdict = "PASS" if len(ok) == 15 and len(records) == 15 else "PARTIAL" if ok else "FAIL"
    print(verdict, flush=True)
    print(json.dumps({"http_200": len(ok), "errors": len(err), "hash": workload_hash}, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
