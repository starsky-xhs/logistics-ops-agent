"""FastAPI 可选接口测试。

没安装 fastapi / httpx 时整个模块 skip，核心测试不依赖网络或 Web 框架。
"""

from __future__ import annotations

import importlib.util
import json
import unittest

from tests.support import make_service

HAS_API = all(
    importlib.util.find_spec(name) is not None
    for name in ("fastapi", "httpx", "pydantic")
)


@unittest.skipUnless(HAS_API, "未安装 fastapi/httpx，跳过可选 API 测试")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from app.api.app import create_app

        cls.service = make_service()
        cls.app = create_app(service=cls.service)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.service.close()

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["orders"], 30)
        self.assertEqual(payload["status"], "ok")

    def test_tools(self) -> None:
        response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200)
        tools = response.json()
        self.assertEqual(len(tools), 11)
        names = {item["name"] for item in tools}
        self.assertIn("change_delivery_address", names)

    def test_sse_chat_query(self) -> None:
        with self.client.stream(
            "POST",
            "/api/chat",
            json={"message": "订单 SH20250003 到哪了", "session_id": "api-sse"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            text = "".join(response.iter_text())
        self.assertIn("event: step", text)
        self.assertIn("event: tool_result", text)
        self.assertIn("event: done", text)
        self.assertIn("query_order", text)
        self.assertIn("track_shipment", text)

    def test_approval_flow(self) -> None:
        with self.client.stream(
            "POST",
            "/api/chat",
            json={
                "message": "我的订单 SH20250006 收货地址改成杭州市滨江区江南大道 88 号",
                "session_id": "api-approval",
            },
        ) as response:
            text = "".join(response.iter_text())
        self.assertIn("event: approval_required", text)
        approval_id = ""
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if payload.get("type") == "approval_required":
                approval_id = payload["approval_id"]
        self.assertTrue(approval_id)

        resolved = self.client.post(
            f"/api/approvals/{approval_id}",
            json={"approved": True, "approver": "api-test"},
        )
        self.assertEqual(resolved.status_code, 200)
        body = resolved.json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("新收货地址", body["final_answer"])

        audit = self.client.get(
            "/api/audit", params={"run_id": body["run_id"]}
        )
        self.assertEqual(audit.status_code, 200)
        records = audit.json()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["approved"])


if __name__ == "__main__":
    unittest.main()
