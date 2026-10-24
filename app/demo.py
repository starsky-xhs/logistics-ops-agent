"""离线演示：不联网、不需要 API Key。

运行 ``python -m app.demo`` 会依次演示：
1. 查订单 + 查轨迹（低风险，直接完成）；
2. 改收货地址（高风险，中断 → 人工批准 → 恢复执行）；
3. 知识库外问题（转人工并生成工单）。
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from app.agent.policy import PolicyEngine
from app.config import Settings
from app.models import AgentRun
from app.service import AgentService


def _configure_console() -> None:
    """Windows 默认 GBK 控制台无法写 ¥ 等字符，尽量切到 UTF-8。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _print_run(service: AgentService, run: AgentRun) -> None:
    policy = PolicyEngine.from_settings(service.settings)
    print(f"  运行 {run.run_id}｜规划器 {run.planner}｜意图 {run.intent}")
    for step in run.steps:
        spec = service.registry.get(step.tool_name) if service.registry.has(step.tool_name) else None
        risk = spec.risk_level if spec else "unknown"
        approval = "需要人工审批" if step.needs_approval else "无需审批"
        print(
            f"  [{step.index + 1}] {step.intent} → {step.tool_name}"
            f"｜风险 {policy.risk_label(risk)}（{approval}）"
        )
        print(f"      为什么选它：{step.reason}")
        print(f"      参数：{step.arguments}")
        if step.result_summary:
            print(f"      结果：{step.result_summary}")
        print(f"      状态：{step.status}")
    print(f"  最终状态：{run.status}")
    if run.final_answer:
        print("  最终回复：")
        for line in run.final_answer.splitlines():
            print(f"    {line}")
    if run.status == "awaiting_approval":
        print(f"  ⚠ 等待审批单：{run.approval_id}（demo 将自动调用 approve 演示恢复）")


def _print_approval(service: AgentService, approval_id: str) -> None:
    approval = service.get_approval(approval_id)
    if approval is None:
        return
    print("\n  -- 人工审批检查点 --")
    print(f"  审批单：{approval.approval_id}")
    print(f"  工具：{approval.tool_name}｜风险：{approval.risk_level}｜状态：{approval.status}")
    print(f"  原因：{approval.reason}")
    print(f"  参数：{approval.arguments}")


def scenario_one(service: AgentService) -> None:
    print("\n" + "=" * 78)
    print("场景 1：查订单 + 查轨迹（只读、低风险、串联槽位）")
    print("=" * 78)
    user_input = "帮我查下订单 SH20250001 现在到哪了"
    print(f"用户：{user_input}\n")
    run = service.run("demo-session-1", user_input)
    _print_run(service, run)


def scenario_two(service: AgentService) -> None:
    print("\n" + "=" * 78)
    print("场景 2：修改收货地址（高风险 → 审批 → 恢复执行）")
    print("=" * 78)
    user_input = "我的订单 SH20250006 收货地址改成杭州市滨江区江南大道 88 号"
    print(f"用户：{user_input}\n")
    run = service.run("demo-session-2", user_input)
    _print_run(service, run)
    if run.status != "awaiting_approval" or not run.approval_id:
        print("  预期中的审批中断没有发生，请检查策略配置。")
        return

    _print_approval(service, run.approval_id)
    print("  演示操作：调用 approve('...')，模拟客服主管确认。")
    resumed = service.approve(run.approval_id, approver="demo-supervisor")
    print("\n  -- 审批后继续执行 --")
    _print_run(service, resumed)

    approval = service.get_approval(run.approval_id)
    if approval is not None:
        print(f"  审批单最终状态：{approval.status}｜处理人：{approval.resolved_by}")


def scenario_three(service: AgentService) -> None:
    print("\n" + "=" * 78)
    print("场景 3：知识库外问题（不猜测，转人工生成工单）")
    print("=" * 78)
    user_input = "你们能寄到火星吗？"
    print(f"用户：{user_input}\n")
    run = service.run("demo-session-3", user_input)
    _print_run(service, run)


def run_demo() -> None:
    _configure_console()
    print("物流业务 Agent · 离线演示")
    print("模式：PLANNER=mock（规则规划器）｜数据库：内存 SQLite｜网络：未使用")
    settings = Settings(db_path=Path(":memory:"), planner="mock", approval_policy="high_only")
    with AgentService(settings) as service:
        scenario_one(service)
        scenario_two(service)
        scenario_three(service)
        print("\n" + "=" * 78)
        print("审计日志（最近记录；参数与地址已脱敏）")
        print("=" * 78)
        from app.audit import render_table

        print(render_table(service.list_audit(limit=6)))


def main() -> None:
    run_demo()


if __name__ == "__main__":
    main()
