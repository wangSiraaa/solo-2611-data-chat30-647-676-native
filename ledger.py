"""独立库存账本（SQLite，真实事务）。

账本只认识三种业务事件：入库、出库、同 SKU 跨库位移动、以及由盘点工作台
通过公开接口发布的差异调整。所有写操作都走同一条幂等申请通道：

* 全局严格递增的 ``commit_seq``（只有成功操作才占用序号）；
* 稳定请求号 ``request_no``：同号同文直接复读原回执，同号异文拒绝；
* 数量为非负整数，非法出库在事务内被 CHECK 与前置校验双重拦截，
  不会留下负库存，也不会留下半次移动；
* 可在任意已提交前缀 k 上物化快照，并支持逐操作事件查询。

本模块不依赖工作台，可单独给没有盘点业务的仓库客户使用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 工具与异常
# ---------------------------------------------------------------------------

def canonical_text(payload: Any) -> str:
    """把请求正文规范化为稳定字节串：键排序、无空白、键均为字符串。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class LedgerError(Exception):
    """账本层基础异常。"""


class IdempotencyConflict(LedgerError):
    """同请求号但请求正文不同。"""


class InvalidOperation(LedgerError):
    """非法业务操作（数量非法、库存不足、同库位移动等）。"""


@dataclass(frozen=True)
class MoveLine:
    """一条移动明细。direction: in / out / adjust。"""

    sku: str
    location: str
    direction: str
    qty: int
    line_no: int


@dataclass(frozen=True)
class OperationReceipt:
    """一次成功操作的稳定回执。"""

    commit_seq: int
    request_no: str
    op_type: str
    created_at: str
    lines: tuple[MoveLine, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_seq": self.commit_seq,
            "request_no": self.request_no,
            "op_type": self.op_type,
            "created_at": self.created_at,
            "lines": [line.__dict__ for line in self.lines],
        }


@dataclass(frozen=True)
class Snapshot:
    prefix: int
    label: str
    created_at: str
    balances: dict[tuple[str, str], int]


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_LEDGER_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS l_requests (
    request_no   TEXT PRIMARY KEY,
    op_type      TEXT NOT NULL,
    body_text    TEXT NOT NULL,
    -- 插入后立即用 rowid 回填；UNIQUE 保证全局严格递增且一操作一序号
    commit_seq   INTEGER UNIQUE,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS l_balances (
    sku       TEXT NOT NULL,
    location  TEXT NOT NULL,
    qty       INTEGER NOT NULL CHECK (qty >= 0),
    PRIMARY KEY (sku, location)
);

CREATE TABLE IF NOT EXISTS l_movements (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_seq  INTEGER NOT NULL UNIQUE,
    request_no  TEXT NOT NULL,
    op_type     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (request_no) REFERENCES l_requests(request_no)
);

CREATE TABLE IF NOT EXISTS l_movement_lines (
    movement_id INTEGER NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out', 'adjust')),
    qty         INTEGER NOT NULL,
    delta       INTEGER NOT NULL,
    PRIMARY KEY (movement_id, line_no),
    FOREIGN KEY (movement_id) REFERENCES l_movements(movement_id)
);

CREATE INDEX IF NOT EXISTS idx_lines_sku_loc
    ON l_movement_lines (sku, location);

CREATE TABLE IF NOT EXISTS l_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix      INTEGER NOT NULL,
    label       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS l_snapshot_lines (
    snapshot_id INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    qty         INTEGER NOT NULL CHECK (qty >= 0),
    PRIMARY KEY (snapshot_id, sku, location),
    FOREIGN KEY (snapshot_id) REFERENCES l_snapshots(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_prefix ON l_snapshots(prefix);
"""


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------

class InventoryLedger:
    """SQLite 库存账本。

    同进程内通过可重入锁串行化写事务；跨进程/跨连接由 SQLite 写锁和
    全部唯一约束兜底，保证并发重试不会重复调增/调减。
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        # check_same_thread=False：并发测试用两个独立连接实例，
        # 本实例仍由 self._lock 保护，不跨线程共享同一个连接。
        self.conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._lock = threading.RLock()
        self.conn.executescript(_LEDGER_DDL)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- 内部 -------------------------------------------------------------

    def _load_receipt(self, request_no: str) -> Optional[OperationReceipt]:
        row = self.conn.execute(
            "SELECT commit_seq, op_type, created_at FROM l_requests WHERE request_no=?",
            (request_no,),
        ).fetchone()
        if row is None:
            return None
        line_rows = self.conn.execute(
            """
            SELECT ml.sku, ml.location, ml.direction, ml.qty, ml.line_no
            FROM l_movement_lines ml
            JOIN l_movements m ON m.movement_id = ml.movement_id
            WHERE m.request_no=?
            ORDER BY ml.line_no
            """,
            (request_no,),
        ).fetchall()
        lines = tuple(
            MoveLine(
                sku=r["sku"],
                location=r["location"],
                direction=r["direction"],
                qty=r["qty"],
                line_no=r["line_no"],
            )
            for r in line_rows
        )
        return OperationReceipt(
            commit_seq=row["commit_seq"],
            request_no=request_no,
            op_type=row["op_type"],
            created_at=row["created_at"],
            lines=lines,
        )

    def _apply_in_tx(
        self,
        request_no: str,
        op_type: str,
        body_text: str,
        lines: list[dict[str, Any]],
    ) -> OperationReceipt:
        """在已开启 IMMEDIATE 事务的连接上提交一次操作（工作台复用）。"""
        existing = self.conn.execute(
            "SELECT op_type, body_text FROM l_requests WHERE request_no=?",
            (request_no,),
        ).fetchone()
        if existing is not None:
            if existing["body_text"] != body_text or existing["op_type"] != op_type:
                raise IdempotencyConflict(
                    f"请求号 {request_no} 已用于不同正文的 "
                    f"{existing['op_type']} 操作"
                )
            return self._load_receipt(request_no)  # type: ignore[return-value]

        # 汇总每个 (sku, location) 的净增量，逐位置做非负前置校验
        net: dict[tuple[str, str], int] = {}
        for ln in lines:
            if ln["direction"] not in ("in", "out", "adjust"):
                raise InvalidOperation(f"未知方向: {ln['direction']}")
            if not isinstance(ln["qty"], int) or isinstance(ln["qty"], bool):
                raise InvalidOperation("数量必须为整数")
            if ln["qty"] < 0:
                raise InvalidOperation("明细数量不能为负数")
            if ln["direction"] == "adjust":
                delta = ln["delta"]
            elif ln["qty"] == 0:
                raise InvalidOperation("入库/出库/移动数量必须为正整数")
            else:
                delta = ln["qty"] if ln["direction"] == "in" else -ln["qty"]
            ln["delta"] = delta
            key = (ln["sku"], ln["location"])
            net[key] = net.get(key, 0) + delta

        for (sku, location), delta in net.items():
            row = self.conn.execute(
                "SELECT qty FROM l_balances WHERE sku=? AND location=?",
                (sku, location),
            ).fetchone()
            current = row["qty"] if row else 0
            if current + delta < 0:
                raise InvalidOperation(
                    f"{sku}@{location} 库存不足: 当前 {current}, 净变动 {delta}"
                )

        cur = self.conn.execute(
            "INSERT INTO l_requests(request_no, op_type, body_text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (request_no, op_type, body_text, now_iso()),
        )
        # commit_seq 直接用 l_requests 行 id，保证全局严格递增且只被成功操作占用
        commit_seq = cur.lastrowid
        movement_id = self.conn.execute(
            "INSERT INTO l_movements(commit_seq, request_no, op_type, created_at) "
            "VALUES (?, ?, ?, ?)",
            (commit_seq, request_no, op_type, now_iso()),
        ).lastrowid

        for idx, ln in enumerate(lines):
            self.conn.execute(
                "INSERT INTO l_movement_lines"
                "(movement_id, line_no, sku, location, direction, qty, delta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    movement_id,
                    idx,
                    ln["sku"],
                    ln["location"],
                    ln["direction"],
                    ln["qty"],
                    ln["delta"],
                ),
            )
            if ln["delta"] != 0:
                # 新行的 delta 已由前置校验保证非负；已存在行的插入值恒为 0
                # 以通过列 CHECK，净变动完全由 ON CONFLICT 的 UPDATE 表达式完成
                self.conn.execute(
                    """
                    INSERT INTO l_balances(sku, location, qty)
                    VALUES (?, ?, MAX(0, ?))
                    ON CONFLICT(sku, location) DO UPDATE
                    SET qty = l_balances.qty + ?
                    """,
                    (
                        ln["sku"],
                        ln["location"],
                        ln["delta"],
                        ln["delta"],
                    ),
                )

        self.conn.execute(
            "UPDATE l_requests SET commit_seq=? WHERE rowid=?",
            (commit_seq, cur.lastrowid),
        )
        return self._load_receipt(request_no)  # type: ignore[return-value]

    def _apply(
        self,
        request_no: str,
        op_type: str,
        payload: Any,
        lines: list[dict[str, Any]],
    ) -> OperationReceipt:
        body = canonical_text(payload)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt = self._apply_in_tx(request_no, op_type, body, lines)
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- 公开写接口 -------------------------------------------------------

    def inbound(
        self, request_no: str, sku: str, location: str, qty: int
    ) -> OperationReceipt:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InvalidOperation("入库数量必须为正整数")
        payload = {"sku": sku, "location": location, "qty": qty, "op": "inbound"}
        lines = [
            {"sku": sku, "location": location, "direction": "in", "qty": qty}
        ]
        return self._apply(request_no, "inbound", payload, lines)

    def outbound(
        self, request_no: str, sku: str, location: str, qty: int
    ) -> OperationReceipt:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InvalidOperation("出库数量必须为正整数")
        payload = {"sku": sku, "location": location, "qty": qty, "op": "outbound"}
        lines = [
            {"sku": sku, "location": location, "direction": "out", "qty": qty}
        ]
        return self._apply(request_no, "outbound", payload, lines)

    def transfer(
        self,
        request_no: str,
        sku: str,
        from_location: str,
        to_location: str,
        qty: int,
    ) -> OperationReceipt:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InvalidOperation("移动数量必须为正整数")
        if from_location == to_location:
            raise InvalidOperation("移动的源库位与目标库位不能相同")
        payload = {
            "sku": sku,
            "from_location": from_location,
            "to_location": to_location,
            "qty": qty,
            "op": "transfer",
        }
        lines = [
            {"sku": sku, "location": from_location, "direction": "out", "qty": qty},
            {"sku": sku, "location": to_location, "direction": "in", "qty": qty},
        ]
        return self._apply(request_no, "transfer", payload, lines)

    def adjust(
        self,
        request_no: str,
        deltas: list[dict[str, Any]],
        reason: str = "count",
    ) -> OperationReceipt:
        """差异调整公开接口。deltas 元素: {sku, location, delta}。

        整批作为一次原子操作；任一会使库存变负则全部拒绝。
        工作台只能通过本接口发布差异，不能直接改 l_balances。
        """
        norm = sorted(
            (
                {"sku": d["sku"], "location": d["location"], "delta": int(d["delta"])}
                for d in deltas
            ),
            key=lambda d: (d["sku"], d["location"]),
        )
        payload = {"reason": reason, "deltas": norm}
        lines: list[dict[str, Any]] = []
        for d in norm:
            if d["delta"] == 0:
                lines.append(
                    {
                        "sku": d["sku"],
                        "location": d["location"],
                        "direction": "adjust",
                        "qty": 0,
                        "delta": 0,
                    }
                )
            else:
                lines.append(
                    {
                        "sku": d["sku"],
                        "location": d["location"],
                        "direction": "adjust",
                        "qty": abs(d["delta"]),
                        "delta": d["delta"],
                    }
                )
        return self._apply(request_no, "adjust", payload, lines)

    def publish_adjustment_in_tx(
        self, request_no: str, deltas: list[dict[str, Any]], reason: str
    ) -> OperationReceipt:
        """公开的事务内调整接口：供盘点工作台在自己的 IMMEDIATE 事务里
        原子发布差异，保证“调整落账 + 盘点单关闭”同生共死。"""
        norm = sorted(
            (
                {"sku": d["sku"], "location": d["location"], "delta": int(d["delta"])}
                for d in deltas
            ),
            key=lambda d: (d["sku"], d["location"]),
        )
        payload = {"reason": reason, "deltas": norm}
        lines = [
            {
                "sku": d["sku"],
                "location": d["location"],
                "direction": "adjust",
                "qty": abs(d["delta"]),
                "delta": d["delta"],
            }
            for d in norm
        ]
        return self._apply_in_tx(
            request_no, "adjust", canonical_text(payload), lines
        )

    # -- 快照 -------------------------------------------------------------

    def head_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(commit_seq), 0) AS s FROM l_requests").fetchone()
        return int(row["s"])

    def retain_snapshot(self, prefix: int, label: str) -> Snapshot:
        """在固定前缀 prefix 上物化一份带稳定标签的数量快照。

        同 prefix 或同 label 重复调用直接复读已物化的快照；
        label 已绑定到其它 prefix 则拒绝。
        """
        head = self.head_seq()
        if prefix < 0 or prefix > head:
            raise InvalidOperation(f"快照前缀 {prefix} 超出已提交范围 [0,{head}]")
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                snap = self._retain_snapshot_in_tx(prefix, label)
                self.conn.execute("COMMIT")
                return snap
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def retain_snapshot_in_tx(self, prefix: int, label: str) -> Snapshot:
        """公开的事务内快照物化：供盘点工作台在自己的 IMMEDIATE 事务里
        取得并冻结基准，保证“冻结前缀 + 盘点单 + 位置占用”原子可见。"""
        return self._retain_snapshot_in_tx(prefix, label)

    def _retain_snapshot_in_tx(self, prefix: int, label: str) -> Snapshot:
        """事务内版本：供工作台在自己的 IMMEDIATE 事务里复用。

        同一前缀可被多张盘点单共享（每个 label 一份独立物化）。
        """
        same_label = self.conn.execute(
            "SELECT prefix FROM l_snapshots WHERE label=?", (label,)
        ).fetchone()
        if same_label is not None:
            if int(same_label["prefix"]) != prefix:
                raise InvalidOperation(
                    f"标签 {label} 已绑定前缀 {same_label['prefix']}"
                )
            return self._get_snapshot_in_tx(label=label)
        self.conn.execute(
            "INSERT INTO l_snapshots(prefix, label, created_at) VALUES (?, ?, ?)",
            (prefix, label, now_iso()),
        )
        snapshot_id = self.conn.execute(
            "SELECT snapshot_id FROM l_snapshots WHERE label=?", (label,)
        ).fetchone()["snapshot_id"]
        balances = self._as_of_in_tx(prefix)
        self.conn.executemany(
            "INSERT INTO l_snapshot_lines(snapshot_id, sku, location, qty) "
            "VALUES (?, ?, ?, ?)",
            [(snapshot_id, sku, loc, qty) for (sku, loc), qty in balances.items()],
        )
        return self._get_snapshot_in_tx(label=label)

    def get_snapshot(self, prefix: Optional[int] = None, label: Optional[str] = None) -> Snapshot:
        if prefix is None and label is None:
            raise InvalidOperation("需要 prefix 或 label")
        with self._lock:
            return self._get_snapshot_in_tx(prefix=prefix, label=label)

    def _get_snapshot_in_tx(
        self, prefix: Optional[int] = None, label: Optional[str] = None
    ) -> Snapshot:
        if prefix is None and label is None:
            raise InvalidOperation("需要 prefix 或 label")
        if label is not None:
            row = self.conn.execute(
                "SELECT snapshot_id, prefix, label, created_at FROM l_snapshots WHERE label=?",
                (label,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT snapshot_id, prefix, label, created_at FROM l_snapshots WHERE prefix=? "
                "ORDER BY snapshot_id LIMIT 1",
                (prefix,),
            ).fetchone()
        if row is None:
            raise InvalidOperation("快照不存在")
        lines = self.conn.execute(
            "SELECT sku, location, qty FROM l_snapshot_lines "
            "WHERE snapshot_id=? ORDER BY sku, location",
            (row["snapshot_id"],),
        ).fetchall()
        return Snapshot(
            prefix=int(row["prefix"]),
            label=row["label"],
            created_at=row["created_at"],
            balances={(r["sku"], r["location"]): int(r["qty"]) for r in lines},
        )

    def list_snapshots(self) -> list[Snapshot]:
        rows = self.conn.execute(
            "SELECT label FROM l_snapshots ORDER BY snapshot_id"
        ).fetchall()
        return [self.get_snapshot(label=r["label"]) for r in rows]

    # -- 查询 -------------------------------------------------------------

    def _as_of_in_tx(self, k: int) -> dict[tuple[str, str], int]:
        """重放到前缀 k（含 commit_seq <= k 的全部事件）。"""
        rows = self.conn.execute(
            """
            SELECT ml.sku AS sku, ml.location AS location, SUM(ml.delta) AS d
            FROM l_movement_lines ml
            JOIN l_movements m ON m.movement_id = ml.movement_id
            WHERE m.commit_seq <= ?
            GROUP BY ml.sku, ml.location
            HAVING SUM(ml.delta) <> 0
            """,
            (k,),
        ).fetchall()
        return {(r["sku"], r["location"]): int(r["d"]) for r in rows}

    def as_of(self, k: int) -> dict[tuple[str, str], int]:
        """任意已提交前缀 k 时刻的全量数量（只列非零位置）。"""
        head = self.head_seq()
        if k < 0 or k > head:
            raise InvalidOperation(f"前缀 {k} 超出已提交范围 [0,{head}]")
        with self._lock:
            return self._as_of_in_tx(k)

    def balance(self, sku: str, location: str) -> int:
        row = self.conn.execute(
            "SELECT qty FROM l_balances WHERE sku=? AND location=?",
            (sku, location),
        ).fetchone()
        return int(row["qty"]) if row else 0

    def all_balances(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute(
            "SELECT sku, location, qty FROM l_balances WHERE qty > 0 ORDER BY sku, location"
        ).fetchall()
        return {(r["sku"], r["location"]): int(r["qty"]) for r in rows}

    def get_operation(self, commit_seq: int) -> Optional[OperationReceipt]:
        row = self.conn.execute(
            "SELECT request_no FROM l_requests WHERE commit_seq=?", (commit_seq,)
        ).fetchone()
        if row is None:
            return None
        return self._load_receipt(row["request_no"])

    def list_operations(
        self, since_seq: int = 0, limit: Optional[int] = None
    ) -> list[OperationReceipt]:
        sql = (
            "SELECT request_no FROM l_requests WHERE commit_seq > ? "
            "ORDER BY commit_seq"
        )
        params: list[Any] = [since_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._load_receipt(r["request_no"]) for r in rows]  # type: ignore[list-item]

    def events_touching(
        self, after_seq: int, positions: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        """公开查询：commit_seq > after_seq 且影响给定位置的逐行事件。

        供盘点解释“冻结后该位置发生过什么”，调用方不接触底层表名。
        """
        if not positions:
            return []
        match_clause = " OR ".join(["(ml.sku = ? AND ml.location = ?)"] * len(positions))
        params: list[Any] = [v for pair in positions for v in pair]
        rows = self.conn.execute(
            f"""
            SELECT m.commit_seq AS commit_seq, m.request_no AS request_no,
                   m.op_type AS op_type, ml.sku AS sku, ml.location AS location,
                   ml.direction AS direction, ml.qty AS qty, ml.delta AS delta
            FROM l_movement_lines ml
            JOIN l_movements m ON m.movement_id = ml.movement_id
            WHERE m.commit_seq > ? AND ({match_clause})
            ORDER BY m.commit_seq, ml.line_no
            """,
            [after_seq, *params],
        ).fetchall()
        return [dict(r) for r in rows]
