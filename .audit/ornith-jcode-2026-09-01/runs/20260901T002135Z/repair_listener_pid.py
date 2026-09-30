#!/usr/bin/env python3
"""Offline backfill of null listener_pid from each artifact's saved lsof text."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEED = HERE / "private-speed-v2"
CAP = HERE / "capability"


def parse_listener_pid(lsof_text: str | None):
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


def evidence_hash(lsof_text: str) -> str:
    return hashlib.sha256(lsof_text.encode()).hexdigest()


def load(p: Path):
    return json.loads(p.read_text())


def dump(p: Path, o):
    p.write_text(json.dumps(o, indent=2) + "\n")


def pointer_set(obj, pointer: str, value):
    parts = [p for p in pointer.split("/") if p]
    cur = obj
    for p in parts[:-1]:
        cur = cur[int(p)] if p.isdigit() else cur[p]
    last = parts[-1]
    cur[int(last) if last.isdigit() else last] = value


def walk_null_listener(obj, prefix=""):
    found = []
    if isinstance(obj, dict):
        if "listener_pid" in obj:
            found.append((f"{prefix}/listener_pid", obj.get("listener_pid"), obj.get("lsof")))
        for k, v in obj.items():
            found.extend(walk_null_listener(v, f"{prefix}/{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(walk_null_listener(v, f"{prefix}/{i}"))
    return found


def adjacent_lsof(path: Path) -> str | None:
    if path.parent.name == "records":
        before = load(SPEED / "ownership-before.json")
        after = load(SPEED / "ownership-after.json")
        return before.get("lsof") or after.get("lsof")
    if path == CAP / "summary.json":
        before = load(CAP / "ownership-before.json")
        after = load(CAP / "ownership-after.json")
        return before.get("lsof") or after.get("lsof")
    return None


def targets():
    files = [
        SPEED / "ownership-before.json",
        SPEED / "ownership-after.json",
        SPEED / "summary.json",
        CAP / "ownership-before.json",
        CAP / "ownership-after.json",
        CAP / "summary.json",
    ]
    files.extend(sorted((SPEED / "records").glob("*-rep*.json")))
    return files


def main():
    repairs = []
    for path in targets():
        obj = load(path)
        for pointer, old, embedded in walk_null_listener(obj):
            if old is not None:
                continue
            lsof = embedded if isinstance(embedded, str) and embedded else adjacent_lsof(path)
            if not lsof:
                raise SystemExit(f"no lsof for {path} {pointer}")
            pid = parse_listener_pid(lsof)
            if pid is None:
                raise SystemExit(f"unparsed lsof for {path} {pointer}")
            pointer_set(obj, pointer, pid)
            repairs.append(
                {
                    "file": str(path.relative_to(HERE)),
                    "json_pointer": pointer,
                    "old": old,
                    "new": pid,
                    "lsof_evidence_hash": evidence_hash(lsof),
                }
            )
        if any(r["file"] == str(path.relative_to(HERE)) for r in repairs):
            dump(path, obj)
    manifest = {"repaired_field_count": len(repairs), "repairs": repairs}
    dump(HERE / "listener-pid-repair-manifest.json", manifest)
    print(json.dumps({"repaired_field_count": len(repairs), "pid_set": sorted({r["new"] for r in repairs})}))


if __name__ == "__main__":
    main()
