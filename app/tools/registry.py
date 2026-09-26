"""工具注册表与参数校验。

这里没有引入 jsonschema / pydantic：工具契约只使用一个很小的 JSON Schema 子集，
手写校验器可以让错误信息更贴近业务，也保证核心链路零第三方依赖。

安全边界分两层：
1. 注册表拒绝在没有审批上下文时执行 high 风险工具；
2. 高风险工具实现内部再次检查 ``ToolContext.approval_id``。
真正审批单是否合法由 AgentService 在恢复状态机时校验。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models import RiskLevel, ToolResult, ToolSpec


class ToolError(Exception):
    """工具层统一异常。``code`` 供 Agent / API 做稳定分支。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ToolNotFound(ToolError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(f"工具不存在：{tool_name}", code="tool_not_found")
        self.tool_name = tool_name


class ApprovalRequired(ToolError):
    """高风险工具缺少人工审批时抛出。"""

    def __init__(self, tool_name: str, risk_level: RiskLevel) -> None:
        super().__init__(
            f"工具 {tool_name} 的风险等级为 {risk_level}，必须先完成人工审批",
            code="approval_required",
            details={"tool_name": tool_name, "risk_level": risk_level},
        )
        self.tool_name = tool_name
        self.risk_level = risk_level


class InvalidArguments(ToolError):
    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__(message, code="invalid_arguments", details={"path": path})
        self.path = path


ToolHandler = Callable[["ToolContext", dict[str, Any]], ToolResult]


@dataclass(slots=True)
class ToolContext:
    """一次工具执行所需的运行时上下文，不含全局状态。"""

    connection: sqlite3.Connection
    actor: str = "agent"
    run_id: str = ""
    session_id: str = ""
    approval_id: str = ""
    idempotency_key: str = ""
    call_id: str = ""
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def timestamp(self) -> str:
        return self.now.isoformat(timespec="seconds")


@dataclass(slots=True)
class _RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """注册、校验并执行工具。实例不持有数据库连接。"""

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if not spec.name or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", spec.name):
            raise ValueError(f"非法工具名：{spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        if spec.risk_level not in {"low", "medium", "high"}:
            raise ValueError(f"非法风险等级：{spec.risk_level}")
        if not isinstance(spec.json_schema, dict) or spec.json_schema.get("type") != "object":
            raise ValueError(f"工具 {spec.name} 的 schema 必须是 object")
        self._tools[spec.name] = _RegisteredTool(spec=spec, handler=handler)

    def get(self, name: str) -> ToolSpec:
        registered = self._tools.get(name)
        if registered is None:
            raise ToolNotFound(name)
        return registered.spec

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_specs(self) -> list[ToolSpec]:
        return [item.spec for item in self._tools.values()]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self.list_specs()]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """校验后执行；未知工具、参数错误、审批缺失都转成可测试的异常。"""

        registered = self._tools.get(tool_name)
        if registered is None:
            raise ToolNotFound(tool_name)

        if not isinstance(arguments, dict):
            raise InvalidArguments("工具参数必须是 JSON object")

        validate_arguments(registered.spec.json_schema, arguments)
        if registered.spec.risk_level == "high" and not context.approval_id:
            raise ApprovalRequired(tool_name, registered.spec.risk_level)

        started = time.perf_counter()
        try:
            result = registered.handler(context, arguments)
        except ToolError:
            raise
        except sqlite3.Error as exc:
            raise ToolError(
                f"数据访问失败，请稍后重试：{exc}",
                code="storage_error",
            ) from exc
        except Exception as exc:  # pragma: no cover - 防御未预期实现错误
            raise ToolError(f"工具执行失败：{exc}", code="tool_failed") from exc

        result.tool_name = tool_name
        result.call_id = context.call_id or result.call_id
        result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        result.ok = True
        return result

    @classmethod
    def default(cls) -> ToolRegistry:
        """构造内置工具集。延迟 import 避免 tools 与 registry 循环依赖。"""

        registry = cls()
        from app.tools import billing, handoff, orders, shipments

        orders.register(registry)
        shipments.register(registry)
        billing.register(registry)
        handoff.register(registry)
        return registry


_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    check = _TYPE_CHECKS.get(expected)
    if check is None:
        raise ValueError(f"校验器不支持 type={expected!r}")
    return isinstance(value, check)


def validate_arguments(schema: dict[str, Any], arguments: Any) -> None:
    """校验 JSON Schema 子集。

    支持：type / required / properties / additionalProperties / enum / pattern /
    minLength / maxLength / minimum / maximum / minItems / maxItems / items。
    不支持 $ref、oneOf 等重型特性；跨字段规则放在工具实现里，报错更准确。
    """

    if not isinstance(schema, dict):
        raise ValueError("工具 schema 必须是 dict")
    if schema.get("type") != "object":
        raise ValueError("工具 schema 根节点必须是 object")
    _validate_value(schema, arguments, "$")


def _validate_value(schema: dict[str, Any], value: Any, path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            raise InvalidArguments(f"参数 {path} 类型应为 {expected}", path=path)
    elif expected and not _matches_type(value, expected):
        raise InvalidArguments(f"参数 {path} 类型应为 {expected}", path=path)

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise InvalidArguments(
            f"参数 {path} 只能取 {enum} 中的一个，当前为 {value!r}", path=path
        )

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise InvalidArguments(f"参数 {path} 长度不能少于 {min_length}", path=path)
        if isinstance(max_length, int) and len(value) > max_length:
            raise InvalidArguments(f"参数 {path} 长度不能超过 {max_length}", path=path)
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            raise InvalidArguments(
                f"参数 {path} 格式不正确，应匹配 {pattern}", path=path
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise InvalidArguments(f"参数 {path} 不能小于 {minimum}", path=path)
        if maximum is not None and value > maximum:
            raise InvalidArguments(f"参数 {path} 不能大于 {maximum}", path=path)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("schema.properties 必须是 dict")
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise InvalidArguments(f"缺少必填参数 {path}.{key}", path=f"{path}.{key}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_value(properties[key], item, f"{path}.{key}")
            elif additional is False:
                raise InvalidArguments(f"不支持未知参数 {path}.{key}", path=f"{path}.{key}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise InvalidArguments(f"参数 {path} 至少需要 {min_items} 项", path=path)
        if isinstance(max_items, int) and len(value) > max_items:
            raise InvalidArguments(f"参数 {path} 最多允许 {max_items} 项", path=path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item_schema, item, f"{path}[{index}]")
