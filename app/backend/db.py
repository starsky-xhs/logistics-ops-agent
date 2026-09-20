"""SQLite 连接与初始化。

选择 SQLite 不是为了简化业务，而是为了让个人项目可以一条命令跑起来：
WAL 模式允许 Web 进程读审计日志时不被写请求长期阻塞，外键约束则守住
订单/运单/发票之间的关系。生产环境换 PostgreSQL 时，这一层是主要替换点。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """打开连接并设置 WAL、外键、忙等待。

    ``isolation_level=None`` 使用 autocommit；需要原子性的地方显式开启事务。
    服务层用一把进程内锁串行化一次 Agent 回合，避免同一连接并发写。
    """

    path_text = str(db_path)
    if path_text != ":memory:":
        Path(path_text).expanduser().parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        path_text,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def init_db(db_path: str | Path, *, with_seed: bool = True) -> sqlite3.Connection:
    """建表并按需灌入演示数据，返回已打开的连接。"""

    connection = connect(db_path)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if with_seed:
        from app.backend.seed import seed_if_empty

        seed_if_empty(connection)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务；出错时回滚，避免部分写入。"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def health(db_path: str | Path) -> dict[str, object]:
    """供 /healthz 使用的轻量健康检查，不暴露业务数据。"""

    path = Path(str(db_path))
    try:
        connection = connect(db_path)
        try:
            orders = connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
            approvals = connection.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'"
            ).fetchone()["n"]
        finally:
            connection.close()
        return {
            "ok": True,
            "database": str(path),
            "orders": int(orders),
            "pending_approvals": int(approvals),
            "wal": True,
        }
    except Exception as exc:  # pragma: no cover - 路径不可写等环境问题
        return {"ok": False, "database": str(path), "error": str(exc)}
