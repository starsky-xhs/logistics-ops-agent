"""审计写入、脱敏与表格渲染测试。"""

from __future__ import annotations

import unittest

from app import audit
from app.models import ToolResult
from tests.support import memory_db


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = memory_db()

    def tearDown(self) -> None:
        self.connection.close()

    def _record(self, **overrides: object) -> int:
        arguments = {
            "order_no": "SH20250006",
            "new_address": "杭州市滨江区江南大道 88 号",
            "phone": "13800138001",
        }
        arguments.update(overrides.pop("arguments", {}))  # type: ignore[arg-type]
        result = ToolResult(
            tool_name="change_delivery_address",
            ok=True,
            summary="地址已更新：杭州市滨江区江南大道 88 号，联系电话 13800138001",
            latency_ms=1.25,
        )
        return audit.record(
            self.connection,
            actor="agent",
            session_id="sess_1",
            run_id="run_1",
            tool_name="change_delivery_address",
            arguments=arguments,
            result=result,
            risk_level="high",
            approval_id="appr_1",
            approved=True,
            mask_pii=True,
            idempotency_key="",
        )

    def test_record_and_read_back(self) -> None:
        audit_id = self._record()
        records = audit.list_records(self.connection)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.audit_id, audit_id)
        self.assertEqual(record.tool_name, "change_delivery_address")
        self.assertTrue(record.approved)
        self.assertEqual(record.risk_level, "high")
        self.assertIn("****", record.arguments_summary)
        self.assertNotIn("13800138001", record.arguments_summary)
        self.assertNotIn("88 号", record.arguments_summary)

    def test_arguments_and_result_are_masked(self) -> None:
        self._record()
        record = audit.list_records(self.connection)[0]
        self.assertNotIn("江南大道 88 号", record.result_summary)
        self.assertIn("***", record.result_summary)

    def test_filter_by_run_and_session(self) -> None:
        self._record()
        self.assertEqual(len(audit.list_records(self.connection, run_id="run_1")), 1)
        self.assertEqual(len(audit.list_records(self.connection, run_id="run_x")), 0)
        self.assertEqual(len(audit.list_records(self.connection, session_id="sess_1")), 1)

    def test_empty_table(self) -> None:
        self.assertEqual(audit.render_table([]), "（暂无审计记录）")

    def test_table_contains_headers(self) -> None:
        self._record()
        table = audit.render_table(audit.list_records(self.connection))
        self.assertIn("工具", table)
        self.assertIn("审批", table)
        self.assertIn("change_delivery_address", table)

    def test_mask_can_be_disabled_for_debug(self) -> None:
        result = ToolResult(
            tool_name="query_order",
            ok=True,
            summary="手机号 13800138001",
            latency_ms=0.1,
        )
        audit.record(
            self.connection,
            actor="debug",
            session_id="sess_2",
            run_id="run_2",
            tool_name="query_order",
            arguments={"phone": "13800138001"},
            result=result,
            risk_level="low",
            mask_pii=False,
        )
        record = audit.list_records(self.connection, run_id="run_2")[0]
        self.assertIn("13800138001", record.arguments_summary)


if __name__ == "__main__":
    unittest.main()
