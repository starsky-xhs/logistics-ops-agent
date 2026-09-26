"""转人工 / 工单工具。

它不操作钱和履约数据，风险为 low；但它是 Agent 承认能力边界的方式，
所以仍然写入独立工单表和全局审计。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models import ToolSpec
from app.tools.common import success
from app.tools.registry import ToolContext, ToolRegistry, ToolResult

CATEGORY_LABELS = {
    "out_of_scope": "超出业务范围",
    "complaint": "投诉",
    "manual_request": "用户要求人工",
    "missing_slot": "缺少关键信息",
    "security": "安全拦截",
    "other": "其他",
}


def escalate_to_human(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """创建人工工单并返回工单号。"""

    reason = str(arguments["reason"]).strip()
    category = str(arguments.get("category", "other"))
    ticket_no = f"HO{context.now:%Y%m%d}{uuid.uuid4().hex[:6].upper()}"
    context.connection.execute(
        """
        INSERT INTO handoffs
            (ticket_no, session_id, run_id, reason, category, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            ticket_no,
            context.session_id,
            context.run_id,
            reason,
            category,
            context.timestamp,
        ),
    )
    label = CATEGORY_LABELS.get(category, category)
    data = {
        "ticket_no": ticket_no,
        "category": category,
        "category_label": label,
        "reason": reason,
        "status": "open",
        "sla": "工作日 4 小时内首次响应",
    }
    return success(
        context,
        "escalate_to_human",
        data=data,
        summary=f"已创建人工工单 {ticket_no}（{label}），等待客服接手。",
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="escalate_to_human",
            description="把无法自动处理的问题转人工，记录原因并生成工单号。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 300,
                        "description": "转人工原因，写给人工客服看",
                    },
                    "category": {
                        "type": "string",
                        "enum": list(CATEGORY_LABELS),
                        "description": "工单分类",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        ),
        escalate_to_human,
    )
