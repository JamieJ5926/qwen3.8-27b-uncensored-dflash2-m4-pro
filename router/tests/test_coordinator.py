"""Coordinator contracts: pipeline, validation, follow-up, idempotency, replay."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from router.artifacts import ArtifactStore
from router.coordinator import Coordinator, replay_final_response
from router.domain import (
    FinalResponse,
    LimitError,
    NoRouteError,
    ParseError,
    RuntimeConfig,
    RouterError,
    WorkerJob,
    WorkerResult,
)
from router.normalize import normalize_chat_body
from router.packet import compile_packet
from router.rules import load_route_table_path, resolve_lane
from router.transport import TransportResponse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"

BODY = {
    "model": "qwen3.8-27b",
    "messages": [{"role": "user", "content": "def hello(): pass"}],
    "max_tokens": 128,
    "tools": [
        {"type": "function", "function": {"name": "lookup_log"}},
        {"type": "function", "function": {"name": "analyst"}},
    ],
    "metadata": {
        "intent": "coding",
        "risk_class": "high",
        "needs_qwen_judgment": True,
    },
}

FORBIDDEN_TOKEN_STRINGS = (
    "authorization",
    "bearer",
    "api_key",
    "api-key",
    "sk-",
    "lease_token",
)


class FakeTransport:
    def __init__(self, contents):
        self.calls: list[str] = []
        self.contents = list(contents)
        self.base_url = "http://127.0.0.1:8083/v1"
        self.model = "qwen3.8-27b"
        self.timeout_s = 60.0

    def ask(self, packet, *, observation=""):
        self.calls.append(observation)
        if not self.contents:
            raise AssertionError("model called with no scripted response")
        return TransportResponse(
            content=self.contents.pop(0),
            prompt_tokens=4,
            completion_tokens=2,
            model="qwen3.8-27b",
        )


class ExplodingTransport(FakeTransport):
    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    def ask(self, packet, *, observation=""):
        self.calls.append(observation)
        raise self.exc


class FakeLFMTransport:
    def __init__(self, content="lfm answer", exc=None):
        self.calls = []
        self.content = content
        self.exc = exc
        self.timeout_s = 60.0

    def chat(self, envelope, decision):
        self.calls.append((envelope.request_id, decision.rule_id))
        if self.exc is not None:
            raise self.exc
        return TransportResponse(
            content=self.content,
            prompt_tokens=3,
            completion_tokens=2,
            model="lfm-heretic",
        )


def action_json(kind, name, arguments, answer, confidence, packet, follow_up):
    return json.dumps(
        {
            "kind": kind,
            "action_name": name,
            "arguments": arguments,
            "answer": answer,
            "confidence": confidence,
            "packet_hash": packet.packet_hash,
            "action_budget": packet.action_budget,
            "follow_up_budget": follow_up,
        }
    )


def envelope_decision(rid):
    _, rules = load_route_table_path(FIXTURES / "default.json")
    envelope = normalize_chat_body(BODY, request_id=rid)
    return envelope, resolve_lane(envelope, rules)


def routed_envelope_decision(
    rid, *, intent, risk_class="low", needs_qwen_judgment=False, content="hello"
):
    _, rules = load_route_table_path(FIXTURES / "default.json")
    body = {
        "model": "router",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 128,
        "metadata": {
            "intent": intent,
            "risk_class": risk_class,
            "needs_qwen_judgment": needs_qwen_judgment,
        },
    }
    envelope = normalize_chat_body(body, request_id=rid)
    return envelope, resolve_lane(envelope, rules)


def worker(status="ok", answer="found", confidence=0.9, error=None):
    def adapter(job: WorkerJob) -> WorkerResult:
        return WorkerResult(
            job_id=job.job_id,
            status=status,
            answer=answer,
            artifact_refs=(),
            tool_calls=(),
            evidence=("src:log",),
            confidence=confidence,
            error=error,
        )

    return adapter


class CoordinatorPipelineTests(unittest.TestCase):
    def test_direct_answer_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("c-1")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "the answer", 0.95, packet, 0
            )
            transport = FakeTransport([direct])
            final = Coordinator(
                transport=transport, artifacts=store
            )(envelope, decision, _config())
            self.assertEqual(final.status, "ok")
            self.assertEqual(final.answer, "the answer")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(final.lane_trace["rule_id"], "coding-qwen")
            self.assertEqual(final.lane_trace["packet_hash"], packet.packet_hash)
            self.assertEqual(final.action_trace["steps"][0]["kind"], "direct_answer")

    def test_tool_and_specialist_actions_dispatch(self) -> None:
        for kind, name, arguments in (
            ("tool", "lookup_log", {"query": "x"}),
            ("specialist", "analyst", {"brief": "inspect"}),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                store = ArtifactStore(Path(tmp))
                envelope, decision = envelope_decision(f"c-{name}")
                packet = compile_packet(envelope, decision)
                content = action_json(kind, name, arguments, "", 0.8, packet, 0)
                adapters = {name: worker(answer=f"done:{name}")}
                final = Coordinator(
                    transport=FakeTransport([content]), artifacts=store, adapters=adapters
                )(envelope, decision, _config())
                self.assertEqual(final.status, "ok")
                self.assertEqual(final.answer, f"done:{name}")

    def test_unknown_action_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("c-unknown")
            packet = compile_packet(envelope, decision)
            content = action_json(
                "tool", "no_such_tool", {"query": "x"}, "", 0.8, packet, 0
            )
            coordinator = Coordinator(
                transport=FakeTransport([content]), artifacts=store
            )
            with self.assertRaises(ParseError) as ctx:
                coordinator(envelope, decision, _config())
            self.assertIn("unknown action", str(ctx.exception))
            self.assertTrue(
                (Path(tmp) / "c-unknown" / "error.txt").exists()
            )

    def test_forbidden_lane_field_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("c-lane")
            packet = compile_packet(envelope, decision)
            content = json.loads(
                action_json("direct_answer", "direct_answer", {}, "a", 0.9, packet, 0)
            )
            content["lane"] = "lfm_direct"
            transport = FakeTransport([json.dumps(content)])
            with self.assertRaises(ParseError) as ctx:
                Coordinator(transport=transport, artifacts=store)(
                    envelope, decision, _config()
                )
            self.assertIn("lane fields", str(ctx.exception))

    def test_packet_hash_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("c-hash")
            packet = compile_packet(envelope, decision)
            content = json.loads(
                action_json("direct_answer", "direct_answer", {}, "a", 0.9, packet, 0)
            )
            content["packet_hash"] = "0" * 64
            transport = FakeTransport([json.dumps(content)])
            with self.assertRaises(ParseError) as ctx:
                Coordinator(transport=transport, artifacts=store)(
                    envelope, decision, _config()
                )
            self.assertIn("packet_hash", str(ctx.exception))

    def test_transport_timeout_mapped_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("c-timeout")
            coordinator = Coordinator(
                transport=ExplodingTransport(TimeoutError()), artifacts=store
            )
            with self.assertRaises(LimitError):
                coordinator(envelope, decision, _config())
            error = (Path(tmp) / "c-timeout" / "error.txt").read_text()
            self.assertIn("timeout", error)


class CoordinatorConfigurationTests(unittest.TestCase):
    def test_min_confidence_must_be_numeric_probability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            for value in (True, "0.5", -0.1, 1.1):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        Coordinator(
                            transport=FakeTransport([]),
                            artifacts=store,
                            min_confidence=value,
                        )
            for value, expected in ((0, 0.0), (0.5, 0.5), (1, 1.0)):
                with self.subTest(value=value):
                    self.assertEqual(
                        Coordinator(
                            transport=FakeTransport([]),
                            artifacts=store,
                            min_confidence=value,
                        ).min_confidence,
                        expected,
                    )


class CoordinatorLaneRoutingTests(unittest.TestCase):
    def test_lfm_direct_uses_only_lfm_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qwen = ExplodingTransport(AssertionError("qwen called for lfm lane"))
            lfm = FakeLFMTransport("direct lfm answer")
            envelope, decision = routed_envelope_decision("lane-lfm", intent="chat")
            self.assertEqual(decision.lane, "lfm_direct")
            final = Coordinator(
                transport=qwen,
                lfm_transport=lfm,
                artifacts=ArtifactStore(Path(tmp)),
            )(envelope, decision, _config())
            self.assertEqual(final.answer, "direct lfm answer")
            self.assertEqual(final.lane_trace["lane"], "lfm_direct")
            self.assertEqual(final.action_trace["steps"][0]["action_name"], "lfm")
            self.assertEqual(qwen.calls, [])
            self.assertEqual(len(lfm.calls), 1)

    def test_lfm_missing_transport_fails_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qwen = ExplodingTransport(AssertionError("qwen fallback is forbidden"))
            envelope, decision = routed_envelope_decision(
                "lane-lfm-missing", intent="chat"
            )
            with self.assertRaises(RouterError) as ctx:
                Coordinator(
                    transport=qwen, artifacts=ArtifactStore(Path(tmp))
                )(envelope, decision, _config())
            self.assertEqual(
                (ctx.exception.code, ctx.exception.http_status),
                ("service_unavailable", 503),
            )
            self.assertEqual(qwen.calls, [])

    def test_lfm_replays_with_fresh_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = routed_envelope_decision(
                "lane-lfm-replay", intent="chat"
            )
            first_lfm = FakeLFMTransport("stored lfm answer")
            first = Coordinator(
                transport=ExplodingTransport(AssertionError("qwen called")),
                lfm_transport=first_lfm,
                artifacts=store,
            )(envelope, decision, _config())
            restarted_lfm = FakeLFMTransport(
                exc=AssertionError("lfm called after restart")
            )
            second = Coordinator(
                transport=ExplodingTransport(
                    AssertionError("qwen called after restart")
                ),
                lfm_transport=restarted_lfm,
                artifacts=store,
            )(envelope, decision, _config())
            self.assertEqual(first, second)
            self.assertEqual(restarted_lfm.calls, [])

    def test_fixed_worker_uses_rule_owned_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def retrieval_worker(envelope, decision, config):
                calls.append((envelope.request_id, decision.rule_id))
                return WorkerResult(
                    job_id="retrieval-1",
                    status="ok",
                    answer="retrieved",
                    artifact_refs=(),
                    tool_calls=(),
                    evidence=("offline-index",),
                    confidence=0.9,
                )

            qwen = ExplodingTransport(AssertionError("qwen called for fixed worker"))
            envelope, decision = routed_envelope_decision(
                "lane-worker", intent="retrieval"
            )
            self.assertEqual(decision.lane, "fixed_worker")
            final = Coordinator(
                transport=qwen,
                fixed_workers={decision.rule_id: retrieval_worker},
                artifacts=ArtifactStore(Path(tmp)),
            )(envelope, decision, _config())
            self.assertEqual(final.answer, "retrieved")
            self.assertEqual(final.citations, ("offline-index",))
            self.assertEqual(calls, [("lane-worker", "retrieval-worker")])
            self.assertEqual(qwen.calls, [])

    def test_fixed_worker_missing_adapter_fails_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qwen = ExplodingTransport(AssertionError("qwen fallback is forbidden"))
            envelope, decision = routed_envelope_decision(
                "lane-worker-missing", intent="retrieval"
            )
            with self.assertRaises(RouterError) as ctx:
                Coordinator(
                    transport=qwen, artifacts=ArtifactStore(Path(tmp))
                )(envelope, decision, _config())
            self.assertEqual(
                (ctx.exception.code, ctx.exception.http_status),
                ("worker_unavailable", 503),
            )
            self.assertEqual(qwen.calls, [])

    def test_fail_visible_never_calls_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qwen = ExplodingTransport(AssertionError("qwen called for fail lane"))
            envelope, decision = routed_envelope_decision(
                "lane-fail",
                intent="chat",
                risk_class="high",
                needs_qwen_judgment=True,
            )
            self.assertEqual(decision.lane, "fail_visible")
            with self.assertRaises(NoRouteError):
                Coordinator(
                    transport=qwen, artifacts=ArtifactStore(Path(tmp))
                )(envelope, decision, _config())
            self.assertEqual(qwen.calls, [])


class CoordinatorFollowUpTests(unittest.TestCase):
    def test_rejected_result_triggers_single_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("f-1")
            packet = compile_packet(envelope, decision)
            tool = action_json("tool", "lookup_log", {"query": "x"}, "", 0.8, packet, 1)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "recovered", 0.95, packet, 0
            )
            transport = FakeTransport([tool, direct])
            final = Coordinator(
                transport=transport,
                artifacts=store,
                adapters={"lookup_log": worker(status="error", answer="", error="crash")},
            )(envelope, decision, _config())
            self.assertEqual(len(transport.calls), 2)
            self.assertNotEqual(transport.calls[1], "")
            self.assertEqual(final.status, "ok")
            self.assertEqual(final.answer, "recovered")
            self.assertEqual(
                sorted(p.name for p in (Path(tmp) / "f-1").iterdir()),
                sorted(
                    [
                        "route.json",
                        "packet.json",
                        "action-1.json",
                        "action-2.json",
                        "step-1.txt",
                        "step-2.txt",
                        "timing.json",
                        "usage.json",
                        "result.json",
                    ]
                ),
            )

    def test_follow_up_exhaustion_fails_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("f-2")
            packet = compile_packet(envelope, decision)
            tool = action_json("tool", "lookup_log", {"query": "x"}, "", 0.8, packet, 1)
            transport = FakeTransport([tool, tool, tool])
            final = Coordinator(
                transport=transport,
                artifacts=store,
                adapters={"lookup_log": worker(status="error", answer="", error="crash")},
            )(envelope, decision, _config())
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(final.status, "failed")
            self.assertIn("status:error", json.dumps(final.action_trace))

    def test_low_confidence_triggers_follow_up_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("f-3")
            packet = compile_packet(envelope, decision)
            weak = action_json(
                "direct_answer", "direct_answer", {}, "unsure", 0.5, packet, 1
            )
            strong = action_json(
                "direct_answer", "direct_answer", {}, "certain", 0.95, packet, 0
            )
            transport = FakeTransport([weak, strong])
            final = Coordinator(
                transport=transport, artifacts=store, min_confidence=0.9
            )(envelope, decision, _config())
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(final.answer, "certain")
            self.assertIn("low_confidence", json.dumps(final.action_trace))

    def test_zero_follow_up_budget_fails_without_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("f-4")
            decision = replace(
                decision, lane_budget=replace(decision.lane_budget, follow_up_budget=0)
            )
            packet = compile_packet(envelope, decision)
            content = action_json("tool", "lookup_log", {"query": "x"}, "", 0.8, packet, 0)
            transport = FakeTransport([content])
            final = Coordinator(
                transport=transport,
                artifacts=store,
                adapters={"lookup_log": worker(status="error", answer="", error="crash")},
            )(envelope, decision, _config())
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(final.status, "failed")


class CoordinatorIdempotencyTests(unittest.TestCase):
    def test_repeated_request_reuses_stored_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("i-1")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "stable", 0.95, packet, 0
            )
            transport = FakeTransport([direct])
            coordinator = Coordinator(transport=transport, artifacts=store)
            first = coordinator(envelope, decision, _config())
            second = coordinator(envelope, decision, _config())
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(first, second)
            self.assertEqual(first.status, "ok")

    def test_repeated_request_replays_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("i-2")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "durable", 0.95, packet, 0
            )
            first = Coordinator(
                transport=FakeTransport([direct]), artifacts=store
            )(envelope, decision, _config())
            restarted = Coordinator(
                transport=ExplodingTransport(
                    AssertionError("model called after restart")
                ),
                artifacts=store,
            )
            second = restarted(envelope, decision, _config())
            self.assertEqual(first, second)
            self.assertEqual(len(restarted.transport.calls), 0)

    def test_reused_request_id_with_different_content_fails_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("i-3")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "original", 0.95, packet, 0
            )
            Coordinator(
                transport=FakeTransport([direct]), artifacts=store
            )(envelope, decision, _config())
            other_body = dict(BODY)
            other_body["messages"] = [
                {"role": "user", "content": "completely different request"}
            ]
            _, rules = load_route_table_path(FIXTURES / "default.json")
            reused = normalize_chat_body(other_body, request_id="i-3")
            other_decision = resolve_lane(reused, rules)
            restarted = Coordinator(
                transport=ExplodingTransport(
                    AssertionError("model called for reused identity")
                ),
                artifacts=store,
            )
            with self.assertRaises(ParseError) as ctx:
                restarted(reused, other_decision, _config())
            self.assertIn("already completed", str(ctx.exception))
            error = (Path(tmp) / "i-3" / "error.txt").read_text()
            self.assertIn("already completed", error)

    def test_corrupt_stored_result_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            store.write("i-4", "result.json", "not json")
            envelope, decision = envelope_decision("i-4")
            coordinator = Coordinator(
                transport=ExplodingTransport(
                    AssertionError("model called over corrupt state")
                ),
                artifacts=store,
            )
            with self.assertRaises(ParseError) as ctx:
                coordinator(envelope, decision, _config())
            self.assertIn("malformed", str(ctx.exception))

    def test_rule_version_mismatch_fails_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("i-version")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "versioned", 0.95, packet, 0
            )
            Coordinator(
                transport=FakeTransport([direct]), artifacts=store
            )(envelope, decision, _config())
            stored = json.loads(
                (Path(tmp) / "i-version" / "result.json").read_text()
            )
            self.assertEqual(
                stored["lane_trace"]["rule_version"], decision.rule_version
            )
            changed = replace(decision, rule_version="2")
            restarted = Coordinator(
                transport=ExplodingTransport(
                    AssertionError("transport called after version mismatch")
                ),
                artifacts=store,
            )
            with self.assertRaises(ParseError) as ctx:
                restarted(envelope, changed, _config())
            self.assertIn("version", str(ctx.exception))
            self.assertEqual(restarted.transport.calls, [])


class CoordinatorArtifactTests(unittest.TestCase):
    def test_artifacts_complete_and_token_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("a-1")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "answer text", 0.95, packet, 0
            )
            Coordinator(
                transport=FakeTransport([direct]), artifacts=store
            )(envelope, decision, _config())
            run_dir = Path(tmp) / "a-1"
            names = {p.name for p in run_dir.iterdir()}
            self.assertIn("route.json", names)
            self.assertIn("packet.json", names)
            self.assertIn("action-1.json", names)
            self.assertIn("result.json", names)
            self.assertIn("timing.json", names)
            self.assertIn("usage.json", names)
            self.assertIn("step-1.txt", names)
            for path in run_dir.iterdir():
                text = path.read_text().lower()
                for forbidden in FORBIDDEN_TOKEN_STRINGS:
                    self.assertNotIn(forbidden, text, f"{path.name} leaks {forbidden}")
            result = json.loads((run_dir / "result.json").read_text())
            self.assertEqual(result["request_id"], "a-1")
            self.assertIn("prompt_tokens", result["usage"])

    def test_replay_rebuilds_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            envelope, decision = envelope_decision("r-1")
            packet = compile_packet(envelope, decision)
            direct = action_json(
                "direct_answer", "direct_answer", {}, "replayed", 0.95, packet, 0
            )
            coordinator = Coordinator(
                transport=FakeTransport([direct]), artifacts=store
            )
            final = coordinator(envelope, decision, _config())
            replayed = replay_final_response(store, "r-1")
            self.assertEqual(replayed, final)
            self.assertEqual(replayed.answer, "replayed")


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        qwen_base_url="http://127.0.0.1:8083/v1", lfm_base_url="http://example.invalid/v1"
    )


if __name__ == "__main__":
    unittest.main()
