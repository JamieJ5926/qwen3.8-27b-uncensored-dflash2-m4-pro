"""One-step dispatch through injected adapters. No network or subprocesses."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Protocol

from router.artifacts import ArtifactStore
from router.domain import (
    ActionDecision,
    CompactObservation,
    DecisionPacket,
    LaneDecision,
    ParseError,
    WorkerJob,
    WorkerResult,
)
from router.validate import parse_action_decision

Adapter = Callable[[WorkerJob], WorkerResult]


class TargetAdapter(Protocol):
    def __call__(self, job: WorkerJob) -> WorkerResult: ...


def idempotency_key(request_id: str, rule_id: str, packet_hash: str, step: int) -> str:
    return f"{request_id}:{rule_id}:{packet_hash}:{step}"


class Dispatcher:
    """Request-scoped dispatch. Follow-up budget is a counter, not durable workflow state."""

    def __init__(
        self,
        adapters: MutableMapping[str, Adapter],
        artifacts: ArtifactStore,
        cache: MutableMapping[str, WorkerResult] | None = None,
    ) -> None:
        self.adapters = adapters
        self.artifacts = artifacts
        self.cache: MutableMapping[str, WorkerResult] = cache if cache is not None else {}
        self.follow_up_used = 0
        self.step = 0

    def dispatch(
        self,
        packet: DecisionPacket,
        action: ActionDecision | str,
        decision: LaneDecision,
        *,
        wall_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tps: float = 0.0,
        isolation: str = "offline",
        max_tool_calls: int = 1,
    ) -> tuple[WorkerResult, CompactObservation]:
        parsed = (
            action
            if isinstance(action, ActionDecision)
            else parse_action_decision(action, packet)
        )
        if parsed.packet_hash != packet.packet_hash:
            raise ParseError("packet_hash does not match compiled packet")
        step = 1 + self.follow_up_used
        self.step = step
        key = idempotency_key(
            packet.request_id,
            decision.rule_id,
            packet.packet_hash,
            step,
        )
        cached = self.cache.get(key)
        if cached is not None:
            observation = self._observe(
                packet,
                cached,
                wall_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                tps=0.0,
                acceptance_summary="idempotent_reuse",
            )
            return cached, observation
        job = WorkerJob(
            job_id=key,
            lane_rule_id=decision.rule_id,
            action=parsed,
            packet=packet,
            timeout_s=decision.lane_budget.timeout_s,
            max_tool_calls=max_tool_calls,
            isolation=isolation,
            idempotency_key=key,
        )
        adapter = self._adapter(parsed)
        result = adapter(job)
        self.cache[key] = result
        summary = "accepted" if result.status == "ok" else f"rejected:{result.status}"
        observation = self._observe(
            packet,
            result,
            wall_ms=wall_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tps=tps,
            acceptance_summary=summary,
        )
        return result, observation

    def consume_follow_up(self, packet: DecisionPacket) -> None:
        budget = min(1, packet.follow_up_budget)
        if budget < 1:
            raise ParseError("follow-up budget is exhausted")
        if self.follow_up_used >= budget:
            raise ParseError("follow-up budget is exhausted")
        self.follow_up_used += 1

    def _adapter(self, action: ActionDecision) -> Adapter:
        if action.kind == "direct_answer":
            return self.adapters.get("direct_answer", _direct_answer)
        adapter = self.adapters.get(action.action_name) or self.adapters.get(action.kind)
        if adapter is None:
            raise ParseError(f"no adapter for action: {action.action_name}")
        return adapter

    def _observe(
        self,
        packet: DecisionPacket,
        result: WorkerResult,
        *,
        wall_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        tps: float,
        acceptance_summary: str,
    ) -> CompactObservation:
        extras = result.evidence
        if result.error:
            extras = extras + (result.error,)
        payload = "\n".join((result.answer, *extras))
        artifact_ref = self.artifacts.write(
            packet.request_id,
            f"step-{self.step}.txt",
            payload,
        )
        return CompactObservation(
            request_id=packet.request_id,
            step=self.step,
            wall_ms=wall_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tps=tps,
            acceptance_summary=acceptance_summary,
            artifact_ref=artifact_ref,
        )


def _direct_answer(job: WorkerJob) -> WorkerResult:
    return WorkerResult(
        job_id=job.job_id,
        status="ok",
        answer=job.action.answer,
        artifact_refs=(),
        tool_calls=(),
        evidence=(),
        confidence=job.action.confidence,
        error=None,
    )
