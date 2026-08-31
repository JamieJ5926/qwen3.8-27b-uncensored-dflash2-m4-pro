"""Ordered route table. LaneDecision only. No action selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from router.domain import (
    AmbiguousRouteError,
    ConversationEnvelope,
    LaneBudget,
    LaneDecision,
    MatchSpec,
    NoRouteError,
    RouteRule,
    RouteTableError,
    as_intent,
    as_lane,
    as_risk,
)

SCHEMA_PATH = Path(__file__).with_name("rules.schema.json")


def _match_rule(rule: RouteRule, envelope: ConversationEnvelope) -> bool:
    spec = rule.match
    if spec.intent is not None and spec.intent != envelope.intent:
        return False
    if spec.risk_class is not None and spec.risk_class != envelope.risk_class:
        return False
    if spec.needs_qwen_judgment is not None and spec.needs_qwen_judgment != envelope.needs_qwen_judgment:
        return False
    if spec.max_context_tokens is not None and envelope.context_token_count > spec.max_context_tokens:
        return False
    if spec.min_context_tokens is not None and envelope.context_token_count < spec.min_context_tokens:
        return False
    return True


def parse_rule(raw: dict[str, Any], default_version: str) -> RouteRule:
    match_raw = raw.get("match") or {}
    if not isinstance(match_raw, dict):
        raise RouteTableError("rule match must be an object")
    intent = as_intent(match_raw["intent"]) if "intent" in match_raw else None
    risk = as_risk(match_raw["risk_class"]) if "risk_class" in match_raw else None
    follow = int(raw.get("follow_up_budget", 0))
    if follow not in (0, 1):
        raise RouteTableError(f"rule {raw.get('id')}: follow_up_budget must be 0 or 1")
    return RouteRule(
        id=str(raw["id"]),
        match=MatchSpec(
            intent=intent,
            risk_class=risk,
            max_context_tokens=match_raw.get("max_context_tokens"),
            min_context_tokens=match_raw.get("min_context_tokens"),
            needs_qwen_judgment=match_raw.get("needs_qwen_judgment"),
        ),
        lane=as_lane(str(raw["lane"])),
        lane_input_budget=int(raw["lane_input_budget"]),
        lane_output_budget=int(raw["lane_output_budget"]),
        timeout_s=float(raw["timeout_s"]),
        fallback_lane=as_lane(str(raw["fallback_lane"])),
        enabled=bool(raw.get("enabled", True)),
        follow_up_budget=follow,
        version=str(raw.get("version", default_version)),
    )


def load_route_table(raw: dict[str, Any]) -> tuple[str, tuple[RouteRule, ...]]:
    if not isinstance(raw, dict):
        raise RouteTableError("route table must be an object")
    version = str(raw.get("version") or "")
    if not version:
        raise RouteTableError("route table version is required")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        raise RouteTableError("rules must be a list")
    rules = tuple(parse_rule(item, version) for item in rules_raw)
    validate_route_table(rules)
    return version, rules


def load_route_table_path(path: str | Path) -> tuple[str, tuple[RouteRule, ...]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_route_table(data)


def validate_route_table(rules: Iterable[RouteRule]) -> None:
    seen: dict[str, RouteRule] = {}
    ordered = tuple(rules)
    for rule in ordered:
        if rule.id in seen:
            raise RouteTableError(f"duplicate rule id: {rule.id}")
        seen[rule.id] = rule
        if rule.lane == "qwen_coordinator" and rule.follow_up_budget > 1:
            raise RouteTableError(f"{rule.id}: follow_up_budget exceeds 1")
        if rule.fallback_lane is None:
            raise RouteTableError(f"{rule.id}: missing fallback_lane")
    _assert_no_static_ambiguity(ordered)


def _assert_no_static_ambiguity(rules: tuple[RouteRule, ...]) -> None:
    """Two enabled rules that match the same predicate set are a config error.

    Precedence is list order, so a later rule that is a strict subset of an
    earlier enabled rule is dead, not ambiguous. Ambiguity is two enabled
    rules whose match predicates are identical.
    """
    enabled = [r for r in rules if r.enabled]
    keys: dict[tuple[Any, ...], str] = {}
    for rule in enabled:
        key = (
            rule.match.intent,
            rule.match.risk_class,
            rule.match.max_context_tokens,
            rule.match.min_context_tokens,
            rule.match.needs_qwen_judgment,
        )
        if key in keys:
            raise AmbiguousRouteError(
                f"ambiguous rules {keys[key]} and {rule.id}: identical match"
            )
        keys[key] = rule.id


def resolve_lane(
    envelope: ConversationEnvelope,
    rules: Iterable[RouteRule],
) -> LaneDecision:
    matches: list[RouteRule] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if _match_rule(rule, envelope):
            matches.append(rule)
    if not matches:
        raise NoRouteError("no route rule matched the request")
    if len(matches) > 1:
        # Ordered first-match is the resolver. Identical remaining matches
        # after the first are a config error already rejected at load time
        # when predicates are identical. Distinct overlapping predicates
        # take the first in list order.
        chosen = matches[0]
        later_ids = [r.id for r in matches[1:]]
        reason = (
            "first_match",
            f"overlapped:{','.join(later_ids)}",
        )
    else:
        chosen = matches[0]
        reason = ("first_match",)
    budget = LaneBudget(
        input_tokens=min(chosen.lane_input_budget, envelope.budgets.input_tokens),
        output_tokens=min(chosen.lane_output_budget, envelope.budgets.output_tokens),
        timeout_s=min(chosen.timeout_s, envelope.budgets.timeout_s),
        follow_up_budget=chosen.follow_up_budget,
    )
    return LaneDecision(
        rule_id=chosen.id,
        lane=chosen.lane,
        reason_codes=reason,
        rule_version=chosen.version,
        lane_budget=budget,
        fallback_lane=chosen.fallback_lane,
    )
