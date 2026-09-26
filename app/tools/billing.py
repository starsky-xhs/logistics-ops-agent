"""发票与退款工具。

开票是 medium 风险、幂等写操作：同一个 idempotency_key 只会产生一张发票。
退款是 high 风险，必须携带审批上下文。金额始终使用整数分。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.models import ToolSpec
from app.tools.common import INVOICE_STATUS_LABELS, money, require_approval, success
from app.tools.registry import (
    InvalidArguments,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)

INVOICE_FIELDS = """
    i.id, i.invoice_no, i.title, i.amount_cents, i.tax_rate_bp, i.status,
    i.idempotency_key, i.created_at, i.updated_at,
    o.order_no, o.status AS order_status, o.amount_cents AS order_amount_cents,
    c.name AS customer_name
"""


def _invoice_payload(row: Any, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "invoice_no": row["invoice_no"],
        "title": row["title"],
        "amount": money(int(row["amount_cents"])),
        "amount_cents": int(row["amount_cents"]),
        "tax_rate_percent": int(row["tax_rate_bp"]) / 100,
        "status": row["status"],
        "status_label": INVOICE_STATUS_LABELS.get(row["status"], row["status"]),
        "order_no": row["order_no"],
        "created_at": row["created_at"],
        "replayed": replayed,
    }


def _find_invoice(context: ToolContext, *, invoice_no: str = "", order_no: str = "") -> Any:
    if invoice_no:
        row = context.connection.execute(
            f"""
            SELECT {INVOICE_FIELDS}
            FROM invoices i
            JOIN orders o ON o.id = i.order_id
            JOIN customers c ON c.id = o.customer_id
            WHERE i.invoice_no = ?
            """,  # noqa: S608 - 字段白名单常量
            (invoice_no,),
        ).fetchone()
    else:
        row = context.connection.execute(
            f"""
            SELECT {INVOICE_FIELDS}
            FROM invoices i
            JOIN orders o ON o.id = i.order_id
            JOIN customers c ON c.id = o.customer_id
            WHERE o.order_no = ?
            ORDER BY i.id DESC LIMIT 1
            """,  # noqa: S608 - 字段白名单常量
            (order_no,),
        ).fetchone()
    if row is None:
        target = invoice_no or order_no
        raise ToolError(f"未找到发票（{target}）", code="not_found")
    return row


def query_invoice(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """按发票号或订单号查询发票。"""

    invoice_no = str(arguments.get("invoice_no", "")).strip()
    order_no = str(arguments.get("order_no", "")).strip()
    if not invoice_no and not order_no:
        raise InvalidArguments("invoice_no 与 order_no 至少提供一个")
    row = _find_invoice(context, invoice_no=invoice_no, order_no=order_no)
    data = _invoice_payload(row)
    return success(
        context,
        "query_invoice",
        data=data,
        summary=(
            f"发票 {row['invoice_no']} 当前{data['status_label']}，"
            f"抬头 {row['title']}，金额 {data['amount']}。"
        ),
    )


def _next_invoice_no(context: ToolContext) -> str:
    row = context.connection.execute(
        "SELECT MAX(CAST(SUBSTR(invoice_no, 4) AS INTEGER)) AS max_no FROM invoices"
    ).fetchone()
    max_no = int(row["max_no"] or 2025000000)
    return f"INV{max_no + 1}"


def create_invoice(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """为订单开票。medium 风险，幂等。"""

    if not context.idempotency_key:
        raise ToolError(
            "create_invoice 是幂等工具，调用上下文缺少 idempotency_key",
            code="missing_idempotency_key",
        )

    replay = context.connection.execute(
        f"""
        SELECT {INVOICE_FIELDS}
        FROM invoices i
        JOIN orders o ON o.id = i.order_id
        JOIN customers c ON c.id = o.customer_id
        WHERE i.idempotency_key = ?
        """,  # noqa: S608 - 字段白名单常量
        (context.idempotency_key,),
    ).fetchone()
    if replay is not None:
        data = _invoice_payload(replay, replayed=True)
        return success(
            context,
            "create_invoice",
            data=data,
            summary=(
                f"幂等命中：发票 {replay['invoice_no']} 已存在，"
                "本次未重复开票。"
            ),
        )

    order_no = str(arguments["order_no"])
    order = context.connection.execute(
        """
        SELECT o.id, o.order_no, o.status, o.amount_cents, c.name AS customer_name
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.order_no = ?
        """,
        (order_no,),
    ).fetchone()
    if order is None:
        raise ToolError(f"未找到订单 {order_no}", code="not_found")
    if order["status"] == "cancelled":
        raise ToolError(f"订单 {order_no} 已取消，不能开票", code="invalid_state")

    existing = context.connection.execute(
        "SELECT invoice_no, status FROM invoices WHERE order_id = ? ORDER BY id DESC LIMIT 1",
        (order["id"],),
    ).fetchone()
    if existing is not None:
        raise ToolError(
            f"订单 {order_no} 已存在发票 {existing['invoice_no']}，请勿重复开票",
            code="conflict",
        )

    title = str(arguments.get("title", "")).strip()
    if not title:
        title = f"{order['customer_name']}（个人）"
    tax_rate = int(arguments.get("tax_rate", 6))
    invoice_no = _next_invoice_no(context)
    try:
        context.connection.execute(
            """
            INSERT INTO invoices
                (invoice_no, order_id, title, amount_cents, tax_rate_bp,
                 status, idempotency_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, ?)
            """,
            (
                invoice_no,
                order["id"],
                title,
                int(order["amount_cents"]),
                tax_rate * 100,
                context.idempotency_key,
                context.timestamp,
                context.timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # 并发请求可能同时插入同一幂等键，输了就回读赢家结果。
        replay = context.connection.execute(
            f"""
            SELECT {INVOICE_FIELDS}
            FROM invoices i
            JOIN orders o ON o.id = i.order_id
            JOIN customers c ON c.id = o.customer_id
            WHERE i.idempotency_key = ?
            """,  # noqa: S608
            (context.idempotency_key,),
        ).fetchone()
        if replay is not None:
            return success(
                context,
                "create_invoice",
                data=_invoice_payload(replay, replayed=True),
                summary=f"幂等命中：发票 {replay['invoice_no']} 已存在。",
            )
        raise ToolError(f"开票失败：{exc}", code="conflict") from exc

    data = {
        "invoice_no": invoice_no,
        "title": title,
        "amount": money(int(order["amount_cents"])),
        "amount_cents": int(order["amount_cents"]),
        "tax_rate_percent": tax_rate,
        "status": "issued",
        "status_label": INVOICE_STATUS_LABELS["issued"],
        "order_no": order_no,
        "created_at": context.timestamp,
        "replayed": False,
    }
    return success(
        context,
        "create_invoice",
        data=data,
        summary=(
            f"已为订单 {order_no} 开具发票 {invoice_no}，抬头 {title}，"
            f"价税合计 {money(int(order['amount_cents']))}。"
        ),
    )


def refund(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """对已开票订单发起全额退款。高风险，需要审批上下文。"""

    require_approval(context, "refund")
    invoice_no = str(arguments.get("invoice_no", "")).strip()
    order_no = str(arguments.get("order_no", "")).strip()
    reason = str(arguments.get("reason", "客户申请退款")).strip()
    if not invoice_no and not order_no:
        raise InvalidArguments("invoice_no 与 order_no 至少提供一个")

    row = _find_invoice(context, invoice_no=invoice_no, order_no=order_no)
    if row["status"] == "refunded":
        raise ToolError(f"发票 {row['invoice_no']} 已退款，不能重复操作", code="conflict")
    if row["status"] == "void":
        raise ToolError(f"发票 {row['invoice_no']} 已作废，无法退款", code="invalid_state")

    context.connection.execute(
        "UPDATE invoices SET status = 'refunded', updated_at = ? WHERE id = ?",
        (context.timestamp, row["id"]),
    )
    data = {
        "invoice_no": row["invoice_no"],
        "order_no": row["order_no"],
        "refund_amount": money(int(row["amount_cents"])),
        "refund_amount_cents": int(row["amount_cents"]),
        "status": "refunded",
        "status_label": INVOICE_STATUS_LABELS["refunded"],
        "reason": reason,
        "approval_id": context.approval_id,
    }
    return success(
        context,
        "refund",
        data=data,
        summary=(
            f"订单 {row['order_no']} 的发票 {row['invoice_no']} 已全额退款 "
            f"{money(int(row['amount_cents']))}。"
        ),
    )


# -- 工具契约 ---------------------------------------------------------------

INVOICE_NO_SCHEMA = {
    "type": "string",
    "pattern": r"^INV\d{10}$",
    "description": "发票号，例如 INV2025000002",
    "maxLength": 13,
}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="query_invoice",
            description="按发票号或订单号查询发票状态、抬头与金额。",
            risk_level="low",
            json_schema={
                "type": "object",
                "properties": {
                    "invoice_no": INVOICE_NO_SCHEMA,
                    "order_no": {
                        "type": "string",
                        "pattern": r"^SH\d{8}$",
                        "description": "订单号",
                    },
                },
                "additionalProperties": False,
            },
        ),
        query_invoice,
    )
    registry.register(
        ToolSpec(
            name="create_invoice",
            description=(
                "为订单开具发票。medium 风险、幂等；title 缺省时使用客户姓名。"
            ),
            risk_level="medium",
            idempotent=True,
            json_schema={
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "pattern": r"^SH\d{8}$"},
                    "title": {
                        "type": "string",
                        "maxLength": 80,
                        "description": "发票抬头；缺省为客户姓名（个人）",
                    },
                    "tax_rate": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 13,
                        "description": "税率百分比，默认 6",
                    },
                },
                "required": ["order_no"],
                "additionalProperties": False,
            },
        ),
        create_invoice,
    )
    registry.register(
        ToolSpec(
            name="refund",
            description=(
                "对已开票订单发起全额退款。高风险操作，必须人工审批。"
            ),
            risk_level="high",
            json_schema={
                "type": "object",
                "properties": {
                    "invoice_no": INVOICE_NO_SCHEMA,
                    "order_no": {
                        "type": "string",
                        "pattern": r"^SH\d{8}$",
                        "description": "订单号（与 invoice_no 二选一）",
                    },
                    "reason": {"type": "string", "maxLength": 100},
                },
                "additionalProperties": False,
            },
        ),
        refund,
    )
