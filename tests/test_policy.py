"""风险、审批、幂等键与预算策略测试。"""

from __future__ import annotations

import time
import unittest

from app.agent.policy import PolicyEngine
from app.models import ToolSpec


def spec(name: str, risk: str, idempotent: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test",
        json_schema={"type": "object", "properties": {}},
        risk_level=risk,  # type: ignore[arg-type]
        idempotent=idempotent,
    )


class PolicyTests(unittest.TestCase):
    def test_high_risk_always_requires_approval(self) -> None:
        high_only = PolicyEngine(approval_policy="high_only")
        conservative = PolicyEngine(approval_policy="conservative")
        high = spec("refund", "high")
        self.assertTrue(high_only.requires_approval(high))
        self.assertTrue(conservative.requires_approval(high))

    def test_medium_only_in_conservative_mode(self) -> None:
        medium = spec("create_invoice", "medium", True)
        self.assertFalse(PolicyEngine(approval_policy="high_only").requires_approval(medium))
        self.assertTrue(PolicyEngine(approval_policy="conservative").requires_approval(medium))

    def test_low_never_requires_approval(self) -> None:
        low = spec("query_order", "low")
        self.assertFalse(PolicyEngine().requires_approval(low))

    def test_approval_reason_is_human_readable(self) -> None:
        policy = PolicyEngine()
        reason = policy.approval_reason(spec("cancel_order", "high"), "取消订单")
        self.assertIn("高风险", reason)
        self.assertIn("cancel_order", reason)
        self.assertIn("取消订单", reason)

    def test_fingerprint_stable_and_order_independent(self) -> None:
        first = PolicyEngine.fingerprint("query_order", {"order_no": "SH20250001", "x": 1})
        second = PolicyEngine.fingerprint("query_order", {"x": 1, "order_no": "SH20250001"})
        different = PolicyEngine.fingerprint("query_order", {"order_no": "SH20250002"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    def test_idempotency_key_stable(self) -> None:
        args = {"order_no": "SH20250007", "title": "测试"}
        first = PolicyEngine.make_idempotency_key("run_1", "create_invoice", args, 0)
        second = PolicyEngine.make_idempotency_key("run_1", "create_invoice", args, 0)
        other_run = PolicyEngine.make_idempotency_key("run_2", "create_invoice", args, 0)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_run)
        self.assertTrue(first.startswith("idem_"))

    def test_deadline_helpers(self) -> None:
        policy = PolicyEngine(time_budget_seconds=0.2)
        deadline = policy.deadline_from_now()
        self.assertFalse(policy.timed_out(deadline))
        self.assertGreater(policy.seconds_left(deadline), 0)
        self.assertTrue(policy.timed_out(time.time() - 1))
        self.assertFalse(policy.timed_out(0))

    def test_risk_label(self) -> None:
        policy = PolicyEngine()
        self.assertEqual(policy.risk_label("high"), "高")
        self.assertEqual(policy.risk_label("medium"), "中")


if __name__ == "__main__":
    unittest.main()
