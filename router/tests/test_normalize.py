import json
import unittest

from router.domain import LimitError, ParseError
from router.normalize import HARD_MAX_MESSAGES, normalize_chat_body


class NormalizeChatBodyTests(unittest.TestCase):
    def test_parses_openai_chat_shape(self) -> None:
        body = {
            "model": "qwen3.8-27b",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
            ],
            "max_tokens": 64,
            "stream": False,
        }
        env = normalize_chat_body(body, request_id="req-1")
        self.assertEqual(env.request_id, "req-1")
        self.assertEqual(env.model, "qwen3.8-27b")
        self.assertEqual(len(env.messages), 2)
        self.assertEqual(env.intent, "chat")
        self.assertEqual(env.risk_class, "low")
        self.assertFalse(env.needs_qwen_judgment)
        self.assertEqual(env.budgets.output_tokens, 64)

    def test_parses_json_bytes(self) -> None:
        raw = json.dumps(
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "extract ids"}]}]}
        ).encode("utf-8")
        env = normalize_chat_body(raw, request_id="r")
        self.assertEqual(env.intent, "extraction")
        self.assertEqual(env.messages[0].content, "extract ids")

    def test_rejects_missing_messages(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            normalize_chat_body({"model": "x"})
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertEqual(ctx.exception.openai_body()["error"]["type"], "invalid_request_error")

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(ParseError):
            normalize_chat_body("{")

    def test_rejects_too_many_messages(self) -> None:
        messages = [{"role": "user", "content": "x"} for _ in range(HARD_MAX_MESSAGES + 1)]
        with self.assertRaises(LimitError):
            normalize_chat_body({"messages": messages})

    def test_tools_mark_qwen_judgment(self) -> None:
        env = normalize_chat_body(
            {
                "messages": [{"role": "user", "content": "run it"}],
                "tools": [{"type": "function", "function": {"name": "search"}}],
            },
            request_id="t",
        )
        self.assertIn("search", env.available_tools)
        self.assertTrue(env.needs_qwen_judgment)

    def test_metadata_overrides_intent(self) -> None:
        env = normalize_chat_body(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "metadata": {"intent": "coding", "risk_class": "high", "needs_qwen_judgment": True},
            },
            request_id="m",
        )
        self.assertEqual(env.intent, "coding")
        self.assertEqual(env.risk_class, "high")
        self.assertTrue(env.needs_qwen_judgment)


if __name__ == "__main__":
    unittest.main()
