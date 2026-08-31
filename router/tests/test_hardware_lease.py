"""Offline hardware-lease coverage. Do not start, stop, or call models."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from router.hardware_lease import (
    DEFAULT_LEASE_PATH,
    HardwareLeaseRecord,
    LeaseContentionError,
    LeaseOwnerError,
    MalformedLeaseError,
    StaleLeaseError,
    acquire,
    inspect,
    recover_stale,
    release,
)


def _record(**overrides: object) -> HardwareLeaseRecord:
    base = dict(
        owner="bench-owner",
        pid=4242,
        model="qwen",
        port=8080,
        model_path="/models/qwen.gguf",
        run_id="run-1",
        action="benchmark",
        started_at="2026-08-31T00:00:00Z",
        cleanup="stop-server",
    )
    base.update(overrides)
    return HardwareLeaseRecord(**base)  # type: ignore[arg-type]


class HardwareLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = Path(self.tmp.name) / "hardware-lease"
        self.alive: dict[int, bool] = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _alive(self, pid: int) -> bool:
        return self.alive.get(pid, False)

    def test_default_path_is_host_wide(self) -> None:
        self.assertTrue(DEFAULT_LEASE_PATH.is_absolute())
        self.assertIn("hardware-lease", str(DEFAULT_LEASE_PATH))

    def test_acquire_persists_record(self) -> None:
        held = acquire(self.lease, _record(), pid_alive=self._alive)
        loaded = inspect(self.lease)
        self.assertEqual(held, loaded)
        self.assertEqual(loaded.owner, "bench-owner")
        self.assertEqual(loaded.action, "benchmark")
        self.assertTrue(loaded.lease_token)
        self.assertTrue((self.lease / "record.json").is_file())

    def test_contention_live_pid(self) -> None:
        first = _record(owner="a", pid=11)
        self.alive[11] = True
        held = acquire(self.lease, first, pid_alive=self._alive)
        with self.assertRaises(LeaseContentionError):
            acquire(self.lease, _record(owner="b", pid=22), pid_alive=self._alive)
        self.assertEqual(inspect(self.lease), held)

    def test_stale_recovery_then_acquire(self) -> None:
        stale = _record(owner="dead", pid=99)
        self.alive[99] = False
        held_stale = acquire(self.lease, stale, pid_alive=self._alive)
        with self.assertRaises(StaleLeaseError):
            acquire(self.lease, _record(owner="live", pid=100), pid_alive=self._alive)
        recover_stale(self.lease, held_stale, pid_alive=self._alive)
        held = acquire(self.lease, _record(owner="live", pid=100), pid_alive=self._alive)
        self.assertEqual(held.owner, "live")

    def test_never_auto_recover_live_pid(self) -> None:
        live = _record(owner="keep", pid=7)
        self.alive[7] = True
        held = acquire(self.lease, live, pid_alive=self._alive)
        with self.assertRaises(LeaseContentionError):
            recover_stale(self.lease, held, pid_alive=self._alive)
        self.assertEqual(inspect(self.lease), held)

    def test_wrong_owner_release(self) -> None:
        held = acquire(self.lease, _record(owner="real", pid=3), pid_alive=self._alive)
        with self.assertRaises(LeaseOwnerError):
            release(self.lease, "other", held.pid, held.lease_token)
        self.assertIsNotNone(inspect(self.lease))

    def test_same_name_different_pid_release(self) -> None:
        held = acquire(self.lease, _record(owner="real", pid=3), pid_alive=self._alive)
        with self.assertRaises(LeaseOwnerError):
            release(self.lease, "real", 9999, held.lease_token)
        self.assertIsNotNone(inspect(self.lease))

    def test_wrong_token_release(self) -> None:
        held = acquire(self.lease, _record(owner="real", pid=3), pid_alive=self._alive)
        with self.assertRaises(LeaseOwnerError):
            release(self.lease, held.owner, held.pid, "0" * 32)
        self.assertIsNotNone(inspect(self.lease))

    def test_successful_release(self) -> None:
        held = acquire(self.lease, _record(owner="real", pid=3), pid_alive=self._alive)
        release(self.lease, held.owner, held.pid, held.lease_token)
        self.assertIsNone(inspect(self.lease))
        self.assertFalse(self.lease.exists())

    def test_malformed_record(self) -> None:
        os.mkdir(self.lease)
        (self.lease / "record.json").write_text("{not-json", encoding="utf-8")
        with self.assertRaises(MalformedLeaseError):
            inspect(self.lease)
        empty = Path(self.tmp.name) / "empty-lease-dir"
        os.mkdir(empty)
        with self.assertRaises(MalformedLeaseError):
            inspect(empty)
        broken = Path(self.tmp.name) / "broken-fields"
        os.mkdir(broken)
        (broken / "record.json").write_text(json.dumps({"owner": "x"}), encoding="utf-8")
        with self.assertRaises(MalformedLeaseError):
            inspect(broken)

    def test_idempotent_same_owner_inspection(self) -> None:
        record = _record(owner="same", pid=5)
        self.alive[5] = True
        first = acquire(self.lease, record, pid_alive=self._alive)
        second = acquire(self.lease, record, pid_alive=self._alive)
        self.assertEqual(first, second)
        self.assertEqual(inspect(self.lease), first)
        self.assertTrue(first.lease_token)

    def test_same_owner_pid_mismatched_record_acquisition(self) -> None:
        first = acquire(self.lease, _record(owner="same", pid=5, model="qwen"), pid_alive=self._alive)
        with self.assertRaises(LeaseContentionError):
            acquire(
                self.lease,
                _record(owner="same", pid=5, model="lfm"),
                pid_alive=self._alive,
            )
        self.assertEqual(inspect(self.lease), first)

    def test_lease_directory_and_record_permissions(self) -> None:
        acquire(self.lease, _record(), pid_alive=self._alive)
        dir_mode = self.lease.stat().st_mode & 0o777
        record_mode = (self.lease / "record.json").stat().st_mode & 0o777
        self.assertEqual(dir_mode, 0o700)
        self.assertEqual(record_mode, 0o600)



if __name__ == "__main__":
    unittest.main()
