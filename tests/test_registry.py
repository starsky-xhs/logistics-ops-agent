"""工具注册表与安全边界测试。"""

from __future__ import annotations

import unittest

from app.models import ToolSpec
from app.tools import ApprovalRequired, ToolContext, ToolNotFound, ToolRegistry
from app.tools.registry import ToolError
from tests.support import memory_db


def _dummy_handler(context: ToolContext, arguments: dict) -> object:
    from app.models import ToolResult

    return ToolResult(tool_name="dummy_tool", ok=True, data={"ok": True})


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry.default()
        self.connection = memory_db()
        self.context = ToolContext(connection=self.connection, actor="test")

    def tearDown(self) -> None:
        self.connection.close()

    def test_builtin_tool_set(self) -> None:
        names = {spec.name for spec in self.registry.list_specs()}
        self.assertEqual(len(names), 11)
        self.assertTrue(
            {
                "query_order",
                "list_orders",
                "change_delivery_address",
                "cancel_order",
                "track_shipment",
                "estimate_delivery",
                "reroute",
                "query_invoice",
                "create_invoice",
                "refund",
                "escalate_to_human",
            }
            <= names
        )

    def test_risk_metadata(self) -> None:
        high = {
            spec.name for spec in self.registry.list_specs() if spec.risk_level == "high"
        }
        self.assertEqual(
            high,
            {"change_delivery_address", "cancel_order", "reroute", "refund"},
        )
        invoice = self.registry.get("create_invoice")
        self.assertTrue(invoice.idempotent)
        self.assertEqual(invoice.risk_level, "medium")

    def test_openai_tool_shape(self) -> None:
        tools = self.registry.openai_tools()
        self.assertTrue(tools)
        first = tools[0]
        self.assertEqual(first["type"], "function")
        self.assertIn("name", first["function"])
        self.assertIn("parameters", first["function"])

    def test_duplicate_registration_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                ToolSpec(
                    name="query_order",
                    description="duplicate",
                    json_schema={"type": "object", "properties": {}},
                    risk_level="low",
                ),
                _dummy_handler,
            )

    def test_invalid_tool_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                ToolSpec(
                    name="Bad-Name",
                    description="bad",
                    json_schema={"type": "object", "properties": {}},
                    risk_level="low",
                ),
                _dummy_handler,
            )

    def test_unknown_tool(self) -> None:
        with self.assertRaises(ToolNotFound):
            self.registry.get("does_not_exist")
        with self.assertRaises(ToolNotFound):
            self.registry.execute("does_not_exist", {}, self.context)

    def test_high_risk_without_approval_raises(self) -> None:
        with self.assertRaises(ApprovalRequired) as ctx:
            self.registry.execute(
                "change_delivery_address",
                {
                    "order_no": "SH20250006",
                    "new_address": "杭州市滨江区江南大道 88 号",
                },
                self.context,
            )
        self.assertEqual(ctx.exception.code, "approval_required")
        self.assertEqual(ctx.exception.risk_level, "high")

    def test_high_risk_with_approval_context_executes(self) -> None:
        approved = ToolContext(
            connection=self.connection,
            actor="test",
            approval_id="appr_test",
        )
        result = self.registry.execute(
            "change_delivery_address",
            {
                "order_no": "SH20250006",
                "new_address": "杭州市滨江区江南大道 88 号",
            },
            approved,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["approval_id"], "appr_test")

    def test_handler_tool_error_preserved(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.registry.execute(
                "query_order", {"order_no": "SH20259999"}, self.context
            )
        self.assertEqual(ctx.exception.code, "not_found")

    def test_handler_unexpected_exception_wrapped(self) -> None:
        registry = ToolRegistry()

        def broken(context: ToolContext, arguments: dict) -> object:
            raise RuntimeError("boom")

        registry.register(
            ToolSpec(
                name="broken_tool",
                description="test",
                json_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                risk_level="low",
            ),
            broken,
        )
        with self.assertRaises(ToolError) as ctx:
            registry.execute("broken_tool", {}, self.context)
        self.assertEqual(ctx.exception.code, "tool_failed")


if __name__ == "__main__":
    unittest.main()
