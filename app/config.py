"""运行配置。

所有配置都来自环境变量，并允许项目根目录下的 ``.env`` 提供默认值。
项目刻意不依赖 python-dotenv：核心路径必须能在纯净的标准库环境里跑通。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "logistics.db"

PLANNERS = frozenset({"mock", "llm"})
APPROVAL_POLICIES = frozenset({"high_only", "conservative"})


class ConfigError(RuntimeError):
    """配置非法或缺少必需项。"""


def load_dotenv(path: Path | None = None) -> None:
    """把项目根目录的 ``.env`` 读入环境变量（不覆盖已有值）。

    只实现最常见的 ``KEY=VALUE`` 语法，支持 ``#`` 注释、单双引号与 ``export``
    前缀。解析失败的行会被忽略，避免一个笔误让整个服务无法启动。
    """

    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _env_text(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} 必须位于 [{minimum}, {maximum}]，当前值为 {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前值为 {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} 必须位于 [{minimum}, {maximum}]，当前值为 {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是布尔值（true/false），当前值为 {raw!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    """一次进程生命周期的不可变配置。"""

    db_path: Path = DEFAULT_DB_PATH
    planner: str = "mock"
    max_steps: int = 8
    time_budget_seconds: float = 20.0
    approval_policy: str = "high_only"
    audit_mask_pii: bool = True

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量构建配置，并在启动阶段完成校验。"""

        load_dotenv()

        planner = _env_text("PLANNER", "mock").lower()
        if planner not in PLANNERS:
            raise ConfigError(
                f"PLANNER 只支持 {sorted(PLANNERS)}，当前值为 {planner!r}"
            )

        approval_policy = _env_text("APPROVAL_POLICY", "high_only").lower()
        if approval_policy not in APPROVAL_POLICIES:
            raise ConfigError(
                "APPROVAL_POLICY 只支持 high_only / conservative，"
                f"当前值为 {approval_policy!r}"
            )

        db_raw = _env_text("DB_PATH", str(DEFAULT_DB_PATH))
        db_path = Path(db_raw).expanduser()
        if not db_path.is_absolute():
            db_path = (PROJECT_ROOT / db_path).resolve()

        api_key = os.environ.get("LLM_API_KEY", "").strip()
        if planner == "llm" and not api_key:
            raise ConfigError(
                "PLANNER=llm 需要设置 LLM_API_KEY；离线演示请保持 PLANNER=mock。"
            )

        return cls(
            db_path=db_path,
            planner=planner,
            max_steps=_env_int("MAX_STEPS", 8, minimum=1, maximum=50),
            time_budget_seconds=_env_float(
                "TIME_BUDGET_SECONDS", 20.0, minimum=1.0, maximum=600.0
            ),
            approval_policy=approval_policy,
            audit_mask_pii=_env_bool("AUDIT_MASK_PII", True),
            llm_base_url=_env_text("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            llm_api_key=api_key,
            llm_model=_env_text("LLM_MODEL", "deepseek-chat"),
            llm_timeout_seconds=_env_float(
                "LLM_TIMEOUT_SECONDS", 15.0, minimum=1.0, maximum=120.0
            ),
        )

    @property
    def db_file(self) -> str:
        """返回 sqlite3.connect 可使用的字符串路径。"""

        return str(self.db_path)
