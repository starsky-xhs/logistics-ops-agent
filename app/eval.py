"""离线评测。

30 条 JSONL 用例逐条在独立 SQLite 上运行，覆盖只读查询、串联槽位、开票、
高风险审批、转人工和提示词注入。审批类用例会自动批准，以便测量“审批后能否
继续完成”，同时单独统计审批触发是否正确。

指标口径：
- 工具选择准确率：实际工具序列与期望序列完全一致；
- 参数抽取准确率：按字段比对（订单号、运单号、地址、抬头等）；
- 审批触发正确率：是否产生审批与期望一致；
- 任务完成率：工具序列、关键参数、最终状态全部正确（只看状态会掩盖选错工具）。
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.backend.seed import seed_database
from app.config import PROJECT_ROOT, Settings
from app.models import AgentRun
from app.service import AgentService

DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "eval_cases.jsonl"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "docs" / "EVAL_REPORT.md"
EVAL_TMP_DIR = PROJECT_ROOT / "data" / ".eval_tmp"


class EvalError(RuntimeError):
    """评测文件格式错误。"""


@dataclass(slots=True)
class EvalCase:
    case_id: str
    utterance: str
    expected_tools: list[str]
    expected_args: dict[str, dict[str, Any]]
    expect_approval: bool
    expect_status: str
    diagnosis: str = ""


@dataclass(slots=True)
class CaseResult:
    case_id: str
    utterance: str
    expected_tools: list[str]
    actual_tools: list[str]
    tool_correct: bool
    parameter_total: int
    parameter_correct: int
    parameter_errors: list[str]
    approval_expected: bool
    approval_actual: bool
    approval_correct: bool
    expected_status: str
    actual_status: str
    status_correct: bool
    final_answer: str
    elapsed_ms: float
    task_complete: bool
    diagnosis: str = ""

    @property
    def passed(self) -> bool:
        return self.task_complete and self.approval_correct


@dataclass(slots=True)
class EvalReport:
    total: int
    tool_correct: int
    parameter_total: int
    parameter_correct: int
    approval_correct: int
    status_correct: int
    results: list[CaseResult] = field(default_factory=list)
    generated_at: str = ""
    elapsed_seconds: float = 0.0

    @property
    def tool_accuracy(self) -> float:
        return self.tool_correct / self.total if self.total else 0.0

    @property
    def parameter_accuracy(self) -> float:
        return (
            self.parameter_correct / self.parameter_total
            if self.parameter_total
            else 0.0
        )

    @property
    def approval_accuracy(self) -> float:
        return self.approval_correct / self.total if self.total else 0.0

    @property
    def completion_accuracy(self) -> float:
        """任务完成 = 工具序列、关键参数、最终状态都正确。"""

        if not self.total:
            return 0.0
        return sum(1 for item in self.results if item.task_complete) / self.total

    def metrics(self) -> dict[str, float]:
        return {
            "tool_accuracy": self.tool_accuracy,
            "parameter_accuracy": self.parameter_accuracy,
            "approval_accuracy": self.approval_accuracy,
            "completion_accuracy": self.completion_accuracy,
        }


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[EvalCase]:
    """读取并校验 JSONL。空行允许存在，字段缺失直接失败。"""

    case_path = Path(path)
    if not case_path.is_file():
        raise EvalError(f"评测文件不存在：{case_path}")
    cases: list[EvalCase] = []
    ids: set[str] = set()
    with case_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvalError(f"第 {line_no} 行不是合法 JSON：{exc}") from exc
            try:
                case = EvalCase(
                    case_id=str(raw["id"]),
                    utterance=str(raw["utterance"]),
                    expected_tools=[str(item) for item in raw["expected_tools"]],
                    expected_args={
                        str(tool): dict(args)
                        for tool, args in dict(raw.get("expected_args", {})).items()
                    },
                    expect_approval=bool(raw["expect_approval"]),
                    expect_status=str(raw.get("expect_status", "completed")),
                    diagnosis=str(raw.get("diagnosis", "")).strip(),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise EvalError(f"第 {line_no} 行字段不完整：{exc}") from exc
            if case.case_id in ids:
                raise EvalError(f"用例 id 重复：{case.case_id}")
            ids.add(case.case_id)
            cases.append(case)
    if not cases:
        raise EvalError("评测用例为空")
    return cases


def _diagnose(
    case: EvalCase,
    *,
    tool_correct: bool,
    actual_tools: list[str],
    parameter_errors: list[str],
    status_correct: bool,
    actual_status: str,
) -> str:
    """给失败用例生成可执行的原因说明，而不是只贴一个红叉。"""

    if case.diagnosis:
        return case.diagnosis
    if not tool_correct:
        if "escalate_to_human" in case.expected_tools and actual_tools:
            return (
                "该追问/拒答的分支没有触发，规划器继续调用了业务工具；"
                "需要补充澄清槽位或安全分支。"
            )
        if len(actual_tools) < len(case.expected_tools):
            return (
                "复合意图只执行了部分工具；当前规划器一次只保留一个主意图，"
                "缺少多工具拆分。"
            )
        return (
            f"选错或漏选工具：期望 {case.expected_tools}，实际 {actual_tools}；"
            "检查规则优先级、工具描述与槽位抽取。"
        )
    if parameter_errors:
        return (
            "工具选对了但槽位不正确："
            + "；".join(parameter_errors)
            + "。检查正则边界、多实体抽取和参数绑定。"
        )
    if not status_correct:
        return (
            f"流程结果不符合预期（实际 {actual_status}）："
            "检查工具失败后的收尾与升级分支。"
        )
    return "未归因到规则/槽位/状态，请查看运行轨迹。"


def _actual_steps(run: AgentRun) -> list[Any]:
    """只统计真正进入执行/审批中断的步骤，跳过未执行计划。"""

    allowed = {"completed", "failed", "awaiting_approval", "rejected"}
    return [step for step in run.steps if step.status in allowed]


def _compare_parameters(
    case: EvalCase, run: AgentRun
) -> tuple[int, int, list[str]]:
    actual_by_tool = {
        step.tool_name: step.arguments for step in _actual_steps(run)
    }
    total = 0
    correct = 0
    errors: list[str] = []
    for tool_name, expected_fields in case.expected_args.items():
        actual = actual_by_tool.get(tool_name, {})
        for key, expected in expected_fields.items():
            total += 1
            actual_value = actual.get(key)
            if actual_value == expected:
                correct += 1
            else:
                errors.append(
                    f"{tool_name}.{key}: 期望 {expected!r}，实际 {actual_value!r}"
                )
    return total, correct, errors


def run_eval(
    cases_path: str | Path = DEFAULT_CASES_PATH,
    *,
    limit: int | None = None,
    report_path: str | Path | None = DEFAULT_REPORT_PATH,
    verbose: bool = False,
) -> EvalReport:
    """执行评测，可选写入 Markdown 报告。"""

    cases = load_cases(cases_path)
    if limit is not None:
        cases = cases[: max(0, limit)]

    EVAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
    eval_db = EVAL_TMP_DIR / "eval.db"
    started = time.perf_counter()
    results: list[CaseResult] = []

    # 不使用 tempfile.TemporaryDirectory：在受限 Windows 沙箱里它会创建 700 ACL
    # 的临时目录，SQLite 随后无法打开。固定工作目录 + 每例 force 重建更可控。
    for index, case in enumerate(cases, start=1):
        case_started = time.perf_counter()
        seed_database(str(eval_db), force=True)
        settings = Settings(
            db_path=eval_db,
            planner="mock",
            approval_policy="high_only",
        )
        service = AgentService(settings)
        try:
            run = service.run(None, case.utterance)
            approval_actual = bool(run.approval_id)
            approvals = 0
            while run.status == "awaiting_approval" and run.approval_id:
                approvals += 1
                if approvals > 3:
                    break
                run = service.approve(run.approval_id, approver="eval-bot")
            actual_steps = _actual_steps(run)
            actual_tools = [step.tool_name for step in actual_steps]
            tool_correct = actual_tools == case.expected_tools
            param_total, param_correct, param_errors = _compare_parameters(case, run)
            approval_correct = approval_actual == case.expect_approval
            status_correct = run.status == case.expect_status
            task_complete = (
                tool_correct and not param_errors and status_correct
            )
            diagnosis = _diagnose(
                case,
                tool_correct=tool_correct,
                actual_tools=actual_tools,
                parameter_errors=param_errors,
                status_correct=status_correct,
                actual_status=run.status,
            )
            result = CaseResult(
                case_id=case.case_id,
                utterance=case.utterance,
                expected_tools=case.expected_tools,
                actual_tools=actual_tools,
                tool_correct=tool_correct,
                parameter_total=param_total,
                parameter_correct=param_correct,
                parameter_errors=param_errors,
                approval_expected=case.expect_approval,
                approval_actual=approval_actual,
                approval_correct=approval_correct,
                expected_status=case.expect_status,
                actual_status=run.status,
                status_correct=status_correct,
                final_answer=run.final_answer,
                elapsed_ms=round((time.perf_counter() - case_started) * 1000, 2),
                task_complete=task_complete,
                diagnosis=diagnosis,
            )
            results.append(result)
            if verbose:
                flag = "PASS" if result.passed else "FAIL"
                print(
                    f"[{index:02d}/{len(cases)}] {flag} {case.case_id} "
                    f"{actual_tools} {run.status}"
                )
        finally:
            service.close()

    report = EvalReport(
        total=len(results),
        tool_correct=sum(1 for item in results if item.tool_correct),
        parameter_total=sum(item.parameter_total for item in results),
        parameter_correct=sum(item.parameter_correct for item in results),
        approval_correct=sum(1 for item in results if item.approval_correct),
        status_correct=sum(1 for item in results if item.status_correct),
        results=results,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        elapsed_seconds=round(time.perf_counter() - started, 2),
    )
    if report_path is not None:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(render_markdown(report), encoding="utf-8")
    return report


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(report: EvalReport) -> str:
    """生成可提交到仓库的 Markdown 报告。"""

    failures = [item for item in report.results if not item.passed]
    lines = [
        "# 评测报告",
        "",
        "> 本文件由 `python -m app.cli eval` 自动生成，数值来自本地离线运行，未做人工修饰。",
        "",
        f"- 生成时间（UTC）：`{report.generated_at}`",
        f"- Python：`{platform.python_version()}`",
        "- 规划器：`MockPlanner`（确定性规则，不联网）",
        f"- 用例数：`{report.total}`",
        f"- 总耗时：`{report.elapsed_seconds:.2f}s`",
        "",
        "## 指标",
        "",
        "| 指标 | 结果 | 口径 |",
        "| --- | ---: | --- |",
        f"| 工具选择准确率 | {_percent(report.tool_accuracy)} "
        f"| 工具序列完全一致 {report.tool_correct}/{report.total} |",
        f"| 参数抽取准确率 | {_percent(report.parameter_accuracy)} "
        f"| 期望字段逐项匹配 {report.parameter_correct}/{report.parameter_total} |",
        f"| 审批触发正确率 | {_percent(report.approval_accuracy)} "
        f"| 是否产生审批与期望一致 {report.approval_correct}/{report.total} |",
        f"| 任务完成率 | {_percent(report.completion_accuracy)} "
        f"| 工具序列 + 关键参数 + 最终状态全部正确 "
        f"{sum(1 for item in report.results if item.task_complete)}/{report.total} |",
        "",
        "## 口径说明",
        "",
        "- 每条用例使用独立 SQLite 数据库和全新种子数据，避免前一条用例修改状态影响后一条。",
        "- 预期需要审批的用例由脚本自动调用 `approve()`；审批触发指标在批准前记录。",
        "- 参数准确率按字段计数，例如订单号、运单号、新地址、发票抬头分别算一项。",
        "- 任务完成率要求工具序列、关键参数和最终状态全部正确；只比较“completed”会掩盖选错工具但状态碰巧正常的情况。",
        "- MockPlanner 的规则是通用意图 + 槽位匹配，不是按本文件用例反写；难例结果就是当前规则的真实上限。",
        "- LLM planner 未纳入离线回归：它需要外部模型和网络，只做单测/手工验证。",
        "",
        "## 逐条结果",
        "",
        "| 用例 | 话术 | 期望工具 | 实际工具 | 参数 | 审批 | 状态 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.results:
        param = "OK" if not item.parameter_errors else "FAIL"
        approval = (
            "OK" if item.approval_correct
            else f"{item.approval_actual}≠{item.approval_expected}"
        )
        status = "OK" if item.status_correct else f"{item.actual_status}≠{item.expected_status}"
        lines.append(
            f"| {item.case_id} | {item.utterance} | "
            f"{', '.join(item.expected_tools)} | {', '.join(item.actual_tools) or '-'} | "
            f"{param} {item.parameter_correct}/{item.parameter_total} | "
            f"{approval} | {status} | {'PASS' if item.passed else 'FAIL'} |"
        )

    lines.extend(["", "## 失败 / 降级明细", ""])
    lines.append(
        "- 降级：本次 MockPlanner 为本地规则执行，没有发生 LLM 网络降级；"
        "LLM planner 不进入离线回归。"
    )
    lines.append("")
    if not failures:
        lines.append("本次运行没有失败用例。")
    else:
        for item in failures:
            lines.append(f"### {item.case_id}")
            lines.append("")
            lines.append(f"- 话术：{item.utterance}")
            lines.append(f"- 工具：期望 {item.expected_tools}，实际 {item.actual_tools}")
            lines.append(
                f"- 审批：期望 {item.approval_expected}，实际 {item.approval_actual}"
            )
            lines.append(
                f"- 状态：期望 {item.expected_status}，实际 {item.actual_status}"
            )
            if item.parameter_errors:
                for error in item.parameter_errors:
                    lines.append(f"- 参数：{error}")
            else:
                lines.append("- 参数：关键字段匹配")
            lines.append(f"- 可能原因：{item.diagnosis}")
            lines.append(
                f"- 实际回复：{item.final_answer.replace(chr(10), ' / ')[:220]}"
            )
            lines.append("")
    lines.append("")
    return "\n".join(lines)


def render_console(report: EvalReport) -> str:
    """终端摘要。"""

    lines = [
        f"评测完成：{report.total} 条，耗时 {report.elapsed_seconds:.2f}s",
        f"  工具选择准确率  {_percent(report.tool_accuracy)} "
        f"({report.tool_correct}/{report.total})",
        f"  参数抽取准确率  {_percent(report.parameter_accuracy)} "
        f"({report.parameter_correct}/{report.parameter_total})",
        f"  审批触发正确率  {_percent(report.approval_accuracy)} "
        f"({report.approval_correct}/{report.total})",
        f"  任务完成率      {_percent(report.completion_accuracy)} "
        f"({sum(1 for item in report.results if item.task_complete)}/{report.total})",
    ]
    failures = [item for item in report.results if not item.passed]
    if failures:
        lines.append(f"  失败用例：{', '.join(item.case_id for item in failures)}")
    else:
        lines.append("  失败用例：无")
    return "\n".join(lines)


def main() -> None:
    """允许 ``python -m app.eval`` 直接运行。"""

    report = run_eval(verbose=True)
    print(render_console(report))


if __name__ == "__main__":
    main()
