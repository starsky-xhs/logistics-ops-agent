"""规划器：把自然语言转成有序的工具调用计划。

- ``MockPlanner`` 是默认实现，关键词 + 槽位抽取，完全离线且确定；
- ``LlmPlanner`` 走 OpenAI 兼容的 /chat/completions function calling，
  网络或协议失败时自动降级到 MockPlanner。

规划器只负责“选工具 + 填参数”，审批、循环检测、超时和真正执行都在状态机里。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.agent.policy import PolicyEngine
from app.config import Settings
from app.models import PlanStep, ToolSpec
from app.tools import ToolRegistry

LOGGER = logging.getLogger(__name__)


class PlannerError(RuntimeError):
    """规划器无法产生可用计划。"""


class Planner(Protocol):
    name: str

    def plan(self, user_input: str) -> list[PlanStep]:
        """返回按顺序执行的计划；空列表表示需要转人工。"""
        ...


# 规则按从上到下的优先级执行。越靠前的规则越“具体”，例如退款优先于开票，
# 改派运单优先于普通订单查询。
RULE_PRIORITY: tuple[str, ...] = (
    "explicit_handoff",
    "reroute_with_tracking",
    "change_delivery_address",
    "refund",
    "cancel_order",
    "create_invoice",
    "query_invoice",
    "estimate_delivery",
    "track_shipment",
    "query_order",
    "list_orders",
    "fallback",
)

ORDER_RE = re.compile(r"SH\d{8}", re.IGNORECASE)
TRACKING_RE = re.compile(r"(?:SF|TD)\d{10,16}", re.IGNORECASE)
INVOICE_RE = re.compile(r"INV\d{10}", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
NEW_ADDRESS_RE = re.compile(
    r"(?:改成|改为|换到|换成|改派到|改投到|新地址(?:是|为)?|地址是)"
    r"[：:\s]*([^，。；;\n]{5,200})"
)
INVOICE_TITLE_RE = re.compile(
    r"(?:抬头(?:是|为)?|开给|公司名称(?:是|为)?)[：:\s]*([^，。；;\n]{2,80})"
)

ORDER_STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("待付款", "pending"),
    ("待支付", "pending"),
    ("已取消", "cancelled"),
    ("已签收", "delivered"),
    ("已完成", "delivered"),
    ("运输中", "shipped"),
    ("已发货", "shipped"),
    ("已确认", "confirmed"),
    ("已打包", "packed"),
)


@dataclass(slots=True)
class ParsedRequest:
    """一次用户输入的结构化视图，避免每条规则重复写正则。"""

    text: str
    order_no: str = ""
    tracking_no: str = ""
    invoice_no: str = ""
    phone: str = ""
    new_address: str = ""
    invoice_title: str = ""
    order_status: str = ""
    limit: int = 5
    has_change_words: bool = False
    has_tracking_words: bool = False
    has_estimate_words: bool = False
    has_invoice_words: bool = False


def parse_request(user_input: str) -> ParsedRequest:
    """抽取 demo / eval 覆盖到的槽位。

    电话号码只在完整 11 位时抽取；地址必须由“改成/改为/地址是”等触发词引导，
    避免把普通句子误当地址。
    """

    text = re.sub(r"\s+", " ", user_input).strip()
    order_match = ORDER_RE.search(text)
    tracking_match = TRACKING_RE.search(text)
    invoice_match = INVOICE_RE.search(text)
    phone_match = PHONE_RE.search(text)
    address_match = NEW_ADDRESS_RE.search(text)
    title_match = INVOICE_TITLE_RE.search(text)
    limit_match = re.search(r"(?:最近|前|最多)\s*(\d{1,2})\s*(?:个|条)?", text)

    status = ""
    for keyword, value in ORDER_STATUS_KEYWORDS:
        if keyword in text:
            status = value
            break

    return ParsedRequest(
        text=text,
        order_no=order_match.group(0).upper() if order_match else "",
        tracking_no=tracking_match.group(0).upper() if tracking_match else "",
        invoice_no=invoice_match.group(0).upper() if invoice_match else "",
        phone=phone_match.group(0) if phone_match else "",
        new_address=address_match.group(1).strip() if address_match else "",
        invoice_title=title_match.group(1).strip() if title_match else "",
        order_status=status,
        limit=max(1, min(int(limit_match.group(1)), 20)) if limit_match else 5,
        has_change_words=bool(re.search(r"(?:地址|派送).{0,6}(?:改|换|变)|(?:改|换|变).{0,6}(?:地址|派送)", text)),
        has_tracking_words=bool(
            re.search(r"物流|轨迹|运单|快递|到哪|到哪儿|派送|签收|运输", text)
        ),
        has_estimate_words=bool(
            re.search(r"预计|多久|什么时候.{0,4}(?:到|送)|时效|几天|几号", text)
        ),
        has_invoice_words="发票" in text or "开票" in text,
    )


def _step(intent: str, tool_name: str, arguments: dict[str, Any], reason: str) -> PlanStep:
    return PlanStep(
        index=0,
        intent=intent,
        tool_name=tool_name,
        arguments=arguments,
        reason=reason,
    )


def _handoff(reason: str, category: str) -> PlanStep:
    return _step(
        "handoff",
        "escalate_to_human",
        {"reason": reason[:300], "category": category},
        "没有足够信息或超出可自动处理范围；转人工比猜测参数更安全。",
    )


class MockPlanner:
    """确定性的规则规划器。"""

    name = "mock"

    def plan(self, user_input: str) -> list[PlanStep]:
        parsed = parse_request(user_input)
        text = parsed.text

        # 规则 1：显式要求人工 / 投诉。
        if re.search(r"转人工|人工客服|投诉|工单", text):
            category = "complaint" if "投诉" in text else "manual_request"
            reason = "用户明确要求人工介入或提出投诉，按流程生成工单。" if "投诉" in text \
                else "用户明确要求转人工，保留上下文生成工单。"
            steps = [_handoff(reason, category)]
            return _reindex(steps)

        # 规则 2：运单改派。只有拿到运单号才允许，避免把订单地址误改到在途运单。
        if parsed.tracking_no and (
            "改派" in text or "改投" in text or "转寄" in text or
            (parsed.has_change_words and "地址" in text)
        ):
            if not parsed.new_address:
                return _reindex([
                    _handoff(
                        "用户要求改派运单，但没有提供新的完整地址；请人工补充地址后处理。",
                        "missing_slot",
                    )
                ])
            return _reindex([
                _step(
                    "shipment.reroute",
                    "reroute",
                    {
                        "tracking_no": parsed.tracking_no,
                        "new_address": parsed.new_address,
                        "reason": "客户申请改派收货地址",
                    },
                    "用户提供了运单号和新的派送地址；改派会改变在途履约，"
                    "标记为高风险，执行前必须审批。",
                )
            ])

        # 规则 3：订单改地址。
        if parsed.has_change_words and parsed.order_no and "地址" in text:
            if not parsed.new_address:
                return _reindex([
                    _handoff(
                        "用户想修改收货地址，但消息中没有可解析的新地址；请人工向客户确认。",
                        "missing_slot",
                    )
                ])
            return _reindex([
                _step(
                    "order.change_address",
                    "change_delivery_address",
                    {
                        "order_no": parsed.order_no,
                        "new_address": parsed.new_address,
                        "reason": "客户申请修改收货地址",
                    },
                    "用户提供了订单号和新的收货地址；地址变更影响履约，"
                    "按高风险工具处理，必须人工审批。",
                )
            ])

        # 规则 4：退款。
        if re.search(r"退款|退钱|退一下|退回", text):
            if not parsed.invoice_no and not parsed.order_no:
                return _reindex([
                    _handoff(
                        "客户申请退款，但未提供订单号或发票号；请人工核实后处理。",
                        "missing_slot",
                    )
                ])
            arguments: dict[str, Any] = {
                "reason": _extract_reason(text, "客户申请退款"),
            }
            if parsed.invoice_no:
                arguments["invoice_no"] = parsed.invoice_no
            else:
                arguments["order_no"] = parsed.order_no
            return _reindex([
                _step(
                    "billing.refund",
                    "refund",
                    arguments,
                    "用户明确要求退款；退款会改变资金状态，标记为高风险，"
                    "必须人工审批。",
                )
            ])

        # 规则 5：取消订单。
        if "取消" in text and parsed.order_no:
            return _reindex([
                _step(
                    "order.cancel",
                    "cancel_order",
                    {
                        "order_no": parsed.order_no,
                        "reason": _extract_reason(text, "客户申请取消订单"),
                    },
                    "用户要求取消订单且提供了订单号；取消会影响库存与履约，"
                    "标记为高风险，必须人工审批。",
                )
            ])

        # 规则 6：开票。开票是 medium 风险，但工具幂等，写操作可用重放保护。
        if re.search(r"开(?:一张)?(?:发)?票|开具发票", text) and "查" not in text:
            if not parsed.order_no:
                return _reindex([
                    _handoff(
                        "用户要求开票，但没有提供订单号；请人工确认关联订单。",
                        "missing_slot",
                    )
                ])
            arguments = {"order_no": parsed.order_no}
            if parsed.invoice_title:
                arguments["title"] = parsed.invoice_title
            return _reindex([
                _step(
                    "billing.create_invoice",
                    "create_invoice",
                    arguments,
                    "用户要求开票；开票属于 medium 风险写操作，工具声明为幂等，"
                    "系统会用 run+step+参数生成 idempotency_key，避免重复出票。",
                )
            ])

        # 规则 7：查询发票。
        if "发票" in text and re.search(r"查|状态|进度|开好|是否|有没有", text):
            if parsed.invoice_no:
                arguments = {"invoice_no": parsed.invoice_no}
            elif parsed.order_no:
                arguments = {"order_no": parsed.order_no}
            else:
                return _reindex([
                    _handoff(
                        "用户想查询发票，但消息中没有发票号或订单号；请人工核实。",
                        "missing_slot",
                    )
                ])
            return _reindex([
                _step(
                    "billing.query_invoice",
                    "query_invoice",
                    arguments,
                    "识别到发票查询意图，并拿到了发票号或订单号；只读操作，直接查询。",
                )
            ])

        # 规则 8/9：订单 + 物流/时效。先用 query_order 拿运单号，再串联。
        if parsed.order_no and parsed.has_estimate_words:
            return _reindex([
                _step(
                    "order.query",
                    "query_order",
                    {"order_no": parsed.order_no},
                    "用户询问订单的预计送达时间；先用订单号查订单与运单，"
                    "再串联时效工具，避免要求用户重复提供运单号。",
                ),
                _step(
                    "shipment.estimate",
                    "estimate_delivery",
                    {"tracking_no": "$steps[0].shipment.tracking_no"},
                    "时效预估需要运单号；该参数从前一步 query_order 的结果中解析，"
                    "属于低风险只读操作。",
                ),
            ])

        if parsed.tracking_no and parsed.has_estimate_words:
            return _reindex([
                _step(
                    "shipment.estimate",
                    "estimate_delivery",
                    {"tracking_no": parsed.tracking_no},
                    "用户直接给了运单号并询问预计送达时间；调用时效预估，只读、低风险。",
                )
            ])

        if parsed.order_no and parsed.has_tracking_words:
            return _reindex([
                _step(
                    "order.query",
                    "query_order",
                    {"order_no": parsed.order_no},
                    "用户询问订单的物流进度；订单查询本身是低风险的，"
                    "同时可以从结果中拿到运单号。",
                ),
                _step(
                    "shipment.track",
                    "track_shipment",
                    {"tracking_no": "$steps[0].shipment.tracking_no"},
                    "运单查询需要运单号，用户只给了订单号；"
                    "通过上一步结果补全槽位，避免多轮追问。",
                ),
            ])

        if parsed.tracking_no and parsed.has_tracking_words:
            return _reindex([
                _step(
                    "shipment.track",
                    "track_shipment",
                    {"tracking_no": parsed.tracking_no},
                    "识别到运单号和物流查询意图；只读、低风险，直接查询轨迹。",
                )
            ])

        # 规则 10：普通订单查询。
        if parsed.order_no:
            return _reindex([
                _step(
                    "order.query",
                    "query_order",
                    {"order_no": parsed.order_no},
                    "识别到订单号与订单查询意图；只读、低风险，直接查询订单详情。",
                )
            ])

        # 规则 11：订单列表，可按手机号 / 状态筛选。
        if "订单" in text and re.search(r"最近|列出|列表|哪些|所有|查一下|查下|有", text):
            arguments: dict[str, Any] = {"limit": parsed.limit}
            if parsed.phone:
                arguments["phone"] = parsed.phone
            if parsed.order_status:
                arguments["status"] = parsed.order_status
            return _reindex([
                _step(
                    "order.list",
                    "list_orders",
                    arguments,
                    "用户想浏览订单列表；按手机号/状态过滤，只读且限制返回条数。",
                )
            ])

        if parsed.phone:
            return _reindex([
                _step(
                    "order.list",
                    "list_orders",
                    {"phone": parsed.phone, "limit": parsed.limit},
                    "消息中只有客户手机号，没有具体订单号；用列表工具按手机号查询。",
                )
            ])

        if parsed.invoice_no:
            return _reindex([
                _step(
                    "billing.query_invoice",
                    "query_invoice",
                    {"invoice_no": parsed.invoice_no},
                    "消息中只有发票号；调用只读发票查询。",
                )
            ])

        if parsed.tracking_no:
            return _reindex([
                _step(
                    "shipment.track",
                    "track_shipment",
                    {"tracking_no": parsed.tracking_no},
                    "消息中只有运单号；调用只读轨迹查询。",
                )
            ])

        # 规则 12：兜底转人工。绝不凭空编造订单号或地址。
        return _reindex([
            _handoff(
                "该问题不在订单/运单/发票/退款等可自动处理范围内，"
                "当前知识库没有可靠答案；请人工客服跟进。",
                "out_of_scope",
            )
        ])


def _extract_reason(text: str, default: str) -> str:
    match = re.search(r"(?:因为|原因(?:是)?|理由)[：:\s]*([^，。；;\n]{2,100})", text)
    return match.group(1).strip() if match else default


def _reindex(steps: list[PlanStep]) -> list[PlanStep]:
    for index, step in enumerate(steps):
        step.index = index
    return steps


class LlmPlanner:
    """OpenAI 兼容的 function-calling 规划器。

    默认不启用；需要显式 ``PLANNER=llm`` 且配置 ``LLM_API_KEY``。模型返回的
    tool_calls 会按顺序转成 PlanStep，并行调用会保留全部结果。任何网络/解析错误
    都降级到 MockPlanner，保证 demo 和线上不会因为模型抖动而完全不可用。
    """

    name = "llm"

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        *,
        fallback: Planner | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.fallback = fallback or MockPlanner()
        self.last_fallback_reason = ""

    def plan(self, user_input: str) -> list[PlanStep]:
        try:
            payload = self._request(user_input)
            steps = self._parse_tool_calls(payload)
            if not steps:
                raise PlannerError("模型没有返回工具调用")
            return _reindex(steps[: self.settings.max_steps])
        except Exception as exc:
            self.last_fallback_reason = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("LLM 规划失败，降级到 MockPlanner：%s", exc)
            steps = self.fallback.plan(user_input)
            for step in steps:
                step.reason = (
                    f"{step.reason}（LLM 规划失败，已降级到规则规划器：{type(exc).__name__}）"
                )
            return steps

    def _request(self, user_input: str) -> dict[str, Any]:
        tools = self.registry.openai_tools()
        if not tools:
            raise PlannerError("没有注册可用的工具")
        url = self._chat_url()
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": user_input},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "temperature": 0,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.llm_timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise PlannerError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PlannerError(f"LLM 网络错误：{exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"LLM 返回非 JSON：{raw[:200]}") from exc

    def _chat_url(self) -> str:
        base = self.settings.llm_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _system_prompt(self) -> str:
        return (
            "你是一个物流业务 Agent 的规划器。你只负责选择工具并填写参数，"
            "不要直接回答业务问题。\n"
            "规则：\n"
            "1. 只使用提供的工具；参数必须符合 schema，订单号/运单号不能编造。\n"
            "2. 高风险工具由系统暂停并请求人工审批，不要因为风险而拒绝规划。\n"
            "3. 用户要求取消/退款/改地址/改派时，优先选择对应高风险工具。\n"
            "4. 信息不足或问题超出范围时，调用 escalate_to_human 并说明原因。\n"
            "5. 如果用户只给订单号又要查物流，先 query_order，再用结果中的运单号。\n"
            "6. 可以并行选择互不依赖的工具；有依赖时按顺序返回。\n"
            f"当前日期：{date.today().isoformat()}"
        )

    def _parse_tool_calls(self, payload: dict[str, Any]) -> list[PlanStep]:
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlannerError("无法解析 LLM 响应结构") from exc

        tool_calls = message.get("tool_calls") or []
        if not tool_calls and message.get("function_call"):
            tool_calls = [{"function": message["function_call"]}]

        steps: list[PlanStep] = []
        for item in tool_calls:
            function = item.get("function") or {}
            name = str(function.get("name", "")).strip()
            if not self.registry.has(name):
                raise PlannerError(f"模型返回了未注册工具：{name!r}")
            raw_arguments = function.get("arguments", "{}")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise PlannerError(f"工具 {name} 的参数不是合法 JSON") from exc
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise PlannerError(f"工具 {name} 的参数类型非法")
            if not isinstance(arguments, dict):
                raise PlannerError(f"工具 {name} 的参数必须是 object")
            steps.append(
                _step(
                    f"llm.{name}",
                    name,
                    arguments,
                    f"LLM 通过 function calling 选择 {name}；参数由模型抽取，"
                    "执行前仍会经过注册表 schema 校验与风险策略。",
                )
            )
        return steps


def build_planner(settings: Settings, registry: ToolRegistry) -> Planner:
    """按配置构建规划器；默认 mock，完全离线。"""

    if settings.planner == "mock":
        return MockPlanner()
    return LlmPlanner(settings, registry)


def tool_catalog_for_prompt(registry: ToolRegistry) -> list[dict[str, Any]]:
    """给调试/文档用的工具摘要（不含 handler）。"""

    return [
        {
            "name": spec.name,
            "risk": spec.risk_level,
            "idempotent": spec.idempotent,
            "description": spec.description,
        }
        for spec in registry.list_specs()
    ]


def describe_risk(spec: ToolSpec, policy: PolicyEngine) -> str:
    """工具风险的人类可读描述，供 CLI 和 SSE 使用。"""

    suffix = "（执行前需人工审批）" if policy.requires_approval(spec) else "（可直接执行）"
    return f"{policy.risk_label(spec.risk_level)}风险{suffix}"
