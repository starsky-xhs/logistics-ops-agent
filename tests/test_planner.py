"""规划器测试：Mock 规则、LLM 解析与离线降级。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.planner import (
    LlmPlanner,
    MockPlanner,
    PlannerError,
    build_planner,
    parse_request,
)
from app.config import Settings
from app.tools import ToolRegistry
from tests.support import memory_settings


class MockPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = MockPlanner()
        self.registry = ToolRegistry.default()

    def test_parse_slots(self) -> None:
        parsed = parse_request(
            "我的订单 SH20250006 收货地址改成杭州市滨江区江南大道 88 号，电话 13800138001"
        )
        self.assertEqual(parsed.order_no, "SH20250006")
        self.assertEqual(parsed.phone, "13800138001")
        self.assertEqual(parsed.new_address, "杭州市滨江区江南大道 88 号")
        self.assertTrue(parsed.has_change_words)

    def test_combined_order_and_tracking_plan(self) -> None:
        steps = self.planner.plan("订单 SH20250003 到哪了")
        self.assertEqual([step.tool_name for step in steps], ["query_order", "track_shipment"])
        self.assertEqual(
            steps[1].arguments["tracking_no"], "$steps[0].shipment.tracking_no"
        )
        self.assertTrue(steps[0].reason)

    def test_out_of_scope_goes_to_human(self) -> None:
        steps = self.planner.plan("能帮我写一份竞品分析吗")
        self.assertEqual(steps[0].tool_name, "escalate_to_human")
        self.assertEqual(steps[0].arguments["category"], "out_of_scope")

    def test_missing_address_goes_to_human(self) -> None:
        steps = self.planner.plan("帮我把订单 SH20250003 的地址改一下")
        self.assertEqual(steps[0].tool_name, "escalate_to_human")
        self.assertEqual(steps[0].arguments["category"], "missing_slot")

    def test_high_risk_plan_is_marked_by_policy_not_planner(self) -> None:
        steps = self.planner.plan("取消订单 SH20250009")
        self.assertEqual(steps[0].tool_name, "cancel_order")
        self.assertFalse(steps[0].needs_approval)


class LlmPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry.default()
        self.settings = Settings(
            db_path=Path(":memory:"),
            planner="llm",
            llm_api_key="test-key",
            llm_model="test-model",
        )

    def test_build_planner_modes(self) -> None:
        self.assertIsInstance(
            build_planner(memory_settings(planner="mock"), self.registry), MockPlanner
        )
        planner = build_planner(self.settings, self.registry)
        self.assertIsInstance(planner, LlmPlanner)

    def test_parallel_tool_calls_parsed(self) -> None:
        planner = LlmPlanner(self.settings, self.registry)
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "query_order",
                                    "arguments": '{"order_no": "SH20250001"}',
                                }
                            },
                            {
                                "function": {
                                    "name": "query_invoice",
                                    "arguments": '{"order_no": "SH20250001"}',
                                }
                            },
                        ]
                    }
                }
            ]
        }
        steps = planner._parse_tool_calls(payload)
        self.assertEqual(
            [step.tool_name for step in steps], ["query_order", "query_invoice"]
        )
        self.assertEqual(steps[0].arguments["order_no"], "SH20250001")

    def test_unknown_tool_in_llm_response_rejected(self) -> None:
        planner = LlmPlanner(self.settings, self.registry)
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "drop_database",
                                    "arguments": "{}",
                                }
                            }
                        ]
                    }
                }
            ]
        }
        with self.assertRaises(PlannerError):
            planner._parse_tool_calls(payload)

    def test_network_failure_falls_back_to_mock(self) -> None:
        planner = LlmPlanner(self.settings, self.registry)
        with patch.object(planner, "_request", side_effect=RuntimeError("offline")):
            steps = planner.plan("取消订单 SH20250009")
        self.assertEqual([step.tool_name for step in steps], ["cancel_order"])
        self.assertIn("offline", planner.last_fallback_reason)
        self.assertIn("降级", steps[0].reason)

    def test_invalid_json_arguments_falls_back_to_mock(self) -> None:
        planner = LlmPlanner(self.settings, self.registry)
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "query_order",
                                    "arguments": "{not-json",
                                }
                            }
                        ]
                    }
                }
            ]
        }
        with self.assertRaises(PlannerError):
            planner._parse_tool_calls(payload)


if __name__ == "__main__":
    unittest.main()
