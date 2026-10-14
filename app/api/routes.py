"""FastAPI 路由。

``POST /api/chat`` 返回 SSE：
- ``step``：intake/plan/act 轨迹；
- ``tool_result``：工具结果摘要；
- ``approval_required``：高风险中断；
- ``done``：本回合结束（可能仍等待审批）。

审批恢复走独立 JSON 接口。SSE 生成器把同步 Agent 放到线程里跑，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    ApprovalRequest,
    AuditResponse,
    ChatRequest,
    RunResponse,
    ToolResponse,
)
from app.service import AgentService, ApprovalError

router = APIRouter()


def _service(request: Request) -> AgentService:
    return request.app.state.service


@router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    payload = _service(request).health()
    payload["status"] = "ok" if payload.get("ok") else "degraded"
    return payload


@router.get("/api/tools", response_model=list[ToolResponse])
def list_tools(request: Request) -> list[ToolResponse]:
    return [ToolResponse.from_spec(spec) for spec in _service(request).list_tools()]


@router.post("/api/chat")
def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    service = _service(request)
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def sink(event: dict[str, Any]) -> None:
        events.put(event)

    def worker() -> None:
        try:
            service.run(payload.session_id, payload.message, on_event=sink)
        except Exception as exc:  # 兜底：把异常变成可观测的 SSE 事件
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, name="agent-sse", daemon=True).start()

    async def event_stream() -> Any:
        while True:
            item = await asyncio.to_thread(events.get)
            if item is None:
                break
            event_type = str(item.get("type", "message"))
            data = json.dumps(item, ensure_ascii=False)
            yield f"event: {event_type}\ndata: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/approvals/{approval_id}", response_model=RunResponse)
def resolve_approval(
    approval_id: str, payload: ApprovalRequest, request: Request
) -> RunResponse:
    service = _service(request)
    try:
        if payload.approved:
            run = service.approve(approval_id, approver=payload.approver)
        else:
            run = service.reject(
                approval_id,
                approver=payload.approver,
                reason=payload.reason,
            )
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse.from_run(run)


@router.get("/api/approvals/{approval_id}")
def get_approval(approval_id: str, request: Request) -> dict[str, Any]:
    approval = _service(request).get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    return approval.to_dict()


@router.get("/api/audit", response_model=list[AuditResponse])
def list_audit(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    session_id: str = "",
    run_id: str = "",
) -> list[AuditResponse]:
    records = _service(request).list_audit(
        limit=limit, session_id=session_id, run_id=run_id
    )
    return [AuditResponse.from_record(record) for record in records]


@router.get("/api/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, request: Request) -> RunResponse:
    run = _service(request).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    return RunResponse.from_run(run)
