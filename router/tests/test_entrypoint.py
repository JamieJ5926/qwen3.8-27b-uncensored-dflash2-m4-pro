"""Entrypoint contracts: URL precedence, replay CLI, pre-bind rule validation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from router.__main__ import (
    DEFAULT_PORT,
    build_handle_chat,
    resolve_lfm_base_url,
    resolve_lfm_model,
    resolve_qwen_base_url,
)
from router.artifacts import ArtifactStore
from router.coordinator import Coordinator
from router.domain import RouterError, RuntimeConfig
from router.http_boundary import handle_chat_request
from router.normalize import normalize_chat_body
from router.packet import compile_packet
from router.rules import load_route_table_path, resolve_lane
from router.transport import TransportResponse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"

BODY = {
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

LFM_BODY = {
    "model": "router",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 128,
    "metadata": {
        "intent": "chat",
        "risk_class": "low",
        "needs_qwen_judgment": False,
    },
}


class FakeTransport:
    def __init__(self, content=None):
        self.content = content
        self.calls = []
        self.base_url = "http://127.0.0.1:8083/v1"
        self.model = "qwen3.8-27b"
        self.timeout_s = 60.0

    def ask(self, packet, *, observation=""):
        self.calls.append(observation)
        if self.content is None:
            raise AssertionError("qwen transport called")
        return TransportResponse(
            content=self.content,
            prompt_tokens=3,
            completion_tokens=2,
            model="qwen3.8-27b",
        )


class FakeLFMTransport:
    def __init__(self, content="entrypoint lfm answer"):
        self.content = content
        self.calls = []
        self.timeout_s = 60.0

    def chat(self, envelope, decision):
        self.calls.append((envelope.request_id, decision.rule_id))
        return TransportResponse(
            content=self.content,
            prompt_tokens=5,
            completion_tokens=3,
            model="lfm-heretic",
        )


class ResolveUrlTests(unittest.TestCase):
    def test_explicit_overrides_env_and_default(self) -> None:
        resolved = resolve_qwen_base_url(
            "http://explicit:1/v1", {"QWEN_BASE_URL": "http://env:2/v1"}
        )
        self.assertEqual(resolved, "http://explicit:1/v1")

    def test_env_overrides_default(self) -> None:
        resolved = resolve_qwen_base_url(None, {"QWEN_BASE_URL": "http://env:2/v1"})
        self.assertEqual(resolved, "http://env:2/v1")

    def test_default_is_8083(self) -> None:
        self.assertEqual(
            resolve_qwen_base_url(None, {}), "http://127.0.0.1:8083/v1"
        )

    def test_lfm_precedence_and_model_default(self) -> None:
        self.assertEqual(
            resolve_lfm_base_url(
                "http://explicit:8090/v1",
                {"LFM_BASE_URL": "http://env:8091/v1"},
            ),
            "http://explicit:8090/v1",
        )
        self.assertEqual(
            resolve_lfm_base_url(None, {"LFM_BASE_URL": "http://env:8091/v1"}),
            "http://env:8091/v1",
        )
        self.assertEqual(resolve_lfm_base_url(None, {}), "")
        self.assertEqual(resolve_lfm_model(None, {}), "lfm-heretic")


class ReplayCliTests(unittest.TestCase):
    def test_replay_prints_stored_result(self) -> None:
        from router.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            _, rules = load_route_table_path(FIXTURES / "default.json")
            envelope = normalize_chat_body(BODY, request_id="cli-1")
            decision = resolve_lane(envelope, rules)
            packet = compile_packet(envelope, decision)
            content = json.dumps(
                {
                    "kind": "direct_answer",
                    "action_name": "direct_answer",
                    "arguments": {},
                    "answer": "cli answer",
                    "confidence": 0.9,
                    "packet_hash": packet.packet_hash,
                    "action_budget": packet.action_budget,
                    "follow_up_budget": 0,
                }
            )
            Coordinator(
                transport=FakeTransport(content), artifacts=store
            )(envelope, decision, RuntimeConfig(qwen_base_url="u", lfm_base_url=""))
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    ["replay", "--artifacts", tmp, "--request-id", "cli-1"]
                )
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["id"], "chatcmpl-cli-1")
            self.assertEqual(payload["choices"][0]["message"]["content"], "cli answer")
            self.assertEqual(
                payload["usage"],
                {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )

    def test_replay_missing_request_fails_closed(self) -> None:
        from router.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    ["replay", "--artifacts", tmp, "--request-id", "nope"]
                )
            self.assertEqual(code, 1)


class EntryPointCompositionTests(unittest.TestCase):
    def test_fake_lfm_handle_path_without_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, rules = load_route_table_path(FIXTURES / "default.json")
            config = RuntimeConfig(
                qwen_base_url="http://qwen.invalid/v1",
                lfm_base_url="http://lfm.invalid/v1",
            )
            qwen = FakeTransport()
            lfm = FakeLFMTransport()
            handle_chat = build_handle_chat(
                config=config,
                artifacts=ArtifactStore(Path(tmp)),
                qwen_transport=qwen,
                lfm_transport=lfm,
            )
            status, headers, payload = handle_chat_request(
                LFM_BODY,
                handle_chat=handle_chat,
                config=config,
                rules=rules,
                request_id="entry-lfm",
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Lane"], "lfm_direct")
            body = json.loads(payload)
            self.assertEqual(
                body["choices"][0]["message"]["content"],
                "entrypoint lfm answer",
            )
            self.assertEqual(qwen.calls, [])
            self.assertEqual(lfm.calls, [("entry-lfm", "tiny-chat-lfm")])


class ServeCliTests(unittest.TestCase):
    def test_serve_validates_rules_before_binding(self) -> None:
        from router.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RouterError):
                main(
                    [
                        "serve",
                        "--rules",
                        str(FIXTURES / "ambiguous.json"),
                        "--artifacts",
                        tmp,
                    ]
                )

    def test_serve_defaults(self) -> None:
        from router.__main__ import _parser

        args = _parser().parse_args(["serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertEqual(
            str(args.rules), "router/fixtures/routes/default.json"
        )
        self.assertEqual(
            resolve_qwen_base_url(args.qwen_url), "http://127.0.0.1:8083/v1"
        )

    def test_serve_accepts_explicit_lfm_flags(self) -> None:
        from router.__main__ import _parser

        args = _parser().parse_args(
            [
                "serve",
                "--lfm-url",
                "http://lfm.example:8090/v1",
                "--lfm-model",
                "lfm-custom",
            ]
        )
        self.assertEqual(args.lfm_url, "http://lfm.example:8090/v1")
        self.assertEqual(args.lfm_model, "lfm-custom")


if __name__ == "__main__":
    unittest.main()
