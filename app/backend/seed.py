"""生成可复现的演示数据。

数据数量是 30 个订单 / 10 个客户，尽量让演示看起来像真实后台快照：
订单号带年份、金额到分、运单分 SF/TD、只有部分订单出票。所有日期固定，
这样 demo、评测和截图不会因为运行日期不同而漂移。
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta

from app.backend.db import connect, init_db, transaction

CUSTOMERS: tuple[tuple[str, str, str, str, str], ...] = (
    ("林晓", "13800138001", "lin.xiao@example.com", "杭州市", "杭州市西湖区文三路 128 号 5 幢 402"),
    ("陈默", "13900139002", "chen.mo@example.com", "上海市", "上海市浦东新区张江路 66 号 3 号楼 1102"),
    ("王芳", "13700137003", "wang.fang@example.com", "北京市", "北京市朝阳区望京街 9 号院 2 单元 801"),
    ("赵磊", "13600136004", "zhao.lei@example.com", "深圳市", "深圳市南山区科技园科苑路 15 号"),
    ("周雨", "13500135005", "zhou.yu@example.com", "成都市", "成都市武侯区天府三街 88 号 6 栋 501"),
    ("吴昊", "13400134006", "wu.hao@example.com", "武汉市", "武汉市洪山区光谷大道 77 号 A 座 1203"),
    ("郑楠", "13300133007", "zheng.nan@example.com", "南京市", "南京市鼓楼区中山北路 105 号"),
    ("孙悦", "13200132008", "sun.yue@example.com", "广州市", "广州市天河区体育西路 191 号 B 塔 2806"),
    ("刘洋", "13100131009", "liu.yang@example.com", "西安市", "西安市雁塔区高新路 25 号 2 单元 902"),
    ("何静", "13000131010", "he.jing@example.com", "苏州市", "苏州市工业园区星湖街 328 号 7 栋 303"),
)

ORDER_STATUSES: tuple[str, ...] = (
    "shipped", "delivered", "shipped", "delivered", "shipped",
    "confirmed", "delivered", "packed", "pending", "delivered",
    "confirmed", "confirmed", "pending", "delivered", "cancelled",
    "shipped", "pending", "delivered", "confirmed", "shipped",
    "packed", "delivered", "pending", "confirmed", "cancelled",
    "shipped", "pending", "delivered", "confirmed", "packed",
)

AMOUNTS_CENTS: tuple[int, ...] = (
    12800, 45900, 6990, 138000, 25900,
    32900, 18800, 9990, 56800, 7800,
    245000, 15900, 23400, 42000, 8800,
    35900, 12600, 89000, 47200, 16600,
    30500, 21800, 3990, 129000, 67000,
    27800, 9500, 55800, 18900, 7400,
)

INVOICE_ORDERS: frozenset[int] = frozenset({2, 4, 10, 14, 18, 22, 26, 30})

INVOICE_TITLES: dict[int, str] = {
    2: "林晓（个人）",
    4: "深圳市南山区云帆科技有限公司",
    10: "何静（个人）",
    14: "广州星野文化传播有限公司",
    18: "杭州栖木供应链有限公司",
    22: "刘洋（个人）",
    26: "苏州见山智能科技有限公司",
    30: "北京远川设计咨询有限公司",
}

SHIPMENT_STATUS: dict[str, str] = {
    "shipped": "in_transit",
    "delivered": "delivered",
    "packed": "picked_up",
    "confirmed": "created",
}

SHIPMENT_EVENT: dict[str, str] = {
    "created": "物流单已创建，等待揽收",
    "picked_up": "已揽收快件，等待发往分拨中心",
    "in_transit": "快件已到达{city}转运中心",
    "out_for_delivery": "派件员正在派送，请保持电话畅通",
    "delivered": "已签收，签收人：本人",
    "exception": "收件地址异常，等待处理",
    "cancelled": "运单已取消",
}

BASE_TIME = datetime(2025, 5, 6, 9, 30)


def _order_no(index: int) -> str:
    return f"SH2025{index:04d}"


def _tracking_no(index: int, carrier: str) -> str:
    return f"{carrier}202500{index:02d}0001"


def _timestamp(offset_days: int = 0, hours: int = 0) -> str:
    return (BASE_TIME + timedelta(days=offset_days, hours=hours)).isoformat(
        timespec="seconds"
    )


def seed_if_empty(connection: sqlite3.Connection) -> bool:
    """订单表非空时跳过，返回是否实际写入。"""

    existing = connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    if existing:
        return False

    with transaction(connection):
        customer_ids: list[int] = []
        for offset, (name, phone, email, _city, _address) in enumerate(CUSTOMERS):
            cursor = connection.execute(
                """
                INSERT INTO customers (name, phone, email, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, phone, email, _timestamp(offset - 40)),
            )
            customer_ids.append(int(cursor.lastrowid))

        for index in range(1, 31):
            customer_index = (index - 1) % len(CUSTOMERS)
            customer_id = customer_ids[customer_index]
            name, _phone, _email, city, address = CUSTOMERS[customer_index]
            status = ORDER_STATUSES[index - 1]
            created_at = _timestamp(index - 1, (index * 3) % 9)
            updated_at = _timestamp(index + 1, (index * 2) % 8)
            connection.execute(
                """
                INSERT INTO orders
                    (order_no, customer_id, status, amount_cents, currency,
                     delivery_address, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'CNY', ?, ?, ?)
                """,
                (
                    _order_no(index),
                    customer_id,
                    status,
                    AMOUNTS_CENTS[index - 1],
                    address,
                    created_at,
                    updated_at,
                ),
            )
            order_id = connection.execute(
                "SELECT id FROM orders WHERE order_no = ?", (_order_no(index),)
            ).fetchone()["id"]

            if status in {"shipped", "delivered", "packed"}:
                carrier = "TD" if index % 5 == 0 else "SF"
                shipment_status = SHIPMENT_STATUS[status]
                if index == 20:
                    shipment_status = "out_for_delivery"
                if index == 30:
                    shipment_status = "exception"
                event = SHIPMENT_EVENT[shipment_status].format(city=city)
                estimated = (BASE_TIME + timedelta(days=index + 6)).date().isoformat()
                connection.execute(
                    """
                    INSERT INTO shipments
                        (tracking_no, order_id, carrier, status, current_city,
                         last_event, estimated_delivery, delivery_address, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _tracking_no(index, carrier),
                        order_id,
                        carrier,
                        shipment_status,
                        city,
                        event,
                        estimated,
                        address,
                        updated_at,
                    ),
                )

            if index in INVOICE_ORDERS:
                connection.execute(
                    """
                    INSERT INTO invoices
                        (invoice_no, order_id, title, amount_cents, tax_rate_bp,
                         status, idempotency_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 600, 'issued', ?, ?, ?)
                    """,
                    (
                        f"INV2025{index:06d}",
                        order_id,
                        INVOICE_TITLES[index],
                        AMOUNTS_CENTS[index - 1],
                        f"seed:order:{_order_no(index)}",
                        _timestamp(index, 4),
                        _timestamp(index, 4),
                    ),
                )

        # 让演示数据引用得到明确的下单客户名称，便于人工核对。
        missing = connection.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)"
        ).fetchone()["n"]
        if missing:
            raise RuntimeError("种子数据存在悬空客户引用")
    return True


def seed_database(db_path: str, *, force: bool = False) -> bool:
    """初始化任意路径的数据库；``force=True`` 时清空后重建。"""

    connection = init_db(db_path, with_seed=False)
    try:
        if force:
            with transaction(connection):
                for table in (
                    "audit_log", "approvals", "handoffs", "messages",
                    "runs", "invoices", "shipments", "orders", "customers", "sessions",
                ):
                    connection.execute(f"DELETE FROM {table}")  # noqa: S608 - 固定白名单
        return seed_if_empty(connection)
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化物流 Agent 的演示 SQLite 数据")
    parser.add_argument("--db", default="data/logistics.db", help="数据库路径")
    parser.add_argument("--force", action="store_true", help="清空业务表后重建")
    args = parser.parse_args()
    changed = seed_database(args.db, force=args.force)
    connection = connect(args.db)
    try:
        count = connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    finally:
        connection.close()
    print(f"{'已写入' if changed else '已存在'}种子数据，orders={count}，db={args.db}")


if __name__ == "__main__":
    main()
