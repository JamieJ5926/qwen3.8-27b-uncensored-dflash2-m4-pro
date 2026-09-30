"""Offline hardware-lane coverage. Do not start, stop, or call models."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from router.hardware_lane import (
    DEFAULT_FORBIDDEN_GENERATION_PATTERNS,
    HardwareRunSpec,
    ListenerObservation,
    ProcessObservation,
    _cli_exit_code,
    is_generation_conflict_name,
    main,
    observe_listeners,
    observe_processes,
    preflight,
    resolve_forbidden_generation_patterns,
    run_lane,
)
from router.hardware_lease import inspect


def _spec(**overrides: object) -> HardwareRunSpec:
    base: dict[str, object] = dict(
        owner="lane-owner",
        model="qwen",
        port=8080,
        model_path="/models/qwen.gguf",
        run_id="run-1",
        action="observe",
        cleanup="",
        adopted_pid=None,
        allowed_generation_ports=(),
        forbidden_generation_patterns=("llama-server", "qwen"),
        artifact_path="",
        command_argv=("echo", "ok"),
    )
    base.update(overrides)
    return HardwareRunSpec(**base)  # type: ignore[arg-type]


class HardwareLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = Path(self.tmp.name) / "hardware-lease"
        self.artifact = Path(self.tmp.name) / "artifact.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_does_not_lock_or_execute(self) -> None:
        calls: list[object] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            raise AssertionError("dry-run must not execute")

        result = run_lane(
            _spec(artifact_path=str(self.artifact)),
            mode="dry-run",
            lease_path=self.lease,
            subprocess_run=fake_run,
        )
        self.assertEqual(result["status"], "dry-run")
        self.assertFalse(result["acquired"])
        self.assertIsNone(inspect(self.lease))
        self.assertFalse(self.lease.exists())
        self.assertEqual(calls, [])
        self.assertFalse(self.artifact.exists())

    def test_acquire_around_execution_and_release(self) -> None:
        events: list[str] = []
        released: list[tuple[object, ...]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            events.append("run")
            self.assertIsNotNone(inspect(self.lease))
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(argv, ["true"])
            return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="")

        def fake_release(path: object, owner: str, pid: int, token: str) -> None:
            released.append((path, owner, pid, token))
            from router.hardware_lease import release as real_release

            real_release(path, owner, pid, token)

        result = run_lane(
            _spec(
                artifact_path=str(self.artifact),
                command_argv=("true",),
                forbidden_generation_patterns=(),
            ),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            release_lease=fake_release,
            processes=(),
            listeners=(),
        )
        self.assertEqual(events, ["run"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "out")
        self.assertNotIn("lease_token", result)
        self.assertIsNone(inspect(self.lease))
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["exit_code"], 0)
        self.assertNotIn("lease_token", payload)
        self.assertEqual(len(released), 1)
        self.assertTrue(released[0][3])
        self.assertEqual(released[0][1], "lane-owner")

    def test_release_on_command_failure(self) -> None:
        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 7, stdout="", stderr="boom")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("false",), forbidden_generation_patterns=()),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(),
            listeners=(),
        )
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["status"], "command_failed")
        self.assertEqual(result["error"]["code"], "command_failed")
        self.assertIsNone(inspect(self.lease))
        self.assertTrue(self.artifact.is_file())
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "command_failed")
        self.assertEqual(payload["error"]["code"], "command_failed")

    def test_release_on_timeout(self) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=0.01, output="partial")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("sleep", "9"), forbidden_generation_patterns=()),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(),
            listeners=(),
            timeout_s=0.01,
        )
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(inspect(self.lease))
        self.assertTrue(self.artifact.is_file())

    def test_never_uses_shell(self) -> None:
        seen: dict[str, object] = {}

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["argv"] = argv
            seen["shell"] = kwargs.get("shell")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("printf", "a"), forbidden_generation_patterns=()),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(),
            listeners=(),
        )
        self.assertEqual(seen["argv"], ["printf", "a"])
        self.assertIs(seen["shell"], False)

    def test_conflict_rejects_command(self) -> None:
        ran = {"n": 0}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["x"], 0, stdout="", stderr="")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(ProcessObservation(pid=99, command="llama-server --port 9090"),),
            listeners=(ListenerObservation(pid=99, port=9090),),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "preflight_conflict")
        self.assertIsNone(inspect(self.lease))
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertFalse(payload["preflight"]["ok"])
        self.assertEqual(payload["preflight"]["listeners"], [{"pid": 99, "port": 9090}])

    def test_unrelated_listener_does_not_conflict(self) -> None:
        pf = preflight(
            _spec(artifact_path=str(self.artifact)),
            processes=(ProcessObservation(pid=10, command="postgres"),),
            listeners=(ListenerObservation(pid=10, port=5432),),
        )
        self.assertTrue(pf.ok)
        self.assertEqual(pf.conflicts, ())
        self.assertEqual(pf.unknown_listeners, ())

    def test_forbidden_generation_listener_conflicts_and_skips_command(self) -> None:
        ran = {"n": 0}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["x"], 0, stdout="", stderr="")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(ProcessObservation(pid=99, command="llama-server --port 9090"),),
            listeners=(ListenerObservation(pid=99, port=9090),),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "preflight_conflict")
        self.assertTrue(
            any("forbidden generation listener pid=99 port=9090" in c for c in result["preflight"]["conflicts"])
        )

    def test_listener_correlates_by_pid_not_port_guess(self) -> None:
        pf = preflight(
            _spec(artifact_path=str(self.artifact)),
            processes=(
                ProcessObservation(pid=99, command="llama-server --port 9090"),
                ProcessObservation(pid=10, command="postgres"),
            ),
            listeners=(ListenerObservation(pid=10, port=5432),),
        )
        self.assertFalse(pf.ok)
        self.assertTrue(any("forbidden generation process pid=99" in c for c in pf.conflicts))
        self.assertFalse(any("5432" in c for c in pf.conflicts))
        self.assertFalse(any("pid=10" in c and "listener" in c for c in pf.conflicts))

    def test_unknown_owner_listener_is_telemetry_not_conflict(self) -> None:
        pf = preflight(
            _spec(artifact_path=str(self.artifact)),
            processes=(),
            listeners=(ListenerObservation(pid=7, port=9090),),
        )
        self.assertTrue(pf.ok)
        self.assertEqual(pf.conflicts, ())
        self.assertEqual(pf.unknown_listeners, (ListenerObservation(pid=7, port=9090),))

    def test_adopted_pid_and_port_allowed(self) -> None:
        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        result = run_lane(
            _spec(
                artifact_path=str(self.artifact),
                command_argv=("true",),
                adopted_pid=42,
                port=8080,
                allowed_generation_ports=(8080,),
            ),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(ProcessObservation(pid=42, command="llama-server --port 8080"),),
            listeners=(ListenerObservation(pid=42, port=8080),),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stdout"], "ok")

    def test_observe_listeners_parses_pid_and_port(self) -> None:
        sample = (
            "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "postgres   10 jamie   6u  IPv4  0x1      0t0  TCP *:5432 (LISTEN)\n"
        )

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["lsof"], 0, stdout=sample, stderr="")

        observed = observe_listeners(run=fake_run)
        self.assertEqual(observed, (ListenerObservation(pid=10, port=5432),))

    def test_embedding_and_reranker_not_generation_conflicts(self) -> None:
        self.assertFalse(is_generation_conflict_name("text-embeddings-server"))
        self.assertFalse(is_generation_conflict_name("reranker --port 1"))

    def test_ps_fails_closed_on_missing(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_ps(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("ps")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: observe_processes(run=fake_ps),
            observe_listeners=lambda: (),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("ps not found", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_ps_fails_closed_on_nonzero(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_ps(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="broken")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: observe_processes(run=fake_ps),
            observe_listeners=lambda: (),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("ps exited", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_lsof_fails_closed_on_missing(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("lsof")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: (ProcessObservation(pid=1, command="idle"),),
            observe_listeners=lambda: observe_listeners(run=fake_lsof),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("lsof not found", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_lsof_fails_closed_on_nonzero(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["lsof"], 1, stdout="", stderr="broken")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: (ProcessObservation(pid=1, command="idle"),),
            observe_listeners=lambda: observe_listeners(run=fake_lsof),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("lsof exited", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_empty_ps_output_fails_closed(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_ps(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["ps"], 0, stdout="", stderr="")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: observe_processes(run=fake_ps),
            observe_listeners=lambda: (),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("ps produced no usable output", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_empty_lsof_output_fails_closed(self) -> None:
        ran = {"n": 0}

        def fake_cmd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran["n"] += 1
            return subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")

        def fake_lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["lsof"], 0, stdout="", stderr="")

        result = run_lane(
            _spec(artifact_path=str(self.artifact), command_argv=("true",)),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_cmd,
            observe_procs=lambda: (ProcessObservation(pid=1, command="idle"),),
            observe_listeners=lambda: observe_listeners(run=fake_lsof),
        )
        self.assertEqual(ran["n"], 0)
        self.assertEqual(result["status"], "discovery_failed")
        self.assertIn("lsof produced no usable output", result["error"]["message"])
        self.assertIsNone(inspect(self.lease))

    def test_release_called_even_on_artifact_write_failure(self) -> None:
        def fake_write(*args: object, **kwargs: object) -> None:
            raise OSError("write failed")

        def fake_release(path: object, owner: str, pid: int, token: str) -> None:
            self.assertIsNotNone(inspect(self.lease))
            from router.hardware_lease import release as real_release

            real_release(path, owner, pid, token)

        result = run_lane(
            _spec(
                artifact_path=str(self.artifact),
                command_argv=("true",),
                forbidden_generation_patterns=(),
            ),
            mode="run",
            lease_path=self.lease,
            subprocess_run=lambda *a, **k: subprocess.CompletedProcess(a[0], 0),
            release_lease=fake_release,
            write_artifact=fake_write,
            processes=(),
            listeners=(),
        )
        self.assertEqual(result["status"], "artifact_error")
        self.assertIsNone(inspect(self.lease))

    def test_cli_remainder_after_double_dash(self) -> None:
        argv = [
            "--owner",
            "o",
            "--model",
            "m",
            "--port",
            "1",
            "--model-path",
            "/x",
            "--run-id",
            "r",
            "--action",
            "a",
            "--artifact-path",
            str(self.artifact),
            "--lease-path",
            str(self.lease),
            "--dry-run",
            "--",
            "echo",
            "hi",
        ]
        code = main(argv)
        self.assertEqual(code, 0)
        self.assertFalse(self.lease.exists())

    def test_no_default_stop_commands(self) -> None:
        source = Path(__file__).resolve().parents[1] / "hardware_lane.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)
        self.assertNotIn("lfm stop", text)
        self.assertNotIn("qwen stop", text)

    def test_default_patterns_detect_qwen_dflash_and_lfm_llama_server(self) -> None:
        pf = preflight(
            _spec(artifact_path=str(self.artifact), forbidden_generation_patterns=()),
            processes=(
                ProcessObservation(pid=11, command="python server/dflash_server.py --port 8081"),
                ProcessObservation(pid=12, command="/opt/bin/llama-server --port 9090"),
            ),
            listeners=(),
        )
        self.assertFalse(pf.ok)
        joined = " ".join(pf.conflicts)
        self.assertIn("dflash_server.py", joined)
        self.assertIn("llama-server", joined)

    def test_default_patterns_exclude_embedding_and_reranker(self) -> None:
        pf = preflight(
            _spec(artifact_path=str(self.artifact), forbidden_generation_patterns=()),
            processes=(
                ProcessObservation(pid=1, command="llama-server --embedding --port 1"),
                ProcessObservation(pid=2, command="dflash_server.py --reranker"),
            ),
            listeners=(),
        )
        self.assertTrue(pf.ok)

    def test_user_patterns_extend_defaults(self) -> None:
        resolved = resolve_forbidden_generation_patterns(("custom-gen",))
        self.assertEqual(
            resolved,
            (*DEFAULT_FORBIDDEN_GENERATION_PATTERNS, "custom-gen"),
        )
        pf = preflight(
            _spec(artifact_path=str(self.artifact), forbidden_generation_patterns=("custom-gen",)),
            processes=(
                ProcessObservation(pid=3, command="custom-gen --port 7"),
                ProcessObservation(pid=4, command="llama-server --port 8"),
            ),
            listeners=(),
        )
        self.assertFalse(pf.ok)
        joined = " ".join(pf.conflicts)
        self.assertIn("custom-gen", joined)
        self.assertIn("llama-server", joined)

    def test_replace_forbidden_patterns_overrides_defaults(self) -> None:
        pf = preflight(
            _spec(
                artifact_path=str(self.artifact),
                forbidden_generation_patterns=("only-this",),
                replace_forbidden_generation_patterns=True,
            ),
            processes=(ProcessObservation(pid=5, command="llama-server --port 8"),),
            listeners=(),
        )
        self.assertTrue(pf.ok)

    def test_cli_nonzero_command_returns_two(self) -> None:
        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        result = run_lane(
            _spec(
                artifact_path=str(self.artifact),
                command_argv=("false",),
                forbidden_generation_patterns=(),
            ),
            mode="run",
            lease_path=self.lease,
            subprocess_run=fake_run,
            processes=(),
            listeners=(),
        )
        self.assertEqual(result["status"], "command_failed")
        self.assertEqual(result["exit_code"], 1)
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "command_failed")
        self.assertEqual(_cli_exit_code("run", result), 2)


class TestHardwareLaneSelfExclusion(unittest.TestCase):
    def test_current_lane_contains_llama_server(self) -> None:
        current_pid = __import__("os").getpid()
        ancestor_pid = current_pid + 1

        def fake_ps(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=(
                    f"{current_pid} {ancestor_pid} python -m router.hardware_lane "
                    "-- /bin/bash -c llama-server\n"
                    f"{ancestor_pid} 1 bash -c llama-server\n"
                    "10 1 postgres\n"
                ),
                stderr="",
            )

        observed = observe_processes(run=fake_ps)
        self.assertEqual(
            observed,
            (ProcessObservation(pid=10, command="postgres"),),
        )
        self.assertTrue(
            preflight(
                _spec(artifact_path=""),
                processes=observed,
                listeners=(),
            ).ok
        )

        conflicting = (
            *observed,
            ProcessObservation(pid=99, command="llama-server --port 9090"),
        )
        result = preflight(
            _spec(artifact_path=""),
            processes=conflicting,
            listeners=(),
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("forbidden generation process pid=99" in item for item in result.conflicts)
        )
if __name__ == "__main__":
    unittest.main()

