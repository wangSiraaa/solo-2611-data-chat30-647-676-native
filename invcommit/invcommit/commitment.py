"""客户交付承诺仓（独立模块二）。

核心纪律
========
1. 本模块不复制任何库存：可用量全部现场从调拨账本的 led_balance /
   led_transfer 计算，本模块只持久化“逐份来源占额” cmt_portion。
2. 承诺来源只有两类真实供给：
   - 真实现货的未占额（spot - 调拨预留 - 已占现货份额）；
   - 有明确预计签收日的特定在途调拨的剩余量（未结在途 - 已占在途份额），
     不把所有在途当成已到货。
3. 同一份量只能支持一个未结承诺；单个订单不能全部覆盖则一份不占。
4. 实际签收后，份额身份由“某调拨在途份额”转为“目标仓实物份额”，
   不再释放任何在途额度；预计日延后/报损只重判未发客户订单，
   已实际发出的客户量不可反向复活。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .constants import (
    MAX_ORDERS,
    PREFIX_COMMIT_EVENT,
    PREFIX_ORDER,
    PREFIX_PORTION,
)
from .errors import CommitmentError, InventoryError

SCHEMA = """
CREATE TABLE IF NOT EXISTS cmt_event (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,                  -- CEV 前缀
    kind     TEXT NOT NULL,
    ledger_anchor_seq INTEGER NOT NULL,             -- 写入时账本版本
    payload  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cmt_transit_days (
    warehouse_id TEXT PRIMARY KEY,                  -- 客户段固定运输天数
    transit_days INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cmt_order (
    order_id        TEXT PRIMARY KEY,               -- ORD 前缀
    sku             TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    accepted_whs    TEXT NOT NULL,                  -- JSON 数组
    latest_date     TEXT NOT NULL,                  -- 最迟交付日 ISO
    priority        INTEGER NOT NULL,
    status          TEXT NOT NULL                   -- OPEN/ALLOCATED/PARTIALLY_SHIPPED/SHIPPED
);
CREATE TABLE IF NOT EXISTS cmt_portion (
    portion_id   TEXT PRIMARY KEY,                  -- PTN 前缀
    order_id     TEXT NOT NULL,
    sku          TEXT NOT NULL,
    source_type  TEXT NOT NULL,                     -- SPOT / TRANSIT
    warehouse_id TEXT NOT NULL,                     -- SPOT: 发货仓；TRANSIT: 目标仓
    transfer_id  TEXT,                              -- TRANSIT 来源的特定调拨
    qty          INTEGER NOT NULL,                  -- 份额数量（拆分只减不增）
    expected_date TEXT NOT NULL,                    -- 预计可交付日
    state        TEXT NOT NULL,                     -- HELD/CONVERTED/SHIPPED/INVALID/SUPERSEDED
    root_portion_id TEXT,                           -- 更正重算的谱系根
    created_event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cmt_counter (
    name TEXT PRIMARY KEY,
    n    INTEGER NOT NULL DEFAULT 0
);
-- 汇总索引：占额查询的两条热路径
CREATE INDEX IF NOT EXISTS idx_cmt_portion_spot
    ON cmt_portion(sku, warehouse_id, state);
CREATE INDEX IF NOT EXISTS idx_cmt_portion_transfer
    ON cmt_portion(transfer_id, state);
CREATE INDEX IF NOT EXISTS idx_cmt_portion_order
    ON cmt_portion(order_id, state);
"""

# 份额状态
PS_HELD = "HELD"                  # 占额中（现货冻结或在途锁定）
PS_CONVERTED = "CONVERTED"        # 在途份额随实际签收转为目标仓实物
PS_SHIPPED = "SHIPPED"            # 已对外出库，客户量不可复活
PS_INVALID = "INVALID"            # 延迟/报损导致失效，订单重新开放
PS_SUPERSEDED = "SUPERSEDED"      # 更正重算时被新份额取代（保留可追溯）

# 订单状态
OS_OPEN = "OPEN"
OS_ALLOCATED = "ALLOCATED"
OS_PARTIAL = "PARTIALLY_SHIPPED"
OS_SHIPPED = "SHIPPED"


def _add_days(iso_day: str, days: int) -> str:
    return (date.fromisoformat(iso_day) + timedelta(days=days)).isoformat()


@dataclass(frozen=True)
class CommitEvent:
    event_id: str
    seq: int
    kind: str
    ledger_anchor_seq: int
    payload: dict


class CommitmentStore:
    """交付承诺仓。ledger 仅用于读取事实表与版本号，不通过它写库存。"""

    def __init__(self, conn: sqlite3.Connection, ledger):
        self.conn = conn
        self.ledger = ledger
        conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    def _next_event_id(self) -> str:
        n = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM cmt_event"
        ).fetchone()[0]
        return f"{PREFIX_COMMIT_EVENT}-{n:06d}"

    def _next_order_id(self) -> str:
        n = self.conn.execute(
            "SELECT COUNT(*) FROM cmt_order"
        ).fetchone()[0]
        if n >= MAX_ORDERS:
            raise CommitmentError(f"订单数量上限 {MAX_ORDERS} 已达")
        return f"{PREFIX_ORDER}-{n + 1:04d}"

    def _next_portion_id(self) -> str:
        # 份额独立计数器：一次分配产生多份份额时编号仍唯一；
        # cmt_portion.portion_id 主键提供最后一道唯一性兜底。
        self.conn.execute(
            "INSERT INTO cmt_counter(name, n) VALUES('portion', 1) "
            "ON CONFLICT(name) DO UPDATE SET n=n+1"
        )
        n = self.conn.execute(
            "SELECT n FROM cmt_counter WHERE name='portion'"
        ).fetchone()[0]
        return f"{PREFIX_PORTION}-{n:06d}"

    def _append(self, kind: str, payload: dict, anchor: int) -> CommitEvent:
        cur = self.conn.execute(
            """INSERT INTO cmt_event(event_id, kind, ledger_anchor_seq, payload)
                   VALUES (?,?,?,?)""",
            (self._next_event_id(), kind, anchor,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        seq = cur.lastrowid
        row = self.conn.execute(
            "SELECT event_id FROM cmt_event WHERE seq=?", (seq,)
        ).fetchone()
        return CommitEvent(row[0], seq, kind, anchor, payload)

    def _anchor(self, expected_ledger_version: int | None) -> int:
        """共享事务 + 版本重验：锚定当前账本版本，必要时与调用方预期比对。"""
        v = self.ledger.version()
        if expected_ledger_version is not None and v != expected_ledger_version:
            raise CommitmentError(
                f"账本版本过期：期望 {expected_ledger_version}，当前 {v}，"
                "请重读后重试"
            )
        return v

    def _get_order(self, order_id: str) -> dict:
        row = self.conn.execute(
            """SELECT order_id, sku, qty, accepted_whs, latest_date,
                      priority, status FROM cmt_order WHERE order_id=?""",
            (order_id,),
        ).fetchone()
        if row is None:
            raise CommitmentError(f"订单不存在：{order_id}")
        return {
            "order_id": row[0], "sku": row[1], "qty": row[2],
            "accepted_whs": json.loads(row[3]), "latest_date": row[4],
            "priority": row[5], "status": row[6],
        }

    def _transit_days(self, wh: str) -> int:
        row = self.conn.execute(
            "SELECT transit_days FROM cmt_transit_days WHERE warehouse_id=?",
            (wh,),
        ).fetchone()
        if row is None:
            raise CommitmentError(
                f"仓 {wh} 的固定运输天数未登记，无法计算可交付日"
            )
        return row[0]

    # ------------------------------------------------------------------ #
    # 登记：运输天数 / 订单
    # ------------------------------------------------------------------ #
    def register_transit_days(
        self, warehouse_id: str, transit_days: int,
        expected_ledger_version: int | None = None,
    ) -> CommitEvent:
        if transit_days < 0:
            raise CommitmentError("运输天数不能为负")
        anchor = self._anchor(expected_ledger_version)
        self.conn.execute(
            """INSERT INTO cmt_transit_days(warehouse_id, transit_days)
                   VALUES (?,?)
               ON CONFLICT(warehouse_id) DO UPDATE SET
                   transit_days=excluded.transit_days""",
            (warehouse_id, transit_days),
        )
        return self._append("TRANSIT_DAYS_SET", {
            "warehouse_id": warehouse_id, "transit_days": transit_days,
        }, anchor)

    def register_order(
        self,
        order_id: str | None,
        sku: str,
        qty: int,
        accepted_warehouses: set[str] | list[str],
        latest_delivery_date: str,
        priority: int,
        expected_ledger_version: int | None = None,
    ) -> CommitEvent:
        if qty <= 0:
            raise CommitmentError("订单数量必须为正")
        if not accepted_warehouses:
            raise CommitmentError("可接受发货仓集合不能为空")
        # 校验日期格式
        date.fromisoformat(latest_delivery_date)
        anchor = self._anchor(expected_ledger_version)
        if self.conn.execute("SELECT COUNT(*) FROM cmt_order").fetchone()[0] >= MAX_ORDERS:
            raise CommitmentError(f"订单数量上限 {MAX_ORDERS} 已达")
        oid = order_id or self._next_order_id()
        whs = sorted(set(accepted_warehouses))
        self.conn.execute(
            """INSERT INTO cmt_order(order_id, sku, qty, accepted_whs,
                    latest_date, priority, status)
                   VALUES (?,?,?,?,?,?,?)""",
            (oid, sku, qty, json.dumps(whs),
             latest_delivery_date, priority, OS_OPEN),
        )
        return self._append("ORDER_REGISTERED", {
            "order_id": oid, "sku": sku, "qty": qty,
            "accepted_whs": whs, "latest_date": latest_delivery_date,
            "priority": priority,
        }, anchor)

    # ------------------------------------------------------------------ #
    # 真实供给枚举（全部现场从账本计算，本模块不存库存）
    # ------------------------------------------------------------------ #
    def spot_occupancy(self, warehouse_id: str, sku: str) -> int:
        """该仓该 SKU 被未结承诺占用的真实现货量（HELD + CONVERTED）。

        调拨预留与交付承诺是同一份现货上的两类独立冻结，
        可用口径统一为：spot - 调拨预留 - 承诺占额。
        """
        return self.conn.execute(
            """SELECT COALESCE(SUM(qty), 0) FROM cmt_portion
                WHERE sku=? AND warehouse_id=? AND source_type='SPOT'
                  AND state IN (?, ?)""",
            (sku, warehouse_id, PS_HELD, PS_CONVERTED),
        ).fetchone()[0]

    def _spot_candidates(self, order: dict) -> list[dict]:
        """真实现货的未占额：账本现货 - 调拨预留 - 已占现货份额。"""
        out = []
        for wh in order["accepted_whs"]:
            row = self.conn.execute(
                "SELECT spot, reserved FROM led_balance WHERE warehouse_id=? AND sku=?",
                (wh, order["sku"]),
            ).fetchone()
            spot, reserved = row if row else (0, 0)
            occupied = self.conn.execute(
                """SELECT COALESCE(SUM(qty), 0) FROM cmt_portion
                    WHERE sku=? AND warehouse_id=? AND source_type='SPOT'
                      AND state IN (?, ?)""",
                (order["sku"], wh, PS_HELD, PS_CONVERTED),
            ).fetchone()[0]
            avail = spot - reserved - occupied
            if avail <= 0:
                continue
            expected = _add_days(
                self._today(), self._transit_days(wh)
            )
            out.append({
                "source_type": "SPOT", "warehouse_id": wh,
                "transfer_id": None, "available": avail,
                "expected_date": expected,
            })
        return out

    def _today(self) -> str:
        # 集中一个“当前日”，便于演示与测试；不接外部时钟服务
        row = self.conn.execute(
            "SELECT value FROM cmt_meta WHERE key='today'"
        ).fetchone() if self._has_meta() else None
        if row:
            return row[0]
        return date.today().isoformat()

    def _has_meta(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cmt_meta'"
        ).fetchone() is not None

    def _transit_candidates(self, order: dict) -> list[dict]:
        """特定在途调拨剩余量：必须有明确预计签收日且目标仓可接受。"""
        out = []
        rows = self.conn.execute(
            """SELECT transfer_id, target_wh, shipped_qty, received_qty,
                      lost_qty, expected_receipt_date
                 FROM led_transfer
                WHERE status=? AND sku=?
             ORDER BY transfer_id""",
            ("IN_TRANSIT", order["sku"]),
        ).fetchall()
        for trf, target, shipped, received, lost, eta in rows:
            if target not in order["accepted_whs"] or not eta:
                continue
            outstanding = shipped - received - lost
            occupied = self.conn.execute(
                """SELECT COALESCE(SUM(qty), 0) FROM cmt_portion
                    WHERE transfer_id=? AND state=?""",
                (trf, PS_HELD),
            ).fetchone()[0]
            avail = outstanding - occupied
            if avail <= 0:
                continue
            expected = _add_days(eta, self._transit_days(target))
            out.append({
                "source_type": "TRANSIT", "warehouse_id": target,
                "transfer_id": trf, "available": avail,
                "expected_date": expected,
            })
        return out

    def _candidates(self, order: dict) -> list[dict]:
        cands = self._spot_candidates(order) + self._transit_candidates(order)
        # 同单多来源定序：预计可交付日、仓 ID、调拨 ID（现货调拨 ID 为空排前）
        cands.sort(key=lambda c: (
            c["expected_date"], c["warehouse_id"],
            c["transfer_id"] or "",
        ))
        return cands

    # ------------------------------------------------------------------ #
    # 分配
    # ------------------------------------------------------------------ #
    def run_allocation(
        self, expected_ledger_version: int | None = None
    ) -> list[CommitEvent]:
        """按 优先级 DESC、最迟日 ASC、订单 ID ASC 逐单全有或全无分配。"""
        anchor = self._anchor(expected_ledger_version)
        orders = [
            {
                "order_id": r[0], "sku": r[1], "qty": r[2],
                "accepted_whs": json.loads(r[3]), "latest_date": r[4],
                "priority": r[5],
            }
            for r in self.conn.execute(
                """SELECT order_id, sku, qty, accepted_whs, latest_date,
                          priority FROM cmt_order
                    WHERE status=?
                 ORDER BY priority DESC, latest_date ASC, order_id ASC""",
                (OS_OPEN,),
            )
        ]
        results: list[CommitEvent] = []
        for order in orders:
            ev = self._allocate_one(order, anchor)
            if ev is not None:
                results.append(ev)
        return results

    def _derive_order_status(self, order_id: str, order_qty: int) -> str:
        """订单状态的唯一推导口径（生产端与核验器共用同一语义）。

        只看两类活份额：HELD/CONVERTED（未发）与 SHIPPED（已发）；
        INVALID/SUPERSEDED 是已终结历史，不参与推导。
        """
        shipped = self.conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM cmt_portion WHERE order_id=? AND state=?",
            (order_id, PS_SHIPPED),
        ).fetchone()[0]
        live = self.conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM cmt_portion "
            "WHERE order_id=? AND state IN (?, ?)",
            (order_id, PS_HELD, PS_CONVERTED),
        ).fetchone()[0]
        if shipped >= order_qty:
            return OS_SHIPPED
        if live == 0:
            return OS_OPEN
        if shipped + live >= order_qty:
            return OS_PARTIAL if shipped > 0 else OS_ALLOCATED
        return OS_OPEN

    def _allocate_one(self, order: dict, anchor: int) -> CommitEvent | None:
        # 只补“净缺口”：订单量 - 已发 - 仍存活的未发份额。
        # 报损/延迟重开时会保留订单内不受影响的现货等份额，绝不能重复占额。
        shipped = self.conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM cmt_portion WHERE order_id=? AND state=?",
            (order["order_id"], PS_SHIPPED),
        ).fetchone()[0]
        live = self.conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM cmt_portion "
            "WHERE order_id=? AND state IN (?, ?)",
            (order["order_id"], PS_HELD, PS_CONVERTED),
        ).fetchone()[0]
        need_total = order["qty"] - shipped - live
        if need_total <= 0:
            # 活份额已覆盖缺口：按统一口径校正订单状态后无需再占
            self.conn.execute(
                "UPDATE cmt_order SET status=? WHERE order_id=?",
                (self._derive_order_status(order["order_id"], order["qty"]),
                 order["order_id"]),
            )
            return None
        candidates = self._candidates(order)
        # 最迟交付日过滤：预计可交付日晚于最迟日的来源不可用
        candidates = [
            c for c in candidates if c["expected_date"] <= order["latest_date"]
        ]
        total_avail = sum(c["available"] for c in candidates)
        if total_avail < need_total:
            # 不能全部覆盖：一份不占，订单保持 OPEN
            return None
        need = need_total
        portions = []
        for c in candidates:
            if need == 0:
                break
            take = min(need, c["available"])
            portions.append((c, take))
            need -= take
        if need != 0:  # 理论上不可达，防御性回滚
            raise CommitmentError("分配中途供给不足，事务回滚")
        saved = []
        for c, take in portions:
            pid = self._next_portion_id()
            self.conn.execute(
                """INSERT INTO cmt_portion(portion_id, order_id, sku,
                        source_type, warehouse_id, transfer_id, qty,
                        expected_date, state, root_portion_id,
                        created_event_seq)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, order["order_id"], order["sku"], c["source_type"],
                 c["warehouse_id"], c["transfer_id"], take,
                 c["expected_date"], PS_HELD, pid, 0),
            )
            saved.append({
                "portion_id": pid, "source_type": c["source_type"],
                "warehouse_id": c["warehouse_id"], "transfer_id": c["transfer_id"],
                "qty": take, "expected_date": c["expected_date"],
            })
        self.conn.execute(
            "UPDATE cmt_order SET status=? WHERE order_id=?",
            (self._derive_order_status(order["order_id"], order["qty"]),
             order["order_id"]),
        )
        return self._append("ALLOCATION", {
            "order_id": order["order_id"], "sku": order["sku"],
            "qty": need_total, "shipped_qty": shipped,
            "live_qty_before": live, "portions": saved,
        }, anchor)

    # ------------------------------------------------------------------ #
    # 账本联动反应（由 Facade 在同一事务内调用）
    # ------------------------------------------------------------------ #
    def on_transfer_received(
        self, transfer_id: str, received_qty: int,
        anchor: int,
    ) -> CommitEvent:
        """实际签收：本调拨的 HELD 在途份额按 FIFO 转为目标仓实物份额。

        转换只改身份不释放额度——现货占用同时增加，别的订单不会因此
        多出一份可承诺量。
        """
        rows = self.conn.execute(
            """SELECT portion_id, order_id, qty, warehouse_id, sku,
                      expected_date
                 FROM cmt_portion
                WHERE transfer_id=? AND state=?
             ORDER BY portion_id""",
            (transfer_id, PS_HELD),
        ).fetchall()
        remaining = received_qty
        conversions = []
        for pid, oid, qty, wh, sku, expected in rows:
            if remaining <= 0:
                break
            take = min(qty, remaining)
            if take == qty:
                # 整份转换：在途份额身份变为目标仓实物，来源类型必须同步改 SPOT
                self.conn.execute(
                    "UPDATE cmt_portion SET state=?, source_type='SPOT' "
                    "WHERE portion_id=?",
                    (PS_CONVERTED, pid),
                )
                conversions.append({
                    "portion_id": pid, "order_id": oid,
                    "transfer_id": transfer_id, "qty": take,
                    "warehouse_id": wh, "new_state": PS_CONVERTED,
                })
            else:
                # 部分转换：旧份额缩减保留 HELD，另立 CONVERTED 实物份额；
                # 新份额自成谱系根，与旧份额的来源关系由事件 split_from 记录
                self.conn.execute(
                    "UPDATE cmt_portion SET qty=? WHERE portion_id=?",
                    (qty - take, pid),
                )
                new_pid = self._next_portion_id()
                self.conn.execute(
                    """INSERT INTO cmt_portion(portion_id, order_id, sku,
                            source_type, warehouse_id, transfer_id, qty,
                            expected_date, state, root_portion_id,
                            created_event_seq)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_pid, oid, sku, "SPOT", wh, transfer_id, take,
                     expected, PS_CONVERTED, new_pid, 0),
                )
                conversions.append({
                    "portion_id": new_pid, "order_id": oid,
                    "transfer_id": transfer_id, "qty": take,
                    "warehouse_id": wh, "new_state": PS_CONVERTED,
                    "split_from": pid,
                })
            remaining -= take
        return self._append("PORTIONS_CONVERTED", {
            "transfer_id": transfer_id, "received_qty": received_qty,
            "conversions": conversions,
        }, anchor)

    def on_transfer_lost(
        self, transfer_id: str, lost_qty: int, anchor: int,
    ) -> tuple[CommitEvent, list[str]]:
        """报损：受影响在途份额失效，整单回到 OPEN 重新判断。

        已 CONVERTED / SHIPPED 的客户量不回滚（不可反向复活）。
        """
        invalid_parts, reopened = self._invalidate_transfer_held(
            transfer_id, reason="LOSS", extra={"lost_qty": lost_qty}
        )
        ev = self._append("PORTIONS_INVALIDATED", {
            "transfer_id": transfer_id, "reason": "LOSS",
            "lost_qty": lost_qty, "invalid_portions": invalid_parts,
            "reopened_orders": reopened,
        }, anchor)
        return ev, reopened

    def on_eta_changed(
        self, transfer_id: str, new_eta: str, anchor: int,
    ) -> tuple[CommitEvent, list[str]]:
        """预计日延后：重判本调拨未发份额，迟于订单最迟日者失效。"""
        rows = self.conn.execute(
            """SELECT p.portion_id, p.order_id, p.qty, p.warehouse_id,
                      o.latest_date
                 FROM cmt_portion p JOIN cmt_order o USING(order_id)
                WHERE p.transfer_id=? AND p.state=?""",
            (transfer_id, PS_HELD),
        ).fetchall()
        invalid_parts = []
        affected_orders = set()
        for pid, oid, qty, wh, latest in rows:
            new_expected = _add_days(new_eta, self._transit_days(wh))
            if new_expected > latest:
                invalid_parts.append({
                    "portion_id": pid, "order_id": oid, "qty": qty,
                    "new_expected_date": new_expected,
                    "latest_date": latest,
                })
                affected_orders.add(oid)
        # 先把迟于最迟日的在途份额真正置为 INVALID，再重开订单
        for part in invalid_parts:
            self.conn.execute(
                "UPDATE cmt_portion SET state=? WHERE portion_id=?",
                (PS_INVALID, part["portion_id"]),
            )
        reopened = []
        for oid in sorted(affected_orders):
            self._reopen_order(oid, reason="ETA_DELAY", keep_other_portions=True)
            reopened.append(oid)
        ev = self._append("PORTIONS_INVALIDATED", {
            "transfer_id": transfer_id, "reason": "ETA_DELAY",
            "new_eta": new_eta, "invalid_portions": invalid_parts,
            "reopened_orders": reopened,
        }, anchor)
        return ev, reopened

    def _invalidate_transfer_held(
        self, transfer_id: str, reason: str, extra: dict,
    ) -> tuple[list[dict], list[str]]:
        """按报损量 FIFO 精确失效本调拨的 HELD 在途份额。

        部分报损只吃掉 lost_qty 对应的份额，剩余在途上的承诺保持 HELD；
        被拆分到的订单整单重开，但其现货等其他来源份额原样保留。
        """
        lost_qty = extra["lost_qty"]
        rows = self.conn.execute(
            """SELECT portion_id, order_id, qty FROM cmt_portion
                WHERE transfer_id=? AND state=?
             ORDER BY portion_id""",
            (transfer_id, PS_HELD),
        ).fetchall()
        invalid_parts = []
        affected_orders = set()
        remaining = lost_qty
        for pid, oid, qty in rows:
            if remaining <= 0:
                break
            take = min(qty, remaining)
            if take == qty:
                self.conn.execute(
                    "UPDATE cmt_portion SET state=? WHERE portion_id=?",
                    (PS_INVALID, pid),
                )
                invalid_parts.append(
                    {"portion_id": pid, "order_id": oid, "qty": take})
            else:
                self.conn.execute(
                    "UPDATE cmt_portion SET qty=? WHERE portion_id=?",
                    (qty - take, pid),
                )
                new_pid = self._next_portion_id()
                self.conn.execute(
                    """INSERT INTO cmt_portion(portion_id, order_id, sku,
                            source_type, warehouse_id, transfer_id, qty,
                            expected_date, state, root_portion_id,
                            created_event_seq)
                       SELECT ?, order_id, sku, source_type, warehouse_id,
                              transfer_id, ?, expected_date, ?, ?, 0
                         FROM cmt_portion WHERE portion_id=?""",
                    (new_pid, take, PS_INVALID, new_pid, pid),
                )
                invalid_parts.append(
                    {"portion_id": new_pid, "order_id": oid, "qty": take,
                     "split_from": pid})
            affected_orders.add(oid)
            remaining -= take
        reopened = []
        for oid in sorted(affected_orders):
            self._reopen_order(oid, reason=reason, keep_other_portions=True)
            reopened.append(oid)
        return invalid_parts, reopened

    def _reopen_order(
        self, order_id: str, reason: str, keep_other_portions: bool = False,
    ) -> None:
        """整单回 OPEN 等待重新判断。

        keep_other_portions=True（报损/延迟场景）：只失效触发事件的那批
        在途份额，订单内现货等其他份额保留——重新分配时它们仍可被复用，
        绝不能因为一笔调拨出事就把无关占额也抹掉。
        keep_other_portions=False（全量重算）：未发份额全部标 SUPERSEDED；
        已发 SHIPPED 份额在任何场景下都不动（客户量不可反向复活）。
        """
        if not keep_other_portions:
            rows = self.conn.execute(
                """SELECT portion_id FROM cmt_portion
                    WHERE order_id=? AND state IN (?, ?)""",
                (order_id, PS_HELD, PS_CONVERTED),
            ).fetchall()
            for (pid,) in rows:
                self.conn.execute(
                    "UPDATE cmt_portion SET state=? WHERE portion_id=?",
                    (PS_SUPERSEDED if reason == "REBUILD" else PS_INVALID, pid),
                )
        order = self._get_order(order_id)
        new_status = self._derive_order_status(order_id, order["qty"])
        self.conn.execute(
            "UPDATE cmt_order SET status=? WHERE order_id=?",
            (new_status, order_id),
        )

    # ------------------------------------------------------------------ #
    # 客户发货（只能从实际现货原子扣减）
    # ------------------------------------------------------------------ #
    def ship_customer_order(
        self,
        order_id: str,
        warehouse_id: str,
        qty: int,
        ledger,
        expected_ledger_version: int | None = None,
    ) -> tuple[object, CommitEvent]:
        """通过账本对外出库入口发货，并把对应实物份额标记 SHIPPED。

        未签收的 TRANSIT/HELD 份额只表示计划，不能直接发货。
        """
        anchor = self._anchor(expected_ledger_version)
        order = self._get_order(order_id)
        if order["status"] == OS_SHIPPED:
            raise CommitmentError(f"{order_id} 已全部发货")
        rows = self.conn.execute(
            """SELECT portion_id, qty, state, source_type FROM cmt_portion
                WHERE order_id=? AND warehouse_id=?
                  AND state IN (?, ?)
             ORDER BY portion_id""",
            (order_id, warehouse_id, PS_HELD, PS_CONVERTED),
        ).fetchall()
        # 只有实物份额（现货 HELD / 已转换 CONVERTED）可用于发货；
        # 未签收的在途份额只表示计划，不计入可发量
        usable = [(pid, q, st) for pid, q, st, stype in rows if stype == "SPOT"]
        held_transit = [
            (pid, q) for pid, q, st, stype in rows if stype == "TRANSIT"
        ]
        if sum(q for _, q, _ in usable) < qty:
            detail = (f"；另有未签收在途承诺 {sum(q for _, q in held_transit)} 件"
                      "只表示计划，不能直接发货") if held_transit else ""
            raise CommitmentError(
                f"{order_id} 在 {warehouse_id} 可发实物份额不足 {qty}{detail}"
            )
        # 先过账本入口做原子现货扣减（账本自身再验 spot-reserved）
        led_ev = ledger.outbound(
            warehouse_id, order["sku"], qty, ref=order_id
        )
        need = qty
        marked = []
        for pid, pqty, state in usable:
            if need <= 0:
                break
            take = min(pqty, need)
            if take == pqty:
                self.conn.execute(
                    "UPDATE cmt_portion SET state=? WHERE portion_id=?",
                    (PS_SHIPPED, pid),
                )
            else:
                self.conn.execute(
                    "UPDATE cmt_portion SET qty=? WHERE portion_id=?",
                    (pqty - take, pid),
                )
                new_pid = self._next_portion_id()
                self.conn.execute(
                    """INSERT INTO cmt_portion(portion_id, order_id, sku,
                            source_type, warehouse_id, transfer_id, qty,
                            expected_date, state, root_portion_id,
                            created_event_seq)
                       SELECT ?, order_id, sku, source_type, warehouse_id,
                              transfer_id, ?, expected_date, ?, ?, 0
                         FROM cmt_portion WHERE portion_id=?""",
                    (new_pid, take, PS_SHIPPED, new_pid, pid),
                )
                marked.append({"portion_id": new_pid, "qty": take,
                               "split_from": pid})
                need -= take
                continue
            marked.append({"portion_id": pid, "qty": take})
            need -= take
        new_status = self._derive_order_status(order_id, order["qty"])
        self.conn.execute(
            "UPDATE cmt_order SET status=? WHERE order_id=?",
            (new_status, order_id),
        )
        ev = self._append("CUSTOMER_SHIPPED", {
            "order_id": order_id, "warehouse_id": warehouse_id,
            "qty": qty, "ledger_event_id": led_ev.event_id,
            "portions": marked,
        }, anchor)
        return led_ev, ev

    # ------------------------------------------------------------------ #
    # 更正与全量重算（旧份额产品与实际出库仍可追溯）
    # ------------------------------------------------------------------ #
    def rebuild(self, expected_ledger_version: int | None = None) -> list[CommitEvent]:
        """全量重算：OPEN 订单重新分配；旧未发份额标 SUPERSEDED 不删除。"""
        anchor = self._anchor(expected_ledger_version)
        # 全量重算：除已全部发货外的订单都重新判断（OPEN/ALLOCATED/PARTIAL）
        open_before = [r[0] for r in self.conn.execute(
            "SELECT order_id FROM cmt_order WHERE status != ?",
            (OS_SHIPPED,),
        )]
        superseded = []
        for oid in open_before:
            rows = self.conn.execute(
                """SELECT portion_id FROM cmt_portion
                    WHERE order_id=? AND state IN (?, ?)""",
                (oid, PS_HELD, PS_CONVERTED),
            ).fetchall()
            for (pid,) in rows:
                self.conn.execute(
                    "UPDATE cmt_portion SET state=? WHERE portion_id=?",
                    (PS_SUPERSEDED, pid),
                )
                superseded.append(pid)
            # 已发部分保留；订单回到 OPEN 等待重新分配
            self.conn.execute(
                "UPDATE cmt_order SET status=? WHERE order_id=?",
                (OS_OPEN, oid),
            )
        ev0 = self._append("REBUILD_STARTED", {
            "superseded_portions": superseded,
            "reopened_orders": open_before,
        }, anchor)
        events = [ev0]
        events.extend(self.run_allocation(expected_ledger_version=anchor))
        return events

    # ------------------------------------------------------------------ #
    # 只读视图
    # ------------------------------------------------------------------ #
    def portions_view(self, order_id: str | None = None) -> list[dict]:
        sql = """SELECT portion_id, order_id, sku, source_type, warehouse_id,
                        transfer_id, qty, expected_date, state, root_portion_id
                   FROM cmt_portion"""
        args = ()
        if order_id:
            sql += " WHERE order_id=?"
            args = (order_id,)
        sql += " ORDER BY portion_id"
        return [dict(zip(
            ("portion_id", "order_id", "sku", "source_type", "warehouse_id",
             "transfer_id", "qty", "expected_date", "state", "root_portion_id"),
            r,
        )) for r in self.conn.execute(sql, args)]

    def orders_view(self) -> list[dict]:
        return [dict(zip(
            ("order_id", "sku", "qty", "accepted_whs", "latest_date",
             "priority", "status"),
            r,
        )) for r in self.conn.execute(
            """SELECT order_id, sku, qty, accepted_whs, latest_date,
                      priority, status FROM cmt_order ORDER BY order_id"""
        )]

    def events(self) -> list[CommitEvent]:
        return [
            CommitEvent(r[0], r[1], r[2], r[3], json.loads(r[4]))
            for r in self.conn.execute(
                """SELECT event_id, seq, kind, ledger_anchor_seq, payload
                     FROM cmt_event ORDER BY seq"""
            )
        ]

    def set_clock(self, today_iso: str) -> None:
        """设置演示/测试用当前日（现货可交付日 = today + 固定运输天数）。"""
        date.fromisoformat(today_iso)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cmt_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.execute(
            "INSERT INTO cmt_meta(key, value) VALUES('today', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (today_iso,),
        )
