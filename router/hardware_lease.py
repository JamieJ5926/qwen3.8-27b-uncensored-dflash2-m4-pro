"""Host-wide hardware lease. Offline lock only; never starts or calls models."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable, Mapping

DEFAULT_LEASE_PATH = Path("/var/tmp/local-model-hardware-lease")
_RECORD_NAME = "record.json"
_IDENTITY_FIELDS = ("model", "port", "model_path", "run_id", "action", "cleanup")


@dataclass(frozen=True)
class HardwareLeaseRecord:
    owner: str
    pid: int
    model: str
    port: int
    model_path: str
    run_id: str
    action: str
    started_at: str
    cleanup: str
    lease_token: str = ""


class HardwareLeaseError(Exception):
    """Lease failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LeaseContentionError(HardwareLeaseError):
    def __init__(self, message: str) -> None:
        super().__init__("lease_contention", message)


class StaleLeaseError(HardwareLeaseError):
    def __init__(self, message: str) -> None:
        super().__init__("stale_lease", message)


class LeaseOwnerError(HardwareLeaseError):
    def __init__(self, message: str) -> None:
        super().__init__("lease_owner_mismatch", message)


class MalformedLeaseError(HardwareLeaseError):
    def __init__(self, message: str) -> None:
        super().__init__("malformed_lease", message)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def inspect(path: Path | str) -> HardwareLeaseRecord | None:
    lease_dir = Path(path)
    if not lease_dir.exists():
        return None
    record_path = lease_dir / _RECORD_NAME
    if not record_path.is_file():
        raise MalformedLeaseError(f"lease directory missing record: {record_path}")
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedLeaseError(f"lease record is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise MalformedLeaseError("lease record must be a JSON object")
    try:
        return _record_from_mapping(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedLeaseError(f"lease record is malformed: {exc}") from exc


def acquire(
    path: Path | str,
    requested_record: HardwareLeaseRecord,
    pid_alive: Callable[[int], bool] = pid_alive,
) -> HardwareLeaseRecord:
    lease_dir = Path(path)
    try:
        os.mkdir(lease_dir, 0o700)
    except FileExistsError:
        existing = inspect(lease_dir)
        if existing is None:
            raise MalformedLeaseError(f"lease path exists but is not a lease: {lease_dir}")
        if existing.owner == requested_record.owner and existing.pid == requested_record.pid:
            if not _identity_matches(existing, requested_record):
                raise LeaseContentionError(
                    "same owner and pid requested a different lease identity"
                )
            return existing
        if pid_alive(existing.pid):
            raise LeaseContentionError(
                f"hardware lease held by {existing.owner} pid {existing.pid}"
            )
        raise StaleLeaseError(
            f"stale hardware lease from {existing.owner} pid {existing.pid}; recover explicitly"
        )
    except FileNotFoundError:
        lease_dir.parent.mkdir(parents=True, exist_ok=True)
        return acquire(lease_dir, requested_record, pid_alive=pid_alive)
    held = replace(requested_record, lease_token=secrets.token_hex(16))
    try:
        _write_record(lease_dir, held)
    except Exception:
        _remove_lease_dir(lease_dir)
        raise
    return held


def recover_stale(
    path: Path | str,
    expected_snapshot: HardwareLeaseRecord,
    pid_alive: Callable[[int], bool] = pid_alive,
) -> None:
    existing = inspect(path)
    if existing is None:
        return
    if existing != expected_snapshot:
        raise LeaseContentionError("stale recovery snapshot does not match the recorded lease")
    if pid_alive(existing.pid):
        raise LeaseContentionError(
            f"refusing to recover live lease pid {existing.pid}"
        )
    _remove_lease_dir(Path(path))


def release(path: Path | str, owner: str, pid: int, lease_token: str) -> None:
    existing = inspect(path)
    if existing is None:
        return
    if (
        existing.owner != owner
        or existing.pid != pid
        or existing.lease_token != lease_token
        or not lease_token
    ):
        raise LeaseOwnerError("release requires matching owner, pid, and lease_token")
    _remove_lease_dir(Path(path))


def _identity_matches(existing: HardwareLeaseRecord, requested: HardwareLeaseRecord) -> bool:
    return all(getattr(existing, name) == getattr(requested, name) for name in _IDENTITY_FIELDS)


def _record_from_mapping(raw: Mapping[str, object]) -> HardwareLeaseRecord:
    names = {f.name for f in fields(HardwareLeaseRecord)}
    extra = set(raw) - names
    if extra:
        raise ValueError(f"unknown fields: {sorted(extra)}")
    missing = names - set(raw)
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")
    token = str(raw["lease_token"])
    if not token:
        raise ValueError("lease_token is empty")
    return HardwareLeaseRecord(
        owner=str(raw["owner"]),
        pid=int(raw["pid"]),
        model=str(raw["model"]),
        port=int(raw["port"]),
        model_path=str(raw["model_path"]),
        run_id=str(raw["run_id"]),
        action=str(raw["action"]),
        started_at=str(raw["started_at"]),
        cleanup=str(raw["cleanup"]),
        lease_token=token,
    )


def _write_record(lease_dir: Path, record: HardwareLeaseRecord) -> None:
    payload = json.dumps(asdict(record), sort_keys=True, indent=2) + "\n"
    tmp_path = lease_dir / f".{_RECORD_NAME}.tmp"
    final_path = lease_dir / _RECORD_NAME
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
        dir_fd = os.open(lease_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_lease_dir(lease_dir: Path) -> None:
    record_path = lease_dir / _RECORD_NAME
    tmp_path = lease_dir / f".{_RECORD_NAME}.tmp"
    try:
        record_path.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)
        lease_dir.rmdir()
    except OSError as exc:
        raise HardwareLeaseError("lease_release_failed", f"could not remove lease: {exc}") from exc
