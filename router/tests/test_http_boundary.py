import json
import unittest
from io import BytesIO

from router.domain import (
    ConversationEnvelope,
    FinalResponse,
    LaneDecision,
    RuntimeConfig,
)
from router.http_boundary import handle_chat_request, make_wsgi_app, map_error
from router.normalize import ParseError
from router.rules import load_route_table_path
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


def _echo(envelope: ConversationEnvelope, decision: LaneDecision, config: RuntimeConfig) -> FinalResponse:
    return FinalResponse(
        request_id=envelope.request_id,
        answer=f"lane={decision.lane}",
        artifact_refs=(),
        citations=(),
        lane_trace={"rule_id": decision.rule_id, "lane": decision.lane},
        action_trace={},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        status="ok",
    )


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RuntimeConfig(
            qwen_base_url="http://127.0.0.1:8083/v1",
            lfm_base_url="http://example.invalid/v1",
        )
        _, self.rules = load_route_table_path(FIXTURES / "default.json")

    def test_openai_success_and_trace_headers(self) -> None:
        status, headers, payload = handle_chat_request(
            {
                "model": "qwen3.8-27b",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
            handle_chat=_echo,
            config=self.config,
            rules=self.rules,
            request_id="trace-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Trace-Id"], "trace-1")
        self.assertEqual(headers["X-Route-Trace"], "tiny-chat-lfm")
        self.assertEqual(headers["X-Lane"], "lfm_direct")
        body = json.loads(payload)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["role"], "assistant")
        self.assertIn("lane=lfm_direct", body["choices"][0]["message"]["content"])

    def test_malformed_json_maps_400(self) -> None:
        status, headers, payload = handle_chat_request(
            "{",
            handle_chat=_echo,
            config=self.config,
            rules=self.rules,
        )
        self.assertEqual(status, 400)
        body = json.loads(payload)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("Content-Type", headers)

    def test_map_error_passthrough(self) -> None:
        status, _, payload = map_error(ParseError("nope"))
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_request_error", payload)

    def test_wsgi_unknown_path(self) -> None:
        app = make_wsgi_app(handle_chat=_echo, config=self.config, rules=self.rules)
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/nope",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        }
        captured = {}

        def start(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        result = b"".join(app(environ, start))
        self.assertTrue(captured["status"].startswith("404"))
        self.assertIn(b"not_found", result)

    def test_wsgi_chat_path(self) -> None:
        app = make_wsgi_app(handle_chat=_echo, config=self.config, rules=self.rules)
        raw = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode()
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/chat/completions",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": BytesIO(raw),
        }
        captured = {}

        def start(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        result = b"".join(app(environ, start))
        self.assertTrue(captured["status"].startswith("200"))
        self.assertIn("X-Route-Trace", captured["headers"])
        self.assertIn(b"chat.completion", result)

    def test_no_default_startup_config_required(self) -> None:
        from router import RuntimeConfig as Exported

        self.assertIs(Exported, RuntimeConfig)


if __name__ == "__main__":
    unittest.main()
