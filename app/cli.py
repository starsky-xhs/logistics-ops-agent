"""命令行入口。

用法::

    python -m app.cli demo
    python -m app.cli run "查一下订单 SH20250001"
    python -m app.cli approve appr_xxx
    python -m app.cli eval
    python -m app.cli audit --limit 20
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import replace
from pathlib import Path

from app.agent.policy import PolicyEngine
from app.audit import render_table
from app.config import ConfigError, Settings
from app.demo import run_demo
from app.eval import DEFAULT_CASES_PATH, DEFAULT_REPORT_PATH, render_console, run_eval
from app.models import AgentRun
from app.service import AgentService, ApprovalError


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings = replace(settings, db_path=Path(args.db).expanduser().resolve())
    return settings


def _print_run(service: AgentService, run: AgentRun) -> None:
    policy = PolicyEngine.from_settings(service.settings)
    print(f"run_id: {run.run_id}")
    print(f"session: {run.session_id}｜status: {run.status}｜intent: {run.intent}")
    for step in run.steps:
        spec = service.registry.get(step.tool_name) if service.registry.has(step.tool_name) else None
        risk = policy.risk_label(spec.risk_level) if spec else "未知"
        approval = "需审批" if step.needs_approval else "直接执行"
        print(
            f"  {step.index + 1}. {step.tool_name}｜{risk}风险/{approval}｜"
            f"状态 {step.status}"
        )
        print(f"     参数：{step.arguments}")
        print(f"     理由：{step.reason}")
        if step.result_summary:
            print(f"     结果：{step.result_summary}")
    if run.final_answer:
        print("\n最终回复：")
        print(run.final_answer)
    if run.status == "awaiting_approval":
        print(
            f"\n需要人工审批：{run.approval_id}\n"
            f"执行：python -m app.cli approve {run.approval_id}"
        )


def command_demo(_args: argparse.Namespace) -> int:
    run_demo()
    return 0


def command_run(args: argparse.Namespace) -> int:
    with AgentService(_settings(args)) as service:
        run = service.run(args.session, args.text)
        _print_run(service, run)
        print(f"\n审计记录数：{len(service.list_audit(limit=500, run_id=run.run_id))}")
    return 0


def command_approve(args: argparse.Namespace) -> int:
    with AgentService(_settings(args)) as service:
        try:
            run = service.approve(args.approval_id, approver=args.approver)
        except ApprovalError as exc:
            print(f"审批失败：{exc}", file=sys.stderr)
            return 2
        print(f"审批单 {args.approval_id} 已批准，继续执行。\n")
        _print_run(service, run)
    return 0


def command_reject(args: argparse.Namespace) -> int:
    with AgentService(_settings(args)) as service:
        try:
            run = service.reject(
                args.approval_id,
                approver=args.approver,
                reason=args.reason,
            )
        except ApprovalError as exc:
            print(f"审批失败：{exc}", file=sys.stderr)
            return 2
        print(f"审批单 {args.approval_id} 已拒绝。\n")
        _print_run(service, run)
    return 0


def command_eval(args: argparse.Namespace) -> int:
    report = run_eval(
        Path(args.cases),
        limit=args.limit,
        report_path=None if args.no_report else Path(args.report),
        verbose=args.verbose,
    )
    print(render_console(report))
    if not args.no_report:
        print(f"Markdown 报告：{Path(args.report)}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    with AgentService(_settings(args)) as service:
        records = service.list_audit(
            limit=args.limit,
            session_id=args.session or "",
            run_id=args.run or "",
        )
        print(render_table(records))
    return 0


def command_tools(args: argparse.Namespace) -> int:
    with AgentService(_settings(args)) as service:
        for spec in service.list_tools():
            idem = "，幂等" if spec.idempotent else ""
            print(f"{spec.name:<26} {spec.risk_level:<6}{idem}  {spec.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="物流业务 Agent 离线 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="运行三个离线演示场景")
    demo.set_defaults(func=command_demo)

    run = subparsers.add_parser("run", help="执行一条消息")
    run.add_argument("text", help="用户输入")
    run.add_argument("--session", default="", help="复用已有会话 id")
    run.add_argument("--db", default="", help="覆盖数据库路径")
    run.set_defaults(func=command_run)

    approve = subparsers.add_parser("approve", help="批准高风险审批单并恢复执行")
    approve.add_argument("approval_id")
    approve.add_argument("--approver", default="cli-operator")
    approve.add_argument("--db", default="", help="覆盖数据库路径")
    approve.set_defaults(func=command_approve)

    reject = subparsers.add_parser("reject", help="拒绝高风险审批单")
    reject.add_argument("approval_id")
    reject.add_argument("--approver", default="cli-operator")
    reject.add_argument("--reason", default="审批人拒绝该高风险操作")
    reject.add_argument("--db", default="", help="覆盖数据库路径")
    reject.set_defaults(func=command_reject)

    evaluation = subparsers.add_parser("eval", help="运行 30 条离线评测")
    evaluation.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="JSONL 用例路径")
    evaluation.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    evaluation.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="Markdown 报告路径")
    evaluation.add_argument("--no-report", action="store_true", help="不写 Markdown")
    evaluation.add_argument("--verbose", action="store_true", help="逐条打印")
    evaluation.set_defaults(func=command_eval)

    audit = subparsers.add_parser("audit", help="查看审计日志")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--session", default="", help="按会话过滤")
    audit.add_argument("--run", default="", help="按 run_id 过滤")
    audit.add_argument("--db", default="", help="覆盖数据库路径")
    audit.set_defaults(func=command_audit)

    tools = subparsers.add_parser("tools", help="列出工具与风险等级")
    tools.add_argument("--db", default="", help="覆盖数据库路径")
    tools.set_defaults(func=command_tools)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
