#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8083/v1/chat/completions"
PORT = 8083
MODEL = "/Volumes/BBALL_GM_DATA/models/Ornith-1.5-35B-A3B-Abliterated-MLX-4bit"
RUN_ID = "ornith-mlx-pi-20260901"
REQ_TIMEOUT = 60
CHECK_TIMEOUT = 10
MAX_TOKENS = 1024

FORBIDDEN = re.compile(r"\b(eval|test|judge|score|compare|benchmark|candidate|model identity)\b", re.I)


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(p: Path, o):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2) + "\n")


def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s)


def sha_obj(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


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
    ok = False
    listener = lane = rss = None
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
    snap = {
        "ok": ok and (f":{PORT}" in lsof.stdout),
        "run_id": RUN_ID,
        "model": MODEL,
        "listener_pid": listener,
        "lane_pid": lane,
        "rss_kb": rss,
        "lsof": lsof.stdout,
        "utc": utc(),
    }
    if not snap["ok"]:
        snap["status"] = "OWNERSHIP_CHANGED"
        dump(HERE / "ownership-fail.json", snap)
        print("OWNERSHIP_CHANGED", file=sys.stderr)
        sys.exit(2)
    return snap


def strip_fence(text: str) -> str:
    s = (text or "").strip()
    m = re.match(r"^```(?:python|py)?\s*\n(.*)\n```\s*$", s, re.S | re.I)
    if m:
        return m.group(1)
    return s


def post(body, timeout):
    req = Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    t0 = time.perf_counter()
    raw = b""
    status = None
    err = None
    timed_out = False
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:
            raw = b""
        err = str(e)
    except (URLError, TimeoutError, Exception) as e:
        err = str(e)
        timed_out = "timed out" in str(e).lower() or isinstance(e, TimeoutError)
    total = time.perf_counter() - t0
    text_raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text_raw) if text_raw else None
    except Exception:
        parsed = None
    usage = (parsed or {}).get("usage") or {}
    ch = ((parsed or {}).get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return {
        "http_status": status,
        "timed_out": timed_out,
        "total_s": total,
        "error": err,
        "raw_body": text_raw,
        "parsed": parsed,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "finish_reason": ch.get("finish_reason"),
        "content": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls") or [],
        "message": msg,
    }


def run_py(code_path: Path, extra_args=None, stdin=None, cwd=None):
    work = Path(cwd or code_path.parent)
    cmd = ["python3", "-I", "-B", str(code_path)] + (extra_args or [])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(work) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
            cwd=str(work),
            input=stdin,
            env=env,
        )
        return {
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "timed_out": False,
            "elapsed_s": time.perf_counter() - t0,
            "cmd": cmd,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "returncode": None,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "timed_out": True,
            "elapsed_s": time.perf_counter() - t0,
            "cmd": cmd,
        }


def tool_args(tc):
    fn = (tc or {}).get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {"_malformed": True, "_raw": raw}


def tool_name(tc):
    return ((tc or {}).get("function") or {}).get("name")


def build_manifest():
    c2_jsonl = '{"kind":"io","n":1}\n{"kind":"parse","n":2}\n{"kind":"io","n":3}\n'
    c2_bad = '{"kind":"io","n":1}\nnot-json\n{"kind":"io","n":3}\n'
    tasks = {
        "C1": {
            "kind": "coding",
            "file": "slug.py",
            "prompt": (
                "Write Python code only. Create slug.py defining slug(text) that returns a string: "
                "lowercase the input, replace any run of spaces or hyphens with a single hyphen, "
                "and strip leading and trailing hyphens. Do not print anything. No markdown fences, no explanation."
            ),
        },
        "C2": {
            "kind": "coding",
            "file": "count_errors.py",
            "prompt": (
                "Write Python code only. Create count_errors.py that reads a JSONL file path from sys.argv[1], "
                "counts how many times each object's kind field appears, and prints one JSON object whose keys "
                "are the kind values sorted alphabetically and whose values are the counts. "
                "If a line is not valid JSON, exit with a non-zero status. No markdown fences, no explanation."
            ),
            "fixture_jsonl": c2_jsonl,
            "fixture_bad": c2_bad,
        },
        "C3": {
            "kind": "coding",
            "file": "merge.py",
            "prompt": (
                "Write Python code only. Create merge.py defining merge(defaults, file_values, env) that returns a new dict. "
                "Later mappings override earlier ones: defaults first, then file_values, then env. "
                "Do not mutate any input mapping. No markdown fences, no explanation."
            ),
        },
        "C4": {
            "kind": "tool",
            "prompt": "What is the weather in Paris right now? Use get_weather.",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string", "description": "City name"},
                                "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                            },
                            "required": ["city"],
                        },
                    },
                }
            ],
        },
        "C5": {
            "kind": "tool",
            "prompt": "What is the sum of 4 and 7, and the product of 3 and 5? Use the tools.",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "add",
                        "description": "Add two numbers.",
                        "parameters": {
                            "type": "object",
                            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            "required": ["a", "b"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "multiply",
                        "description": "Multiply two numbers.",
                        "parameters": {
                            "type": "object",
                            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            "required": ["a", "b"],
                        },
                    },
                },
            ],
        },
        "C6": {
            "kind": "tool",
            "prompt": "Look up ticket T-42 and tell me its status. Use lookup_ticket.",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_ticket",
                        "description": "Look up a support ticket by id.",
                        "parameters": {
                            "type": "object",
                            "properties": {"ticket_id": {"type": "string"}},
                            "required": ["ticket_id"],
                        },
                    },
                }
            ],
        },
    }
    return tasks


def assert_no_forbidden(tasks):
    for label, t in tasks.items():
        if FORBIDDEN.search(t["prompt"]):
            raise SystemExit(f"forbidden token in {label} prompt")


def persist_step(task_dir: Path, name: str, obj):
    dump(task_dir / name, obj)


def check_c1(task_dir: Path, code: str):
    src = task_dir / "slug.py"
    write_text(src, code)
    checker = task_dir / "_check_c1.py"
    write_text(
        checker,
        "ns = {}\n"
        "exec(open('slug.py', encoding='utf-8').read(), ns)\n"
        "slug = ns['slug']\n"
        "assert slug('Hello World') == 'hello-world'\n"
        "assert slug('foo---bar   baz') == 'foo-bar-baz'\n"
        "assert slug('') == ''\n"
        "assert slug('  Hello, World!!  ') == 'hello,-world!!'\n"
        "print('C1_OK')\n",
    )
    r = run_py(checker, cwd=task_dir)
    persist_step(task_dir, "check.json", r)
    ok = (not r["timed_out"]) and r["returncode"] == 0 and "C1_OK" in (r["stdout"] or "")
    return ok, r


def check_c2(task_dir: Path, code: str, fixtures: dict):
    src = task_dir / "count_errors.py"
    write_text(src, code)
    good = task_dir / "rows.jsonl"
    bad = task_dir / "bad.jsonl"
    write_text(good, fixtures["fixture_jsonl"])
    write_text(bad, fixtures["fixture_bad"])
    r_good = run_py(src, extra_args=[str(good)], cwd=task_dir)
    r_bad = run_py(src, extra_args=[str(bad)], cwd=task_dir)
    persist_step(task_dir, "check-good.json", r_good)
    persist_step(task_dir, "check-bad.json", r_bad)
    out_ok = False
    try:
        obj = json.loads((r_good["stdout"] or "").strip())
        out_ok = obj == {"io": 2, "parse": 1}
    except Exception:
        out_ok = False
    good_ok = (not r_good["timed_out"]) and r_good["returncode"] == 0 and out_ok
    bad_ok = (not r_bad["timed_out"]) and (r_bad["returncode"] not in (0, None))
    return good_ok and bad_ok, {"good": r_good, "bad": r_bad, "parsed_ok": out_ok}


def check_c3(task_dir: Path, code: str):
    src = task_dir / "merge.py"
    write_text(src, code)
    checker = task_dir / "_check_c3.py"
    write_text(
        checker,
        "import copy\n"
        "ns = {}\n"
        "exec(open('merge.py', encoding='utf-8').read(), ns)\n"
        "merge = ns['merge']\n"
        "d = {'a': 1, 'b': 2, 'c': 3}\n"
        "f = {'b': 20, 'd': 4}\n"
        "e = {'c': 30}\n"
        "d0, f0, e0 = copy.deepcopy(d), copy.deepcopy(f), copy.deepcopy(e)\n"
        "got = merge(d, f, e)\n"
        "assert got == {'a': 1, 'b': 20, 'c': 30, 'd': 4}\n"
        "assert merge({'x': 1}, {}, {}) == {'x': 1}\n"
        "assert merge({'x': 1}, {'y': 2}, {}) == {'x': 1, 'y': 2}\n"
        "assert d == d0 and f == f0 and e == e0\n"
        "assert got is not d and got is not f and got is not e\n"
        "print('C3_OK')\n",
    )
    r = run_py(checker, cwd=task_dir)
    persist_step(task_dir, "check.json", r)
    ok = (not r["timed_out"]) and r["returncode"] == 0 and "C3_OK" in (r["stdout"] or "")
    return ok, r


def assistant_msg(resp):
    msg = dict(resp.get("message") or {})
    if not msg:
        msg = {"role": "assistant", "content": resp.get("content") or ""}
        if resp.get("tool_calls"):
            msg["tool_calls"] = resp["tool_calls"]
    msg.setdefault("role", "assistant")
    return msg


def run_c4(task_dir: Path, spec: dict, wall_acc: list):
    malformed = 0
    steps = []
    messages = [{"role": "user", "content": spec["prompt"]}]
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "tools": spec["tools"],
        "tool_choice": "auto",
        "messages": messages,
    }
    persist_step(task_dir, "req-1.json", body)
    ownership()
    r1 = post(body, REQ_TIMEOUT)
    persist_step(task_dir, "resp-1.json", r1)
    wall_acc.append(r1["total_s"])
    steps.append(r1)
    tcs = r1.get("tool_calls") or []
    names = [tool_name(tc) for tc in tcs]
    if not tcs or names.count("get_weather") != 1 or any(n != "get_weather" for n in names):
        if tcs:
            for tc in tcs:
                if tool_args(tc).get("_malformed"):
                    malformed += 1
        return False, {"reason": "need_one_get_weather", "malformed": malformed}, r1, None
    for tc in tcs:
        if tool_args(tc).get("_malformed"):
            malformed += 1
            return False, {"reason": "malformed_args", "malformed": malformed}, r1, None
    messages = messages + [assistant_msg(r1)]
    for tc in tcs:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id") or "call_weather",
                "name": "get_weather",
                "content": "18 C, clear",
            }
        )
    persist_step(task_dir, "tool-result-1.json", messages[-len(tcs) :])
    body2 = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "tools": spec["tools"],
        "tool_choice": "auto",
        "messages": messages,
    }
    persist_step(task_dir, "req-2.json", body2)
    ownership()
    r2 = post(body2, REQ_TIMEOUT)
    persist_step(task_dir, "resp-2.json", r2)
    wall_acc.append(r2["total_s"])
    text = (r2.get("content") or "").lower()
    ok = (
        r2.get("http_status") == 200
        and not r2.get("timed_out")
        and "paris" in text
        and "18" in text
        and "clear" in text
        and not (r2.get("tool_calls") or [])
    )
    return ok, {"malformed": malformed, "final": r2.get("content")}, r1, r2


def _nums(args, *keys):
    try:
        return float(args[keys[0]]), float(args[keys[1]])
    except Exception:
        return None


def run_c5(task_dir: Path, spec: dict, wall_acc: list):
    malformed = 0
    messages = [{"role": "user", "content": spec["prompt"]}]
    seen_add = seen_mul = False
    add_ok = mul_ok = False
    serial_used = False
    turns = 0
    last = None
    while turns < 3:
        turns += 1
        body = {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "tools": spec["tools"],
            "tool_choice": "auto",
            "messages": messages,
        }
        persist_step(task_dir, f"req-{turns}.json", body)
        ownership()
        resp = post(body, REQ_TIMEOUT)
        persist_step(task_dir, f"resp-{turns}.json", resp)
        wall_acc.append(resp["total_s"])
        last = resp
        tcs = resp.get("tool_calls") or []
        if not tcs:
            break
        if turns == 2:
            serial_used = True
        messages.append(assistant_msg(resp))
        tool_payloads = []
        for tc in tcs:
            name = tool_name(tc)
            args = tool_args(tc)
            if args.get("_malformed") or name not in ("add", "multiply"):
                malformed += 1
                persist_step(task_dir, "malformed.json", {"turn": turns, "tc": tc})
                return False, {"malformed": malformed, "reason": "malformed_or_unknown"}, last, serial_used
            pair = _nums(args, "a", "b")
            if pair is None:
                malformed += 1
                return False, {"malformed": malformed, "reason": "bad_args"}, last, serial_used
            a, b = pair
            if name == "add":
                seen_add = True
                add_ok = {a, b} == {4.0, 7.0} or abs(a + b - 11) < 1e-9
                content = "11"
            else:
                seen_mul = True
                mul_ok = {a, b} == {3.0, 5.0} or abs(a * b - 15) < 1e-9
                content = "15"
            tool_payloads.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{name}_{turns}",
                    "name": name,
                    "content": content,
                }
            )
        persist_step(task_dir, f"tool-result-{turns}.json", tool_payloads)
        messages.extend(tool_payloads)
        if seen_add and seen_mul:
            # one more turn for the final answer
            continue
    text = ((last or {}).get("content") or "")
    ok = (
        seen_add
        and seen_mul
        and add_ok
        and mul_ok
        and (last or {}).get("http_status") == 200
        and not (last or {}).get("timed_out")
        and "11" in text
        and "15" in text
        and not ((last or {}).get("tool_calls") or [])
    )
    return ok, {"malformed": malformed, "serial": serial_used, "final": text, "seen_add": seen_add, "seen_mul": seen_mul}, last, serial_used


def run_c6(task_dir: Path, spec: dict, wall_acc: list):
    malformed = 0
    retry = False
    messages = [{"role": "user", "content": spec["prompt"]}]
    lookup_n = 0
    last = None
    for turn in range(1, 5):
        body = {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "tools": spec["tools"],
            "tool_choice": "auto",
            "messages": messages,
        }
        persist_step(task_dir, f"req-{turn}.json", body)
        ownership()
        resp = post(body, REQ_TIMEOUT)
        persist_step(task_dir, f"resp-{turn}.json", resp)
        wall_acc.append(resp["total_s"])
        last = resp
        tcs = resp.get("tool_calls") or []
        if not tcs:
            break
        messages.append(assistant_msg(resp))
        payloads = []
        for tc in tcs:
            name = tool_name(tc)
            args = tool_args(tc)
            if name != "lookup_ticket" or args.get("_malformed"):
                malformed += 1
                return False, {"malformed": malformed, "retry": retry, "reason": "bad_call"}, last
            tid = str(args.get("ticket_id") or "")
            if tid != "T-42":
                malformed += 1
                return False, {"malformed": malformed, "retry": retry, "reason": "wrong_id", "tid": tid}, last
            lookup_n += 1
            if lookup_n == 1:
                content = "transient error: unavailable"
            else:
                retry = True
                content = "resolved"
            payloads.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_lookup_{lookup_n}",
                    "name": "lookup_ticket",
                    "content": content,
                }
            )
        persist_step(task_dir, f"tool-result-{turn}.json", payloads)
        messages.extend(payloads)
    text = ((last or {}).get("content") or "")
    low = text.lower()
    ok = (
        retry
        and lookup_n >= 2
        and (last or {}).get("http_status") == 200
        and not (last or {}).get("timed_out")
        and "T-42" in text
        and "resolved" in low
        and not ((last or {}).get("tool_calls") or [])
    )
    return ok, {"malformed": malformed, "retry": retry, "lookup_n": lookup_n, "final": text}, last


def main():
    t_wall0 = time.perf_counter()
    own_b = ownership()
    dump(HERE / "ownership-before.json", own_b)
    tasks = build_manifest()
    assert_no_forbidden(tasks)
    hashes = {}
    for label, t in tasks.items():
        hashes[label] = {
            "prompt_sha256": sha_text(t["prompt"]),
            "tools_sha256": sha_obj(t.get("tools")) if t.get("tools") else None,
            "fixture_jsonl_sha256": sha_text(t["fixture_jsonl"]) if "fixture_jsonl" in t else None,
            "fixture_bad_sha256": sha_text(t["fixture_bad"]) if "fixture_bad" in t else None,
        }
    dump(HERE / "tasks.json", {k: {kk: vv for kk, vv in t.items() if kk != "tools"} | ({"tools": t["tools"]} if "tools" in t else {}) for k, t in tasks.items()})
    workload_hash = sha_obj({"tasks": {k: hashes[k] | {"prompt": tasks[k]["prompt"]} for k in tasks}, "model": MODEL, "endpoint": ENDPOINT})
    dump(
        HERE / "manifest.json",
        {
            "labels": list(tasks),
            "endpoint": ENDPOINT,
            "model": MODEL,
            "run_id_expected": RUN_ID,
            "temperature": 0,
            "request_timeout_s": REQ_TIMEOUT,
            "check_timeout_s": CHECK_TIMEOUT,
            "hashes": hashes,
            "workload_hash_sha256": workload_hash,
            "frozen_utc": utc(),
        },
    )

    results = {}
    coding_pass = 0
    tool_pass = 0
    malformed_total = 0
    retry_evidence = {}
    latencies = {}
    tokens = {}
    stopped = False

    for label in ("C1", "C2", "C3", "C4", "C5", "C6"):
        if stopped:
            break
        try:
            ownership()
        except SystemExit:
            stopped = True
            results[label] = {"passed": False, "reason": "OWNERSHIP_CHANGED"}
            break
        spec = tasks[label]
        task_dir = HERE / label
        task_dir.mkdir(parents=True, exist_ok=True)
        wall = []
        if spec["kind"] == "coding":
            body = {
                "model": MODEL,
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": spec["prompt"]}],
            }
            persist_step(task_dir, "req.json", body)
            resp = post(body, REQ_TIMEOUT)
            persist_step(task_dir, "resp.json", resp)
            write_text(task_dir / "model-text.txt", resp.get("content") or "")
            wall.append(resp["total_s"])
            code = strip_fence(resp.get("content") or "")
            write_text(task_dir / spec["file"], code)
            if label == "C1":
                ok, detail = check_c1(task_dir, code)
            elif label == "C2":
                ok, detail = check_c2(task_dir, code, spec)
            else:
                ok, detail = check_c3(task_dir, code)
            if ok:
                coding_pass += 1
            latencies[label] = sum(wall)
            tokens[label] = {
                "prompt_tokens": resp.get("prompt_tokens"),
                "completion_tokens": resp.get("completion_tokens"),
            }
            rec = {
                "passed": ok,
                "http_status": resp.get("http_status"),
                "timed_out": resp.get("timed_out"),
                "latency_s": latencies[label],
                "tokens": tokens[label],
                "detail": detail if isinstance(detail, dict) else {"check": detail},
            }
            persist_step(task_dir, "result.json", rec)
            results[label] = rec
        else:
            if label == "C4":
                ok, meta, *_ = run_c4(task_dir, spec, wall)
            elif label == "C5":
                ok, meta, *rest = run_c5(task_dir, spec, wall)
                retry_evidence["C5_serial"] = bool(meta.get("serial"))
            else:
                ok, meta, *_ = run_c6(task_dir, spec, wall)
                retry_evidence["C6"] = bool(meta.get("retry"))
            malformed_total += int(meta.get("malformed") or 0)
            if ok:
                tool_pass += 1
            latencies[label] = sum(wall)
            # tokens from last persisted resp files summed below
            tok_p = tok_c = 0
            for p in sorted(task_dir.glob("resp-*.json")):
                o = json.loads(p.read_text())
                tok_p += o.get("prompt_tokens") or 0
                tok_c += o.get("completion_tokens") or 0
            tokens[label] = {"prompt_tokens": tok_p, "completion_tokens": tok_c}
            rec = {
                "passed": ok,
                "latency_s": latencies[label],
                "tokens": tokens[label],
                "meta": meta,
            }
            persist_step(task_dir, "result.json", rec)
            results[label] = rec

    try:
        own_a = ownership()
    except SystemExit:
        own_a = {"status": "OWNERSHIP_CHANGED"}
    dump(HERE / "ownership-after.json", own_a)

    n_ok = coding_pass + tool_pass
    if n_ok == 6:
        verdict = "PASS_6_OF_6"
    elif n_ok == 0:
        verdict = "FAIL"
    else:
        verdict = "PARTIAL"
    summary = {
        "verdict": verdict,
        "coding_passed": coding_pass,
        "coding_of": 3,
        "tool_passed": tool_pass,
        "tool_of": 3,
        "per_task_latency_s": latencies,
        "per_task_tokens": tokens,
        "malformed_call_count": malformed_total,
        "retry_evidence": retry_evidence,
        "total_wall_s": time.perf_counter() - t_wall0,
        "workload_hash": workload_hash,
        "results": {k: {"passed": v.get("passed"), "latency_s": v.get("latency_s")} for k, v in results.items()},
        "model": MODEL,
        "endpoint": ENDPOINT,
        "resolved_model": "cli-proxy/dddai.grok-4.6.implementer",
        "fallback_state": "none",
        "ownership_before": {"listener_pid": own_b.get("listener_pid"), "lane_pid": own_b.get("lane_pid")},
        "ownership_after": {"listener_pid": own_a.get("listener_pid"), "lane_pid": own_a.get("lane_pid"), "status": own_a.get("status")},
        "stopped_on_ownership_change": own_a.get("status") == "OWNERSHIP_CHANGED",
        "utc": utc(),
    }
    dump(HERE / "summary.json", summary)
    print(json.dumps({"verdict": verdict, "coding": coding_pass, "tool": tool_pass, "malformed": malformed_total}))
    return 0 if verdict == "PASS_6_OF_6" else 1


if __name__ == "__main__":
    sys.exit(main())
