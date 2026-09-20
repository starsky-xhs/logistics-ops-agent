-- 物流 Agent 的本地业务库。所有金额统一存整数分，避免浮点误差。
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no          TEXT NOT NULL UNIQUE,
    customer_id       INTEGER NOT NULL REFERENCES customers(id),
    status            TEXT NOT NULL CHECK (
        status IN ('pending', 'confirmed', 'packed', 'shipped', 'delivered', 'cancelled')
    ),
    amount_cents      INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency          TEXT NOT NULL DEFAULT 'CNY',
    delivery_address  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_no        TEXT NOT NULL UNIQUE,
    order_id           INTEGER NOT NULL REFERENCES orders(id),
    carrier            TEXT NOT NULL CHECK (carrier IN ('SF', 'TD')),
    status             TEXT NOT NULL CHECK (
        status IN ('created', 'picked_up', 'in_transit', 'out_for_delivery',
                   'delivered', 'exception', 'cancelled')
    ),
    current_city       TEXT NOT NULL DEFAULT '',
    last_event         TEXT NOT NULL DEFAULT '',
    estimated_delivery TEXT NOT NULL DEFAULT '',
    delivery_address   TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no       TEXT NOT NULL UNIQUE,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    title            TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    tax_rate_bp      INTEGER NOT NULL DEFAULT 600 CHECK (tax_rate_bp BETWEEN 0 AND 1300),
    status           TEXT NOT NULL CHECK (status IN ('issued', 'void', 'refunded')),
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    tool_name      TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    risk_level     TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'consumed')
    ),
    requested_at   TEXT NOT NULL,
    resolved_at    TEXT NOT NULL DEFAULT '',
    resolved_by    TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS handoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no   TEXT NOT NULL UNIQUE,
    session_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    category    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    session_id     TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    tool_name      TEXT NOT NULL,
    args_summary   TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    risk_level     TEXT NOT NULL,
    approval_id    TEXT NOT NULL DEFAULT '',
    approved       INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
    status         TEXT NOT NULL,
    latency_ms     REAL NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 状态机快照。中断恢复依赖这里的 state_json，而不是进程内内存。
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    user_input  TEXT NOT NULL,
    status      TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    run_id      TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_invoices_order ON invoices(order_id);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
