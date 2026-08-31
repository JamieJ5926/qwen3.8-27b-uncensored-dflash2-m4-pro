"""Offline packet, validation, and dispatch coverage. Do not call models."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from router.artifacts import ArtifactStore
from router.domain import (
    ActionDecision,
    ChatMessage,
    ConversationEnvelope,
    LaneBudget,
    LaneDecision,
    LimitError,
    ParseError,
    WorkerJob,
    WorkerResult,
)
from router.packet import compile_packet, hash_packet_body, packet_body
from router.targets import Dispatcher
from router.validate import parse_action_decision

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "packets"


def _envelope_from(raw: dict) -> ConversationEnvelope:
    env = raw["envelope"]
    budgets = env["budgets"]
    return ConversationEnvelope(
        request_id=env["request_id"],
        task_id=env["task_id"],
        messages=tuple(
            ChatMessage(role=m["role"], content=m["content"]) for m in env["messages"]
        ),
        profile=env["profile"],
        budgets=LaneBudget(
            input_tokens=budgets["input_tokens"],
            output_tokens=budgets["output_tokens"],
            timeout_s=budgets["timeout_s"],
            follow_up_budget=budgets.get("follow_up_budget", 0),
        ),
        available_tools=tuple(env["available_tools"]),
        intent=env["intent"],
        risk_class=env["risk_class"],
        context_token_count=env["context_token_count"],
        needs_qwen_judgment=env["needs_qwen_judgment"],
    )


def _decision_from(raw: dict) -> LaneDecision:
    dec = raw["lane_decision"]
    budgets = dec["lane_budget"]
    return LaneDecision(
        rule_id=dec["rule_id"],
        lane=dec["lane"],
        reason_codes=tuple(dec["reason_codes"]),
        rule_version=dec["rule_version"],
        lane_budget=LaneBudget(
            input_tokens=budgets["input_tokens"],
            output_tokens=budgets["output_tokens"],
            timeout_s=budgets["timeout_s"],
            follow_up_budget=budgets.get("follow_up_budget", 0),
        ),
        fallback_lane=dec["fallback_lane"],
    )


class PacketHashTests(unittest.TestCase):
    def test_deterministic_hash(self) -> None:
        raw = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
        envelope = _envelope_from(raw)
        decision = _decision_from(raw)
        first = compile_packet(envelope, decision)
        second = compile_packet(envelope, decision)
        self.assertEqual(first.packet_hash, second.packet_hash)
        self.assertEqual(len(first.packet_hash), 64)
        self.assertEqual(first.packet_hash, hash_packet_body(packet_body(first)))

    def test_budget_rejection(self) -> None:
        raw = json.loads((FIXTURES / "over_budget.json").read_text(encoding="utf-8"))
        with self.assertRaises(LimitError):
            compile_packet(_envelope_from(raw), _decision_from(raw))


class ActionValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
        self.packet = compile_packet(_envelope_from(raw), _decision_from(raw))

    def test_accepts_allowed_tool(self) -> None:
        action = parse_action_decision(
            {
                "kind": "tool",
                "action_name": "lookup_log",
                "arguments": {"query": "error"},
                "answer": "",
                "confidence": 0.9,
                "packet_hash": self.packet.packet_hash,
                "action_budget": self.packet.action_budget,
                "follow_up_budget": 1,
            },
            self.packet,
        )
        self.assertEqual(action.action_name, "lookup_log")
        self.assertNotIn("lane", action.__dataclass_fields__)

    def test_rejects_lane_field(self) -> None:
        with self.assertRaises(ParseError):
            parse_action_decision(
                {
                    "kind": "direct_answer",
                    "action_name": "direct_answer",
                    "arguments": {},
                    "answer": "ok",
                    "confidence": 1.0,
                    "packet_hash": self.packet.packet_hash,
                    "action_budget": self.packet.action_budget,
                    "follow_up_budget": 0,
                    "lane": "lfm_direct",
                },
                self.packet,
            )

    def test_rejects_unknown_action(self) -> None:
        with self.assertRaises(ParseError):
            parse_action_decision(
                {
                    "kind": "tool",
                    "action_name": "rm_rf",
                    "arguments": {"path": "/"},
                    "answer": "",
                    "confidence": 0.1,
                    "packet_hash": self.packet.packet_hash,
                    "action_budget": self.packet.action_budget,
                    "follow_up_budget": 0,
                },
                self.packet,
            )

    def test_rejects_invalid_arguments(self) -> None:
        with self.assertRaises(ParseError):
            parse_action_decision(
                {
                    "kind": "tool",
                    "action_name": "lookup_log",
                    "arguments": [],
                    "answer": "",
                    "confidence": 0.1,
                    "packet_hash": self.packet.packet_hash,
                    "action_budget": self.packet.action_budget,
                    "follow_up_budget": 0,
                },
                self.packet,
            )

    def test_rejects_hash_mismatch(self) -> None:
        with self.assertRaises(ParseError):
            parse_action_decision(
                {
                    "kind": "direct_answer",
                    "action_name": "direct_answer",
                    "arguments": {},
                    "answer": "ok",
                    "confidence": 1.0,
                    "packet_hash": "0" * 64,
                    "action_budget": self.packet.action_budget,
                    "follow_up_budget": 0,
                },
                self.packet,
            )


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
        self.envelope = _envelope_from(raw)
        self.decision = _decision_from(raw)
        self.packet = compile_packet(self.envelope, self.decision)
        self.calls = {"n": 0}

        def lookup(job: WorkerJob) -> WorkerResult:
            self.calls["n"] += 1
            return WorkerResult(
                job_id=job.job_id,
                status="ok",
                answer="found",
                artifact_refs=(),
                tool_calls=(),
                evidence=("line 12",),
                confidence=0.8,
            )

        self.tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tmp.name))
        self.dispatcher = Dispatcher({"lookup_log": lookup}, self.store)
        self.action = ActionDecision(
            kind="tool",
            action_name="lookup_log",
            arguments={"query": "error"},
            answer="",
            confidence=0.9,
            packet_hash=self.packet.packet_hash,
            action_budget=self.packet.action_budget,
            follow_up_budget=1,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_idempotent_reuse(self) -> None:
        first, obs1 = self.dispatcher.dispatch(self.packet, self.action, self.decision)
        second, obs2 = self.dispatcher.dispatch(self.packet, self.action, self.decision)
        self.assertIs(first, second)
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(obs2.acceptance_summary, "idempotent_reuse")
        self.assertEqual(obs1.artifact_ref, obs2.artifact_ref)
        self.assertTrue(obs1.artifact_ref.startswith("artifact:"))
        payload = self.store.read(obs1.artifact_ref)
        self.assertIn("found", payload)
        self.assertLess(len(json.dumps(obs1.__dict__).encode("utf-8")), 2048)

    def test_compact_observation_payload(self) -> None:
        _, obs = self.dispatcher.dispatch(self.packet, self.action, self.decision)
        encoded = json.dumps(obs.__dict__, sort_keys=True).encode("utf-8")
        self.assertLess(len(encoded), 512)
        self.assertNotIn("line 12", encoded.decode("utf-8"))

    def test_follow_up_exhaustion(self) -> None:
        self.dispatcher.dispatch(self.packet, self.action, self.decision)
        self.dispatcher.consume_follow_up(self.packet)
        with self.assertRaises(ParseError):
            self.dispatcher.consume_follow_up(self.packet)
        zero = compile_packet(
            ConversationEnvelope(
                request_id="req-zero",
                task_id="t",
                messages=(ChatMessage(role="user", content="hi"),),
                profile="slim",
                budgets=LaneBudget(256, 64, 5.0, 0),
                available_tools=("direct_answer",),
                intent="chat",
                risk_class="low",
                context_token_count=1,
                needs_qwen_judgment=True,
            ),
            LaneDecision(
                rule_id="zero",
                lane="qwen_coordinator",
                reason_codes=("x",),
                rule_version="1",
                lane_budget=LaneBudget(256, 64, 5.0, 0),
                fallback_lane="fail_visible",
            ),
        )
        with self.assertRaises(ParseError):
            self.dispatcher.consume_follow_up(zero)


if __name__ == "__main__":
    unittest.main()
