"""Injected OpenAI-compatible transports for Qwen actions and direct LFM chat."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from router.domain import (
    ConversationEnvelope,
    DecisionPacket,
    LaneDecision,
    LimitError,
    ParseError,
    RouterError,
)
from router.packet import canonical_dumps, packet_body

DEFAULT_QWEN_BASE_URL = "http://127.0.0.1:8083/v1"
QWEN_BASE_URL_ENV = "QWEN_BASE_URL"
DEFAULT_QWEN_MODEL = "qwen3.8-27b"
DEFAULT_LFM_MODEL = "lfm-heretic"
LFM_BASE_URL_ENV = "LFM_BASE_URL"
LFM_MODEL_ENV = "LFM_MODEL"
DEFAULT_TIMEOUT_S = 60.0

HTTPPost = Callable[[str, bytes, float], bytes]


class TransportHTTPError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"qwen http {status}: {body}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class TransportResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str


TransportErrorMaker = Callable[[BaseException, float, str], RouterError]

_ERROR_RULES: tuple[tuple[type[BaseException], TransportErrorMaker], ...] = (
    (
        TransportHTTPError,
        lambda exc, timeout_s, backend: RouterError(
            "upstream_error", f"{backend} http {exc.status}: {exc.body}", 502
        ),
    ),
    (
        urllib.error.HTTPError,
        lambda exc, timeout_s, backend: RouterError(
            "upstream_error", f"{backend} http {exc.code}", 502
        ),
    ),
    (
        TimeoutError,
        lambda exc, timeout_s, backend: LimitError(
            f"{backend} transport timeout after {timeout_s}s"
        ),
    ),
    (
        json.JSONDecodeError,
        lambda exc, timeout_s, backend: ParseError(
            f"malformed {backend} json: {exc}"
        ),
    ),
    (
        urllib.error.URLError,
        lambda exc, timeout_s, backend: RouterError(
            "service_unavailable", f"{backend} unreachable: {exc.reason}", 503
        ),
    ),
)


def map_transport_error(exc: BaseException, *, timeout_s: float) -> RouterError:
    return _map_transport_error(exc, timeout_s=timeout_s, backend="qwen")


def map_lfm_transport_error(exc: BaseException, *, timeout_s: float) -> RouterError:
    return _map_transport_error(exc, timeout_s=timeout_s, backend="lfm")


def _map_transport_error(
    exc: BaseException, *, timeout_s: float, backend: str
) -> RouterError:
    if isinstance(exc, RouterError):
        return exc
    for kind, make in _ERROR_RULES:
        if isinstance(exc, kind):
            return make(exc, timeout_s, backend)
    return RouterError("internal_error", f"{backend} transport failed: {exc}", 500)


def _urllib_post(url: str, body: bytes, timeout_s: float) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


_QWEN_SYSTEM_PROMPT = (
    "You are the Qwen coordinator. Choose exactly one action from the "
    "allowed_actions of the packet. Reply with a single JSON object with "
    "keys kind, action_name, arguments, answer, confidence, packet_hash, "
    "action_budget, follow_up_budget. Echo packet_hash exactly. No other text."
)

_LFM_SYSTEM_PROMPT = (
    "Answer the user's request directly. Do not choose an execution lane or "
    "invoke a tool. Return only the answer."
)


class QwenTransport:
    """Action-selection adapter for one Qwen server."""

    def __init__(
        self,
        *,
        http: HTTPPost | None = None,
        base_url: str = DEFAULT_QWEN_BASE_URL,
        model: str = DEFAULT_QWEN_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._http = http if http is not None else _urllib_post
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def ask(self, packet: DecisionPacket, *, observation: str = "") -> TransportResponse:
        packet_envelope = {**packet_body(packet), "packet_hash": packet.packet_hash}
        user_content = packet.goal
        if observation:
            user_content = (
                f"{packet.goal}\n\nObservation from previous step:\n{observation}"
            )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{_QWEN_SYSTEM_PROMPT}\n\n{canonical_dumps(packet_envelope)}",
                },
                {"role": "user", "content": user_content},
            ],
            "max_tokens": packet.action_budget,
            "temperature": 0.0,
            "stream": False,
        }
        raw = self._http(
            f"{self.base_url}/chat/completions",
            canonical_dumps(payload).encode("utf-8"),
            self.timeout_s,
        )
        return _parse_chat_response(raw, "qwen")


class LFMTransport:
    """Direct-chat adapter for one explicitly configured LFM endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        http: HTTPPost | None = None,
        model: str = DEFAULT_LFM_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not base_url:
            raise ValueError("lfm base_url is required")
        self._http = http if http is not None else _urllib_post
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def chat(
        self, envelope: ConversationEnvelope, decision: LaneDecision
    ) -> TransportResponse:
        messages = [
            {"role": "system", "content": _LFM_SYSTEM_PROMPT},
            *[
                {"role": message.role, "content": message.content}
                for message in envelope.messages
            ],
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": min(
                envelope.budgets.output_tokens,
                decision.lane_budget.output_tokens,
            ),
            "temperature": 0.0,
            "stream": False,
        }
        timeout_s = min(self.timeout_s, decision.lane_budget.timeout_s)
        raw = self._http(
            f"{self.base_url}/chat/completions",
            canonical_dumps(payload).encode("utf-8"),
            timeout_s,
        )
        return _parse_chat_response(raw, "lfm")


def _parse_chat_response(raw: bytes, backend: str) -> TransportResponse:
    data = json.loads(raw.decode("utf-8"))
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ParseError(f"{backend} response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ParseError(f"{backend} response has no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ParseError(f"{backend} response content is not a string")
    usage = data.get("usage")
    usage_map = usage if isinstance(usage, Mapping) else {}
    return TransportResponse(
        content=content,
        prompt_tokens=_as_int(usage_map.get("prompt_tokens")),
        completion_tokens=_as_int(usage_map.get("completion_tokens")),
        model=str(data.get("model", "")),
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
