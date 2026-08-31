"""Compile hashed, budget-capped DecisionPacket records."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from router.domain import (
    ConversationEnvelope,
    DecisionPacket,
    LaneDecision,
    LimitError,
)

CANONICAL_SEPARATORS = (",", ":")
# Conservative chars-per-token for offline estimates. Real Qwen usage is measured later.
CHARS_PER_TOKEN = 4
MAX_CONTEXT_TURNS = 4
SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "action_name": {"type": "string"},
        "arguments": {"type": "object"},
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "packet_hash": {"type": "string"},
        "action_budget": {"type": "integer"},
        "follow_up_budget": {"type": "integer"},
    },
    "required": [
        "kind",
        "action_name",
        "arguments",
        "answer",
        "confidence",
        "packet_hash",
        "action_budget",
        "follow_up_budget",
    ],
}


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=CANONICAL_SEPARATORS,
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unserializable type: {type(value)!r}")


def packet_body(packet: DecisionPacket) -> dict[str, Any]:
    return {
        "action_budget": packet.action_budget,
        "allowed_actions": list(packet.allowed_actions),
        "constraints": list(packet.constraints),
        "expected_result_schema": dict(packet.expected_result_schema),
        "follow_up_budget": packet.follow_up_budget,
        "goal": packet.goal,
        "relevant_context": packet.relevant_context,
        "request_id": packet.request_id,
        "task_class": packet.task_class,
        "trace_id": packet.trace_id,
    }


def hash_packet_body(body: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_dumps(dict(body)).encode("utf-8"))
    return digest.hexdigest()


def _goal(envelope: ConversationEnvelope) -> str:
    for message in reversed(envelope.messages):
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return envelope.task_id


def _relevant_context(envelope: ConversationEnvelope) -> str:
    selected = envelope.messages[-MAX_CONTEXT_TURNS:]
    lines = [f"{message.role}: {message.content}" for message in selected]
    return "\n".join(lines)


def compile_packet(
    envelope: ConversationEnvelope,
    decision: LaneDecision,
    *,
    expected_result_schema: Mapping[str, Any] | None = None,
    extra_constraints: tuple[str, ...] = (),
) -> DecisionPacket:
    """Build a DecisionPacket. Hash is SHA-256 over canonical JSON without packet_hash."""
    follow_up = min(1, max(0, decision.lane_budget.follow_up_budget))
    allowed = envelope.available_tools
    if not allowed:
        allowed = ("direct_answer",)
    goal = _goal(envelope)
    context = _relevant_context(envelope)
    constraints = (
        f"intent={envelope.intent}",
        f"risk={envelope.risk_class}",
        f"profile={envelope.profile}",
        *extra_constraints,
    )
    action_budget = min(
        envelope.budgets.output_tokens,
        decision.lane_budget.output_tokens,
    )
    token_cap = min(envelope.budgets.input_tokens, decision.lane_budget.input_tokens)
    byte_cap = token_cap * CHARS_PER_TOKEN
    packet = DecisionPacket(
        request_id=envelope.request_id,
        task_class=envelope.intent,
        goal=goal,
        relevant_context=context,
        constraints=constraints,
        allowed_actions=allowed,
        expected_result_schema=dict(expected_result_schema or SCHEMA),
        action_budget=action_budget,
        trace_id=envelope.request_id,
        packet_hash="",
        follow_up_budget=follow_up,
    )
    body = packet_body(packet)
    encoded = canonical_dumps(body).encode("utf-8")
    tokens = estimate_tokens(encoded.decode("utf-8"))
    if len(encoded) > byte_cap:
        raise LimitError(
            f"packet exceeds byte cap {byte_cap}: {len(encoded)} bytes"
        )
    if tokens > token_cap:
        raise LimitError(
            f"packet exceeds token cap {token_cap}: {tokens} tokens"
        )
    digest = hashlib.sha256(encoded).hexdigest()
    return DecisionPacket(
        request_id=packet.request_id,
        task_class=packet.task_class,
        goal=packet.goal,
        relevant_context=packet.relevant_context,
        constraints=packet.constraints,
        allowed_actions=packet.allowed_actions,
        expected_result_schema=packet.expected_result_schema,
        action_budget=packet.action_budget,
        trace_id=packet.trace_id,
        packet_hash=digest,
        follow_up_budget=packet.follow_up_budget,
    )
