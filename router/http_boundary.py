"""Stdlib HTTP front door. Delegates chat handling. Never starts a model."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from uuid import uuid4
from wsgiref.simple_server import make_server

from router.domain import FinalResponse, RouterError, RuntimeConfig
from router.normalize import normalize_chat_body
from router.rules import resolve_lane

HandleChat = Callable[..., FinalResponse]


def openai_error(status: int, code: str, message: str) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps({"error": {"message": message, "type": code, "code": code}}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    return status, headers, body


def map_error(exc: BaseException) -> tuple[int, dict[str, str], bytes]:
    if isinstance(exc, RouterError):
        payload = json.dumps(exc.openai_body()).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
        return exc.http_status, headers, payload
    return openai_error(500, "internal_error", "internal router error")


def final_to_openai(response: FinalResponse, *, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{response.request_id}",
        "object": "chat.completion",
        "model": model or "router",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.answer},
                "finish_reason": "stop" if response.status == "ok" else "error",
            }
        ],
        "usage": {
            "prompt_tokens": int(response.usage.get("prompt_tokens", 0)),
            "completion_tokens": int(response.usage.get("completion_tokens", 0)),
            "total_tokens": int(response.usage.get("total_tokens", 0)),
        },
        "status": response.status,
        "artifact_refs": list(response.artifact_refs),
    }


def handle_chat_request(
    body: Mapping[str, Any] | str | bytes,
    *,
    handle_chat: HandleChat,
    config: RuntimeConfig,
    rules: Any,
    request_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    rid = request_id or str(uuid4())
    try:
        envelope = normalize_chat_body(body, request_id=rid)
        decision = resolve_lane(envelope, rules)
        result = handle_chat(envelope, decision, config)
        payload = json.dumps(final_to_openai(result, model=envelope.model)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Route-Trace": decision.rule_id,
            "X-Trace-Id": envelope.request_id,
            "X-Lane": decision.lane,
        }
        return 200, headers, payload
    except Exception as exc:  # map_error classifies
        status, headers, payload = map_error(exc)
        headers = dict(headers)
        headers["X-Trace-Id"] = rid
        return status, headers, payload


def make_wsgi_app(
    *,
    handle_chat: HandleChat,
    config: RuntimeConfig,
    rules: Any,
) -> Callable:
    def app(environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        if method == "GET" and path == "/health":
            body = b'{"status":"ok"}'
            start_response(
                "200 OK",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        if method != "POST" or path != "/v1/chat/completions":
            status, headers, payload = openai_error(404, "not_found", "unknown path")
            start_response(f"{status} ERR", list(headers.items()))
            return [payload]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b"{}"
        status, headers, payload = handle_chat_request(
            raw, handle_chat=handle_chat, config=config, rules=rules
        )
        start_response(f"{status} X", list(headers.items()))
        return [payload]

    return app


class RouterHTTPServer:
    """Explicit construction only. No module-level default bind."""

    def __init__(
        self,
        *,
        handle_chat: HandleChat,
        config: RuntimeConfig,
        rules: Any,
        host: str,
        port: int,
    ) -> None:
        self._app = make_wsgi_app(handle_chat=handle_chat, config=config, rules=rules)
        self.host = host
        self.port = port
        self._httpd = None

    def serve_forever(self) -> None:
        self._httpd = make_server(self.host, self.port, self._app)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
