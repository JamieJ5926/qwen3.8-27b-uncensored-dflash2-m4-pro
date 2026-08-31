"""Offline pipeline: normalize -> lane -> packet -> validate -> idempotent dispatch."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from router.artifacts import ArtifactStore
from router.domain import FinalResponse, RuntimeConfig, WorkerJob, WorkerResult
from router.http_boundary import handle_chat_request
from router.normalize import normalize_chat_body
from router.packet import compile_packet
from router.rules import load_route_table_path, resolve_lane
from router.targets import Dispatcher
from router.validate import parse_action_decision

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


class OfflinePipelineTests(unittest.TestCase):
    def test_normalize_lane_packet_validate_idempotent_dispatch(self) -> None:
        _, rules = load_route_table_path(FIXTURES / "default.json")
        config = RuntimeConfig(
            qwen_base_url="http://example.invalid:8083/v1",
            lfm_base_url="http://example.invalid/v1",
        )
        self.assertTrue(config.qwen_base_url)
        body = {
            "model": "qwen3.8-27b",
            "messages": [{"role": "user", "content": "def hello(): pass"}],
            "max_tokens": 128,
            "tools": [{"type": "function", "function": {"name": "lookup_log"}}],
            "metadata": {
                "intent": "coding",
                "risk_class": "high",
                "needs_qwen_judgment": True,
            },
        }
        envelope = normalize_chat_body(body, request_id="int-1")
        decision = resolve_lane(envelope, rules)
        self.assertEqual(decision.lane, "qwen_coordinator")
        self.assertEqual(decision.rule_id, "coding-qwen")
        self.assertEqual(decision.lane_budget.follow_up_budget, 1)
        self.assertNotIn("kind", decision.__dataclass_fields__)

        packet = compile_packet(envelope, decision)
        self.assertEqual(packet.follow_up_budget, 1)
        self.assertIn("lookup_log", packet.allowed_actions)

        raw_action = {
            "kind": "tool",
            "action_name": "lookup_log",
            "arguments": {"query": "error"},
            "answer": "",
            "confidence": 0.8,
            "packet_hash": packet.packet_hash,
            "action_budget": packet.action_budget,
            "follow_up_budget": 1,
        }
        action = parse_action_decision(raw_action, packet)
        self.assertNotIn("lane", action.__dataclass_fields__)

        calls = {"n": 0}

        def lookup(job: WorkerJob) -> WorkerResult:
            calls["n"] += 1
            return WorkerResult(
                job_id=job.job_id,
                status="ok",
                answer="found",
                artifact_refs=(),
                tool_calls=(),
                evidence=("offline",),
                confidence=0.8,
            )

        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = Dispatcher({"lookup_log": lookup}, ArtifactStore(Path(tmp)))
            first, obs1 = dispatcher.dispatch(packet, action, decision)
            second, obs2 = dispatcher.dispatch(packet, action, decision)
            self.assertIs(first, second)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(obs2.acceptance_summary, "idempotent_reuse")
            self.assertEqual(obs1.artifact_ref, obs2.artifact_ref)

            def handle_chat(env, lane, cfg):
                compiled = compile_packet(env, lane)
                parsed = parse_action_decision(
                    {
                        **raw_action,
                        "packet_hash": compiled.packet_hash,
                        "action_budget": compiled.action_budget,
                    },
                    compiled,
                )
                result, _ = dispatcher.dispatch(compiled, parsed, lane)
                return FinalResponse(
                    request_id=env.request_id,
                    answer=result.answer,
                    artifact_refs=result.artifact_refs,
                    citations=(),
                    lane_trace={"rule_id": lane.rule_id, "lane": lane.lane},
                    action_trace={"kind": parsed.kind},
                    usage={
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                    status="ok",
                )

            status, headers, _payload = handle_chat_request(
                body,
                handle_chat=handle_chat,
                config=config,
                rules=rules,
                request_id="int-1",
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Lane"], "qwen_coordinator")
            self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
