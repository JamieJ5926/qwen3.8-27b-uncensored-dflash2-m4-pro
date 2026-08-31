"""Parse OpenAI chat bodies into ConversationEnvelope. No model loading."""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from router.domain import (
    ChatMessage,
    ConversationEnvelope,
    Intent,
    LaneBudget,
    LimitError,
    ParseError,
    RiskClass,
    as_intent,
    as_risk,
)

HARD_MAX_MESSAGES = 64
HARD_MAX_CONTENT_CHARS = 32_768
HARD_MAX_TOKENS = 8_192
HARD_MAX_OUTPUT_TOKENS = 2_048
HARD_TIMEOUT_S = 120.0
DEFAULT_PROFILE = "full_pi"


def _as_mapping(body: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError("request body is not utf-8") from exc
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ParseError("request body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ParseError("request body must be a JSON object")
        return parsed
    if isinstance(body, dict):
        return dict(body)
    raise ParseError("request body must be a JSON object")


def _message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                raise ParseError("unsupported message content part")
        return "".join(parts)
    raise ParseError("message content must be a string or content parts")


def _parse_messages(raw: Any) -> tuple[ChatMessage, ...]:
    if not isinstance(raw, list) or not raw:
        raise ParseError("messages must be a non-empty array")
    if len(raw) > HARD_MAX_MESSAGES:
        raise LimitError(f"messages exceed hard limit of {HARD_MAX_MESSAGES}")
    out: list[ChatMessage] = []
    total_chars = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ParseError("each message must be an object")
        role = item.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise ParseError(f"invalid message role: {role}")
        text = _message_content(item.get("content"))
        total_chars += len(text)
        if total_chars > HARD_MAX_CONTENT_CHARS:
            raise LimitError(f"content exceeds hard limit of {HARD_MAX_CONTENT_CHARS} chars")
        out.append(ChatMessage(role=str(role), content=text))
    return tuple(out)


def _approx_tokens(messages: tuple[ChatMessage, ...]) -> int:
    chars = sum(len(m.content) for m in messages)
    return max(1, (chars + 3) // 4)


def _metadata_intent(meta: Mapping[str, Any], messages: tuple[ChatMessage, ...]) -> Intent:
    if "intent" in meta:
        return as_intent(str(meta["intent"]))
    joined = " ".join(m.content.lower() for m in messages)
    if any(word in joined for word in ("def ", "class ", "traceback", "compile error")):
        return "coding"
    if "extract" in joined:
        return "extraction"
    if "classify" in joined:
        return "classification"
    if "log" in joined and ("error" in joined or "warn" in joined):
        return "log_interpret"
    if "search" in joined or "retrieve" in joined:
        return "retrieval"
    if "tool" in joined or "call function" in joined:
        return "tool_op"
    return "chat"


def _metadata_risk(meta: Mapping[str, Any], intent: Intent) -> RiskClass:
    if "risk_class" in meta:
        return as_risk(str(meta["risk_class"]))
    if intent in ("coding", "tool_op"):
        return "high"
    if intent in ("retrieval", "log_interpret"):
        return "medium"
    return "low"


def normalize_chat_body(
    body: Mapping[str, Any] | str | bytes,
    *,
    request_id: str | None = None,
) -> ConversationEnvelope:
    data = _as_mapping(body)
    if "messages" not in data:
        raise ParseError("messages is required")
    messages = _parse_messages(data["messages"])
    meta_raw = data.get("metadata") or {}
    if meta_raw is None:
        meta_raw = {}
    if not isinstance(meta_raw, dict):
        raise ParseError("metadata must be an object")
    meta = {str(k): str(v) if not isinstance(v, (dict, list)) else json.dumps(v) for k, v in meta_raw.items()}
    intent = _metadata_intent(meta_raw, messages)
    risk = _metadata_risk(meta_raw, intent)
    context_tokens = _approx_tokens(messages)
    if context_tokens > HARD_MAX_TOKENS:
        raise LimitError(f"context exceeds hard limit of {HARD_MAX_TOKENS} tokens")
    max_tokens = data.get("max_tokens", HARD_MAX_OUTPUT_TOKENS)
    try:
        max_tokens_i = int(max_tokens)
    except (TypeError, ValueError) as exc:
        raise ParseError("max_tokens must be an integer") from exc
    if max_tokens_i < 1:
        raise ParseError("max_tokens must be >= 1")
    output_tokens = min(max_tokens_i, HARD_MAX_OUTPUT_TOKENS)
    timeout_s = float(meta_raw.get("timeout_s", HARD_TIMEOUT_S))
    if timeout_s <= 0 or timeout_s > HARD_TIMEOUT_S:
        timeout_s = HARD_TIMEOUT_S
    tools = data.get("tools") or []
    tool_names: list[str] = []
    if tools:
        if not isinstance(tools, list):
            raise ParseError("tools must be an array")
        for tool in tools:
            if isinstance(tool, dict):
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    tool_names.append(str(name))
    stream = bool(data.get("stream", False))
    needs = meta_raw.get("needs_qwen_judgment")
    if needs is None:
        needs_qwen = intent in ("coding", "tool_op", "log_interpret") or bool(tool_names)
    elif isinstance(needs, bool):
        needs_qwen = needs
    elif isinstance(needs, str) and needs in ("true", "false"):
        needs_qwen = needs == "true"
    else:
        raise ParseError("needs_qwen_judgment must be a boolean")
    rid = request_id or str(data.get("request_id") or uuid.uuid4())
    task_id = str(meta_raw.get("task_id") or rid)
    profile = str(data.get("profile") or meta_raw.get("profile") or DEFAULT_PROFILE)
    return ConversationEnvelope(
        request_id=rid,
        task_id=task_id,
        messages=messages,
        profile=profile,
        budgets=LaneBudget(
            input_tokens=HARD_MAX_TOKENS,
            output_tokens=output_tokens,
            timeout_s=timeout_s,
            follow_up_budget=0,
        ),
        available_tools=tuple(tool_names),
        intent=intent,
        risk_class=risk,
        context_token_count=context_tokens,
        needs_qwen_judgment=needs_qwen,
        stream=stream,
        model=str(data.get("model") or ""),
        metadata=meta,
    )
