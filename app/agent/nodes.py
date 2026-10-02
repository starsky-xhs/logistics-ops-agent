"""状态机节点实现。

节点只读写 ``AgentState`` 并返回下一个节点名；所有外部副作用都经过
``GraphDependencies`` 的回调，避免业务逻辑与 SQLite / FastAPI 耦合。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app import guardrails
from app.models import PlanStep, ToolResult, utc_now_iso
from app.tools.registry import ToolError

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from app.agent.graph import AgentState, GraphDependencies


NODE_PLAN = "plan"
NODE_ACT = "act"
NODE_OBSERVE = "observe"
NODE_RESPOND = "respond"
NODE_ESCALATE = "escalate"
NODE_FAIL = "fail"
DONE = "done"
SUSPEND = "suspend"


class SlotResolutionError(RuntimeError):
    """前序工具结果中缺少计划所需的槽位。"""


def intake(state: AgentState, deps: GraphDependencies) -> str:
    """清洗输入并做提示词注入检测。"""

    text = re.sub(r"[ \t]+", " ", state.user_input).strip()
    if not text:
        state.error = "用户输入为空。"
        state.status = "failed"
        return NODE_FAIL
    if len(text) > 4000:
        state.error = "输入超过 4000 字符，已拒绝处理。"
        state.status = "failed"
        return NODE_FAIL

    state.sanitized_input = text
    check = guardrails.scan_prompt_injection(text)
    state.injection_blocked = check.blocked
    state.injection_reason = check.reason
    if check.blocked:
        state.intent = "security.blocked"
        deps.publish(
            {
                "type": "step",
                "phase": "intake",
                "blocked": True,
                "reason": check.reason,
                "matches": check.matches,
            }
        )
        return NODE_PLAN

    deps.publish(
        {
            "type": "step",
            "phase": "intake",
            "blocked": False,
            "length": len(text),
        }
    )
    return NODE_PLAN


def plan(state: AgentState, deps: GraphDependencies) -> str:
    """调用规划器生成计划；注入或空计划一律转人工。"""

    if state.injection_blocked:
        steps = [
            PlanStep(
                index=0,
                intent="security.blocked",
                tool_name="escalate_to_human",
                arguments={
                    "reason": (
                        f"输入被提示词注入护栏拦截：{state.injection_reason}。"
                        "为避免绕过审批或泄漏系统信息，转人工复核。"
                    ),
                    "category": "security",
                },
                reason="检测到疑似提示词注入，停止自动工具编排并转人工复核。",
            )
        ]
    else:
        try:
            steps = deps.planner.plan(state.sanitized_input)
        except Exception as exc:
            state.error = f"规划失败：{exc}"
            state.status = "failed"
            return NODE_FAIL
        if not steps:
            steps = [
                PlanStep(
                    index=0,
                    intent="handoff.fallback",
                    tool_name="escalate_to_human",
                    arguments={
                        "reason": "规划器没有产生可用工具调用，转人工处理。",
                        "category": "out_of_scope",
                    },
                    reason="空计划不能假装完成，按能力边界转人工。",
                )
            ]

    for index, step in enumerate(steps):
        step.index = index
    state.plan = steps
    state.intent = steps[0].intent or steps[0].tool_name

    plan_payload = []
    for step in steps:
        try:
            spec = deps.registry.get(step.tool_name)
            risk = spec.risk_level
            needs = deps.policy.requires_approval(spec)
        except ToolError:
            risk = "unknown"
            needs = False
        plan_payload.append(
            {
                "index": step.index,
                "intent": step.intent,
                "tool": step.tool_name,
                "arguments": guardrails.redact_mapping(step.arguments),
                "reason": step.reason,
                "risk": risk,
                "needs_approval": needs,
            }
        )
    deps.publish(
        {
            "type": "step",
            "phase": "plan",
            "intent": state.intent,
            "planner": state.planner,
            "steps": plan_payload,
        }
    )
    return NODE_ACT


def act(state: AgentState, deps: GraphDependencies) -> str:
    """执行（或挂起审批）当前计划步骤。"""

    if state.cursor >= len(state.plan):
        return NODE_RESPOND
    step = state.plan[state.cursor]

    try:
        spec = deps.registry.get(step.tool_name)
    except ToolError as exc:
        _mark_failure(state, step, f"未知工具：{step.tool_name}", "tool_not_found")
        state.error = str(exc)
        state.cursor += 1
        return NODE_OBSERVE

    try:
        resolved = resolve_arguments(step.arguments, state.observations)
    except SlotResolutionError as exc:
        _mark_failure(state, step, str(exc), "missing_dependency")
        state.error = str(exc)
        state.cursor += 1
        return NODE_OBSERVE

    step.arguments = resolved
    step.needs_approval = deps.policy.requires_approval(spec)

    # 预算与循环检查放在审批之前：避免为一个注定不会执行的步骤创建审批单。
    if state.steps_used >= deps.policy.max_steps:
        _mark_failure(
            state,
            step,
            f"超过 max_steps={deps.policy.max_steps}，停止继续调用工具。",
            "max_steps",
        )
        state.error = f"执行超过最大步数 {deps.policy.max_steps}。"
        return NODE_FAIL

    fingerprint = deps.policy.fingerprint(step.tool_name, step.arguments)
    if fingerprint in state.action_history:
        _mark_failure(
            state,
            step,
            "检测到同一工具与同一参数的重复调用，已中断以防死循环。",
            "loop_detected",
        )
        state.error = "检测到重复工具调用，已中断本次执行。"
        return NODE_FAIL

    approved_for_step = (
        state.approved_step_index == step.index
        and bool(state.approved_approval_id)
    )
    if step.needs_approval and not approved_for_step:
        reason = deps.policy.approval_reason(spec, state.user_input)
        approval = deps.request_approval(state, step, reason)
        step.approval_id = approval.approval_id
        step.status = "awaiting_approval"
        state.approval_id = approval.approval_id
        state.approved_step_index = None
        state.approved_approval_id = ""
        state.status = "awaiting_approval"
        deps.publish(
            {
                "type": "approval_required",
                "run_id": state.run_id,
                "approval_id": approval.approval_id,
                "tool": step.tool_name,
                "risk": spec.risk_level,
                "arguments": guardrails.redact_mapping(step.arguments),
                "reason": reason,
            }
        )
        return SUSPEND

    approval_id = state.approved_approval_id if approved_for_step else ""
    step.status = "running"
    step.started_at = utc_now_iso()
    deps.publish(
        {
            "type": "step",
            "phase": "act",
            "index": step.index,
            "tool": step.tool_name,
            "arguments": guardrails.redact_mapping(step.arguments),
            "reason": step.reason,
            "risk": spec.risk_level,
            "needs_approval": step.needs_approval,
        }
    )

    try:
        result = deps.execute_tool(state, step, approval_id)
    except Exception as exc:  # 审批不匹配等安全异常必须转成失败，不能继续。
        result = ToolResult(
            tool_name=step.tool_name,
            ok=False,
            error_code="execution_error",
            error_message=f"工具执行失败：{exc}",
            summary=f"{step.tool_name} 未能执行。",
        )

    state.steps_used += 1
    state.action_history.append(fingerprint)
    step.finished_at = utc_now_iso()
    if result.ok:
        step.status = "completed"
        step.result_summary = result.summary
    else:
        step.status = "failed"
        step.error = result.error_message or result.error_code or "工具返回失败"
        step.result_summary = result.summary or step.error
        state.error = step.error

    state.observations.append(
        {
            "step_index": step.index,
            "tool": step.tool_name,
            "ok": result.ok,
            "summary": result.summary,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "data": result.data,
            "approval_id": step.approval_id,
            "rejected": False,
        }
    )
    deps.publish(
        {
            "type": "tool_result",
            "run_id": state.run_id,
            "tool": step.tool_name,
            "ok": result.ok,
            "summary": guardrails.mask_pii(result.summary or result.error_message),
            "latency_ms": result.latency_ms,
            "approval_id": step.approval_id,
        }
    )

    state.cursor += 1
    state.approval_id = ""
    state.approved_step_index = None
    state.approved_approval_id = ""
    return NODE_OBSERVE


def observe(state: AgentState, deps: GraphDependencies) -> str:
    """根据最后一个工具结果决定继续、转人工还是收尾。"""

    if not state.observations:
        return NODE_ACT
    last = state.observations[-1]

    if last.get("tool") == "escalate_to_human":
        if last.get("ok"):
            return NODE_ESCALATE
        state.error = last.get("error_message") or "转人工工具执行失败。"
        return NODE_FAIL

    if not last.get("ok"):
        if last.get("error_code") != "approval_rejected":
            state.error = last.get("error_message") or "工具执行失败。"
        return NODE_RESPOND

    if state.cursor < len(state.plan):
        return NODE_ACT
    return NODE_RESPOND


def respond(state: AgentState, deps: GraphDependencies) -> str:
    """生成最终中文回复并落定状态。"""

    for step in state.plan[state.cursor:]:
        if step.status == "planned":
            step.status = "skipped"
    answer = compose_answer(state)
    state.final_answer = guardrails.sanitize_output(answer)

    failed = [step for step in state.plan if step.status == "failed"]
    rejected = [step for step in state.plan if step.status == "rejected"]
    if failed:
        state.status = "failed"
    elif rejected and not any(step.status == "completed" for step in state.plan):
        # 用户主动拒绝审批不是系统失败，明确告知“未执行”。
        state.status = "completed"
    else:
        state.status = "completed"
    state.approval_id = ""
    return DONE


def escalate(state: AgentState, deps: GraphDependencies) -> str:
    """使用转人工工具的结果生成最终回复。"""

    last = state.observations[-1] if state.observations else {}
    data = last.get("data") or {}
    if not last.get("ok") or not data.get("ticket_no"):
        state.error = last.get("error_message") or "转人工失败。"
        state.status = "failed"
        state.final_answer = guardrails.sanitize_output(
            "抱歉，自动转人工没有成功，请稍后重试或直接联系人工客服。"
        )
        return DONE

    ticket_no = data.get("ticket_no", "")
    category = data.get("category_label", "其他")
    reason = data.get("reason", "")
    answer = (
        f"已转人工处理，工单号：{ticket_no}。\n"
        f"分类：{category}；原因：{reason}\n"
        f"服务承诺：{data.get('sla', '工作日尽快响应')}。"
    )
    state.final_answer = guardrails.sanitize_output(answer)
    state.status = "escalated"
    state.approval_id = ""
    return DONE


def fail(state: AgentState, deps: GraphDependencies) -> str:
    """统一失败出口，避免把 Python 异常直接抛给终端用户。"""

    reason = state.error or "未知错误"
    state.final_answer = guardrails.sanitize_output(
        "本次处理未能完成："
        f"{reason}\n"
        "为避免误操作，系统已停止后续自动工具调用。你可以补充信息后重试，"
        "或转人工客服继续处理。"
    )
    state.status = "failed"
    return DONE


# -- 槽位解析 ---------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"^\$steps\[(\d+)\]\.([A-Za-z0-9_.]+)$")


def resolve_arguments(
    value: Any, observations: list[dict[str, Any]]
) -> Any:
    """解析 ``$steps[0].shipment.tracking_no`` 形式的前序结果占位符。"""

    if isinstance(value, dict):
        return {key: resolve_arguments(item, observations) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_arguments(item, observations) for item in value]
    if not isinstance(value, str):
        return value

    match = _PLACEHOLDER_RE.match(value)
    if not match:
        return value

    step_index = int(match.group(1))
    path = match.group(2).split(".")
    observation = next(
        (
            item
            for item in observations
            if int(item.get("step_index", -1)) == step_index and item.get("ok")
        ),
        None,
    )
    if observation is None:
        raise SlotResolutionError(
            f"计划依赖第 {step_index + 1} 步的结果，但该步骤尚未成功执行。"
        )
    cursor: Any = observation.get("data", {})
    for segment in path:
        if isinstance(cursor, dict) and segment in cursor:
            cursor = cursor[segment]
        elif isinstance(cursor, list) and segment.isdigit() and int(segment) < len(cursor):
            cursor = cursor[int(segment)]
        else:
            raise SlotResolutionError(
                f"前序工具结果缺少字段 {'.'.join(path)}；无法补全当前参数。"
            )
    if cursor in (None, ""):
        raise SlotResolutionError(
            f"前序工具结果中的 {'.'.join(path)} 为空，无法继续。"
        )
    return cursor


# -- 回复组织 ---------------------------------------------------------------

def compose_answer(state: AgentState) -> str:
    """把工具结果整理成可读中文，不暴露内部字段名。"""

    lines: list[str] = []
    for observation in state.observations:
        if not observation.get("ok"):
            continue
        tool = observation.get("tool")
        data = observation.get("data") or {}
        if tool == "query_order":
            text = (
                f"订单 {data.get('order_no')} 当前{data.get('status_label')}，"
                f"金额 {data.get('amount')}。"
            )
            shipment = data.get("shipment")
            if shipment:
                text += (
                    f"\n  关联运单 {shipment.get('tracking_no')}（"
                    f"{shipment.get('status_label')}），最新节点："
                    f"{shipment.get('last_event')}。"
                )
            address = data.get("delivery_address", "")
            if address:
                text += f"\n  收货地址：{address}"
            lines.append(text)
        elif tool == "list_orders":
            orders = data.get("orders") or []
            if orders:
                lines.append(f"共找到 {len(orders)} 个订单：")
                for order in orders:
                    lines.append(
                        f"  - {order.get('order_no')}｜{order.get('status_label')}｜"
                        f"{order.get('amount')}｜{order.get('created_at', '')[:10]}"
                    )
            else:
                lines.append("没有找到符合条件的订单。")
        elif tool == "track_shipment":
            lines.append(
                f"运单 {data.get('tracking_no')}（{data.get('carrier')}）目前"
                f"{data.get('status_label')}，最新节点：{data.get('last_event')}，"
                f"当前位置：{data.get('current_city')}。"
            )
        elif tool == "estimate_delivery":
            lines.append(f"运单 {data.get('tracking_no')} 时效：{data.get('hint')}")
        elif tool == "query_invoice":
            lines.append(
                f"发票 {data.get('invoice_no')} 当前{data.get('status_label')}，"
                f"抬头 {data.get('title')}，金额 {data.get('amount')}。"
            )
        elif tool == "create_invoice":
            replay = "（幂等重放，未重复开票）" if data.get("replayed") else ""
            lines.append(
                f"已开票：{data.get('invoice_no')}，抬头 {data.get('title')}，"
                f"金额 {data.get('amount')}，税率 {data.get('tax_rate_percent')}%"
                f"{replay}。"
            )
        elif tool == "change_delivery_address":
            lines.append(
                f"订单 {data.get('order_no')} 的新收货地址："
                f"{data.get('new_address')}（审批单 {data.get('approval_id')}）。"
            )
        elif tool == "cancel_order":
            lines.append(
                f"订单 {data.get('order_no')} 已取消"
                f"（审批单 {data.get('approval_id')}），原因：{data.get('reason')}。"
            )
        elif tool == "reroute":
            lines.append(
                f"运单 {data.get('tracking_no')} 的新派送地址：{data.get('new_address')}"
                f"（审批单 {data.get('approval_id')}）。"
            )
        elif tool == "refund":
            lines.append(
                f"发票 {data.get('invoice_no')} 已退款 {data.get('refund_amount')}"
                f"（审批单 {data.get('approval_id')}）。"
            )
        elif data.get("summary"):
            lines.append(str(data["summary"]))

    rejected = [item for item in state.observations if item.get("rejected")]
    if rejected:
        lines.append("人工审批未通过，相关高风险操作没有执行，数据未被修改。")

    failures = [item for item in state.observations if not item.get("ok") and not item.get("rejected")]
    if failures:
        lines.append("未完成的操作：")
        for item in failures:
            message = item.get("error_message") or item.get("error_code") or "未知错误"
            lines.append(f"  - {item.get('tool')}：{message}")

    if not lines:
        return "抱歉，本次没有可返回的业务结果。"
    return "\n".join(lines)


def _mark_failure(
    state: AgentState,
    step: PlanStep,
    message: str,
    code: str,
) -> None:
    step.status = "failed"
    step.error = message
    step.finished_at = utc_now_iso()
    state.observations.append(
        {
            "step_index": step.index,
            "tool": step.tool_name,
            "ok": False,
            "summary": message,
            "error_code": code,
            "error_message": message,
            "data": {},
            "approval_id": step.approval_id,
            "rejected": False,
        }
    )
