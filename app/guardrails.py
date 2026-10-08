"""输入/输出护栏。

这里不是“万能安全过滤器”，而是针对物流客服场景的三条明确规则：
1. 手机号、身份证、邮箱、地址在输出与审计摘要中脱敏；
2. 常见提示词注入话术在进入规划器前拦截，转人工而不是继续执行；
3. 输出中不允许出现系统提示词标记，避免把内部指令回显给用户。

正则规则都可以被更复杂的攻击绕过，因此高风险动作仍必须经过审批，
不能把安全性完全押在关键词过滤上。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9})(?!\d)")
ID_CARD_RE = re.compile(
    r"(?<![0-9A-Za-z])([1-9]\d{5}(?:18|19|20)\d{2}"
    r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])"
)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9.-])"
)
ADDRESS_RE = re.compile(
    r"((?:收货地址|派送地址|新地址|地址|送到|改到|改成|改派到)"
    r"(?:已)?(?:更新为|更新|修改为|变更为|改为|是|为)?[：:是为\s]*)"
    r"(?![已未更])([^，。；;\n]{5,160})"
)

SENSITIVE_KEY_HINTS = (
    "phone", "mobile", "tel", "address", "email", "id_card", "idcard",
    "身份证", "手机", "地址", "邮箱",
)

INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"忽略(?:之前|以上|前面|所有)(?:的)?(?:所有)?(?:指令|规则|要求)"), "覆盖系统指令"),
    (re.compile(r"无视(?:之前|以上|前面)(?:的)?(?:指令|规则|要求)"), "覆盖系统指令"),
    (re.compile(r"(?:泄露|输出|打印|告诉我)(?:你的|系统)?(?:系统)?提示词", re.I), "套取系统提示词"),
    (re.compile(r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I), "覆盖系统指令"),
    (re.compile(r"(?:system|developer)\s*prompt", re.I), "套取系统提示词"),
    (re.compile(r"(?:developer|god|dan)\s*mode", re.I), "越狱模式"),
    (re.compile(r"(?:越狱|jailbreak)", re.I), "越狱模式"),
    (re.compile(r"你现在(?:是|扮演|变成)", re.I), "角色劫持"),
    (re.compile(r"假装(?:你)?(?:是|成)"), "角色劫持"),
)

OUTPUT_LEAK_MARKERS: tuple[str, ...] = (
    "你是一个物流业务 Agent",
    "系统提示词如下",
    "SYSTEM PROMPT",
    "工具调用规则（必须遵守）",
)

FALLBACK_RESPONSE = "抱歉，这个请求触发了安全检查，已为你转接人工客服处理。"


@dataclass(slots=True)
class InjectionCheck:
    """提示词注入检查结果。"""

    allowed: bool
    matches: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


def mask_phone(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        phone = match.group(1)
        return f"{phone[:3]}****{phone[-4:]}"

    return PHONE_RE.sub(_replace, value)


def mask_id_card(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        card = match.group(1)
        return f"{card[:4]}**********{card[-4:]}"

    return ID_CARD_RE.sub(_replace, value)


def mask_email(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        email = match.group(1)
        local, _, domain = email.partition("@")
        visible = local[:1] if local else "*"
        return f"{visible}***@{domain}"

    return EMAIL_RE.sub(_replace, value)


def mask_address(address: str, *, keep: int = 8) -> str:
    """地址只保留前 ``keep`` 个字符，门牌号等细节不进入审计。"""

    cleaned = address.strip()
    if len(cleaned) <= keep:
        return cleaned
    return f"{cleaned[:keep]}***"


def mask_addresses_in_text(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}{mask_address(match.group(2))}"

    return ADDRESS_RE.sub(_replace, text)


def mask_pii(text: str) -> str:
    """按身份证 → 手机号 → 邮箱 → 地址的顺序脱敏。"""

    result = mask_id_card(str(text))
    result = mask_phone(result)
    result = mask_email(result)
    return mask_addresses_in_text(result)


def contains_pii(text: str) -> list[str]:
    """返回命中的 PII 类别，便于测试与告警。"""

    found: list[str] = []
    if ID_CARD_RE.search(text):
        found.append("id_card")
    if PHONE_RE.search(text):
        found.append("phone")
    if EMAIL_RE.search(text):
        found.append("email")
    return found


def check_output_leak(text: str) -> list[str]:
    """检查输出是否回显系统提示词标记。"""

    lowered = text.lower()
    return [marker for marker in OUTPUT_LEAK_MARKERS if marker.lower() in lowered]


def sanitize_output(text: str) -> str:
    """最终输出统一走这里：先脱敏，再拦截系统提示泄漏。"""

    if check_output_leak(text):
        return FALLBACK_RESPONSE
    return mask_pii(text)


def scan_prompt_injection(text: str) -> InjectionCheck:
    """检测提示词注入。命中时由状态机转人工，不调用业务工具。"""

    matches: list[str] = []
    for pattern, label in INJECTION_PATTERNS:
        if pattern.search(text):
            matches.append(label)
    if matches:
        unique = sorted(set(matches))
        return InjectionCheck(
            allowed=False,
            matches=unique,
            reason=f"输入包含疑似提示词注入特征：{'、'.join(unique)}",
        )
    return InjectionCheck(allowed=True)


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def redact_mapping(value: Any, *, max_length: int = 180) -> Any:
    """递归脱敏字典/列表中的敏感字段，并截断过长字符串。"""

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if _sensitive_key(str(key)):
                if isinstance(item, str):
                    output[key] = (
                        mask_address(item)
                        if "地址" in str(key) or "address" in str(key).lower()
                        else mask_pii(item)
                    )
                else:
                    output[key] = "***"
            else:
                output[key] = redact_mapping(item, max_length=max_length)
        return output
    if isinstance(value, list):
        return [redact_mapping(item, max_length=max_length) for item in value]
    if isinstance(value, str):
        masked = mask_pii(value)
        return masked if len(masked) <= max_length else f"{masked[:max_length]}…(截断)"
    return value


def summarize_mapping(value: Any, *, max_length: int = 180) -> str:
    """生成审计用的单行 JSON 摘要。"""

    redacted = redact_mapping(value, max_length=max_length)
    rendered = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    if len(rendered) > 500:
        return f"{rendered[:500]}…"
    return rendered
