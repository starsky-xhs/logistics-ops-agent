"""AgentService：把状态机、工具、审批、审计和 SQLite 会话粘在一起。

对 CLI / FastAPI 暴露三个核心动作：
- ``run(session_id, text)``：执行一个用户回合，可能停在 awaiting_approval；
- ``approve(id)`` / ``reject(id)``：人工处理审批并恢复状态机；
- ``list_audit(...)``：按 run / session 查看审计。

服务层是唯一知道“审批单是否真的 approved”的地方；工具层的
``ApprovalRequired`` 只是第二道防线。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app import audit, guardrails
from app.agent.graph import AgentGraph, AgentState, GraphDependencies
from app.agent.planner import Planner, build_planner
from app.agent.policy import PolicyEngine
from app.backend.db import init_db
from app.config import Settings
from app.models import (
    AgentRun,
    Approval,
    AuditRecord,
    Message,
    PlanStep,
    ToolCall,
    ToolResult,
    ToolSpec,
    utc_now_iso,
)
from app.tools import ApprovalRequired, ToolContext, ToolError, ToolRegistry

LOGGER = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """审批单不存在、状态不对或与运行不匹配。"""


class AgentService:
    """有状态的应用服务。一个实例持有一个 SQLite 连接与一把进程内锁。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db_path: str | Path | None = None,
        seed: bool = True,
        planner: Planner | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        base = settings or Settings.from_env()
        if db_path is not None:
            path = Path(db_path) if str(db_path) == ":memory:" else Path(db_path).expanduser().resolve()
            base = replace(base, db_path=path)
        self.settings = base
        self.connection = init_db(base.db_path, with_seed=seed)
        self.registry = registry or ToolRegistry.default()
        self.policy = PolicyEngine.from_settings(base)
        self.planner = planner or build_planner(base, self.registry)
        self._lock = threading.RLock()
        self._event_sink: Any = None
        self.graph = AgentGraph(
            GraphDependencies(
                registry=self.registry,
                planner=self.planner,
                policy=self.policy,
                execute_tool=self._execute_tool,
                request_approval=self._request_approval,
                persist=self._persist_state,
                emit=self._emit,
            )
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> AgentService:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def run(
        self,
        session_id: str | None,
        user_input: str,
        *,
        on_event: Any = None,
    ) -> AgentRun:
        """执行一个用户回合，必要时暂停在审批中断点。"""

        with self._lock:
            session = self._ensure_session(session_id)
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            now = utc_now_iso()
            state = AgentState(
                run_id=run_id,
                session_id=session,
                user_input=user_input,
                planner=getattr(self.planner, "name", "mock"),
                deadline_epoch=time.time() + self.settings.time_budget_seconds,
                created_at=now,
                updated_at=now,
            )
            self.connection.execute(
                """
                INSERT INTO runs
                    (id, session_id, user_input, status, state_json, created_at, updated_at)
                VALUES (?, ?, ?, 'new', ?, ?, ?)
                """,
                (run_id, session, user_input, json.dumps(state.to_dict(), ensure_ascii=False), now, now),
            )
            self._record_message(session, "user", user_input, run_id=run_id)

            self._event_sink = on_event
            try:
                state = self.graph.run(state)
            finally:
                self._event_sink = None

            self._record_assistant_message(state)
            return self._build_run(state)

    def approve(self, approval_id: str, approver: str = "operator") -> AgentRun:
        """批准审批单，并从中断的 act 节点恢复执行。"""

        with self._lock:
            approver = approver.strip() or "operator"
            row = self._approval_row(approval_id)
            if row["status"] != "pending":
                raise ApprovalError(
                    f"审批单 {approval_id} 当前状态为 {row['status']}，不能重复处理"
                )
            state = self._load_state(row["run_id"])
            self._assert_resumable(state, approval_id)

            resolved_at = utc_now_iso()
            self.connection.execute(
                """
                UPDATE approvals
                SET status = 'approved', resolved_at = ?, resolved_by = ?
                WHERE id = ? AND status = 'pending'
                """,
                (resolved_at, approver, approval_id),
            )
            approval = self._approval_from_row(self._approval_row(approval_id))
            try:
                state = self.graph.resume(state, approval, approved=True)
            except Exception as exc:
                state.status = "failed"
                state.error = f"审批通过后恢复执行失败：{exc}"
                state.final_answer = "审批已记录，但恢复执行失败，未产生业务变更。"
                self._persist_state(state)
                raise ApprovalError(state.error) from exc
            self._record_assistant_message(state)
            return self._build_run(state)

    def reject(
        self,
        approval_id: str,
        approver: str = "operator",
        reason: str = "审批人拒绝该高风险操作",
    ) -> AgentRun:
        """拒绝审批单；工具不会执行，状态机从 observe 收尾。"""

        with self._lock:
            approver = approver.strip() or "operator"
            row = self._approval_row(approval_id)
            if row["status"] != "pending":
                raise ApprovalError(
                    f"审批单 {approval_id} 当前状态为 {row['status']}，不能重复处理"
                )
            state = self._load_state(row["run_id"])
            self._assert_resumable(state, approval_id)

            resolved_at = utc_now_iso()
            self.connection.execute(
                """
                UPDATE approvals
                SET status = 'rejected', resolved_at = ?, resolved_by = ?,
                    result_summary = ?
                WHERE id = ? AND status = 'pending'
                """,
                (resolved_at, approver, reason, approval_id),
            )
            approval = self._approval_from_row(self._approval_row(approval_id))
            approval.reason = reason
            state = self.graph.resume(state, approval, approved=False)
            self._record_assistant_message(state)
            return self._build_run(state)

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT state_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._build_run(AgentState.from_dict(json.loads(row["state_json"])))

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            row = self._approval_row_or_none(approval_id)
            return self._approval_from_row(row) if row is not None else None

    def pending_approval(self, run_id: str) -> Approval | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ? AND status = 'pending'
                ORDER BY requested_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            return self._approval_from_row(row) if row is not None else None

    def list_audit(
        self,
        *,
        limit: int = 50,
        session_id: str = "",
        run_id: str = "",
    ) -> list[AuditRecord]:
        with self._lock:
            return audit.list_records(
                self.connection,
                limit=limit,
                session_id=session_id,
                run_id=run_id,
            )

    def list_messages(self, session_id: str) -> list[Message]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid",
                (session_id,),
            ).fetchall()
            return [
                Message(
                    session_id=row["session_id"],
                    role=row["role"],
                    content=row["content"],
                    message_id=row["id"],
                    run_id=row["run_id"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    def list_tools(self) -> list[ToolSpec]:
        return self.registry.list_specs()

    def health(self) -> dict[str, object]:
        # 不能复用 backend.health 再开连接：内存数据库的 :memory: 在第二个连接里是空的。
        with self._lock:
            try:
                orders = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM orders"
                ).fetchone()["n"]
                pending = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'"
                ).fetchone()["n"]
                ok = True
                error = ""
            except Exception as exc:  # pragma: no cover - 数据库损坏
                orders = 0
                pending = 0
                ok = False
                error = str(exc)
        payload: dict[str, object] = {
            "ok": ok,
            "database": str(self.settings.db_path),
            "orders": int(orders),
            "pending_approvals": int(pending),
            "wal": True,
            "planner": self.settings.planner,
            "approval_policy": self.settings.approval_policy,
            "tools": len(self.registry.list_specs()),
        }
        if error:
            payload["error"] = error
        return payload

    # -- graph callbacks ---------------------------------------------------
    def _request_approval(
        self, state: AgentState, step: PlanStep, reason: str
    ) -> Approval:
        spec = self.registry.get(step.tool_name)
        approval_id = f"appr_{uuid.uuid4().hex[:16]}"
        requested_at = utc_now_iso()
        arguments_json = json.dumps(
            step.arguments, ensure_ascii=False, sort_keys=True
        )
        self.connection.execute(
            """
            INSERT INTO approvals
                (id, run_id, session_id, tool_name, arguments_json, risk_level,
                 reason, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                approval_id,
                state.run_id,
                state.session_id,
                step.tool_name,
                arguments_json,
                spec.risk_level,
                reason,
                requested_at,
            ),
        )
        return Approval(
            approval_id=approval_id,
            run_id=state.run_id,
            session_id=state.session_id,
            tool_name=step.tool_name,
            arguments=step.arguments,
            risk_level=spec.risk_level,
            reason=reason,
            status="pending",
            requested_at=requested_at,
        )

    def _execute_tool(
        self, state: AgentState, step: PlanStep, approval_id: str
    ) -> ToolResult:
        """审批校验 + 注册表执行 + 审计写入。"""

        spec = self.registry.get(step.tool_name)
        approval_row = None
        if step.needs_approval:
            if not approval_id:
                raise ApprovalRequired(step.tool_name, spec.risk_level)
            approval_row = self._approval_row(approval_id)
            self._assert_approval_matches(approval_row, state, step)

        call_id = f"call_{uuid.uuid4().hex[:12]}"
        idempotency_key = (
            self.policy.make_idempotency_key(
                state.run_id, step.tool_name, step.arguments, step.index
            )
            if spec.idempotent
            else ""
        )
        context = ToolContext(
            connection=self.connection,
            actor="agent",
            run_id=state.run_id,
            session_id=state.session_id,
            approval_id=approval_id if step.needs_approval else "",
            idempotency_key=idempotency_key,
            call_id=call_id,
            now=datetime.now(UTC),
        )
        call = ToolCall(
            tool_name=step.tool_name,
            arguments=step.arguments,
            call_id=call_id,
            reason=step.reason,
        )

        try:
            result = self.registry.execute(call.tool_name, call.arguments, context)
        except ToolError as exc:
            result = ToolResult(
                tool_name=step.tool_name,
                ok=False,
                call_id=call_id,
                summary=f"{step.tool_name} 未能执行：{exc.message}",
                error_code=exc.code,
                error_message=exc.message,
            )

        audit.record(
            self.connection,
            actor="agent",
            session_id=state.session_id,
            run_id=state.run_id,
            tool_name=step.tool_name,
            arguments=step.arguments,
            result=result,
            risk_level=spec.risk_level,
            approval_id=approval_id if step.needs_approval else "",
            approved=bool(approval_row),
            mask_pii=self.settings.audit_mask_pii,
            idempotency_key=idempotency_key,
        )

        if approval_row is not None:
            summary = guardrails.mask_pii(result.summary or result.error_message)
            self.connection.execute(
                """
                UPDATE approvals
                SET status = 'consumed', result_summary = ?
                WHERE id = ? AND status = 'approved'
                """,
                (summary[:300], approval_id),
            )
        return result

    def _assert_approval_matches(
        self,
        row: sqlite3.Row,
        state: AgentState,
        step: PlanStep,
    ) -> None:
        if row["status"] != "approved":
            raise ApprovalError(
                f"审批单 {row['id']} 状态为 {row['status']}，未获批准或已被使用"
            )
        if row["run_id"] != state.run_id or row["tool_name"] != step.tool_name:
            raise ApprovalError("审批单与待执行步骤不匹配，已阻止执行")
        try:
            approved_arguments = json.loads(row["arguments_json"])
        except json.JSONDecodeError as exc:
            raise ApprovalError("审批参数损坏，已阻止执行") from exc
        if approved_arguments != step.arguments:
            raise ApprovalError("审批后工具参数发生变化，已阻止执行")

    def _assert_resumable(self, state: AgentState, approval_id: str) -> None:
        if state.status != "awaiting_approval":
            raise ApprovalError(
                f"运行 {state.run_id} 当前状态为 {state.status}，不在等待审批"
            )
        if state.approval_id != approval_id:
            raise ApprovalError("审批单不是该运行当前等待的审批")

    # -- persistence -------------------------------------------------------
    def _persist_state(self, state: AgentState) -> None:
        state.touch()
        self.connection.execute(
            """
            UPDATE runs
            SET status = ?, state_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                state.status,
                json.dumps(state.to_dict(), ensure_ascii=False),
                state.updated_at,
                state.run_id,
            ),
        )
        self.connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (state.updated_at, state.session_id),
        )

    def _load_state(self, run_id: str) -> AgentState:
        row = self.connection.execute(
            "SELECT state_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"run 不存在：{run_id}")
        return AgentState.from_dict(json.loads(row["state_json"]))

    def _build_run(self, state: AgentState) -> AgentRun:
        return AgentRun(
            run_id=state.run_id,
            session_id=state.session_id,
            user_input=state.user_input,
            status=cast(Any, state.status),
            steps=state.plan,
            final_answer=state.final_answer,
            intent=state.intent,
            approval_id=state.approval_id,
            planner=state.planner,
            error=state.error,
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

    def _ensure_session(self, session_id: str | None) -> str:
        session = (session_id or f"sess_{uuid.uuid4().hex[:12]}").strip()
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO sessions (id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (session, now, now),
        )
        return session

    def _record_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO messages (id, session_id, run_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"msg_{uuid.uuid4().hex[:16]}",
                session_id,
                run_id,
                role,
                content,
                utc_now_iso(),
            ),
        )

    def _record_assistant_message(self, state: AgentState) -> None:
        if state.status == "awaiting_approval":
            content = (
                f"该操作需要人工审批，审批单号：{state.approval_id}。"
                "审批通过后我会继续执行。"
            )
        else:
            content = state.final_answer
        if content:
            self._record_message(
                state.session_id,
                "assistant",
                content,
                run_id=state.run_id,
            )

    def _approval_row(self, approval_id: str) -> sqlite3.Row:
        row = self._approval_row_or_none(approval_id)
        if row is None:
            raise ApprovalError(f"审批单不存在：{approval_id}")
        return row

    def _approval_row_or_none(self, approval_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> Approval:
        try:
            arguments = json.loads(row["arguments_json"])
        except json.JSONDecodeError:
            arguments = {}
        return Approval(
            approval_id=row["id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            arguments=arguments,
            risk_level=row["risk_level"],
            reason=row["reason"],
            status=row["status"],
            requested_at=row["requested_at"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
            result_summary=row["result_summary"],
        )

    def _emit(self, event: dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is not None:
            sink(event)
