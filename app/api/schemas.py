"""HTTP 请求/响应模型。

本文件只在 FastAPI 可用时被导入。业务层不依赖 pydantic。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models import AgentRun, AuditRecord, PlanStep, ToolSpec


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)


class ApprovalRequest(BaseModel):
    approved: bool = True
    approver: str = Field(default="api-operator", min_length=1, max_length=64)
    reason: str = Field(default="审批人拒绝该高风险操作", max_length=300)


class StepResponse(BaseModel):
    index: int
    intent: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: str
    needs_approval: bool
    result_summary: str
    error: str
    approval_id: str

    @classmethod
    def from_step(cls, step: PlanStep) -> StepResponse:
        return cls(
            index=step.index,
            intent=step.intent,
            tool_name=step.tool_name,
            arguments=step.arguments,
            reason=step.reason,
            status=step.status,
            needs_approval=step.needs_approval,
            result_summary=step.result_summary,
            error=step.error,
            approval_id=step.approval_id,
        )


class RunResponse(BaseModel):
    run_id: str
    session_id: str
    user_input: str
    status: str
    intent: str
    planner: str
    approval_id: str
    error: str
    final_answer: str
    steps: list[StepResponse]

    @classmethod
    def from_run(cls, run: AgentRun) -> RunResponse:
        return cls(
            run_id=run.run_id,
            session_id=run.session_id,
            user_input=run.user_input,
            status=run.status,
            intent=run.intent,
            planner=run.planner,
            approval_id=run.approval_id,
            error=run.error,
            final_answer=run.final_answer,
            steps=[StepResponse.from_step(step) for step in run.steps],
        )


class ToolResponse(BaseModel):
    name: str
    description: str
    risk_level: str
    idempotent: bool
    json_schema: dict[str, Any]

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> ToolResponse:
        return cls(
            name=spec.name,
            description=spec.description,
            risk_level=spec.risk_level,
            idempotent=spec.idempotent,
            json_schema=spec.json_schema,
        )


class AuditResponse(BaseModel):
    audit_id: int
    timestamp: str
    actor: str
    session_id: str
    run_id: str
    tool_name: str
    arguments_summary: str
    result_summary: str
    risk_level: str
    approval_id: str
    approved: bool
    status: str
    latency_ms: float
    error: str

    @classmethod
    def from_record(cls, record: AuditRecord) -> AuditResponse:
        return cls(
            audit_id=record.audit_id,
            timestamp=record.timestamp,
            actor=record.actor,
            session_id=record.session_id,
            run_id=record.run_id,
            tool_name=record.tool_name,
            arguments_summary=record.arguments_summary,
            result_summary=record.result_summary,
            risk_level=record.risk_level,
            approval_id=record.approval_id,
            approved=record.approved,
            status=record.status,
            latency_ms=record.latency_ms,
            error=record.error,
        )
