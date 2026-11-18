"""调拨账本（独立模块一）。

职责
----
- SKU 的本地入库、对外出库；
- 仓间调拨：创建（源仓预留）→ 发出（预留转在途）→ 分次签收 / 运输损耗 / 未发取消；
- 独立提供各仓现货、预留、每笔在途明细；
- 每次变更追加一条不可变事件（LEV 前缀）并返回固定前缀产品。

守恒口径（逐 SKU，全网）
------------------------
    期初(0) + 累计入库
      = 各仓现货合计 + 各笔在途合计 + 累计损耗 + 累计对外出库

其中“源仓预留”只是源仓现货里被冻结的子集（spot 不减少、reserved 增加），
总量核算时不再单独加入，预留绝不重复计入总量。

本模块不依赖承诺仓；承诺仓只允许读本模块的表，不允许写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .constants import (
    MAX_SKUS,
    MAX_TRANSFERS,
    MAX_WAREHOUSES,
    PREFIX_LEDGER_EVENT,
    PREFIX_RECEIPT,
    PREFIX_TRANSFER,
)
from .errors import InventoryError

SCHEMA = """
CREATE TABLE IF NOT EXISTS led_event (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- 单调账本版本号
    event_id   TEXT NOT NULL UNIQUE,               -- LEV 前缀产品
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL                       -- JSON，仅追加不可变
);
CREATE TABLE IF NOT EXISTS led_balance (
    warehouse_id TEXT NOT NULL,
    sku          TEXT NOT NULL,
    spot         INTEGER NOT NULL DEFAULT 0,       -- 现货（含被预留冻结部分）
    reserved     INTEGER NOT NULL DEFAULT 0,       -- 已为调拨创建预留（spot 的子集）
    PRIMARY KEY (warehouse_id, sku)
);
CREATE TABLE IF NOT EXISTS led_transfer (
    transfer_id   TEXT PRIMARY KEY,                -- TRF 前缀
    sku           TEXT NOT NULL,
    source_wh     TEXT NOT NULL,
    target_wh     TEXT NOT NULL,
    qty           INTEGER NOT NULL,                -- 调拨总量（创建即固定）
    shipped_qty   INTEGER NOT NULL DEFAULT 0,      -- 实际发出量
    received_qty  INTEGER NOT NULL DEFAULT 0,      -- 目标仓已签收量
    lost_qty      INTEGER NOT NULL DEFAULT 0,      -- 运输损耗量
    status        TEXT NOT NULL,                   -- RESERVED/IN_TRANSIT/CLOSED/CANCELLED
    expected_receipt_date TEXT,                    -- 预计签收日（承诺仓据此排期）
    created_seq   INTEGER NOT NULL,
    shipped_seq   INTEGER,
    closed_seq    INTEGER,
    cancelled_seq INTEGER
);
CREATE TABLE IF NOT EXISTS led_receipt (
    receipt_id   TEXT PRIMARY KEY,                 -- RCP 前缀
    transfer_id  TEXT NOT NULL,
    content_hash TEXT NOT NULL,                    -- 幂等指纹
    event_seq    INTEGER NOT NULL
);
-- 逐 SKU 汇总索引：把全量重放才能得到的累计量物化成一行，查询 O(1)
CREATE TABLE IF NOT EXISTS led_sku_flow (
    sku          TEXT PRIMARY KEY,
    inbound_qty  INTEGER NOT NULL DEFAULT 0,
    outbound_qty INTEGER NOT NULL DEFAULT 0,
    lost_qty     INTEGER NOT NULL DEFAULT 0
);
"""

# 调拨状态机
ST_RESERVED = "RESERVED"        # 已创建、已预留，尚未发出
ST_IN_TRANSIT = "IN_TRANSIT"    # 已（全部）发出，在途
ST_CLOSED = "CLOSED"            # 发出量全部被签收/损耗结清
ST_CANCELLED = "CANCELLED"      # 未发取消


@dataclass(frozen=True)
class EventProduct:
    """一次变更对外交付的固定前缀产品。"""
    event_id: str
    seq: int
    kind: str
    payload: dict


class TransferLedger:
    """调拨账本。所有方法都在调用方事务内执行（见 connect() 说明）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _next_id(self, prefix: str, table: str, col: str, limit: int) -> str:
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()
        if row[0] >= limit:
            raise InventoryError(
                f"规模上限 {limit} 已达：{table} 不能再登记新条目"
            )
        n = row[0] + 1
        return f"{prefix}-{n:04d}"

    def _append(self, kind: str, payload: dict) -> EventProduct:
        cur = self.conn.execute(
            "INSERT INTO led_event (event_id, kind, payload) VALUES (?, ?, ?)",
            (
                self._next_event_id(),
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        seq = cur.lastrowid
        return EventProduct(
            event_id=self.conn.execute(
                "SELECT event_id FROM led_event WHERE seq=?", (seq,)
            ).fetchone()[0],
            seq=seq,
            kind=kind,
            payload=payload,
        )

    def _next_event_id(self) -> str:
        n = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM led_event"
        ).fetchone()[0]
        return f"{PREFIX_LEDGER_EVENT}-{n:06d}"

    def _bal(self, wh: str, sku: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT spot, reserved FROM led_balance WHERE warehouse_id=? AND sku=?",
            (wh, sku),
        ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def _ensure_capacity(self, wh: str, sku: str) -> None:
        """最多 4 仓 5 SKU：以余额表里出现过的组合计数。"""
        if self.conn.execute(
            "SELECT COUNT(*) FROM led_balance WHERE warehouse_id=?", (wh,)
        ).fetchone()[0] == 0:
            if self.conn.execute(
                "SELECT COUNT(DISTINCT warehouse_id) FROM led_balance"
            ).fetchone()[0] >= MAX_WAREHOUSES:
                raise InventoryError(f"仓库数量上限 {MAX_WAREHOUSES} 已达")
        if self.conn.execute(
            "SELECT COUNT(*) FROM led_balance WHERE sku=?", (sku,)
        ).fetchone()[0] == 0:
            if self.conn.execute(
                "SELECT COUNT(DISTINCT sku) FROM led_balance"
            ).fetchone()[0] >= MAX_SKUS:
                raise InventoryError(f"SKU 数量上限 {MAX_SKUS} 已达")

    def _adjust_spot(self, wh: str, sku: str, delta: int) -> None:
        self._ensure_capacity(wh, sku)
        spot, reserved = self._bal(wh, sku)
        if spot + delta < 0:
            raise InventoryError(
                f"{wh}/{sku} 现货不足：需要 {-delta}，可用 {spot}"
            )
        self.conn.execute(
            """INSERT INTO led_balance(warehouse_id, sku, spot, reserved)
                   VALUES (?,?,?,?)
               ON CONFLICT(warehouse_id, sku) DO UPDATE SET
                   spot = excluded.spot""",
            (wh, sku, spot + delta, reserved),
        )

    def _adjust_reserved(self, wh: str, sku: str, delta: int) -> None:
        spot, reserved = self._bal(wh, sku)
        if reserved + delta < 0:
            raise InventoryError(f"{wh}/{sku} 预留不能为负")
        if reserved + delta > spot:
            raise InventoryError(
                f"{wh}/{sku} 预留必须是现货子集：现货 {spot}，"
                f"预留将达 {reserved + delta}"
            )
        self.conn.execute(
            "UPDATE led_balance SET reserved=? WHERE warehouse_id=? AND sku=?",
            (reserved + delta, wh, sku),
        )

    def _flow(self, sku: str, *, inbound=0, outbound=0, lost=0) -> None:
        self.conn.execute(
            """INSERT INTO led_sku_flow(sku, inbound_qty, outbound_qty, lost_qty)
                   VALUES (?,?,?,?)
               ON CONFLICT(sku) DO UPDATE SET
                   inbound_qty = inbound_qty + ?,
                   outbound_qty = outbound_qty + ?,
                   lost_qty = lost_qty + ?""",
            (sku, inbound, outbound, lost, inbound, outbound, lost),
        )

    def _get_transfer(self, transfer_id: str) -> dict:
        row = self.conn.execute(
            """SELECT transfer_id, sku, source_wh, target_wh, qty,
                      shipped_qty, received_qty, lost_qty, status,
                      expected_receipt_date, created_seq, shipped_seq,
                      closed_seq, cancelled_seq
                 FROM led_transfer WHERE transfer_id=?""",
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"调拨不存在：{transfer_id}")
        cols = (
            "transfer_id", "sku", "source_wh", "target_wh", "qty",
            "shipped_qty", "received_qty", "lost_qty", "status",
            "expected_receipt_date", "created_seq", "shipped_seq",
            "closed_seq", "cancelled_seq",
        )
        return dict(zip(cols, row))

    @staticmethod
    def _hash_receipt(transfer_id: str, qty: int) -> str:
        # 同文指纹：transfer_id + 签收量。损耗凭据另用独立命名空间。
        h = hashlib.sha256()
        h.update(b"RECEIPT|")
        h.update(transfer_id.encode())
        h.update(b"|")
        h.update(str(qty).encode())
        return h.hexdigest()

    @staticmethod
    def _hash_loss(transfer_id: str, qty: int) -> str:
        h = hashlib.sha256()
        h.update(b"LOSS|")
        h.update(transfer_id.encode())
        h.update(b"|")
        h.update(str(qty).encode())
        return h.hexdigest()

    def _check_receipt_id(
        self, receipt_id: str, transfer_id: str, content_hash: str
    ) -> bool:
        """签收/损耗凭据幂等控制。返回 True 表示同文重投（调用方原样返回）。"""
        row = self.conn.execute(
            "SELECT transfer_id, content_hash FROM led_receipt WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is not None:
            if row[0] != transfer_id or row[1] != content_hash:
                # 同 ID 异文：明确拒绝，防止凭据复用篡改
                raise InventoryError(
                    f"凭据 {receipt_id} 已存在且内容不一致（异文拒绝）"
                )
            return True
        return False

    def _register_receipt(
        self, receipt_id: str, transfer_id: str, content_hash: str, seq: int
    ) -> None:
        self.conn.execute(
            """INSERT INTO led_receipt(receipt_id, transfer_id, content_hash, event_seq)
                   VALUES (?,?,?,?)""",
            (receipt_id, transfer_id, content_hash, seq),
        )

    # ------------------------------------------------------------------ #
    # 本地入库 / 对外出库
    # ------------------------------------------------------------------ #
    def inbound(self, warehouse_id: str, sku: str, qty: int) -> EventProduct:
        if qty <= 0:
            raise InventoryError("入库数量必须为正")
        self._adjust_spot(warehouse_id, sku, qty)
        self._flow(sku, inbound=qty)
        return self._append("INBOUND", {
            "warehouse_id": warehouse_id, "sku": sku, "qty": qty,
        })

    def outbound(
        self, warehouse_id: str, sku: str, qty: int, ref: str | None = None
    ) -> EventProduct:
        """对外出库：客户发货只能走这个入口，从实际现货原子扣减。

        预留是别的调拨的冻结量，因此可扣口径是 spot - reserved。
        """
        if qty <= 0:
            raise InventoryError("出库数量必须为正")
        spot, reserved = self._bal(warehouse_id, sku)
        if spot - reserved < qty:
            raise InventoryError(
                f"{warehouse_id}/{sku} 可出现货不足：需要 {qty}，"
                f"未冻结 {spot - reserved}（现货 {spot} / 预留 {reserved}）"
            )
        self._adjust_spot(warehouse_id, sku, -qty)
        self._flow(sku, outbound=qty)
        return self._append("OUTBOUND", {
            "warehouse_id": warehouse_id, "sku": sku, "qty": qty,
            "ref": ref,
        })

    # ------------------------------------------------------------------ #
    # 仓间调拨
    # ------------------------------------------------------------------ #
    def create_transfer(
        self,
        sku: str,
        source_wh: str,
        target_wh: str,
        qty: int,
        expected_receipt_date: str | None = None,
    ) -> EventProduct:
        """创建调拨：先预留源仓实物（现货不动，预留增加）。"""
        if qty <= 0:
            raise InventoryError("调拨数量必须为正")
        if source_wh == target_wh:
            raise InventoryError("源仓与目标仓不能相同")
        transfer_id = self._next_id(
            PREFIX_TRANSFER, "led_transfer", "transfer_id", MAX_TRANSFERS
        )
        spot, reserved = self._bal(source_wh, sku)
        if spot - reserved < qty:
            raise InventoryError(
                f"源仓 {source_wh}/{sku} 未冻结现货不足，无法预留 {qty}"
            )
        self._adjust_reserved(source_wh, sku, qty)
        ev = self._append("TRANSFER_CREATED", {
            "transfer_id": transfer_id, "sku": sku,
            "source_wh": source_wh, "target_wh": target_wh, "qty": qty,
            "expected_receipt_date": expected_receipt_date,
        })
        self.conn.execute(
            """INSERT INTO led_transfer(transfer_id, sku, source_wh, target_wh,
                    qty, status, expected_receipt_date, created_seq)
                   VALUES (?,?,?,?,?,?,?,?)""",
            (transfer_id, sku, source_wh, target_wh, qty, ST_RESERVED,
             expected_receipt_date, ev.seq),
        )
        return ev

    def ship_transfer(
        self, transfer_id: str, expected_receipt_date: str | None = None
    ) -> EventProduct:
        """发出：预留量转为该 transfer_id 的在途量。

        本实现按整笔发出（shipped_qty = qty）：题面中“实际发出量”即以此为准，
        后续签收 + 损耗不得超过它。
        """
        t = self._get_transfer(transfer_id)
        if t["status"] != ST_RESERVED:
            raise InventoryError(
                f"{transfer_id} 状态 {t['status']}，仅 RESERVED 可发出"
            )
        # 预留 -> 在途：源仓现货与预留同时减少（实物离仓）
        self._adjust_reserved(t["source_wh"], t["sku"], -t["qty"])
        self._adjust_spot(t["source_wh"], t["sku"], -t["qty"])
        eta = expected_receipt_date or t["expected_receipt_date"]
        ev = self._append("TRANSFER_SHIPPED", {
            "transfer_id": transfer_id, "sku": t["sku"],
            "source_wh": t["source_wh"], "target_wh": t["target_wh"],
            "shipped_qty": t["qty"], "expected_receipt_date": eta,
        })
        self.conn.execute(
            """UPDATE led_transfer
                  SET shipped_qty=qty, status=?, expected_receipt_date=?,
                      shipped_seq=?
                WHERE transfer_id=?""",
            (ST_IN_TRANSIT, eta, ev.seq, transfer_id),
        )
        return ev

    def receive_transfer(
        self, transfer_id: str, qty: int, receipt_id: str
    ) -> EventProduct:
        """分次签收：从本调拨未结在途扣减，增加目标仓现货。"""
        if qty <= 0:
            raise InventoryError("签收数量必须为正")
        t = self._get_transfer(transfer_id)
        if t["status"] not in (ST_IN_TRANSIT,):
            raise InventoryError(
                f"{transfer_id} 状态 {t['status']}，仅在途可调拨可签收"
            )
        digest = self._hash_receipt(transfer_id, qty)
        if self._check_receipt_id(receipt_id, transfer_id, digest):
            # 同文重投：幂等复读，返回既有事件，不再动库存
            seq = self.conn.execute(
                "SELECT event_seq FROM led_receipt WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()[0]
            row = self.conn.execute(
                "SELECT event_id, kind, payload FROM led_event WHERE seq=?",
                (seq,),
            ).fetchone()
            return EventProduct(row[0], seq, row[1], json.loads(row[2]))
        outstanding = t["shipped_qty"] - t["received_qty"] - t["lost_qty"]
        if qty > outstanding:
            raise InventoryError(
                f"{transfer_id} 签收 {qty} 超过本调拨未结在途 {outstanding}；"
                "不得消费别的调拨同 SKU 在途"
            )
        self._adjust_spot(t["target_wh"], t["sku"], qty)
        ev = self._append("TRANSFER_RECEIVED", {
            "transfer_id": transfer_id, "receipt_id": receipt_id,
            "sku": t["sku"], "target_wh": t["target_wh"], "qty": qty,
        })
        self._register_receipt(receipt_id, transfer_id, digest, ev.seq)
        new_received = t["received_qty"] + qty
        closed = (new_received + t["lost_qty"]) == t["shipped_qty"]
        self.conn.execute(
            """UPDATE led_transfer
                  SET received_qty=?,
                      status=CASE WHEN ? THEN ? ELSE status END,
                      closed_seq=CASE WHEN ? THEN ? ELSE closed_seq END
                WHERE transfer_id=?""",
            (new_received, closed, ST_CLOSED, closed, ev.seq, transfer_id),
        )
        return ev

    def report_loss(
        self, transfer_id: str, qty: int, receipt_id: str
    ) -> EventProduct:
        """运输损耗：扣减在途但不增目标库存，计入逐 SKU 累计损耗。"""
        if qty <= 0:
            raise InventoryError("损耗数量必须为正")
        t = self._get_transfer(transfer_id)
        if t["status"] != ST_IN_TRANSIT:
            raise InventoryError(
                f"{transfer_id} 状态 {t['status']}，仅在途可调拨可报损"
            )
        digest = self._hash_loss(transfer_id, qty)
        if self._check_receipt_id(receipt_id, transfer_id, digest):
            seq = self.conn.execute(
                "SELECT event_seq FROM led_receipt WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()[0]
            row = self.conn.execute(
                "SELECT event_id, kind, payload FROM led_event WHERE seq=?",
                (seq,),
            ).fetchone()
            return EventProduct(row[0], seq, row[1], json.loads(row[2]))
        outstanding = t["shipped_qty"] - t["received_qty"] - t["lost_qty"]
        if qty > outstanding:
            raise InventoryError(
                f"{transfer_id} 报损 {qty} 超过本调拨未结在途 {outstanding}"
            )
        self._flow(t["sku"], lost=qty)
        ev = self._append("TRANSFER_LOST", {
            "transfer_id": transfer_id, "receipt_id": receipt_id,
            "sku": t["sku"], "qty": qty,
        })
        self._register_receipt(receipt_id, transfer_id, digest, ev.seq)
        new_lost = t["lost_qty"] + qty
        closed = (t["received_qty"] + new_lost) == t["shipped_qty"]
        self.conn.execute(
            """UPDATE led_transfer
                  SET lost_qty=?,
                      status=CASE WHEN ? THEN ? ELSE status END,
                      closed_seq=CASE WHEN ? THEN ? ELSE closed_seq END
                WHERE transfer_id=?""",
            (new_lost, closed, ST_CLOSED, closed, ev.seq, transfer_id),
        )
        return ev

    def cancel_transfer(self, transfer_id: str) -> EventProduct:
        """未发取消：释放预留。发出后不得直接取消。"""
        t = self._get_transfer(transfer_id)
        if t["status"] != ST_RESERVED:
            raise InventoryError(
                f"{transfer_id} 已发出（{t['status']}），不得直接取消；"
                "剩余只能继续签收或报损"
            )
        self._adjust_reserved(t["source_wh"], t["sku"], -t["qty"])
        ev = self._append("TRANSFER_CANCELLED", {
            "transfer_id": transfer_id, "sku": t["sku"],
            "source_wh": t["source_wh"], "qty": t["qty"],
        })
        self.conn.execute(
            "UPDATE led_transfer SET status=?, cancelled_seq=? WHERE transfer_id=?",
            (ST_CANCELLED, ev.seq, transfer_id),
        )
        return ev

    def update_eta(self, transfer_id: str, new_expected_date: str) -> EventProduct:
        """预计签收日延后（不接物流系统，日期由调用方显式给出）。"""
        t = self._get_transfer(transfer_id)
        if t["status"] != ST_IN_TRANSIT:
            raise InventoryError(
                f"{transfer_id} 状态 {t['status']}，仅在途调拨可改预计日"
            )
        if new_expected_date == t["expected_receipt_date"]:
            raise InventoryError("新预计日与当前一致，无需变更")
        ev = self._append("TRANSFER_ETA_CHANGED", {
            "transfer_id": transfer_id, "sku": t["sku"],
            "old_expected_receipt_date": t["expected_receipt_date"],
            "new_expected_receipt_date": new_expected_date,
        })
        self.conn.execute(
            "UPDATE led_transfer SET expected_receipt_date=? WHERE transfer_id=?",
            (new_expected_date, transfer_id),
        )
        return ev

    # ------------------------------------------------------------------ #
    # 只读视图（外部库存客户无需承诺仓即可查询）
    # ------------------------------------------------------------------ #
    def stock_view(self) -> list[dict]:
        """各仓现货 / 预留 / 未冻结可用现货。"""
        return [
            {
                "warehouse_id": r[0], "sku": r[1],
                "spot": r[2], "reserved": r[3],
                "unreserved_spot": r[2] - r[3],
            }
            for r in self.conn.execute(
                """SELECT warehouse_id, sku, spot, reserved
                     FROM led_balance ORDER BY warehouse_id, sku"""
            )
        ]

    def in_transit_view(self) -> list[dict]:
        """每笔在途明细：未结在途 = 发出 - 签收 - 损耗，按 transfer_id 隔离。"""
        return [
            {
                "transfer_id": r[0], "sku": r[1],
                "source_wh": r[2], "target_wh": r[3],
                "shipped_qty": r[4], "received_qty": r[5],
                "lost_qty": r[6],
                "outstanding_in_transit": r[4] - r[5] - r[6],
                "status": r[7], "expected_receipt_date": r[8],
            }
            for r in self.conn.execute(
                """SELECT transfer_id, sku, source_wh, target_wh,
                          shipped_qty, received_qty, lost_qty, status,
                          expected_receipt_date
                     FROM led_transfer
                    WHERE status IN (?, ?)
                 ORDER BY transfer_id""",
                (ST_IN_TRANSIT, ST_CLOSED),
            )
        ]

    def sku_flow(self) -> list[dict]:
        """逐 SKU 汇总索引（累计入库/出库/损耗）。"""
        return [
            {"sku": r[0], "inbound_qty": r[1],
             "outbound_qty": r[2], "lost_qty": r[3]}
            for r in self.conn.execute(
                """SELECT sku, inbound_qty, outbound_qty, lost_qty
                     FROM led_sku_flow ORDER BY sku"""
            )
        ]

    def events(self) -> list[EventProduct]:
        return [
            EventProduct(r[0], r[1], r[2], json.loads(r[3]))
            for r in self.conn.execute(
                "SELECT event_id, seq, kind, payload FROM led_event ORDER BY seq"
            )
        ]

    def version(self) -> int:
        """当前账本版本（事件序号上确界），供承诺仓写入前重验。"""
        return self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM led_event"
        ).fetchone()[0]
