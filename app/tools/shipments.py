"""运单域工具：轨迹、时效预估、改派。"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.models import ToolSpec
from app.tools.common import (
    SHIPMENT_STATUS_LABELS,
    mask_address,
    require_approval,
    success,
)
from app.tools.registry import ToolContext, ToolError, ToolRegistry, ToolResult


def _fetch_shipment(context: ToolContext, tracking_no: str) -> Any:
    row = context.connection.execute(
        """
        SELECT s.id, s.tracking_no, s.carrier, s.status, s.current_city,
               s.last_event, s.estimated_delivery, s.delivery_address, s.updated_at,
               o.order_no, o.status AS order_status
        FROM shipments s JOIN orders o ON o.id = s.order_id
        WHERE s.tracking_no = ?
        """,
        (tracking_no,),
    ).fetchone()
    if row is None:
        raise ToolError(f"未找到运单 {tracking_no}", code="not_found")
    return row


def track_shipment(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """查询运单最新节点。"""

    tracking_no = str(arguments["tracking_no"])
    row = _fetch_shipment(context, tracking_no)
    status_label = SHIPMENT_STATUS_LABELS.get(row["status"], row["status"])
    data = {
        "tracking_no": row["tracking_no"],
        "carrier": row["carrier"],
        "status": row["status"],
        "status_label": status_label,
        "current_city": row["current_city"],
        "last_event": row["last_event"],
        "estimated_delivery": row["estimated_delivery"],
        "updated_at": row["updated_at"],
        "order_no": row["order_no"],
    }
    return success(
        context,
        "track_shipment",
        data=data,
        summary=(
            f"运单 {tracking_no} 目前{status_label}，最新节点："
            f"{row['last_event']}（{row['current_city']}）。"
        ),
    )


def estimate_delivery(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """根据运单状态与预计送达日给出时效说明。"""

    tracking_no = str(arguments["tracking_no"])
    row = _fetch_shipment(context, tracking_no)
    estimated = str(row["estimated_delivery"])
    today = context.now.date()
    remaining = 0
    if estimated:
        try:
            remaining = max(0, (date.fromisoformat(estimated) - today).days)
        except ValueError:
            remaining = 0

    status_label = SHIPMENT_STATUS_LABELS.get(row["status"], row["status"])
    if row["status"] == "delivered":
        hint = "运单已签收，无需继续等待。"
    elif row["status"] == "exception":
        hint = "运单存在异常，建议转人工核查。"
    elif estimated and estimated < today.isoformat():
        hint = (
            f"运单原预计送达日为 {estimated}，该日期已过；"
            "建议人工核查最新路由后再答复客户。"
        )
    elif estimated:
        hint = f"按当前路由，预计 {estimated} 送达（约 {remaining} 天）。"
    else:
        hint = "暂无明确预计送达时间，建议稍后再查。"

    data = {
        "tracking_no": tracking_no,
        "status": row["status"],
        "status_label": status_label,
        "estimated_delivery": estimated,
        "remaining_days": remaining,
        "hint": hint,
        "order_no": row["order_no"],
    }
    return success(
        context,
        "estimate_delivery",
        data=data,
        summary=f"运单 {tracking_no}：{hint}",
    )


def reroute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """修改运单派送地址。高风险，需要审批上下文。"""

    require_approval(context, "reroute")
    tracking_no = str(arguments["tracking_no"])
    new_address = str(arguments["new_address"]).strip()
    reason = str(arguments.get("reason", "客户申请改派")).strip()
    row = _fetch_shipment(context, tracking_no)
    if row["status"] in {"delivered", "cancelled"}:
        raise ToolError(
            f"运单当前为{SHIPMENT_STATUS_LABELS.get(row['status'], row['status'])}，"
            "无法改派。",
            code="invalid_state",
        )

    old_address = row["delivery_address"]
    context.connection.execute(
        """
        UPDATE shipments
        SET delivery_address = ?, last_event = '收件地址已变更，按新地址派送',
            updated_at = ?
        WHERE id = ?
        """,
        (new_address, context.timestamp, row["id"]),
    )
    context.connection.execute(
        "UPDATE orders SET delivery_address = ?, updated_at = ? WHERE order_no = ?",
        (new_address, context.timestamp, row["order_no"]),
    )
    return success(
        context,
        "reroute",
        data={
            "tracking_no": tracking_no,
            "order_no": row["order_no"],
            "old_address": mask_address(old_address),
            "new_address": new_address,
            "new_address_masked": mask_address(new_address),
            "reason": reason,
            "approval_id": context.approval_id,
        },
        summary=(
            f"运单 {tracking_no} 已改派至 {mask_address(new_address)}，"
            "后续按新地址派送。"
        ),
    )


# -- 工具契约 ---------------------------------------------------------------

TRACKING_SCHEMA = {
    "type": "string",
    "pattern": r"^(SF|TD)\d{10,16}$",
    "description": "运单号，例如 SF202500010001",
    "maxLength": 20,
}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="track_shipment",
            description="按运单号查询物流轨迹、当前城市和最新节点。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {"tracking_no": TRACKING_SCHEMA},
                "required": ["tracking_no"],
                "additionalProperties": False,
            },
        ),
        track_shipment,
    )
    registry.register(
        ToolSpec(
            name="estimate_delivery",
            description="根据运单状态和路由预计送达时间，回答‘什么时候到’。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {"tracking_no": TRACKING_SCHEMA},
                "required": ["tracking_no"],
                "additionalProperties": False,
            },
        ),
        estimate_delivery,
    )
    registry.register(
        ToolSpec(
            name="reroute",
            description="修改在途运单的派送地址（改派）。高风险，必须人工审批。",
            risk_level="high",
            json_schema={
                "type": "object",
                "properties": {
                    "tracking_no": TRACKING_SCHEMA,
                    "new_address": {
                        "type": "string",
                        "minLength": 5,
                        "maxLength": 200,
                        "description": "新的完整派送地址",
                    },
                    "reason": {"type": "string", "maxLength": 100},
                },
                "required": ["tracking_no", "new_address"],
                "additionalProperties": False,
            },
        ),
        reroute,
    )
