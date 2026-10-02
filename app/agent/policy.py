"""风险、审批、幂等与预算策略。

策略层不碰 SQL，只做纯函数决策，方便单测覆盖。high 风险永远需要审批，
即使把 APPROVAL_POLICY 改成 conservative 也只是把 medium 纳入审批，不能降低
high 的门槛——这是代码层面的硬约束。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.models import RiskLevel, ToolSpec

RISK_ORDER: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
RISK_LABELS: dict[str, str] = {"low": "低", "medium": "中", "high": "高"}


@dataclass(frozen=True, slots=True)
class PolicyEngine:
    """从配置派生的纯策略对象。"""

    approval_policy: str = "high_only"
    max_steps: int = 8
    time_budget_seconds: float = 20.0

    @classmethod
    def from_settings(cls, settings: Settings) -> PolicyEngine:
        return cls(
            approval_policy=settings.approval_policy,
            max_steps=settings.max_steps,
            time_budget_seconds=settings.time_budget_seconds,
        )

    def requires_approval(self, spec: ToolSpec) -> bool:
        """high 必审批；conservative 模式额外审批 medium。"""

        if spec.risk_level == "high":
            return True
        return spec.risk_level == "medium" and self.approval_policy == "conservative"

    def approval_reason(self, spec: ToolSpec, user_input: str) -> str:
        action = {
            "change_delivery_address": "修改收货地址",
            "cancel_order": "取消订单",
            "reroute": "修改派送地址",
            "refund": "发起退款",
        }.get(spec.name, spec.name)
        return (
            f"高风险操作：{action}（工具 {spec.name}）。"
            f"该动作会改变真实履约数据，需人工确认后执行。用户请求：{user_input[:120]}"
        )

    def risk_label(self, risk_level: RiskLevel | str) -> str:
        return RISK_LABELS.get(str(risk_level), str(risk_level))

    @staticmethod
    def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        """用于循环检测的稳定指纹。"""

        payload = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def make_idempotency_key(
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        step_index: int,
    ) -> str:
        """同一 run 的同一步同一参数重放时得到同一个键。"""

        payload = json.dumps(
            {
                "run_id": run_id,
                "tool": tool_name,
                "step": step_index,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"idem_{digest[:32]}"

    def deadline_from_now(self) -> float:
        return time.time() + self.time_budget_seconds

    @staticmethod
    def seconds_left(deadline_epoch: float) -> float:
        return max(0.0, deadline_epoch - time.time())

    @staticmethod
    def timed_out(deadline_epoch: float) -> bool:
        return deadline_epoch > 0 and time.time() > deadline_epoch
