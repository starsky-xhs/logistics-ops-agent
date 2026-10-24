"""测试共享夹具。

全部使用内存 SQLite，避免污染 data/ 目录；需要验证跨进程恢复的用例
单独使用 data/.test_tmp 下的临时文件。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.planner import Planner
from app.backend.db import init_db
from app.config import Settings
from app.service import AgentService


def memory_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "db_path": Path(":memory:"),
        "planner": "mock",
        "approval_policy": "high_only",
        "max_steps": 8,
        "time_budget_seconds": 20.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_service(
    *,
    planner: Planner | None = None,
    **overrides: object,
) -> AgentService:
    return AgentService(memory_settings(**overrides), planner=planner)


def memory_db():
    return init_db(":memory:")
