"""SQLite 持久化层。

设计原则：

* 生产侧的修订只追加：新修订把旧修订置为 ``superseded``，撤销置 ``cancelled``，
  历史行永远保留；成本侧每个成本切面（cut）及其产品独立成行，重算产生新切面，
  旧切面与旧产品同样保留。关闭重开后全部可查。
* 精确有理数以 ``(分子, 分母)`` 两个整数列存储，不使用浮点。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Iterable, Optional

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------- 生产台账 ----------------
CREATE TABLE IF NOT EXISTS root_lots (
    lot_id     TEXT PRIMARY KEY,
    grams      INTEGER NOT NULL CHECK (grams > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_revisions (
    revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id),
    revision_no    INTEGER NOT NULL,
    input_lot_id   TEXT NOT NULL,
    input_grams    INTEGER NOT NULL CHECK (input_grams > 0),
    loss_grams     INTEGER NOT NULL CHECK (loss_grams >= 0),
    status         TEXT NOT NULL CHECK (status IN ('active','superseded','cancelled')),
    superseded_by  INTEGER REFERENCES batch_revisions(revision_id),
    created_at     TEXT NOT NULL,
    UNIQUE (batch_id, revision_no)
);

CREATE TABLE IF NOT EXISTS revision_outputs (
    revision_id INTEGER NOT NULL REFERENCES batch_revisions(revision_id),
    lot_id      TEXT NOT NULL,
    grams       INTEGER NOT NULL CHECK (grams > 0),
    PRIMARY KEY (revision_id, lot_id)
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id              TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('root','output')),
    origin_batch        TEXT REFERENCES batches(batch_id),
    created_revision_id INTEGER REFERENCES batch_revisions(revision_id)
);

-- ---------------- 成本仓版本化输入 ----------------
CREATE TABLE IF NOT EXISTS cost_root_versions (
    version        TEXT NOT NULL,
    lot_id         TEXT NOT NULL,
    total_cost_num INTEGER NOT NULL,
    total_cost_den INTEGER NOT NULL CHECK (total_cost_den > 0),
    PRIMARY KEY (version, lot_id)
);

CREATE TABLE IF NOT EXISTS processing_fee_versions (
    version TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    fee_num INTEGER NOT NULL,
    fee_den INTEGER NOT NULL CHECK (fee_den > 0),
    PRIMARY KEY (version, batch_id)
);

CREATE TABLE IF NOT EXISTS reference_value_versions (
    version   TEXT NOT NULL,
    lot_id    TEXT NOT NULL,
    value_num INTEGER NOT NULL CHECK (value_num >= 0),
    value_den INTEGER NOT NULL CHECK (value_den > 0),
    PRIMARY KEY (version, lot_id)
);

-- ---------------- 成本切面与产品 ----------------
CREATE TABLE IF NOT EXISTS cost_cuts (
    cut_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root_version TEXT NOT NULL,
    fee_version  TEXT NOT NULL,
    ref_version  TEXT NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('full','incremental')),
    note         TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_products (
    cut_id           TEXT NOT NULL REFERENCES cost_cuts(cut_id),
    lot_id           TEXT NOT NULL,
    produced_grams   INTEGER NOT NULL,
    ref_num          INTEGER NOT NULL,
    ref_den          INTEGER NOT NULL,
    priceable        INTEGER NOT NULL CHECK (priceable IN (0,1)),
    exact_num        INTEGER,
    exact_den        INTEGER,
    published        INTEGER,
    reason           TEXT,
    origin_batch     TEXT,
    PRIMARY KEY (cut_id, lot_id)
);

CREATE TABLE IF NOT EXISTS cut_batches (
    cut_id           TEXT NOT NULL REFERENCES cost_cuts(cut_id),
    batch_id         TEXT NOT NULL,
    input_lot_id     TEXT NOT NULL,
    input_grams      INTEGER NOT NULL,
    input_cost_num   INTEGER,
    input_cost_den   INTEGER,
    fee_num          INTEGER NOT NULL,
    fee_den          INTEGER NOT NULL,
    pool_num         INTEGER,
    pool_den         INTEGER,
    pool_published   INTEGER,
    weight_num       INTEGER NOT NULL,
    weight_den       INTEGER NOT NULL,
    priceable        INTEGER NOT NULL CHECK (priceable IN (0,1)),
    reason           TEXT,
    PRIMARY KEY (cut_id, batch_id)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def frac_to_int(value: Fraction) -> tuple[int, int]:
    return value.numerator, value.denominator


def int_to_frac(num: Optional[int], den: Optional[int]) -> Optional[Fraction]:
    if num is None or den is None:
        return None
    return Fraction(num, den)


class Storage:
    """封装全部 SQLite 访问；仓库层不做业务规则判断。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ---------------- 生产：根批号 ----------------

    def insert_root_lot(self, lot_id: str, grams: int) -> None:
        self.conn.execute(
            "INSERT INTO root_lots(lot_id, grams, created_at) VALUES (?,?,?)",
            (lot_id, grams, _utcnow()),
        )
        self.conn.execute(
            "INSERT INTO lots(lot_id, kind, origin_batch, created_revision_id) "
            "VALUES (?, 'root', NULL, NULL)",
            (lot_id,),
        )

    def get_root_lot(self, lot_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM root_lots WHERE lot_id = ?", (lot_id,)
        ).fetchone()

    def all_root_lots(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM root_lots ORDER BY lot_id"))

    # ---------------- 生产：批次与修订 ----------------

    def insert_batch(self, batch_id: str) -> None:
        self.conn.execute(
            "INSERT INTO batches(batch_id, created_at) VALUES (?,?)",
            (batch_id, _utcnow()),
        )

    def batch_exists(self, batch_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return row is not None

    def insert_revision(
        self,
        batch_id: str,
        revision_no: int,
        input_lot_id: str,
        input_grams: int,
        loss_grams: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO batch_revisions(batch_id, revision_no, input_lot_id, "
            "input_grams, loss_grams, status, superseded_by, created_at) "
            "VALUES (?,?,?,?,?, 'active', NULL, ?)",
            (batch_id, revision_no, input_lot_id, input_grams, loss_grams, _utcnow()),
        )
        return int(cur.lastrowid)

    def insert_outputs(self, revision_id: int, outputs: Iterable[tuple[str, int]]) -> None:
        self.conn.executemany(
            "INSERT INTO revision_outputs(revision_id, lot_id, grams) VALUES (?,?,?)",
            [(revision_id, lot_id, grams) for lot_id, grams in outputs],
        )

    def insert_output_lot(self, lot_id: str, batch_id: str, revision_id: int) -> None:
        self.conn.execute(
            "INSERT INTO lots(lot_id, kind, origin_batch, created_revision_id) "
            "VALUES (?, 'output', ?, ?)",
            (lot_id, batch_id, revision_id),
        )

    def lot_exists(self, lot_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM lots WHERE lot_id = ?", (lot_id,)
        ).fetchone() is not None

    def get_lot_row(self, lot_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM lots WHERE lot_id = ?", (lot_id,)).fetchone()

    def all_lot_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM lots ORDER BY lot_id"))

    def supersede_revision(self, revision_id: int, new_revision_id: int) -> None:
        self.conn.execute(
            "UPDATE batch_revisions SET status='superseded', superseded_by=? "
            "WHERE revision_id=?",
            (new_revision_id, revision_id),
        )

    def cancel_revision(self, revision_id: int) -> None:
        self.conn.execute(
            "UPDATE batch_revisions SET status='cancelled' WHERE revision_id=?",
            (revision_id,),
        )

    def get_revision(self, revision_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()

    def latest_revision(self, batch_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batch_revisions WHERE batch_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            (batch_id,),
        ).fetchone()

    def list_revisions(self, batch_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM batch_revisions WHERE batch_id=? ORDER BY revision_no",
                (batch_id,),
            )
        )

    def all_active_revisions(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM batch_revisions WHERE status='active' ORDER BY batch_id"
            )
        )

    def outputs_of(self, revision_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM revision_outputs WHERE revision_id=? ORDER BY lot_id",
                (revision_id,),
            )
        )

    def all_batches(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM batches ORDER BY batch_id"))

    # ---------------- 成本：版本化输入 ----------------

    def upsert_root_cost(self, version: str, lot_id: str, total_cost: Fraction) -> None:
        num, den = frac_to_int(total_cost)
        self.conn.execute(
            "INSERT OR REPLACE INTO cost_root_versions(version, lot_id, total_cost_num, "
            "total_cost_den) VALUES (?,?,?,?)",
            (version, lot_id, num, den),
        )

    def get_root_costs(self, version: str) -> dict[str, Fraction]:
        rows = self.conn.execute(
            "SELECT * FROM cost_root_versions WHERE version=?", (version,)
        ).fetchall()
        return {r["lot_id"]: Fraction(r["total_cost_num"], r["total_cost_den"]) for r in rows}

    def list_root_cost_versions(self) -> list[str]:
        return [r["version"] for r in self.conn.execute(
            "SELECT DISTINCT version FROM cost_root_versions ORDER BY version")]

    def upsert_fee(self, version: str, batch_id: str, fee: Fraction) -> None:
        num, den = frac_to_int(fee)
        self.conn.execute(
            "INSERT OR REPLACE INTO processing_fee_versions(version, batch_id, fee_num, fee_den) "
            "VALUES (?,?,?,?)",
            (version, batch_id, num, den),
        )

    def get_fees(self, version: str) -> dict[str, Fraction]:
        rows = self.conn.execute(
            "SELECT * FROM processing_fee_versions WHERE version=?", (version,)
        ).fetchall()
        return {r["batch_id"]: Fraction(r["fee_num"], r["fee_den"]) for r in rows}

    def list_fee_versions(self) -> list[str]:
        return [r["version"] for r in self.conn.execute(
            "SELECT DISTINCT version FROM processing_fee_versions ORDER BY version")]

    def upsert_reference_value(self, version: str, lot_id: str, value: Fraction) -> None:
        num, den = frac_to_int(value)
        self.conn.execute(
            "INSERT OR REPLACE INTO reference_value_versions(version, lot_id, value_num, value_den) "
            "VALUES (?,?,?,?)",
            (version, lot_id, num, den),
        )

    def get_reference_values(self, version: str) -> dict[str, Fraction]:
        rows = self.conn.execute(
            "SELECT * FROM reference_value_versions WHERE version=?", (version,)
        ).fetchall()
        return {r["lot_id"]: Fraction(r["value_num"], r["value_den"]) for r in rows}

    def list_ref_versions(self) -> list[str]:
        return [r["version"] for r in self.conn.execute(
            "SELECT DISTINCT version FROM reference_value_versions ORDER BY version")]

    # ---------------- 成本：切面与产品 ----------------

    def insert_cut(
        self, cut_id: str, name: str, root_version: str, fee_version: str,
        ref_version: str, mode: str, note: Optional[str],
    ) -> None:
        self.conn.execute(
            "INSERT INTO cost_cuts(cut_id, name, root_version, fee_version, ref_version, "
            "mode, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (cut_id, name, root_version, fee_version, ref_version, mode, note, _utcnow()),
        )

    def get_cut(self, cut_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cost_cuts WHERE cut_id=?", (cut_id,)
        ).fetchone()

    def latest_cut(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cost_cuts ORDER BY created_at DESC, cut_id DESC LIMIT 1"
        ).fetchone()

    def list_cuts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM cost_cuts ORDER BY created_at, cut_id"))

    def replace_product(self, cut_id: str, p: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cost_products(cut_id, lot_id, produced_grams, ref_num, "
            "ref_den, priceable, exact_num, exact_den, published, reason, origin_batch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cut_id, p.lot_id, p.grams,
                p.reference_unit_value.numerator, p.reference_unit_value.denominator,
                1 if p.priceable else 0,
                None if p.exact_total_cost is None else p.exact_total_cost.numerator,
                None if p.exact_total_cost is None else p.exact_total_cost.denominator,
                p.published_total_cost, p.reason, p.origin_batch,
            ),
        )

    def get_products(self, cut_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM cost_products WHERE cut_id=? ORDER BY lot_id", (cut_id,)
            )
        )

    def replace_cut_batch(self, cut_id: str, b: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cut_batches(cut_id, batch_id, input_lot_id, input_grams, "
            "input_cost_num, input_cost_den, fee_num, fee_den, pool_num, pool_den, "
            "pool_published, weight_num, weight_den, priceable, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cut_id, b.batch_id, b.input_lot_id, b.input_grams,
                None if b.input_exact_cost is None else b.input_exact_cost.numerator,
                None if b.input_exact_cost is None else b.input_exact_cost.denominator,
                b.processing_fee.numerator, b.processing_fee.denominator,
                None if b.pool_exact is None else b.pool_exact.numerator,
                None if b.pool_exact is None else b.pool_exact.denominator,
                b.pool_published,
                b.weight_total.numerator, b.weight_total.denominator,
                1 if b.priceable else 0, b.reason,
            ),
        )

    def get_cut_batches(self, cut_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM cut_batches WHERE cut_id=? ORDER BY batch_id", (cut_id,)
            )
        )
