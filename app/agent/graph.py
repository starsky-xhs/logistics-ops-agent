"""显式 Agent 状态机。

设计对照
--------
LangGraph 的 ``StateGraph`` 用节点（node）+ 条件边（edge）+ checkpointer 描述流程，
在需要人工确认时通过 ``interrupt`` 暂停，再用同一个 thread_id 恢复。这里没有引入
LangGraph，但保留了同样的四个核心概念：

* 节点：intake / plan / act / observe / respond / escalate / fail；
* 边：每个节点返回下一节点名，``suspend`` 表示中断；
* 状态：``AgentState``，完整序列化进 SQLite ``runs.state_json``；
* 中断恢复：高风险动作在 act 节点创建 approvals 记录并 suspend，审批后从 act 恢复。

自己实现的原因不是“重复造轮子”，而是这个项目的状态只有十来个字段、流程稳定，
用两百行显式代码换来零依赖、可读、可单测和可持久化恢复，比引入图框架更划算。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.planner import Planner
from app.agent.policy import PolicyEngine
from app.models import (
    Approval,
    PlanStep,
    ToolResult,
    utc_now_iso,
)
from app.tools import ToolRegistry

LOGGER = logging.getLogger(__name__)

TERMINAL = "done"
SUSPEND = "suspend"

NODE_INTAKE = "intake"
NODE_PLAN = "plan"
NODE_ACT = "act"
NODE_OBSERVE = "observe"
NODE_RESPOND = "respond"
NODE_ESCALATE = "escalate"
NODE_FAIL = "fail"


@dataclass(slots=True)
class AgentState:
    """可序列化的单次运行状态。

    不把 sqlite 连接、planner 对象放进来；恢复时由 AgentService 重新注入依赖。
    """

    run_id: str
    session_id: str
    user_input: str
    status: str = "new"
    node: str = NODE_INTAKE
    sanitized_input: str = ""
    intent: str = ""
    plan: list[PlanStep] = field(default_factory=list)
    cursor: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    error: str = ""
    planner: str = "mock"
    approval_id: str = ""
    approved_step_index: int | None = None
    approved_approval_id: str = ""
    rejection_reason: str = ""
    steps_used: int = 0
    action_history: list[str] = field(default_factory=list)
    deadline_epoch: float = 0.0
    injection_blocked: bool = False
    injection_reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "user_input": self.user_input,
            "status": self.status,
            "node": self.node,
            "sanitized_input": self.sanitized_input,
            "intent": self.intent,
            "plan": [step.to_dict() for step in self.plan],
            "cursor": self.cursor,
            "observations": self.observations,
            "final_answer": self.final_answer,
            "error": self.error,
            "planner": self.planner,
            "approval_id": self.approval_id,
            "approved_step_index": self.approved_step_index,
            "approved_approval_id": self.approved_approval_id,
            "rejection_reason": self.rejection_reason,
            "steps_used": self.steps_used,
            "action_history": self.action_history,
            "deadline_epoch": self.deadline_epoch,
            "injection_blocked": self.injection_blocked,
            "injection_reason": self.injection_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentState:
        return cls(
            run_id=str(raw["run_id"]),
            session_id=str(raw["session_id"]),
            user_input=str(raw.get("user_input", "")),
            status=str(raw.get("status", "new")),
            node=str(raw.get("node", NODE_INTAKE)),
            sanitized_input=str(raw.get("sanitized_input", "")),
            intent=str(raw.get("intent", "")),
            plan=[PlanStep.from_dict(item) for item in raw.get("plan", [])],
            cursor=int(raw.get("cursor", 0)),
            observations=list(raw.get("observations", [])),
            final_answer=str(raw.get("final_answer", "")),
            error=str(raw.get("error", "")),
            planner=str(raw.get("planner", "mock")),
            approval_id=str(raw.get("approval_id", "")),
            approved_step_index=raw.get("approved_step_index"),
            approved_approval_id=str(raw.get("approved_approval_id", "")),
            rejection_reason=str(raw.get("rejection_reason", "")),
            steps_used=int(raw.get("steps_used", 0)),
            action_history=list(raw.get("action_history", [])),
            deadline_epoch=float(raw.get("deadline_epoch", 0.0)),
            injection_blocked=bool(raw.get("injection_blocked", False)),
            injection_reason=str(raw.get("injection_reason", "")),
            created_at=str(raw.get("created_at", utc_now_iso())),
            updated_at=str(raw.get("updated_at", utc_now_iso())),
        )


@dataclass(slots=True)
class GraphDependencies:
    """状态机与外部世界之间的最小接口。

    service 负责把回调接到 SQLite、审计、审批与 SSE；graph/nodes 不直接写表，
    这样状态机可以单独用内存 fake 测试。
    """

    registry: ToolRegistry
    planner: Planner
    policy: PolicyEngine
    execute_tool: Callable[[AgentState, PlanStep, str], ToolResult]
    request_approval: Callable[[AgentState, PlanStep, str], Approval]
    persist: Callable[[AgentState], None]
    emit: Callable[[dict[str, Any]], None] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(event)


class AgentGraph:
    """显式状态机执行器。"""

    def __init__(self, dependencies: GraphDependencies) -> None:
        self.deps = dependencies

    # -- public ------------------------------------------------------------
    def run(self, state: AgentState) -> AgentState:
        """从 intake 开始执行，直到 done / awaiting_approval。"""

        if state.status == "awaiting_approval":
            raise ValueError("该运行正在等待审批，请使用 resume 恢复")
        state.status = "running"
        state.node = NODE_INTAKE
        state.deadline_epoch = state.deadline_epoch or self.deps.policy.deadline_from_now()
        self.deps.persist(state)
        self.deps.publish(
            {
                "type": "run_started",
                "run_id": state.run_id,
                "session_id": state.session_id,
                "planner": state.planner,
            }
        )
        return self._drive(state, NODE_INTAKE)

    def resume(
        self,
        state: AgentState,
        approval: Approval,
        *,
        approved: bool,
    ) -> AgentState:
        """审批后从中断点恢复。

        批准：从 act 节点继续执行被挂起的步骤；
        拒绝：把该步骤记为 rejected，写入一条观察结果，从 observe 继续收尾。
        """

        if state.status != "awaiting_approval":
            raise ValueError("只有 awaiting_approval 的运行可以恢复")
        if state.approval_id != approval.approval_id:
            raise ValueError("审批单与当前运行不匹配")
        if state.cursor >= len(state.plan):
            raise ValueError("审批对应的计划步骤已不存在")

        step = state.plan[state.cursor]
        if step.tool_name != approval.tool_name:
            raise ValueError("审批工具与待执行步骤不匹配")

        if approved:
            state.approved_step_index = state.cursor
            state.approved_approval_id = approval.approval_id
            state.status = "running"
            state.node = NODE_ACT
            self.deps.publish(
                {
                    "type": "approval_resolved",
                    "run_id": state.run_id,
                    "approval_id": approval.approval_id,
                    "approved": True,
                    "approver": approval.resolved_by,
                }
            )
            self.deps.persist(state)
            return self._drive(state, NODE_ACT)

        step.status = "rejected"
        step.finished_at = utc_now_iso()
        step.error = "人工审批被拒绝，未执行"
        state.rejection_reason = approval.reason
        state.observations.append(
            {
                "step_index": step.index,
                "tool": step.tool_name,
                "ok": False,
                "summary": "人工审批被拒绝，工具未执行。",
                "error_code": "approval_rejected",
                "error_message": step.error,
                "data": {},
                "approval_id": approval.approval_id,
                "rejected": True,
            }
        )
        state.cursor += 1
        state.approval_id = ""
        state.approved_step_index = None
        state.approved_approval_id = ""
        state.status = "running"
        state.node = NODE_OBSERVE
        self.deps.publish(
            {
                "type": "approval_resolved",
                "run_id": state.run_id,
                "approval_id": approval.approval_id,
                "approved": False,
                "approver": approval.resolved_by,
            }
        )
        self.deps.persist(state)
        return self._drive(state, NODE_OBSERVE)

    # -- internal ----------------------------------------------------------
    def _drive(self, state: AgentState, start_node: str) -> AgentState:
        from app.agent import nodes

        node = start_node
        while True:
            state.node = node
            if self.deps.policy.timed_out(state.deadline_epoch):
                state.error = (
                    f"执行超过 {self.deps.policy.time_budget_seconds:.0f} 秒时间预算，"
                    "已停止后续工具调用。"
                )
                state.status = "failed"
                node = NODE_FAIL
                continue

            try:
                next_node = self._dispatch(nodes, node, state)
            except Exception as exc:  # pragma: no cover - 由故障注入测试覆盖
                LOGGER.exception("节点 %s 执行失败", node)
                state.error = f"内部错误（{node}）：{exc}"
                state.status = "failed"
                next_node = NODE_FAIL

            state.touch()
            self.deps.persist(state)

            if next_node == SUSPEND:
                self.deps.publish(self._done_event(state, suspended=True))
                return state
            if next_node == TERMINAL:
                self.deps.publish(self._done_event(state, suspended=False))
                return state

            node = next_node

    def _dispatch(self, nodes: Any, node: str, state: AgentState) -> str:
        handlers = {
            NODE_INTAKE: nodes.intake,
            NODE_PLAN: nodes.plan,
            NODE_ACT: nodes.act,
            NODE_OBSERVE: nodes.observe,
            NODE_RESPOND: nodes.respond,
            NODE_ESCALATE: nodes.escalate,
            NODE_FAIL: nodes.fail,
        }
        handler = handlers.get(node)
        if handler is None:
            raise ValueError(f"未知节点：{node}")
        return str(handler(state, self.deps))

    def _done_event(self, state: AgentState, *, suspended: bool) -> dict[str, Any]:
        return {
            "type": "done",
            "run_id": state.run_id,
            "session_id": state.session_id,
            "status": state.status,
            "suspended": suspended,
            "approval_id": state.approval_id,
            "final_answer": state.final_answer,
            "steps_used": state.steps_used,
        }
