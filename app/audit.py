"""审计日志读写。

审计写入放在 AgentService 的工具执行边界，而不是散落在各业务工具里，
这样即使以后新增工具也不会漏记。参数与结果摘要必须经过 PII 脱敏。
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any

from app import guardrails
from app.models import AuditRecord, ToolResult, utc_now_iso


def record(
    connection: sqlite3.Connection,
    *,
    actor: str,
    session_id: str,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    risk_level: str,
    approval_id: str = "",
    approved: bool = False,
    mask_pii: bool = True,
    idempotency_key: str = "",
) -> int:
    """写入一条工具调用审计，返回自增 id。"""

    if mask_pii:
        args_summary = guardrails.summarize_mapping(arguments)
        result_summary = guardrails.mask_pii(result.summary or result.error_message)
        args_summary = guardrails.mask_pii(args_summary)
    else:
        args_summary = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True
        )[:500]
        result_summary = (result.summary or result.error_message)[:500]
    cursor = connection.execute(
        """
        INSERT INTO audit_log
            (ts, actor, session_id, run_id, tool_name, args_summary, result_summary,
             risk_level, approval_id, approved, status, latency_ms, error,
             idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            actor,
            session_id,
            run_id,
            tool_name,
            args_summary,
            result_summary,
            risk_level,
            approval_id,
            1 if approved else 0,
            "ok" if result.ok else "error",
            result.latency_ms,
            result.error_message if not result.ok else "",
            idempotency_key,
        ),
    )
    return int(cursor.lastrowid)


def list_records(
    connection: sqlite3.Connection,
    *,
    limit: int = 50,
    session_id: str = "",
    run_id: str = "",
) -> list[AuditRecord]:
    """按时间倒序读取审计记录。"""

    limit = max(1, min(int(limit), 500))
    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = connection.execute(
        f"""
        SELECT id, ts, actor, session_id, run_id, tool_name, args_summary,
               result_summary, risk_level, approval_id, approved, status,
               latency_ms, error, idempotency_key
        FROM audit_log {where}
        ORDER BY id DESC LIMIT ?
        """,  # noqa: S608 - where 只由固定片段拼接
        tuple(params),
    ).fetchall()
    return [
        AuditRecord(
            audit_id=int(row["id"]),
            timestamp=str(row["ts"]),
            actor=str(row["actor"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            tool_name=str(row["tool_name"]),
            arguments_summary=str(row["args_summary"]),
            result_summary=str(row["result_summary"]),
            risk_level=str(row["risk_level"]),
            approval_id=str(row["approval_id"]),
            approved=bool(row["approved"]),
            status=str(row["status"]),
            latency_ms=float(row["latency_ms"]),
            error=str(row["error"]),
            idempotency_key=str(row["idempotency_key"]),
        )
        for row in rows
    ]


def _display_width(text: str) -> int:
    """粗略计算中英文混排宽度，让 CLI 表格不歪。"""

    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _local_timestamp(value: str) -> str:
    """数据库存 UTC；CLI 展示时转成本地时区。"""

    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")[:19]


def render_table(records: list[AuditRecord]) -> str:
    """输出适合终端阅读的审计表。"""

    if not records:
        return "（暂无审计记录）"

    headers = ("时间", "操作者", "工具", "风险", "审批", "状态", "耗时", "结果摘要")
    rows: list[tuple[str, ...]] = []
    for item in records:
        rows.append(
            (
                _local_timestamp(item.timestamp),
                item.actor,
                item.tool_name,
                item.risk_level,
                "是" if item.approved else "否",
                item.status,
                f"{item.latency_ms:.1f}ms",
                _truncate(item.result_summary.replace("\n", " "), 48),
            )
        )

    widths = []
    for index, header in enumerate(headers):
        widths.append(max(_display_width(header), *(_display_width(row[index]) for row in rows)))
    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [separator]
    lines.append("|" + "|".join(f" {_pad(header, widths[i])} " for i, header in enumerate(headers)) + "|")
    lines.append(separator)
    for row in rows:
        lines.append("|" + "|".join(f" {_pad(cell, widths[i])} " for i, cell in enumerate(row)) + "|")
    lines.append(separator)
    return "\n".join(lines)
