"""订单域工具：查询、列表、改地址、取消。

改地址与取消都会影响真实履约，统一标成 high，并由注册表和实现双重
检查审批上下文。状态机负责在真正执行前暂停并落审批单。
"""

from __future__ import annotations

from typing import Any

from app.models import ToolSpec
from app.tools.common import (
    ORDER_STATUS_LABELS,
    SHIPMENT_STATUS_LABELS,
    mask_address,
    money,
    require_approval,
    success,
)
from app.tools.registry import (
    InvalidArguments,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)

ORDER_FIELDS = """
    o.id, o.order_no, o.status, o.amount_cents, o.currency, o.delivery_address,
    o.created_at, o.updated_at, c.name AS customer_name, c.phone AS customer_phone
"""


def _fetch_order(context: ToolContext, order_no: str) -> Any:
    row = context.connection.execute(
        f"""
        SELECT {ORDER_FIELDS}
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.order_no = ?
        """,  # noqa: S608 - 字段白名单常量
        (order_no,),
    ).fetchone()
    if row is None:
        raise ToolError(f"未找到订单 {order_no}", code="not_found")
    return row


def _fetch_shipment(context: ToolContext, order_id: int) -> Any:
    return context.connection.execute(
        """
        SELECT tracking_no, carrier, status, current_city, last_event,
               estimated_delivery, updated_at
        FROM shipments WHERE order_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (order_id,),
    ).fetchone()


def _order_payload(context: ToolContext, row: Any) -> dict[str, Any]:
    shipment = _fetch_shipment(context, int(row["id"]))
    payload: dict[str, Any] = {
        "order_no": row["order_no"],
        "status": row["status"],
        "status_label": ORDER_STATUS_LABELS.get(row["status"], row["status"]),
        "amount": money(int(row["amount_cents"])),
        "amount_cents": int(row["amount_cents"]),
        "currency": row["currency"],
        "delivery_address": row["delivery_address"],
        "customer_name": row["customer_name"],
        "customer_phone_masked": _mask_phone(row["customer_phone"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tracking_no": "",
        "shipment": None,
    }
    if shipment is not None:
        payload["tracking_no"] = shipment["tracking_no"]
        payload["shipment"] = {
            "tracking_no": shipment["tracking_no"],
            "carrier": shipment["carrier"],
            "status": shipment["status"],
            "status_label": SHIPMENT_STATUS_LABELS.get(
                shipment["status"], shipment["status"]
            ),
            "current_city": shipment["current_city"],
            "last_event": shipment["last_event"],
            "estimated_delivery": shipment["estimated_delivery"],
            "updated_at": shipment["updated_at"],
        }
    return payload


def _mask_phone(phone: str) -> str:
    digits = str(phone)
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


def query_order(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """按订单号返回订单与关联运单，是客服最常用的只读工具。"""

    order_no = str(arguments["order_no"])
    row = _fetch_order(context, order_no)
    data = _order_payload(context, row)
    parts = [
        f"订单 {order_no} 当前{data['status_label']}",
        f"金额 {data['amount']}",
    ]
    if data["shipment"]:
        shipment = data["shipment"]
        parts.append(f"运单 {shipment['tracking_no']}（{shipment['status_label']}）")
    return success(context, "query_order", data=data, summary="，".join(parts) + "。")


def list_orders(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """按状态 / 手机号筛选订单，最多返回 20 条。"""

    conditions: list[str] = []
    params: list[Any] = []
    status = arguments.get("status")
    phone = arguments.get("phone")
    limit = int(arguments.get("limit", 5))

    if status:
        conditions.append("o.status = ?")
        params.append(status)
    if phone:
        conditions.append("c.phone = ?")
        params.append(phone)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = context.connection.execute(
        f"""
        SELECT {ORDER_FIELDS}
        FROM orders o JOIN customers c ON c.id = o.customer_id
        {where}
        ORDER BY o.created_at DESC
        LIMIT ?
        """,  # noqa: S608 - where 只由固定片段拼接
        tuple(params),
    ).fetchall()

    items = []
    for row in rows:
        shipment = _fetch_shipment(context, int(row["id"]))
        items.append(
            {
                "order_no": row["order_no"],
                "status": row["status"],
                "status_label": ORDER_STATUS_LABELS.get(row["status"], row["status"]),
                "amount": money(int(row["amount_cents"])),
                "customer_name": row["customer_name"],
                "delivery_address": mask_address(row["delivery_address"]),
                "created_at": row["created_at"],
                "tracking_no": shipment["tracking_no"] if shipment else "",
            }
        )
    if not items:
        summary = "没有找到符合条件的订单。"
    else:
        preview = "、".join(item["order_no"] for item in items[:5])
        summary = f"找到 {len(items)} 个订单：{preview}。"
    return success(
        context,
        "list_orders",
        data={"total": len(items), "orders": items},
        summary=summary,
    )


def change_delivery_address(
    context: ToolContext, arguments: dict[str, Any]
) -> ToolResult:
    """修改未发货订单的收货地址。高风险，需要审批上下文。"""

    require_approval(context, "change_delivery_address")
    order_no = str(arguments["order_no"])
    new_address = str(arguments["new_address"]).strip()
    reason = str(arguments.get("reason", "客户申请修改收货地址")).strip()
    if len(new_address) < 5:
        raise InvalidArguments("new_address 太短，至少需要包含区县与街道", path="$.new_address")

    row = _fetch_order(context, order_no)
    if row["status"] not in {"pending", "confirmed", "packed"}:
        raise ToolError(
            f"订单当前为{ORDER_STATUS_LABELS.get(row['status'], row['status'])}，"
            "已进入运输环节，不能直接修改地址；请改派运单。",
            code="invalid_state",
        )

    old_address = row["delivery_address"]
    context.connection.execute(
        "UPDATE orders SET delivery_address = ?, updated_at = ? WHERE id = ?",
        (new_address, context.timestamp, row["id"]),
    )
    context.connection.execute(
        "UPDATE shipments SET delivery_address = ?, updated_at = ? WHERE order_id = ?",
        (new_address, context.timestamp, row["id"]),
    )
    data = {
        "order_no": order_no,
        "old_address": mask_address(old_address),
        "new_address": new_address,
        "new_address_masked": mask_address(new_address),
        "approval_id": context.approval_id,
        "reason": reason,
    }
    return success(
        context,
        "change_delivery_address",
        data=data,
        summary=f"订单 {order_no} 的新收货地址：{mask_address(new_address)}，已生效。",
    )


def cancel_order(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """取消未发货订单。高风险，需要审批上下文。"""

    require_approval(context, "cancel_order")
    order_no = str(arguments["order_no"])
    reason = str(arguments.get("reason", "客户申请取消订单")).strip()
    row = _fetch_order(context, order_no)
    if row["status"] in {"shipped", "delivered", "cancelled"}:
        raise ToolError(
            f"订单当前为{ORDER_STATUS_LABELS.get(row['status'], row['status'])}，"
            "无法直接取消。",
            code="invalid_state",
        )

    context.connection.execute(
        "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (context.timestamp, row["id"]),
    )
    context.connection.execute(
        "UPDATE shipments SET status = 'cancelled', last_event = '运单已取消', updated_at = ? "
        "WHERE order_id = ?",
        (context.timestamp, row["id"]),
    )
    return success(
        context,
        "cancel_order",
        data={
            "order_no": order_no,
            "status": "cancelled",
            "status_label": ORDER_STATUS_LABELS["cancelled"],
            "reason": reason,
            "approval_id": context.approval_id,
        },
        summary=f"订单 {order_no} 已取消，取消原因：{reason}。",
    )


# -- 工具契约 ---------------------------------------------------------------

def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="query_order",
            description="按订单号查询订单状态、金额、收货地址及关联运单。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {
                    "order_no": {
                        "type": "string",
                        "pattern": r"^SH\d{8}$",
                        "description": "订单号，例如 SH20250001",
                        "maxLength": 12,
                    }
                },
                "required": ["order_no"],
                "additionalProperties": False,
            },
        ),
        query_order,
    )
    registry.register(
        ToolSpec(
            name="list_orders",
            description="查询订单列表，可按订单状态或客户手机号筛选。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(ORDER_STATUS_LABELS),
                        "description": "订单状态枚举",
                    },
                    "phone": {
                        "type": "string",
                        "pattern": r"^1[3-9]\d{9}$",
                        "description": "客户完整手机号",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "返回条数，默认 5",
                    },
                },
                "additionalProperties": False,
            },
        ),
        list_orders,
    )
    registry.register(
        ToolSpec(
            name="change_delivery_address",
            description="修改未发货订单的收货地址。高风险操作，执行前必须人工审批。",
            risk_level="high",
            json_schema={
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "pattern": r"^SH\d{8}$"},
                    "new_address": {
                        "type": "string",
                        "minLength": 5,
                        "maxLength": 200,
                        "description": "新的完整收货地址",
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 100,
                        "description": "修改原因",
                    },
                },
                "required": ["order_no", "new_address"],
                "additionalProperties": False,
            },
        ),
        change_delivery_address,
    )
    registry.register(
        ToolSpec(
            name="cancel_order",
            description="取消尚未发货的订单。高风险操作，执行前必须人工审批。",
            risk_level="high",
            json_schema={
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "pattern": r"^SH\d{8}$"},
                    "reason": {"type": "string", "maxLength": 100},
                },
                "required": ["order_no"],
                "additionalProperties": False,
            },
        ),
        cancel_order,
    )
