"""Lane-owned coordinator composition with durable replay."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, MutableMapping
from dataclasses import asdict
from typing import Any

from router.artifacts import ArtifactStore
from router.domain import (
    ConversationEnvelope,
    DecisionPacket,
    FinalResponse,
    LaneDecision,
    NoRouteError,
    ParseError,
    RouterError,
    RuntimeConfig,
    WorkerResult,
)
from router.packet import canonical_dumps, compile_packet, packet_body
from router.targets import Adapter, Dispatcher
from router.transport import (
    LFMTransport,
    QwenTransport,
    map_lfm_transport_error,
    map_transport_error,
)
from router.validate import parse_action_decision, result_rejection

Clock = Callable[[], float]
FixedWorkerAdapter = Callable[
    [ConversationEnvelope, LaneDecision, RuntimeConfig], WorkerResult
]
LaneHandler = Callable[
    [ConversationEnvelope, LaneDecision, RuntimeConfig, DecisionPacket], FinalResponse
]


class Coordinator:
    """Builds the HandleChat seam and dispatches each lane to its owner."""

    def __init__(
        self,
        *,
        transport: QwenTransport,
        artifacts: ArtifactStore,
        lfm_transport: LFMTransport | None = None,
        fixed_workers: MutableMapping[str, FixedWorkerAdapter] | None = None,
        adapters: MutableMapping[str, Adapter] | None = None,
        cache: MutableMapping[str, WorkerResult] | None = None,
        clock: Clock = time.monotonic,
        min_confidence: float = 0.0,
    ) -> None:
        if isinstance(min_confidence, bool) or not isinstance(
            min_confidence, (int, float)
        ):
            raise ValueError("min_confidence must be a number")
        if not 0.0 <= float(min_confidence) <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self.transport = transport
        self.lfm_transport = lfm_transport
        self.artifacts = artifacts
        self.fixed_workers = fixed_workers if fixed_workers is not None else {}
        self.adapters = adapters if adapters is not None else {}
        self.cache = cache if cache is not None else {}
        self.clock = clock
        self.min_confidence = float(min_confidence)
        self._handlers: dict[str, LaneHandler] = {
            "qwen_coordinator": self._handle_qwen,
            "lfm_direct": self._handle_lfm,
            "fixed_worker": self._handle_fixed_worker,
        }

    def __call__(
        self,
        envelope: ConversationEnvelope,
        decision: LaneDecision,
        config: RuntimeConfig,
    ) -> FinalResponse:
        rid = envelope.request_id
        self._write(rid, "route.json", canonical_dumps(asdict(decision)))
        try:
            if decision.lane == "fail_visible":
                raise NoRouteError(f"route {decision.rule_id} selected fail_visible")
            packet = compile_packet(envelope, decision)
            self._write(rid, "packet.json", canonical_dumps(packet_body(packet)))
            stored = _stored_final(self.artifacts, rid, packet, decision)
            if stored is not None:
                return stored
            handler = self._handlers.get(decision.lane)
            if handler is None:
                raise NoRouteError(f"no handler for lane {decision.lane}")
            return handler(envelope, decision, config, packet)
        except BaseException as exc:
            self._write(rid, "error.txt", _error_text(exc))
            raise

    def _handle_qwen(
        self,
        envelope: ConversationEnvelope,
        decision: LaneDecision,
        config: RuntimeConfig,
        packet: DecisionPacket,
    ) -> FinalResponse:
        dispatcher = Dispatcher(self.adapters, self.artifacts, cache=self.cache)
        result, trace = self._execute_qwen(
            envelope.request_id, packet, decision, dispatcher
        )
        return self._finalize(
            envelope,
            decision,
            packet,
            answer=result.answer,
            refs=tuple(trace["refs"]),
            citations=tuple(result.evidence),
            action_trace={"steps": trace["actions"]},
            usage=trace["usage"],
            timing=tuple(trace["timing"]),
            status="ok" if trace["final_rejection"] is None else "failed",
        )

    def _execute_qwen(
        self,
        rid: str,
        packet: DecisionPacket,
        decision: LaneDecision,
        dispatcher: Dispatcher,
    ) -> tuple[WorkerResult, dict[str, Any]]:
        steps: list[int] = []
        actions: list[dict[str, Any]] = []
        timing: list[dict[str, Any]] = []
        refs: list[str] = [
            self.artifacts.ref(rid, "route.json"),
            self.artifacts.ref(rid, "packet.json"),
        ]
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        observation_text = ""
        step = 1
        while True:
            started = self.clock()
            try:
                response = self.transport.ask(packet, observation=observation_text)
            except BaseException as exc:
                raise map_transport_error(
                    exc, timeout_s=self.transport.timeout_s
                ) from exc
            wall_ms = int((self.clock() - started) * 1000)
            action = parse_action_decision(response.content, packet)
            self._write(rid, f"action-{step}.json", canonical_dumps(asdict(action)))
            result, observation = dispatcher.dispatch(
                packet,
                action,
                decision,
                wall_ms=wall_ms,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            usage["prompt_tokens"] += response.prompt_tokens
            usage["completion_tokens"] += response.completion_tokens
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            steps.append(step)
            rejection = result_rejection(result, min_confidence=self.min_confidence)
            actions.append(
                {
                    "step": step,
                    "kind": action.kind,
                    "action_name": action.action_name,
                    "status": result.status,
                    "rejection": rejection,
                }
            )
            timing.append({"step": step, "wall_ms": wall_ms})
            refs.append(self.artifacts.ref(rid, f"action-{step}.json"))
            refs.append(observation.artifact_ref)
            refs.extend(result.artifact_refs)
            if rejection is None:
                break
            if dispatcher.follow_up_used >= min(1, packet.follow_up_budget):
                break
            dispatcher.consume_follow_up(packet)
            observation_text = self.artifacts.read(observation.artifact_ref)
            step += 1
        trace = {
            "steps": steps,
            "actions": actions,
            "timing": timing,
            "usage": usage,
            "refs": refs,
            "final_rejection": rejection,
        }
        return result, trace

    def _handle_lfm(
        self,
        envelope: ConversationEnvelope,
        decision: LaneDecision,
        config: RuntimeConfig,
        packet: DecisionPacket,
    ) -> FinalResponse:
        if self.lfm_transport is None:
            raise RouterError(
                "service_unavailable", "lfm transport is not configured", 503
            )
        started = self.clock()
        try:
            response = self.lfm_transport.chat(envelope, decision)
        except BaseException as exc:
            raise map_lfm_transport_error(
                exc, timeout_s=self.lfm_transport.timeout_s
            ) from exc
        wall_ms = int((self.clock() - started) * 1000)
        answer = response.content.strip()
        if not answer:
            raise ParseError("lfm response content is empty")
        rid = envelope.request_id
        step_ref = self.artifacts.write(rid, "step-1.txt", answer)
        usage = {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.prompt_tokens + response.completion_tokens,
        }
        return self._finalize(
            envelope,
            decision,
            packet,
            answer=answer,
            refs=(
                self.artifacts.ref(rid, "route.json"),
                self.artifacts.ref(rid, "packet.json"),
                step_ref,
            ),
            citations=(),
            action_trace={
                "steps": [
                    {
                        "step": 1,
                        "kind": "direct_answer",
                        "action_name": "lfm",
                        "status": "ok",
                        "rejection": None,
                        "model": response.model,
                    }
                ]
            },
            usage=usage,
            timing=({"step": 1, "wall_ms": wall_ms},),
            status="ok",
        )

    def _handle_fixed_worker(
        self,
        envelope: ConversationEnvelope,
        decision: LaneDecision,
        config: RuntimeConfig,
        packet: DecisionPacket,
    ) -> FinalResponse:
        adapter = self.fixed_workers.get(decision.rule_id)
        if adapter is None:
            raise RouterError(
                "worker_unavailable",
                f"no fixed worker for route {decision.rule_id}",
                503,
            )
        started = self.clock()
        result = adapter(envelope, decision, config)
        wall_ms = int((self.clock() - started) * 1000)
        rejection = result_rejection(result, min_confidence=self.min_confidence)
        rid = envelope.request_id
        payload = "\n".join(
            item for item in (result.answer, *result.evidence, result.error or "") if item
        )
        step_ref = self.artifacts.write(rid, "step-1.txt", payload)
        return self._finalize(
            envelope,
            decision,
            packet,
            answer=result.answer,
            refs=(
                self.artifacts.ref(rid, "route.json"),
                self.artifacts.ref(rid, "packet.json"),
                step_ref,
                *result.artifact_refs,
            ),
            citations=tuple(result.evidence),
            action_trace={
                "steps": [
                    {
                        "step": 1,
                        "kind": "fixed_worker",
                        "action_name": decision.rule_id,
                        "status": result.status,
                        "rejection": rejection,
                    }
                ]
            },
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            timing=({"step": 1, "wall_ms": wall_ms},),
            status="ok" if rejection is None else "failed",
        )

    def _finalize(
        self,
        envelope: ConversationEnvelope,
        decision: LaneDecision,
        packet: DecisionPacket,
        *,
        answer: str,
        refs: tuple[str, ...],
        citations: tuple[str, ...],
        action_trace: dict[str, Any],
        usage: dict[str, int],
        timing: tuple[dict[str, Any], ...],
        status: str,
    ) -> FinalResponse:
        final = FinalResponse(
            request_id=envelope.request_id,
            answer=answer,
            artifact_refs=refs,
            citations=citations,
            lane_trace={
                "rule_id": decision.rule_id,
                "rule_version": decision.rule_version,
                "lane": decision.lane,
                "packet_hash": packet.packet_hash,
                "steps": [item["step"] for item in timing],
            },
            action_trace=action_trace,
            usage=usage,
            status=status,
        )
        rid = envelope.request_id
        self._write(rid, "timing.json", canonical_dumps({"steps": timing}))
        self._write(rid, "usage.json", canonical_dumps(usage))
        self._write(rid, "result.json", canonical_dumps(asdict(final)))
        return final

    def _write(self, rid: str, name: str, payload: str) -> None:
        self.artifacts.write(rid, name, payload)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, RouterError):
        return f"{exc.code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


def replay_final_response(artifacts: ArtifactStore, request_id: str) -> FinalResponse:
    raw = artifacts.read(artifacts.ref(request_id, "result.json"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"malformed result artifact: {exc}") from exc
    return _final_from_record(data)


def _stored_final(
    artifacts: ArtifactStore,
    rid: str,
    packet: DecisionPacket,
    decision: LaneDecision,
) -> FinalResponse | None:
    """Replay matching append-only results and reject divergent identities."""

    try:
        raw = artifacts.read(artifacts.ref(rid, "result.json"))
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ParseError(f"stored result for {rid} is malformed: {exc}") from exc
    try:
        lane_trace = data["lane_trace"]
        matches = (
            lane_trace["packet_hash"] == packet.packet_hash
            and lane_trace["rule_id"] == decision.rule_id
            and lane_trace["rule_version"] == decision.rule_version
        )
    except (KeyError, TypeError) as exc:
        raise ParseError(f"stored result for {rid} lacks identity fields") from exc
    if not matches:
        raise ParseError(
            f"request id {rid} already completed with a different packet, rule, or version"
        )
    return _final_from_record(data)


def _final_from_record(data: dict[str, Any]) -> FinalResponse:
    return FinalResponse(
        request_id=data["request_id"],
        answer=data["answer"],
        artifact_refs=tuple(data["artifact_refs"]),
        citations=tuple(data["citations"]),
        lane_trace=data["lane_trace"],
        action_trace=data["action_trace"],
        usage=data["usage"],
        status=data["status"],
    )
