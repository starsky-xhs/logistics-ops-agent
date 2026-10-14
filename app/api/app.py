"""FastAPI factory。

FastAPI 是可选依赖。未安装时本模块仍可 import，但调用 ``create_app`` 会给出
明确安装提示；核心 demo/test/eval 不需要它。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

try:  # pragma: no cover - 依赖是否存在由环境决定
    from fastapi import FastAPI
except ImportError as exc:  # pragma: no cover
    FastAPI = None  # type: ignore[assignment]
    _FASTAPI_IMPORT_ERROR: ImportError | None = exc
else:  # pragma: no cover
    _FASTAPI_IMPORT_ERROR = None

from app.config import Settings
from app.service import AgentService


def create_app(
    settings: Settings | None = None,
    *,
    service: AgentService | None = None,
) -> Any:
    """创建 FastAPI 应用。

    ``uvicorn app.api.app:create_app --factory`` 可直接使用。
    """

    if FastAPI is None:
        raise RuntimeError(
            "FastAPI 未安装，HTTP API 不可用。请执行 "
            "`pip install -r requirements.txt`（fastapi + uvicorn）。"
        ) from _FASTAPI_IMPORT_ERROR

    from app.api.routes import router

    owns_service = service is None
    service_instance = service or AgentService(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        yield
        if owns_service:
            service_instance.close()

    app = FastAPI(
        title="物流业务 Agent 服务",
        version="0.1.0",
        description="订单/运单/发票工具编排，高风险操作强制人工审批。",
        lifespan=lifespan,
    )
    app.state.service = service_instance
    app.state.owns_service = owns_service
    app.include_router(router)
    return app
