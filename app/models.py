"""跨层共享的数据结构。

这里刻意只用 dataclass + 标准库：工具层、状态机、评测器可以复用同一组类型，
不把 ORM 或 Web 框架的模型泄漏到业务代码里。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]
RunStatus = Literal[
    "new",
    "running",
    "awaiting_approval",
    "completed",
    "failed",
    "escalated",
]
ApprovalStatus = Literal["pending", "approved", "rejected", "consumed"]
StepStatus = Literal[
    "planned",
    "awaiting_approval",
    "running",
    "completed",
    "failed",
    "rejected",
    "skipped",
]


def utc_now() -> datetime:
    """返回带时区的当前时间，便于序列化到 SQLite / JSON。"""

    return datetime.now(UTC)


def utc_now_iso() -> str:
    """秒级 ISO-8601 时间戳。"""

    return utc_now().isoformat(timespec="seconds")


@dataclass(slots=True)
class Message:
    """会话中的一条消息。"""

    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    message_id: str = ""
    run_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolSpec:
    """工具的稳定契约，不包含可调用对象，方便序列化给 LLM / HTTP API。"""

    name: str
    description: str
    json_schema: dict[str, Any]
    risk_level: RiskLevel
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "json_schema": self.json_schema,
            "risk_level": self.risk_level,
            "idempotent": self.idempotent,
        }

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI function calling 的 tools 元素。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }


@dataclass(slots=True)
class ToolCall:
    """一次待执行 / 已执行的工具调用。"""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。``data`` 是业务数据，``summary`` 是给人和审计看的一句话。"""

    tool_name: str
    ok: bool
    call_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error_code: str = ""
    error_message: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolResult:
        return cls(
            tool_name=str(raw.get("tool_name", "")),
            ok=bool(raw.get("ok", False)),
            call_id=str(raw.get("call_id", "")),
            data=dict(raw.get("data") or {}),
            summary=str(raw.get("summary", "")),
            error_code=str(raw.get("error_code", "")),
            error_message=str(raw.get("error_message", "")),
            latency_ms=float(raw.get("latency_ms", 0.0)),
        )


@dataclass(slots=True)
class PlanStep:
    """规划器产出的一个动作单元，也是状态机里可恢复的执行游标。"""

    index: int
    intent: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    status: StepStatus = "planned"
    needs_approval: bool = False
    result_summary: str = ""
    error: str = ""
    approval_id: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlanStep:
        return cls(
            index=int(raw.get("index", 0)),
            intent=str(raw.get("intent", "")),
            tool_name=str(raw.get("tool_name", "")),
            arguments=dict(raw.get("arguments") or {}),
            reason=str(raw.get("reason", "")),
            status=raw.get("status", "planned"),  # type: ignore[arg-type]
            needs_approval=bool(raw.get("needs_approval", False)),
            result_summary=str(raw.get("result_summary", "")),
            error=str(raw.get("error", "")),
            approval_id=str(raw.get("approval_id", "")),
            started_at=str(raw.get("started_at", "")),
            finished_at=str(raw.get("finished_at", "")),
        )


@dataclass(slots=True)
class Approval:
    """人工审批单。审批是高风险动作执行前的持久化中断点。"""

    approval_id: str
    run_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk_level: RiskLevel
    reason: str
    status: ApprovalStatus = "pending"
    requested_at: str = field(default_factory=utc_now_iso)
    resolved_at: str = ""
    resolved_by: str = ""
    result_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Approval:
        return cls(
            approval_id=str(raw.get("approval_id", "")),
            run_id=str(raw.get("run_id", "")),
            session_id=str(raw.get("session_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            arguments=dict(raw.get("arguments") or {}),
            risk_level=raw.get("risk_level", "high"),  # type: ignore[arg-type]
            reason=str(raw.get("reason", "")),
            status=raw.get("status", "pending"),  # type: ignore[arg-type]
            requested_at=str(raw.get("requested_at", "")),
            resolved_at=str(raw.get("resolved_at", "")),
            resolved_by=str(raw.get("resolved_by", "")),
            result_summary=str(raw.get("result_summary", "")),
        )


@dataclass(slots=True)
class AuditRecord:
    """一条不可变的工具调用审计记录。"""

    audit_id: int
    timestamp: str
    actor: str
    session_id: str
    run_id: str
    tool_name: str
    arguments_summary: str
    result_summary: str
    risk_level: str
    approval_id: str = ""
    approved: bool = False
    status: str = "ok"
    latency_ms: float = 0.0
    error: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentRun:
    """一次用户回合的可观测快照。"""

    run_id: str
    session_id: str
    user_input: str
    status: RunStatus
    steps: list[PlanStep] = field(default_factory=list)
    final_answer: str = ""
    intent: str = ""
    approval_id: str = ""
    planner: str = "mock"
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data
