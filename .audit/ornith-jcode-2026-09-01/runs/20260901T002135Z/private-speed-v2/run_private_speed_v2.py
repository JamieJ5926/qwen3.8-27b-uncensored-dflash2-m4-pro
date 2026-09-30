#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, statistics, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
HERE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8083/v1/chat/completions"
PORT = 8083
MODEL = "/Volumes/BBALL_GM_DATA/models/Ornith-1.5-35B-A3B-Abliterated-MLX-4bit"
RUN_ID = "ornith-mlx-pi-20260901"
CASES = ("exact", "summary", "coding", "json", "tool")
def utc():
    return datetime.now(timezone.utc).isoformat()
def dump(p, o):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2) + "\n")
def sha(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def parse_listener_pid(lsof_text):
    for ln in (lsof_text or "").splitlines():
        if "127.0.0.1:8083" not in ln:
            continue
        parts = ln.split()
        if len(parts) < 2:
            continue
        try:
            return int(parts[1])
        except ValueError:
            continue
    return None
def ownership():
    lsof = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True)
    ps = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,rss=,command="], capture_output=True, text=True)
    ok = False; listener = lane = rss = None
    for ln in ps.stdout.splitlines():
        if "-m router.hardware_lane" in ln and RUN_ID in ln:
            ok = True
            parts = ln.split(None, 3)
            try:
                pid, rss_kb = int(parts[0]), int(parts[2])
            except (ValueError, IndexError):
                continue
            lane = pid
            rss = rss if rss is not None else rss_kb
    listener = parse_listener_pid(lsof.stdout)
    snap = {"ok": ok and (f":{PORT}" in lsof.stdout), "run_id": RUN_ID, "model": MODEL, "listener_pid": listener, "lane_pid": lane, "rss_kb": rss, "lsof": lsof.stdout, "utc": utc()}
    if not snap["ok"]:
        snap["status"] = "OWNERSHIP_CHANGED"; dump(HERE / "ownership-fail.json", snap); print("OWNERSHIP_CHANGED", file=sys.stderr); sys.exit(2)
    return snap
def passage():
    sents = ["A native inference service on Apple silicon must treat the memory hierarchy as the first-order constraint.", "Weights live in unified memory; every extra copy between host buffers and the Metal command stream is paid twice.", "Prefill cost grows with prompt tokens while decode cost grows with generated tokens, so mixed workloads hide the slower path.", "A frozen request set is the only way to compare two nights of work without confounding the prompt distribution.", "Serial execution avoids queueing artifacts that look like model slowdowns.", "Temperature zero removes sampling noise so finish reasons and token counts stay comparable.", "Tool-calling validity is orthogonal to HTTP success and must be scored separately.", "Exact-output cases are cheap probes for first-byte latency and tokenizer round-trips.", "Longer summaries exercise prefill more than decode and should not be averaged blindly with short replies.", "Coding prompts tend to emit denser tokens and hit max_tokens more often than chat summaries.", "JSON-only prompts test whether the server honors structured constraints without a grammar backend.", "Host RSS and swap snapshots catch silent paging that wall clocks miss.", "A 120-second timeout is a safety rail, not a performance target.", "Identity of the listener process must be re-checked after the run because leases can rotate.", "This passage exists only to pin a stable prefill payload for the summary case."]
    words = []; i = 0
    while len(words) < 900:
        words.extend((sents[i % len(sents)] + f" Cycle {i} records that the workload remains synthetic and locally generated.").split()); i += 1
    return " ".join(words[:900])
def build_requests():
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get current weather for a city.", "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "City name"}, "units": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, "required": ["city"]}}}]
    p = passage()
    return {
        "exact": {"model": MODEL, "temperature": 0, "max_tokens": 32, "messages": [{"role": "user", "content": "Reply with the exact text ORNITH_SPEED_OK and nothing else."}]},
        "summary": {"model": MODEL, "temperature": 0, "max_tokens": 180, "messages": [{"role": "user", "content": "Summarize the following passage in exactly five bullet points. Do not add a title.\n\n" + p}]},
        "coding": {"model": MODEL, "temperature": 0, "max_tokens": 256, "messages": [{"role": "user", "content": "Write a Python function merge(defaults, file_values, env) that returns a new dict. Do not mutate any input. Later mappings override earlier ones with env taking precedence over file_values over defaults. Output code only, no markdown fences, no explanation."}]},
        "json": {"model": MODEL, "temperature": 0, "max_tokens": 128, "messages": [{"role": "user", "content": 'Return one JSON object and nothing else with these exact keys and values: {"ok": true, "suite": "private-speed", "n": 15}.'}]},
        "tool": {"model": MODEL, "temperature": 0, "max_tokens": 128, "tools": tools, "tool_choice": "auto", "messages": [{"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}]},
    }
def pct(xs, p):
    if not xs: return None
    ys = sorted(xs); k = (len(ys) - 1) * p; f = int(k); c = min(f + 1, len(ys) - 1)
    return ys[f] if f == c else ys[f] + (ys[c] - ys[f]) * (k - f)
def validity(case, parsed, text):
    if case == "exact": return text.strip() == "ORNITH_SPEED_OK"
    if case == "json":
        try:
            o = json.loads(text); return o.get("ok") is True and o.get("suite") == "private-speed" and o.get("n") == 15
        except Exception: return False
    if case == "tool":
        try:
            tcs = ((parsed or {}).get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
            fn = (tcs[0].get("function") or {}) if tcs else {}
            return fn.get("name") == "get_weather" and "city" in json.loads(fn.get("arguments") or "{}")
        except Exception: return False
    return None
def post(body, timeout):
    req = Request(ENDPOINT, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    t0 = time.perf_counter(); raw = b""; status = None; err = None; timed_out = False
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode(); raw = resp.read()
    except HTTPError as e:
        status = e.code
        try: raw = e.read()
        except Exception: raw = b""
        err = str(e)
    except (URLError, TimeoutError, Exception) as e:
        err = str(e); timed_out = "timed out" in str(e).lower() or isinstance(e, TimeoutError)
    total = time.perf_counter() - t0
    text_raw = raw.decode("utf-8", errors="replace")
    try: parsed = json.loads(text_raw) if text_raw else None
    except Exception: parsed = None
    usage = (parsed or {}).get("usage") or {}; prompt_t = usage.get("prompt_tokens"); comp_t = usage.get("completion_tokens")
    tps = (comp_t / total) if (isinstance(comp_t, (int, float)) and total > 0) else None
    ch = ((parsed or {}).get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return {"http_status": status, "timed_out": timed_out, "total_s": total, "error": err, "raw_body": text_raw, "parsed": parsed, "prompt_tokens": prompt_t, "completion_tokens": comp_t, "completion_tps": tps, "finish_reason": ch.get("finish_reason"), "content": msg.get("content") or ""}
def summarize(records, own_b, own_a, wh, errors, verdict):
    ok_recs = [r for r in records if r.get("http_status") == 200 and not r.get("timed_out")]
    tps = [r["completion_tps"] for r in ok_recs if isinstance(r.get("completion_tps"), (int, float))]
    lats = [r["total_s"] for r in ok_recs if isinstance(r.get("total_s"), (int, float))]
    per_case = {c: (statistics.median(ct) if (ct := [r["completion_tps"] for r in ok_recs if r.get("case") == c and isinstance(r.get("completion_tps"), (int, float))]) else None) for c in CASES}
    vcounts = {"exact": 0, "json": 0, "tool": 0}
    for r in records:
        if r.get("validity") is True and r.get("case") in vcounts: vcounts[r["case"]] += 1
    summary = {"verdict": verdict, "completed_count": len(ok_recs), "completed_of": 10, "errors": errors, "per_case_median_completion_tps": per_case, "overall_median_completion_tps": statistics.median(tps) if tps else None, "p50_latency_s": pct(lats, 0.50), "p95_latency_s": pct(lats, 0.95), "validity_counts": vcounts, "model": MODEL, "endpoint": ENDPOINT, "workload_hash": wh, "comparison_scope": "new_private_ornith_baseline", "ownership_before": own_b, "ownership_after": own_a, "utc": utc(), "resolved_model": "cli-proxy/dddai.grok-4.6.implementer", "fallback_state": "none"}
    dump(HERE / "summary.json", summary); return summary
def main():
    own_b = ownership(); dump(HERE / "ownership-before.json", own_b)
    reqs = build_requests(); dump(HERE / "requests.json", reqs); wh = sha(reqs)
    dump(HERE / "manifest.json", {"workload_hash_sha256": wh, "endpoint": ENDPOINT, "model": MODEL, "run_id_expected": RUN_ID, "temperature": 0, "cases": list(CASES), "reps": 2, "warmup": "exact", "warmup_timeout_s": 15, "timeout_s": 45, "comparison_scope": "new_private_ornith_baseline", "frozen_utc": utc()})
    recs = []; errors = []; rec_dir = HERE / "records"; rec_dir.mkdir(parents=True, exist_ok=True)
    w = post(reqs["exact"], 15); w.update({"case": "exact", "rep": "warmup", "excluded": True, "rss_before_kb": own_b.get("rss_kb")}); dump(HERE / "warmup" / "exact.json", w)
    if w.get("timed_out") or w.get("http_status") != 200:
        errors.append({"phase": "warmup", "error": w.get("error"), "timed_out": w.get("timed_out"), "http_status": w.get("http_status")})
        try: own_a = ownership()
        except SystemExit: own_a = {"status": "OWNERSHIP_CHANGED"}
        dump(HERE / "ownership-after.json", own_a); s = summarize(recs, own_b, own_a, wh, errors, "FAIL"); print(json.dumps({"verdict": s["verdict"], "completed": s["completed_count"]})); return 1
    stopped = False
    for rep in range(1, 3):
        if stopped: break
        for case in CASES:
            own_mid = ownership(); rec = post(reqs[case], 45)
            rec.update({"case": case, "rep": rep, "excluded": False, "rss_before_kb": own_mid.get("rss_kb"), "validity": validity(case, rec.get("parsed"), rec.get("content") or ""), "ownership": {"listener_pid": own_mid.get("listener_pid"), "lane_pid": own_mid.get("lane_pid"), "rss_kb": own_mid.get("rss_kb")}})
            dump(rec_dir / f"{case}-rep{rep}.json", rec); recs.append(rec)
            if rec.get("timed_out"):
                errors.append({"case": case, "rep": rep, "error": rec.get("error"), "timed_out": True}); stopped = True; break
            if rec.get("http_status") != 200:
                errors.append({"case": case, "rep": rep, "error": rec.get("error"), "http_status": rec.get("http_status")})
    own_a = ownership(); dump(HERE / "ownership-after.json", own_a)
    n200 = sum(1 for r in recs if r.get("http_status") == 200 and not r.get("timed_out"))
    verdict = "PASS" if n200 == 10 else ("PARTIAL" if n200 > 0 else "FAIL")
    s = summarize(recs, own_b, own_a, wh, errors, verdict)
    print(json.dumps({"verdict": s["verdict"], "completed": s["completed_count"], "errors": errors}))
    return 0 if verdict == "PASS" else 1
if __name__ == "__main__":
    sys.exit(main())
