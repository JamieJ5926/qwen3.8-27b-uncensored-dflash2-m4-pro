"""Shared frozen records for the local model coordinator.

Other router modules import these types. Do not define alternate copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, cast

Lane = Literal["lfm_direct", "qwen_coordinator", "fixed_worker", "fail_visible"]
ActionKind = Literal["tool", "specialist", "direct_answer"]
WorkerStatus = Literal["ok", "rejected", "timeout", "error"]
FinalStatus = Literal["ok", "failed"]
Intent = Literal[
    "chat",
    "retrieval",
    "extraction",
    "classification",
    "log_interpret",
    "tool_op",
    "coding",
]
RiskClass = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RuntimeConfig:
    """Backend URLs only. The HTTP boundary never invents a default server."""

    qwen_base_url: str
    lfm_base_url: str
    worker_base_url: str = ""


@dataclass(frozen=True)
class LaneBudget:
    input_tokens: int
    output_tokens: int
    timeout_s: float
    follow_up_budget: int = 0


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ConversationEnvelope:
    request_id: str
    task_id: str
    messages: tuple[ChatMessage, ...]
    profile: str
    budgets: LaneBudget
    available_tools: tuple[str, ...]
    intent: Intent
    risk_class: RiskClass
    context_token_count: int
    needs_qwen_judgment: bool
    stream: bool = False
    model: str = ""
    metadata: Mapping[str, str] | None = None


@dataclass(frozen=True)
class MatchSpec:
    intent: Intent | None = None
    risk_class: RiskClass | None = None
    max_context_tokens: int | None = None
    min_context_tokens: int | None = None
    needs_qwen_judgment: bool | None = None


@dataclass(frozen=True)
class RouteRule:
    id: str
    match: MatchSpec
    lane: Lane
    lane_input_budget: int
    lane_output_budget: int
    timeout_s: float
    fallback_lane: Lane
    enabled: bool = True
    follow_up_budget: int = 0
    version: str = "1"


@dataclass(frozen=True)
class LaneDecision:
    rule_id: str
    lane: Lane
    reason_codes: tuple[str, ...]
    rule_version: str
    lane_budget: LaneBudget
    fallback_lane: Lane


@dataclass(frozen=True)
class DecisionPacket:
    request_id: str
    task_class: str
    goal: str
    relevant_context: str
    constraints: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    expected_result_schema: Mapping[str, Any]
    action_budget: int
    trace_id: str
    packet_hash: str
    follow_up_budget: int = 0


@dataclass(frozen=True)
class ActionDecision:
    """Qwen action choice. Parsed only inside the qwen_coordinator lane. No lane field."""

    kind: ActionKind
    action_name: str
    arguments: Mapping[str, Any]
    answer: str
    confidence: float
    packet_hash: str
    action_budget: int
    follow_up_budget: int


@dataclass(frozen=True)
class WorkerJob:
    job_id: str
    lane_rule_id: str
    action: ActionDecision
    packet: DecisionPacket
    timeout_s: float
    max_tool_calls: int
    isolation: str
    idempotency_key: str


@dataclass(frozen=True)
class WorkerResult:
    job_id: str
    status: WorkerStatus
    answer: str
    artifact_refs: tuple[str, ...]
    tool_calls: tuple[Mapping[str, Any], ...]
    evidence: tuple[str, ...]
    confidence: float
    error: str | None = None


@dataclass(frozen=True)
class CompactObservation:
    request_id: str
    step: int
    wall_ms: int
    prompt_tokens: int
    completion_tokens: int
    tps: float
    acceptance_summary: str
    artifact_ref: str


@dataclass(frozen=True)
class FinalResponse:
    request_id: str
    answer: str
    artifact_refs: tuple[str, ...]
    citations: tuple[str, ...]
    lane_trace: Mapping[str, Any]
    action_trace: Mapping[str, Any]
    usage: Mapping[str, int]
    status: FinalStatus


class RouterError(Exception):
    """Canonical router failure with an OpenAI-shaped mapping."""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def openai_body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.code, "code": self.code}}


class ParseError(RouterError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_request_error", message, 400)


class AmbiguousRouteError(RouterError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_request_error", message, 500)


class NoRouteError(RouterError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_request_error", message, 422)


class RouteTableError(RouterError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_request_error", message, 500)


class LimitError(RouterError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_request_error", message, 400)


def as_lane(value: str) -> Lane:
    allowed: Sequence[Lane] = (
        "lfm_direct",
        "qwen_coordinator",
        "fixed_worker",
        "fail_visible",
    )
    if value not in allowed:
        raise ParseError(f"unknown lane: {value}")
    return cast(Lane, value)


def as_intent(value: str) -> Intent:
    allowed: Sequence[Intent] = (
        "chat",
        "retrieval",
        "extraction",
        "classification",
        "log_interpret",
        "tool_op",
        "coding",
    )
    if value not in allowed:
        raise ParseError(f"unknown intent: {value}")
    return cast(Intent, value)


def as_risk(value: str) -> RiskClass:
    allowed: Sequence[RiskClass] = ("low", "medium", "high")
    if value not in allowed:
        raise ParseError(f"unknown risk_class: {value}")
    return cast(RiskClass, value)


def as_action_kind(value: str) -> ActionKind:
    allowed: Sequence[ActionKind] = ("tool", "specialist", "direct_answer")
    if value not in allowed:
        raise ParseError(f"unknown action kind: {value}")
    return cast(ActionKind, value)
