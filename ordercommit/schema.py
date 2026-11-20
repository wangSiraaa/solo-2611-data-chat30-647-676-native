"""SQLite 表结构：报价服务与分配仓各自的原始状态。

报价服务表（前缀 s_）：
    s_rule_versions    规则版本头
    s_sku_rules        版本内逐 SKU 规则（包装倍数、最低量、可供上限、全量阶梯）
    s_quotes           不可变报价
    s_commitments      已接受的实际供货承诺

分配仓表（前缀 w_）：
    w_demands          门店需求行
    w_commitments      分配仓侧记录的真实承诺（引用 supplier 侧 commitment_no，只存外部事实）
    w_receipts         到货身份（绑定承诺，处理一次）
    w_receipt_lines    到货明细（receipt+sku+qty）
    w_allocations      逐条发放记录（库存流出发票，只追加）
    w_demand_ledgers   需求级已发数量（条件更新防并发超发）
    w_corrections      需求更正 / 取消审计
"""

from __future__ import annotations

#: 报价服务 DDL
SUPPLIER_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS s_rule_versions (
    version_id      TEXT PRIMARY KEY,
    supplier_id     TEXT NOT NULL,
    effective_from  TEXT NOT NULL,          -- ISO 时刻，含
    effective_to    TEXT NOT NULL,          -- ISO 时刻，不含
    created_at      TEXT NOT NULL,
    CHECK (effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS s_sku_rules (
    version_id      TEXT NOT NULL REFERENCES s_rule_versions(version_id),
    sku             TEXT NOT NULL,
    pack_multiple   INTEGER NOT NULL CHECK (pack_multiple >= 1),
    min_qty         INTEGER NOT NULL CHECK (min_qty >= 0),
    capacity        INTEGER NOT NULL CHECK (capacity >= 0),
    PRIMARY KEY (version_id, sku)
);

CREATE TABLE IF NOT EXISTS s_tiers (
    version_id      TEXT NOT NULL REFERENCES s_rule_versions(version_id),
    sku             TEXT NOT NULL,
    lower_bound     INTEGER NOT NULL CHECK (lower_bound >= 0),
    unit_price      INTEGER NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (version_id, sku, lower_bound)
);

CREATE TABLE IF NOT EXISTS s_quotes (
    quote_no        TEXT PRIMARY KEY,
    request_no      TEXT NOT NULL,
    supplier_id     TEXT NOT NULL,
    version_id      TEXT NOT NULL REFERENCES s_rule_versions(version_id),
    request_hash    TEXT NOT NULL,
    items_json      TEXT NOT NULL,          -- 请求快照 [{sku, qty}, ...]
    line_results_json TEXT NOT NULL,        -- 逐 SKU 校验/计价结果
    total_price     INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OPEN','ACCEPTED','REJECTED')),
    created_at      TEXT NOT NULL,
    accepted_at     TEXT,
    commitment_no   TEXT,
    UNIQUE (request_no)
);

CREATE TABLE IF NOT EXISTS s_commitments (
    commitment_no   TEXT PRIMARY KEY,
    quote_no        TEXT NOT NULL UNIQUE REFERENCES s_quotes(quote_no),
    supplier_id     TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    request_json    TEXT NOT NULL,          -- 接受的数量向量快照
    total_price     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS s_commitment_lines (
    commitment_no   TEXT NOT NULL REFERENCES s_commitments(commitment_no),
    sku             TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty >= 0),
    unit_price      INTEGER NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (commitment_no, sku)
);

CREATE INDEX IF NOT EXISTS idx_s_quotes_request ON s_quotes(request_no);
CREATE INDEX IF NOT EXISTS idx_s_commit_lines_version_sku
    ON s_commitment_lines(sku);
"""

#: 分配仓 DDL
WAREHOUSE_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS w_demands (
    demand_id       TEXT PRIMARY KEY,
    sku             TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    priority        INTEGER NOT NULL,
    due_date        TEXT NOT NULL,          -- ISO 日期字符串，字典序即可排序
    status          TEXT NOT NULL CHECK (status IN ('OPEN','PARTIAL','DONE','CANCELLED')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS w_commitments (
    commitment_no   TEXT PRIMARY KEY,
    supplier_id     TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS w_commitment_lines (
    commitment_no   TEXT NOT NULL REFERENCES w_commitments(commitment_no),
    sku             TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty >= 0),
    unit_price      INTEGER NOT NULL CHECK (unit_price >= 0),
    received_qty    INTEGER NOT NULL DEFAULT 0 CHECK (received_qty >= 0),
    PRIMARY KEY (commitment_no, sku)
);

CREATE TABLE IF NOT EXISTS w_receipts (
    receipt_id      TEXT PRIMARY KEY,
    commitment_no   TEXT NOT NULL REFERENCES w_commitments(commitment_no),
    supplier_id     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    allocated       INTEGER NOT NULL DEFAULT 0 CHECK (allocated IN (0, 1))
);

CREATE TABLE IF NOT EXISTS w_receipt_lines (
    receipt_id      TEXT NOT NULL REFERENCES w_receipts(receipt_id),
    sku             TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (receipt_id, sku)
);

CREATE TABLE IF NOT EXISTS w_allocations (
    allocation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id      TEXT NOT NULL REFERENCES w_receipts(receipt_id),
    sku             TEXT NOT NULL,
    demand_id       TEXT NOT NULL REFERENCES w_demands(demand_id),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS w_demand_ledgers (
    demand_id       TEXT PRIMARY KEY REFERENCES w_demands(demand_id),
    allocated_qty   INTEGER NOT NULL DEFAULT 0 CHECK (allocated_qty >= 0)
);

CREATE TABLE IF NOT EXISTS w_corrections (
    correction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id       TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('CORRECT','CANCEL')),
    old_qty         INTEGER,
    new_qty         INTEGER,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_w_alloc_demand ON w_allocations(demand_id);
CREATE INDEX IF NOT EXISTS idx_w_alloc_receipt ON w_allocations(receipt_id);
"""
