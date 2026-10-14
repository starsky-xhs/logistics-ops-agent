"""AgentService：会话、审计、审批与跨进程状态恢复。"""

from __future__ import annotations

import contextlib
import unittest
import uuid
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.service import AgentService, ApprovalError
from tests.support import make_service


class ServiceTests(unittest.TestCase):
    def test_session_messages_persisted(self) -> None:
        with make_service() as service:
            first = service.run("sess_test", "查一下订单 SH20250001")
            second = service.run("sess_test", "发票 INV2025000002 查一下")
            self.assertEqual(first.session_id, "sess_test")
            self.assertEqual(second.session_id, "sess_test")
            messages = service.list_messages("sess_test")
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages[0].role, "user")
            self.assertEqual(messages[-1].role, "assistant")

    def test_get_run(self) -> None:
        with make_service() as service:
            run = service.run(None, "订单 SH20250002 的详情")
            loaded = service.get_run(run.run_id)
        assert loaded is not None
        self.assertEqual(loaded.run_id, run.run_id)
        self.assertEqual(loaded.status, "completed")

    def test_audit_filtered_by_run(self) -> None:
        with make_service() as service:
            run = service.run(None, "订单 SH20250003 到哪了")
            records = service.list_audit(limit=10, run_id=run.run_id)
        self.assertEqual(len(records), 2)
        self.assertEqual({item.tool_name for item in records}, {"query_order", "track_shipment"})

    def test_double_approval_rejected(self) -> None:
        with make_service() as service:
            run = service.run(
                None,
                "取消订单 SH20250009",
            )
            service.approve(run.approval_id, approver="first")
            with self.assertRaises(ApprovalError):
                service.approve(run.approval_id, approver="second")

    def test_unknown_approval(self) -> None:
        with make_service() as service, self.assertRaises(ApprovalError):
            service.approve("appr_missing")

    def test_approval_persists_across_service_reopen(self) -> None:
        tmp_dir = PROJECT_ROOT / "data" / ".test_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_dir / f"service_{uuid.uuid4().hex}.db"
        settings = Settings(db_path=db_path, planner="mock")
        self.addCleanup(self._cleanup_db, db_path)

        first = AgentService(settings)
        try:
            run = first.run(
                "persist_session",
                "我的订单 SH20250006 收货地址改成杭州市滨江区江南大道 88 号",
            )
            self.assertEqual(run.status, "awaiting_approval")
            approval_id = run.approval_id
        finally:
            first.close()

        second = AgentService(settings)
        try:
            pending = second.get_approval(approval_id)
            assert pending is not None
            self.assertEqual(pending.status, "pending")
            resumed = second.approve(approval_id, approver="restart-test")
            self.assertEqual(resumed.status, "completed")
            self.assertIn("新收货地址", resumed.final_answer)
            row = second.connection.execute(
                "SELECT delivery_address FROM orders WHERE order_no = 'SH20250006'"
            ).fetchone()
            self.assertEqual(row["delivery_address"], "杭州市滨江区江南大道 88 号")
        finally:
            second.close()

    def test_health(self) -> None:
        with make_service() as service:
            health = service.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["orders"], 30)
        self.assertEqual(health["tools"], 11)

    @staticmethod
    def _cleanup_db(db_path: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{db_path}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
