"""Transport contracts: URL, request translation, error mapping, usage."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from router.normalize import normalize_chat_body
from router.packet import compile_packet
from router.rules import load_route_table_path, resolve_lane
from router.transport import (
    DEFAULT_LFM_MODEL,
    DEFAULT_QWEN_BASE_URL,
    LFMTransport,
    QwenTransport,
    TransportHTTPError,
    TransportResponse,
    map_lfm_transport_error,
    map_transport_error,
)
from router.domain import LimitError, ParseError, RouterError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


def _packet():
    _, rules = load_route_table_path(FIXTURES / "default.json")
    body = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "def hello(): pass"}],
        "max_tokens": 128,
        "tools": [{"type": "function", "function": {"name": "lookup_log"}}],
        "metadata": {
            "intent": "coding",
            "risk_class": "high",
            "needs_qwen_judgment": True,
        },
    }
    envelope = normalize_chat_body(body, request_id="tr-1")
    decision = resolve_lane(envelope, rules)
    return compile_packet(envelope, decision)


def _lfm_envelope_decision():
    _, rules = load_route_table_path(FIXTURES / "default.json")
    body = {
        "model": "router",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128,
        "metadata": {
            "intent": "chat",
            "risk_class": "low",
            "needs_qwen_judgment": False,
        },
    }
    envelope = normalize_chat_body(body, request_id="lfm-tr-1")
    return envelope, resolve_lane(envelope, rules)


def _response(content="{}", prompt_tokens=7, completion_tokens=3):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "model": "qwen3.8-27b",
        }
    ).encode("utf-8")


class TransportUrlTests(unittest.TestCase):
    def test_default_base_url_is_8083(self) -> None:
        self.assertEqual(DEFAULT_QWEN_BASE_URL, "http://127.0.0.1:8083/v1")

    def test_endpoint_joins_chat_completions(self) -> None:
        captured = {}

        def http(url, body, timeout_s):
            captured["url"] = url
            return _response()

        QwenTransport(http=http, base_url="http://host.example:9999/v1").ask(_packet())
        self.assertEqual(
            captured["url"], "http://host.example:9999/v1/chat/completions"
        )


class TransportRequestTests(unittest.TestCase):
    def test_request_translation_carries_packet_hash(self) -> None:
        captured = {}

        def http(url, body, timeout_s):
            captured["body"] = json.loads(body)
            return _response()

        packet = _packet()
        QwenTransport(http=http).ask(packet)
        request = captured["body"]
        self.assertEqual(request["model"], "qwen3.8-27b")
        self.assertEqual(request["max_tokens"], packet.action_budget)
        self.assertEqual(request["temperature"], 0.0)
        self.assertIs(request["stream"], False)
        system = request["messages"][0]
        user = request["messages"][1]
        self.assertEqual(system["role"], "system")
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["content"], packet.goal)
        self.assertIn(packet.packet_hash, system["content"])
        self.assertIn('"allowed_actions"', system["content"])

    def test_follow_up_observation_reaches_user_content(self) -> None:
        captured = {}

        def http(url, body, timeout_s):
            captured["body"] = json.loads(body)
            return _response()

        QwenTransport(http=http).ask(_packet(), observation="step-1 failed")
        user = captured["body"]["messages"][1]
        self.assertIn("step-1 failed", user["content"])


class LFMTransportTests(unittest.TestCase):
    def test_request_translation_is_direct_chat(self) -> None:
        captured = {}

        def http(url, body, timeout_s):
            captured["url"] = url
            captured["body"] = json.loads(body)
            captured["timeout_s"] = timeout_s
            return _response("lfm answer", 9, 4)

        envelope, decision = _lfm_envelope_decision()
        response = LFMTransport(
            base_url="http://lfm.example:8090/v1", http=http
        ).chat(envelope, decision)
        self.assertEqual(
            captured["url"], "http://lfm.example:8090/v1/chat/completions"
        )
        self.assertEqual(captured["body"]["model"], DEFAULT_LFM_MODEL)
        self.assertEqual(captured["body"]["max_tokens"], 128)
        self.assertEqual(captured["body"]["messages"][-1]["content"], "hello")
        self.assertNotIn("tools", captured["body"])
        self.assertEqual(captured["timeout_s"], decision.lane_budget.timeout_s)
        self.assertEqual(response.content, "lfm answer")

    def test_base_url_is_required(self) -> None:
        with self.assertRaises(ValueError):
            LFMTransport(base_url="")

    def test_malformed_response_is_rejected(self) -> None:
        envelope, decision = _lfm_envelope_decision()
        transport = LFMTransport(
            base_url="http://lfm.invalid/v1",
            http=lambda url, body, timeout_s: b'{"ok": true}',
        )
        with self.assertRaises(ParseError):
            transport.chat(envelope, decision)


class TransportResponseTests(unittest.TestCase):
    def test_usage_extraction(self) -> None:
        transport = QwenTransport(http=lambda url, body, t: _response("x", 11, 5))
        response = transport.ask(_packet())
        self.assertEqual(
            (response.content, response.prompt_tokens, response.completion_tokens),
            ("x", 11, 5),
        )
        self.assertEqual(response.model, "qwen3.8-27b")

    def test_missing_choices_rejected(self) -> None:
        transport = QwenTransport(http=lambda url, body, t: b'{"ok": true}')
        with self.assertRaises(ParseError):
            transport.ask(_packet())

    def test_non_string_content_rejected(self) -> None:
        raw = json.dumps({"choices": [{"message": {"content": 5}}]}).encode()
        transport = QwenTransport(http=lambda url, body, t: raw)
        with self.assertRaises(ParseError):
            transport.ask(_packet())


class TransportErrorMappingTests(unittest.TestCase):
    def test_timeout_maps_to_limit_error(self) -> None:
        def http(url, body, timeout_s):
            raise TimeoutError()

        transport = QwenTransport(http=http)
        with self.assertRaises(TimeoutError):
            transport.ask(_packet())
        mapped = map_transport_error(TimeoutError(), timeout_s=5.0)
        self.assertIsInstance(mapped, LimitError)
        self.assertIn("5", mapped.message)

    def test_connection_error_maps_to_service_unavailable(self) -> None:
        error = urllib.error.URLError(OSError("refused"))
        mapped = map_transport_error(error, timeout_s=5.0)
        self.assertEqual((mapped.code, mapped.http_status), ("service_unavailable", 503))

    def test_http_error_maps_to_upstream_error(self) -> None:
        http_error = urllib.error.HTTPError("u", 500, "boom", None, None)
        try:
            for error in (TransportHTTPError(500, "boom"), http_error):
                mapped = map_transport_error(error, timeout_s=5.0)
                self.assertEqual(
                    (mapped.code, mapped.http_status), ("upstream_error", 502)
                )
        finally:
            http_error.close()

    def test_qwen_error_messages_remain_stable(self) -> None:
        self.assertEqual(
            str(TransportHTTPError(500, "boom")), "qwen http 500: boom"
        )
        self.assertEqual(
            map_transport_error(
                TransportHTTPError(500, "boom"), timeout_s=5.0
            ).message,
            "qwen http 500: boom",
        )
        self.assertEqual(
            map_transport_error(TimeoutError(), timeout_s=5.0).message,
            "qwen transport timeout after 5.0s",
        )

    def test_lfm_error_messages_are_isolated(self) -> None:
        self.assertEqual(
            map_lfm_transport_error(
                TransportHTTPError(500, "boom"), timeout_s=5.0
            ).message,
            "lfm http 500: boom",
        )
        self.assertEqual(
            map_lfm_transport_error(TimeoutError(), timeout_s=5.0).message,
            "lfm transport timeout after 5.0s",
        )

    def test_malformed_json_maps_to_parse_error(self) -> None:
        try:
            json.loads("not json")
        except json.JSONDecodeError as exc:
            mapped = map_transport_error(exc, timeout_s=5.0)
            self.assertIsInstance(mapped, ParseError)
        else:
            self.fail("expected JSONDecodeError")

    def test_router_errors_pass_through(self) -> None:
        original = ParseError("kept")
        self.assertIs(map_transport_error(original, timeout_s=5.0), original)

    def test_unknown_error_wraps_internal(self) -> None:
        mapped = map_transport_error(ValueError("x"), timeout_s=5.0)
        self.assertEqual((mapped.code, mapped.http_status), ("internal_error", 500))


if __name__ == "__main__":
    unittest.main()
