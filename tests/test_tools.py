"""业务工具测试：查询、修改、幂等开票、退款。"""

from __future__ import annotations

import unittest

from app.tools import ApprovalRequired, ToolContext, ToolRegistry
from app.tools.orders import change_delivery_address
from tests.support import memory_db


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = memory_db()
        self.registry = ToolRegistry.default()
        self.ctx = ToolContext(connection=self.connection, actor="test")

    def tearDown(self) -> None:
        self.connection.close()

    def execute(
        self,
        name: str,
        arguments: dict,
        *,
        approval_id: str = "",
        idempotency_key: str = "",
    ):
        context = ToolContext(
            connection=self.connection,
            actor="test",
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )
        return self.registry.execute(name, arguments, context)

    def test_seed_has_30_orders(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
        self.assertEqual(count, 30)
        invoices = self.connection.execute(
            "SELECT COUNT(*) AS n FROM invoices"
        ).fetchone()["n"]
        self.assertGreaterEqual(invoices, 8)

    def test_query_order_includes_shipment(self) -> None:
        result = self.execute("query_order", {"order_no": "SH20250001"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "shipped")
        self.assertEqual(result.data["shipment"]["tracking_no"], "SF202500010001")
        self.assertIn("****", result.data["customer_phone_masked"])

    def test_query_order_not_found(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.execute("query_order", {"order_no": "SH20259999"})
        self.assertEqual(getattr(ctx.exception, "code", ""), "not_found")

    def test_list_orders_by_phone(self) -> None:
        result = self.execute(
            "list_orders", {"phone": "13900139002", "limit": 10}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 3)
        self.assertTrue(all(item["order_no"] for item in result.data["orders"]))

    def test_track_shipment(self) -> None:
        result = self.execute(
            "track_shipment", {"tracking_no": "SF202500030001"}
        )
        self.assertEqual(result.data["status"], "in_transit")
        self.assertIn("北京", result.data["current_city"])

    def test_estimate_delivery(self) -> None:
        result = self.execute(
            "estimate_delivery", {"tracking_no": "SF202500040001"}
        )
        self.assertTrue(result.data["status"] == "delivered")
        self.assertIn("签收", result.data["hint"])

    def test_change_address_requires_approval_at_two_layers(self) -> None:
        args = {
            "order_no": "SH20250006",
            "new_address": "杭州市滨江区江南大道 88 号",
        }
        with self.assertRaises(ApprovalRequired):
            self.execute("change_delivery_address", args)
        with self.assertRaises(ApprovalRequired):
            change_delivery_address(self.ctx, args)

    def test_change_address_with_approval_persists(self) -> None:
        result = self.execute(
            "change_delivery_address",
            {
                "order_no": "SH20250006",
                "new_address": "杭州市滨江区江南大道 88 号",
            },
            approval_id="appr_test",
        )
        self.assertTrue(result.ok)
        row = self.connection.execute(
            "SELECT delivery_address FROM orders WHERE order_no = 'SH20250006'"
        ).fetchone()
        self.assertEqual(row["delivery_address"], "杭州市滨江区江南大道 88 号")

    def test_cancel_order(self) -> None:
        result = self.execute(
            "cancel_order",
            {"order_no": "SH20250009", "reason": "客户下错单"},
            approval_id="appr_cancel",
        )
        self.assertTrue(result.ok)
        row = self.connection.execute(
            "SELECT status FROM orders WHERE order_no = 'SH20250009'"
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_reroute(self) -> None:
        result = self.execute(
            "reroute",
            {
                "tracking_no": "SF202500010001",
                "new_address": "杭州市余杭区文一西路 969 号",
            },
            approval_id="appr_reroute",
        )
        self.assertTrue(result.ok)
        row = self.connection.execute(
            "SELECT delivery_address FROM shipments WHERE tracking_no = 'SF202500010001'"
        ).fetchone()
        self.assertEqual(row["delivery_address"], "杭州市余杭区文一西路 969 号")

    def test_query_invoice_by_invoice_no_and_order_no(self) -> None:
        by_invoice = self.execute(
            "query_invoice", {"invoice_no": "INV2025000002"}
        )
        self.assertEqual(by_invoice.data["order_no"], "SH20250002")
        by_order = self.execute("query_invoice", {"order_no": "SH20250002"})
        self.assertEqual(by_order.data["invoice_no"], "INV2025000002")

    def test_query_invoice_requires_identifier(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.execute("query_invoice", {})
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_arguments")

    def test_create_invoice_idempotent(self) -> None:
        first = self.execute(
            "create_invoice",
            {"order_no": "SH20250007", "title": "杭州云栈科技有限公司"},
            idempotency_key="idem-test-001",
        )
        second = self.execute(
            "create_invoice",
            {"order_no": "SH20250007", "title": "杭州云栈科技有限公司"},
            idempotency_key="idem-test-001",
        )
        self.assertFalse(first.data["replayed"])
        self.assertTrue(second.data["replayed"])
        self.assertEqual(first.data["invoice_no"], second.data["invoice_no"])
        count = self.connection.execute(
            "SELECT COUNT(*) AS n FROM invoices WHERE idempotency_key = 'idem-test-001'"
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_create_invoice_requires_idempotency_key(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.execute("create_invoice", {"order_no": "SH20250007"})
        self.assertEqual(
            getattr(ctx.exception, "code", ""), "missing_idempotency_key"
        )

    def test_create_invoice_duplicate_order_conflicts(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.execute(
                "create_invoice",
                {"order_no": "SH20250002"},
                idempotency_key="idem-test-002",
            )
        self.assertEqual(getattr(ctx.exception, "code", ""), "conflict")

    def test_refund_requires_approval_and_updates_status(self) -> None:
        with self.assertRaises(ApprovalRequired):
            self.execute("refund", {"order_no": "SH20250010"})
        result = self.execute(
            "refund",
            {"order_no": "SH20250010", "reason": "客户退货"},
            approval_id="appr_refund",
        )
        self.assertEqual(result.data["status"], "refunded")
        row = self.connection.execute(
            "SELECT status FROM invoices WHERE invoice_no = 'INV2025000010'"
        ).fetchone()
        self.assertEqual(row["status"], "refunded")

    def test_escalate_creates_ticket(self) -> None:
        result = self.execute(
            "escalate_to_human",
            {"reason": "用户投诉", "category": "complaint"},
        )
        self.assertTrue(result.data["ticket_no"].startswith("HO"))
        row = self.connection.execute(
            "SELECT status FROM handoffs WHERE ticket_no = ?",
            (result.data["ticket_no"],),
        ).fetchone()
        self.assertEqual(row["status"], "open")

    def test_create_invoice_tax_rate_validation(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.execute(
                "create_invoice",
                {"order_no": "SH20250007", "tax_rate": 99},
                idempotency_key="idem-test-003",
            )
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
