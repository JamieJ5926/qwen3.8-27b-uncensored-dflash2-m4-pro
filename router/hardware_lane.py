"""Hardware-lane command runner. Offline lock + probes; never starts models."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from router.hardware_lease import (
    DEFAULT_LEASE_PATH,
    HardwareLeaseError,
    HardwareLeaseRecord,
    acquire,
    release,
)


_NON_GENERATION = re.compile(r"(embed|embedding|rerank|reranker)", re.IGNORECASE)
DEFAULT_FORBIDDEN_GENERATION_PATTERNS: tuple[str, ...] = (
    r"dflash_server\.py",
    r"llama-server",
)


def resolve_forbidden_generation_patterns(
    supplied: Sequence[str] | None,
    *,
    replace: bool = False,
) -> tuple[str, ...]:
    extra = tuple(supplied or ())
    if replace:
        return extra
    seen: dict[str, None] = {}
    for pattern in (*DEFAULT_FORBIDDEN_GENERATION_PATTERNS, *extra):
        seen.setdefault(pattern, None)
    return tuple(seen)


class HardwareLaneError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PreflightConflictError(HardwareLaneError):
    def __init__(self, message: str) -> None:
        super().__init__("preflight_conflict", message)


@dataclass(frozen=True)
class HardwareRunSpec:
    owner: str
    model: str
    port: int
    model_path: str
    run_id: str
    action: str
    cleanup: str
    adopted_pid: int | None
    allowed_generation_ports: tuple[int, ...]
    forbidden_generation_patterns: tuple[str, ...]
    artifact_path: str
    command_argv: tuple[str, ...]
    replace_forbidden_generation_patterns: bool = False


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    command: str


@dataclass(frozen=True)
class ListenerObservation:
    pid: int
    port: int


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    conflicts: tuple[str, ...]
    processes: tuple[ProcessObservation, ...]
    listeners: tuple[ListenerObservation, ...]
    unknown_listeners: tuple[ListenerObservation, ...]


def is_generation_conflict_name(command: str) -> bool:
    return not bool(_NON_GENERATION.search(command))


def observe_processes(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[ProcessObservation, ...]:
    runner = run or subprocess.run
    try:
        completed = runner(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise HardwareLaneError("discovery_failed", f"ps not found: {exc}") from exc
    if completed.returncode != 0:
        raise HardwareLaneError(
            "discovery_failed",
            f"ps exited {completed.returncode}: {(completed.stderr or '').strip()}",
        )
    stdout = completed.stdout or ""
    if not stdout.strip():
        raise HardwareLaneError("discovery_failed", "ps produced no usable output")
    observed: list[ProcessObservation] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_s, _, command = stripped.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        observed.append(ProcessObservation(pid=pid, command=command.strip()))
    if not observed:
        raise HardwareLaneError("discovery_failed", "ps produced no usable process rows")
    return tuple(observed)


def observe_listeners(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[ListenerObservation, ...]:
    runner = run or subprocess.run
    try:
        completed = runner(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise HardwareLaneError("discovery_failed", f"lsof not found: {exc}") from exc
    if completed.returncode != 0:
        raise HardwareLaneError(
            "discovery_failed",
            f"lsof exited {completed.returncode}: {(completed.stderr or '').strip()}",
        )
    stdout = completed.stdout or ""
    if not stdout.strip():
        raise HardwareLaneError("discovery_failed", "lsof produced no usable output")
    seen: set[tuple[int, int]] = set()
    listeners: list[ListenerObservation] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        if parts[0] == "COMMAND":
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        match = re.search(r":(\d+)$", parts[8])
        if not match:
            continue
        port = int(match.group(1))
        key = (pid, port)
        if key in seen:
            continue
        seen.add(key)
        listeners.append(ListenerObservation(pid=pid, port=port))
    return tuple(listeners)


def _is_forbidden_generation_process(
    spec: HardwareRunSpec,
    proc: ProcessObservation,
    patterns: Sequence[re.Pattern[str]],
) -> bool:
    if spec.adopted_pid is not None and proc.pid == spec.adopted_pid:
        return False
    if not is_generation_conflict_name(proc.command):
        return False
    return any(pat.search(proc.command) for pat in patterns)


def preflight(
    spec: HardwareRunSpec,
    *,
    processes: Sequence[ProcessObservation] | None = None,
    listeners: Sequence[ListenerObservation] | None = None,
    observe_procs: Callable[[], Sequence[ProcessObservation]] = observe_processes,
    observe_ports: Callable[[], Sequence[ListenerObservation]] = observe_listeners,
) -> PreflightResult:
    procs = tuple(processes if processes is not None else observe_procs())
    observed_listeners = tuple(listeners if listeners is not None else observe_ports())
    allowed_ports = set(spec.allowed_generation_ports)
    allowed_ports.add(spec.port)
    by_pid = {proc.pid: proc for proc in procs}
    patterns = [
        re.compile(p)
        for p in resolve_forbidden_generation_patterns(
            spec.forbidden_generation_patterns,
            replace=spec.replace_forbidden_generation_patterns,
        )
    ]
    conflicts: list[str] = []
    unknown: list[ListenerObservation] = []
    for listener in observed_listeners:
        owner = by_pid.get(listener.pid)
        if owner is None:
            unknown.append(listener)
            continue
        if listener.port in allowed_ports:
            continue
        if spec.adopted_pid is not None and listener.pid == spec.adopted_pid:
            continue
        if _is_forbidden_generation_process(spec, owner, patterns):
            conflicts.append(
                f"forbidden generation listener pid={listener.pid} port={listener.port}"
            )
    for proc in procs:
        if _is_forbidden_generation_process(spec, proc, patterns):
            conflicts.append(f"forbidden generation process pid={proc.pid} {proc.command}")
    return PreflightResult(
        ok=not conflicts,
        conflicts=tuple(conflicts),
        processes=procs,
        listeners=observed_listeners,
        unknown_listeners=tuple(unknown),
    )



def _write_artifact(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def planned_action(spec: HardwareRunSpec, mode: str) -> dict[str, object]:
    return {
        "mode": mode,
        "owner": spec.owner,
        "model": spec.model,
        "port": spec.port,
        "model_path": spec.model_path,
        "run_id": spec.run_id,
        "action": spec.action,
        "cleanup": spec.cleanup,
        "adopted_pid": spec.adopted_pid,
        "command_argv": list(spec.command_argv),
        "artifact_path": spec.artifact_path,
    }


def run_lane(
    spec: HardwareRunSpec,
    *,
    mode: str,
    lease_path: Path | str = DEFAULT_LEASE_PATH,
    pid: int | None = None,
    timeout_s: float | None = None,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    acquire_lease: Callable[..., HardwareLeaseRecord] = acquire,
    release_lease: Callable[..., None] = release,
    processes: Sequence[ProcessObservation] | None = None,
    listeners: Sequence[ListenerObservation] | None = None,
    observe_procs: Callable[[], Sequence[ProcessObservation]] = observe_processes,
    observe_listeners: Callable[[], Sequence[ListenerObservation]] = observe_listeners,
    write_artifact: Callable[[Path, Mapping[str, object]], None] = _write_artifact,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if mode not in {"observe", "run", "dry-run"}:
        raise HardwareLaneError("invalid_mode", f"unknown mode {mode}")
    plan = planned_action(spec, mode)
    if mode == "dry-run":
        return {"status": "dry-run", "plan": plan, "acquired": False}

    owner_pid = os.getpid() if pid is None else pid
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    requested = HardwareLeaseRecord(
        owner=spec.owner,
        pid=owner_pid,
        model=spec.model,
        port=spec.port,
        model_path=spec.model_path,
        run_id=spec.run_id,
        action=spec.action,
        started_at=started_at,
        cleanup=spec.cleanup,
    )
    held: HardwareLeaseRecord | None = None
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    timed_out = False
    elapsed = 0.0
    preflight_result: PreflightResult | None = None
    status = "ok"
    error: dict[str, str] | None = None
    try:
        held = acquire_lease(lease_path, requested)
        preflight_result = preflight(
            spec,
            processes=processes,
            listeners=listeners,
            observe_procs=observe_procs,
            observe_ports=observe_listeners,
        )
        if mode == "run" and spec.command_argv:
            if not preflight_result.ok:
                status = "preflight_conflict"
                error = {
                    "code": "preflight_conflict",
                    "message": "; ".join(preflight_result.conflicts),
                }
                raise PreflightConflictError(error["message"])
            started = clock()
            try:
                completed = subprocess_run(
                    list(spec.command_argv),
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,
                    timeout=timeout_s,
                )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                exit_code = int(completed.returncode)
                if exit_code != 0:
                    status = "command_failed"
                    error = {
                        "code": "command_failed",
                        "message": f"command exited with status {exit_code}",
                    }
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                status = "timeout"
                stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr or "" if isinstance(exc.stderr, str) else ""
                exit_code = None
                error = {"code": "timeout", "message": str(exc)}
            elapsed = clock() - started
        elif mode == "observe" or not spec.command_argv:
            status = "observe"
    except PreflightConflictError:
        pass
    except HardwareLaneError as exc:
        status = exc.code
        error = {"code": exc.code, "message": exc.message}
    finally:
        artifact = {
            "status": status,
            "mode": mode,
            "plan": plan,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "elapsed_s": elapsed,
            "timed_out": timed_out,
            "preflight": None
            if preflight_result is None
            else {
                "ok": preflight_result.ok,
                "conflicts": list(preflight_result.conflicts),
                "listeners": [asdict(item) for item in preflight_result.listeners],
                "unknown_listeners": [
                    asdict(item) for item in preflight_result.unknown_listeners
                ],
                "processes": [asdict(p) for p in preflight_result.processes],
            },
            "error": error,
        }
        try:
            write_artifact(Path(spec.artifact_path), artifact)
        except OSError as exc:
            status = "artifact_error"
            error = {"code": "artifact_error", "message": str(exc)}
            artifact["status"] = status
            artifact["error"] = error
        finally:
            if held is not None:
                release_lease(lease_path, held.owner, held.pid, held.lease_token)
    return artifact



def _parse_argv(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m router.hardware_lane")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--cleanup", default="")
    parser.add_argument("--adopted-pid", type=int, default=None)
    parser.add_argument("--allowed-generation-port", type=int, action="append", default=[])
    parser.add_argument("--forbidden-generation-pattern", action="append", default=[])
    parser.add_argument(
        "--replace-forbidden-generation-patterns",
        action="store_true",
        default=False,
    )
    parser.add_argument("--artifact-path", required=True)
    parser.add_argument("--lease-path", default=str(DEFAULT_LEASE_PATH))
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(list(argv) if argv is not None else None)


def spec_from_namespace(ns: argparse.Namespace) -> HardwareRunSpec:
    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    return HardwareRunSpec(
        owner=ns.owner,
        model=ns.model,
        port=ns.port,
        model_path=ns.model_path,
        run_id=ns.run_id,
        action=ns.action,
        cleanup=ns.cleanup,
        adopted_pid=ns.adopted_pid,
        allowed_generation_ports=tuple(ns.allowed_generation_port),
        forbidden_generation_patterns=resolve_forbidden_generation_patterns(
            ns.forbidden_generation_pattern,
            replace=ns.replace_forbidden_generation_patterns,
        ),
        replace_forbidden_generation_patterns=ns.replace_forbidden_generation_patterns,
        artifact_path=ns.artifact_path,
        command_argv=tuple(command),
    )



def _cli_exit_code(mode: str, result: Mapping[str, object]) -> int:
    if mode == "dry-run":
        return 0
    if result["status"] in {
        "preflight_conflict",
        "timeout",
        "command_failed",
        "error",
    }:
        return 2
    if result.get("error"):
        return 2
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    ns = _parse_argv(argv)
    spec = spec_from_namespace(ns)
    mode = "run"
    if ns.dry_run:
        mode = "dry-run"
    elif ns.observe:
        mode = "observe"
    try:
        result = run_lane(
            spec,
            mode=mode,
            lease_path=Path(ns.lease_path),
            timeout_s=ns.timeout,
        )
    except HardwareLaneError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}))
        return 2
    except HardwareLeaseError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}))
        return 2
    except OSError as exc:
        print(json.dumps({"status": "error", "code": "artifact_error", "message": str(exc)}))
        return 2
    if mode == "dry-run":
        print(json.dumps(result, indent=2, sort_keys=True))
        return _cli_exit_code(mode, result)
    print(json.dumps({"status": result["status"], "artifact_path": spec.artifact_path}))
    return _cli_exit_code(mode, result)


if __name__ == "__main__":
    raise SystemExit(main())
