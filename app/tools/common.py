"""工具模块共享的小工具函数。"""

from __future__ import annotations

from app.tools.registry import ApprovalRequired, ToolContext, ToolResult

ORDER_STATUS_LABELS: dict[str, str] = {
    "pending": "待付款",
    "confirmed": "已确认",
    "packed": "已打包",
    "shipped": "已发货",
    "delivered": "已签收",
    "cancelled": "已取消",
}

SHIPMENT_STATUS_LABELS: dict[str, str] = {
    "created": "已下单",
    "picked_up": "已揽收",
    "in_transit": "运输中",
    "out_for_delivery": "派送中",
    "delivered": "已签收",
    "exception": "异常",
    "cancelled": "已取消",
}

INVOICE_STATUS_LABELS: dict[str, str] = {
    "issued": "已开票",
    "void": "已作废",
    "refunded": "已退款",
}


def money(cents: int) -> str:
    """整数分转人民币展示字符串。"""

    yuan = cents / 100
    return f"¥{yuan:,.2f}"


def mask_address(address: str) -> str:
    """地址摘要：保留前 8 个字符，避免审计日志出现完整门牌号。"""

    cleaned = address.strip()
    if len(cleaned) <= 8:
        return cleaned
    return f"{cleaned[:8]}***"


def require_approval(context: ToolContext, tool_name: str) -> None:
    """工具实现内部的第二道审批保护。"""

    if not context.approval_id:
        raise ApprovalRequired(tool_name, "high")


def success(
    context: ToolContext,
    tool_name: str,
    *,
    data: dict[str, object],
    summary: str,
) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        ok=True,
        call_id=context.call_id,
        data=data,
        summary=summary,
        latency_ms=0.0,
    )
