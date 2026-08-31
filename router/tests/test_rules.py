import unittest
from pathlib import Path

from router.domain import (
    ActionDecision,
    AmbiguousRouteError,
    ChatMessage,
    ConversationEnvelope,
    LaneBudget,
    LaneDecision,
    NoRouteError,
    RouteTableError,
)
from router.rules import load_route_table, load_route_table_path, resolve_lane

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


def _envelope(**kwargs) -> ConversationEnvelope:
    defaults = dict(
        request_id="r1",
        task_id="t1",
        messages=(ChatMessage(role="user", content="hi"),),
        profile="full_pi",
        budgets=LaneBudget(input_tokens=4096, output_tokens=1024, timeout_s=120),
        available_tools=(),
        intent="chat",
        risk_class="low",
        context_token_count=10,
        needs_qwen_judgment=False,
    )
    defaults.update(kwargs)
    return ConversationEnvelope(**defaults)


class RouteTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.version, self.rules = load_route_table_path(FIXTURES / "default.json")

    def test_tiny_chat_selects_lfm(self) -> None:
        decision = resolve_lane(_envelope(intent="chat", risk_class="low", context_token_count=10), self.rules)
        self.assertIsInstance(decision, LaneDecision)
        self.assertEqual(decision.lane, "lfm_direct")
        self.assertEqual(decision.rule_id, "tiny-chat-lfm")
        self.assertEqual(decision.lane_budget.follow_up_budget, 0)

    def test_coding_selects_qwen_with_follow_up(self) -> None:
        decision = resolve_lane(
            _envelope(intent="coding", risk_class="high", needs_qwen_judgment=True),
            self.rules,
        )
        self.assertEqual(decision.lane, "qwen_coordinator")
        self.assertEqual(decision.rule_id, "coding-qwen")
        self.assertEqual(decision.lane_budget.follow_up_budget, 1)

    def test_list_order_precedence_over_later_catch_all(self) -> None:
        decision = resolve_lane(_envelope(intent="retrieval"), self.rules)
        self.assertEqual(decision.rule_id, "retrieval-worker")
        self.assertEqual(decision.lane, "fixed_worker")

    def test_catch_all_fail_visible(self) -> None:
        decision = resolve_lane(
            _envelope(intent="chat", risk_class="high", needs_qwen_judgment=True, context_token_count=5000),
            self.rules,
        )
        self.assertEqual(decision.rule_id, "fail-closed")
        self.assertEqual(decision.lane, "fail_visible")

    def test_duplicate_ids_fail(self) -> None:
        raw = {
            "version": "1",
            "rules": [
                {
                    "id": "same",
                    "match": {"intent": "chat"},
                    "lane": "lfm_direct",
                    "lane_input_budget": 1,
                    "lane_output_budget": 1,
                    "timeout_s": 1,
                    "fallback_lane": "fail_visible",
                },
                {
                    "id": "same",
                    "match": {"intent": "coding"},
                    "lane": "fail_visible",
                    "lane_input_budget": 1,
                    "lane_output_budget": 1,
                    "timeout_s": 1,
                    "fallback_lane": "fail_visible",
                },
            ],
        }
        with self.assertRaises(RouteTableError):
            load_route_table(raw)

    def test_identical_match_is_ambiguous_config_error(self) -> None:
        with self.assertRaises(AmbiguousRouteError):
            load_route_table_path(FIXTURES / "ambiguous.json")

    def test_empty_rules_no_match(self) -> None:
        with self.assertRaises(NoRouteError):
            resolve_lane(_envelope(), [])

    def test_lane_decision_has_no_action_fields(self) -> None:
        decision = resolve_lane(_envelope(), self.rules)
        self.assertFalse(hasattr(decision, "kind"))
        self.assertFalse(hasattr(decision, "action_name"))
        fields = set(decision.__dataclass_fields__)
        self.assertEqual(
            fields,
            {"rule_id", "lane", "reason_codes", "rule_version", "lane_budget", "fallback_lane"},
        )

    def test_action_decision_has_no_lane_field(self) -> None:
        action = ActionDecision(
            kind="direct_answer",
            action_name="",
            arguments={},
            answer="ok",
            confidence=1.0,
            packet_hash="abc",
            action_budget=16,
            follow_up_budget=0,
        )
        self.assertNotIn("lane", action.__dataclass_fields__)
        self.assertEqual(action.kind, "direct_answer")

    def test_replay_identical(self) -> None:
        env = _envelope(intent="extraction", risk_class="low")
        a = resolve_lane(env, self.rules)
        b = resolve_lane(env, self.rules)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
