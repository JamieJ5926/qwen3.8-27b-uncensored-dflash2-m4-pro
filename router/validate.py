"""Parse and validate ActionDecision payloads for the Qwen coordinator lane."""

from __future__ import annotations

import json
from typing import Any, Mapping

from router.domain import (
    ActionDecision,
    DecisionPacket,
    ParseError,
    WorkerResult,
    as_action_kind,
)

FORBIDDEN_LANE_KEYS = frozenset({"lane", "lane_id", "fallback_lane", "rule_id"})


def parse_action_decision(
    raw: str | Mapping[str, Any],
    packet: DecisionPacket,
) -> ActionDecision:
    payload = _as_mapping(raw)
    extra_lane = FORBIDDEN_LANE_KEYS.intersection(payload)
    if extra_lane:
        raise ParseError(f"ActionDecision must not include lane fields: {sorted(extra_lane)}")
    kind = as_action_kind(str(payload.get("kind", "")))
    action_name = payload.get("action_name")
    if not isinstance(action_name, str):
        raise ParseError("action_name must be a string")
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ParseError("arguments must be an object")
    if any(key in FORBIDDEN_LANE_KEYS for key in arguments):
        raise ParseError("arguments must not include lane fields")
    try:
        json.dumps(dict(arguments), sort_keys=True)
    except TypeError as exc:
        raise ParseError(f"arguments must be JSON-serializable: {exc}") from exc
    answer = payload.get("answer", "")
    if not isinstance(answer, str):
        raise ParseError("answer must be a string")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ParseError("confidence must be a number")
    packet_hash = payload.get("packet_hash")
    if packet_hash != packet.packet_hash:
        raise ParseError("packet_hash does not match compiled packet")
    action_budget = payload.get("action_budget", packet.action_budget)
    if not isinstance(action_budget, int) or isinstance(action_budget, bool):
        raise ParseError("action_budget must be an integer")
    if action_budget > packet.action_budget:
        raise ParseError("action_budget exceeds packet action_budget")
    follow_up_budget = payload.get("follow_up_budget", packet.follow_up_budget)
    if follow_up_budget not in (0, 1):
        raise ParseError("follow_up_budget must be 0 or 1")
    _check_action(kind, action_name, arguments, answer, packet)
    return ActionDecision(
        kind=kind,
        action_name=action_name,
        arguments=dict(arguments),
        answer=answer,
        confidence=float(confidence),
        packet_hash=str(packet_hash),
        action_budget=action_budget,
        follow_up_budget=int(follow_up_budget),
    )



def result_rejection(
    result: WorkerResult, *, min_confidence: float = 0.0
) -> str | None:
    """Acceptance predicate for worker results. None accepts; a reason rejects.

    min_confidence is caller policy, not a hidden default: 0.0 rejects only
    structurally invalid results, and any floor must arrive as explicit input."""

    if result.status != "ok":
        return f"status:{result.status}"
    if result.error is not None:
        return "error_field_set"
    confidence = result.confidence
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return "confidence_not_numeric"
    if not 0.0 <= confidence <= 1.0:
        return "confidence_out_of_range"
    if confidence < min_confidence:
        return f"low_confidence:{confidence}"
    if not result.answer and not result.evidence:
        return "empty_answer_without_evidence"
    return None


def _as_mapping(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"ActionDecision is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ParseError("ActionDecision must be a JSON object")
    return loaded


def _check_action(
    kind: str,
    action_name: str,
    arguments: Mapping[str, Any],
    answer: str,
    packet: DecisionPacket,
) -> None:
    allowed = set(packet.allowed_actions)
    if kind == "direct_answer":
        if action_name not in ("", "direct_answer") and action_name not in allowed:
            raise ParseError(f"unknown action: {action_name}")
        if arguments:
            raise ParseError("direct_answer arguments must be empty")
        if not answer.strip():
            raise ParseError("direct_answer requires a non-empty answer")
        return
    if action_name not in allowed:
        raise ParseError(f"unknown action: {action_name}")
    if kind == "tool" and not arguments:
        raise ParseError("tool actions require an arguments object")
    if kind == "specialist" and "brief" not in arguments:
        raise ParseError("specialist actions require a brief argument")
