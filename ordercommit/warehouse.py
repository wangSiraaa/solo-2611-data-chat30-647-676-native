"""门店到货分配仓。

库存事实只来自两类原始记录，且发放记录只追加、从不改写或删除：

* ``w_receipts / w_receipt_lines`` —— 真实到货，必须绑定已登记的真实承诺，
  同一 receipt_id 全仓只处理一次；同 SKU 累计到货不得超过承诺数量；
* ``w_allocations`` —— 逐笔发放（哪张到货的哪个 SKU 发了多少给哪个需求）。

中央库存不是独立账本，而是从上述原始记录独立重算：
``中央库存 = 累计到货 - 累计发放``。

分配规则（单事务内完成）：优先级降序 → 最迟收货日升序 → 需求 ID；
同一需求可部分到货；到货内部按 (收货时刻, receipt_id) FIFO 出实物，
保证同一箱实物不可能同时承诺给两个门店。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from typing import Any

from .errors import (
    NotFound,
    ReceiptAlreadyProcessed,
    RuleViolation,
    StateConflict,
)
from .schema import WAREHOUSE_SCHEMA


def _iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


class Warehouse:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(WAREHOUSE_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ============================================================ 承诺登记（外部事实）
    def register_commitment(
        self,
        commitment_no: str,
        supplier_id: str,
        version_id: str,
        lines: list[dict[str, Any]],
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """登记一条供应商侧已接受的真实承诺（分配仓只消费，不从报价推导）。"""
        clean = sorted(
            (str(ln["sku"]), int(ln["qty"]), int(ln["unit_price"])) for ln in lines)
        if not clean or any(q < 0 for _, q, _ in clean):
            raise RuleViolation("承诺至少含一行且数量非负")
        ts = _iso(now) if now else datetime(2000, 1, 1).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._conn.execute(
                    "SELECT * FROM w_commitments WHERE commitment_no=?",
                    (commitment_no,)).fetchone()
                if old is not None:
                    old_lines = [
                        (r["sku"], r["qty"], r["unit_price"])
                        for r in self._conn.execute(
                            "SELECT sku, qty, unit_price FROM w_commitment_lines"
                            " WHERE commitment_no=? ORDER BY sku", (commitment_no,))]
                    if (old["supplier_id"], old["version_id"], old_lines) != (
                            supplier_id, version_id, clean):
                        raise StateConflict(
                            f"承诺 {commitment_no} 已存在且内容不同，拒绝异文覆盖")
                    self._conn.execute("ROLLBACK")
                    return self.get_commitment(commitment_no)
                self._conn.execute(
                    "INSERT INTO w_commitments(commitment_no, supplier_id, version_id,"
                    " created_at) VALUES (?,?,?,?)",
                    (commitment_no, supplier_id, version_id, ts))
                for sku, qty, price in clean:
                    self._conn.execute(
                        "INSERT INTO w_commitment_lines(commitment_no, sku, qty,"
                        " unit_price) VALUES (?,?,?,?)",
                        (commitment_no, sku, qty, price))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self.get_commitment(commitment_no)

    def get_commitment(self, commitment_no: str) -> dict[str, Any]:
        with self._lock:
            head = self._conn.execute(
                "SELECT * FROM w_commitments WHERE commitment_no=?",
                (commitment_no,)).fetchone()
            if head is None:
                raise NotFound(f"承诺未在分配仓登记: {commitment_no}")
            lines = [dict(r) for r in self._conn.execute(
                "SELECT sku, qty, unit_price, received_qty FROM w_commitment_lines"
                " WHERE commitment_no=? ORDER BY sku", (commitment_no,))]
        return {"commitment_no": head["commitment_no"],
                "supplier_id": head["supplier_id"],
                "version_id": head["version_id"],
                "lines": lines,
                "created_at": head["created_at"]}

    # ============================================================ 需求
    def add_demand(self, demand_id: str, sku: str, qty: int, priority: int,
                   due_date: str, *, now: str | datetime | None = None) -> dict[str, Any]:
        """登记需求行并立即用现有中央库存尝试发放。"""
        if int(qty) <= 0:
            raise RuleViolation("需求数量必须为正")
        ts = _iso(now) if now else datetime.utcnow().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute(
                        "SELECT 1 FROM w_demands WHERE demand_id=?",
                        (demand_id,)).fetchone():
                    raise StateConflict(f"需求 ID 已存在: {demand_id}")
                self._conn.execute(
                    "INSERT INTO w_demands(demand_id, sku, qty, priority, due_date,"
                    " status, created_at, updated_at) VALUES (?,?,?,?,?,'OPEN',?,?)",
                    (demand_id, sku, int(qty), int(priority), str(due_date), ts, ts))
                self._conn.execute(
                    "INSERT INTO w_demand_ledgers(demand_id, allocated_qty) VALUES (?,0)",
                    (demand_id,))
                made = self._allocate_pass({sku}, ts)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"demand": self.get_demand(demand_id), "allocations": made}

    def correct_demand(self, demand_id: str, new_qty: int, *, note: str = "",
                       now: str | datetime | None = None) -> dict[str, Any]:
        """更正尚未完成的需求数量。

        已发出的数量原样保留；新数量不得低于已发量。更正后对该 SKU 的
        **未发库存**重排（实物发放记录不动）。
        """
        if int(new_qty) <= 0:
            raise RuleViolation("更正后数量必须为正（取消请用 cancel_demand）")
        ts = _iso(now) if now else datetime.utcnow().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT d.*, l.allocated_qty FROM w_demands d"
                    " JOIN w_demand_ledgers l ON l.demand_id=d.demand_id"
                    " WHERE d.demand_id=?", (demand_id,)).fetchone()
                if row is None:
                    raise NotFound(f"需求不存在: {demand_id}")
                if row["status"] == "CANCELLED":
                    raise StateConflict(f"需求 {demand_id} 已取消，不可更正")
                if new_qty < row["allocated_qty"]:
                    raise StateConflict(
                        f"更正数量 {new_qty} 低于已发放 {row['allocated_qty']}，"
                        "已发实物不可撤回")
                old_qty = row["qty"]
                self._conn.execute(
                    "UPDATE w_demands SET qty=?, updated_at=? WHERE demand_id=?",
                    (new_qty, ts, demand_id))
                self._conn.execute(
                    "INSERT INTO w_corrections(demand_id, action, old_qty, new_qty,"
                    " note, created_at) VALUES (?, 'CORRECT', ?, ?, ?, ?)",
                    (demand_id, old_qty, new_qty, note, ts))
                made = self._allocate_pass({row["sku"]}, ts)
                self._refresh_status(demand_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"demand": self.get_demand(demand_id), "allocations": made}

    def cancel_demand(self, demand_id: str, *, note: str = "",
                      now: str | datetime | None = None) -> dict[str, Any]:
        """取消需求：已发数量保留（不凭空退回供应商），未发实物留中央库存服务其他需求。"""
        ts = _iso(now) if now else datetime.utcnow().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM w_demands WHERE demand_id=?",
                    (demand_id,)).fetchone()
                if row is None:
                    raise NotFound(f"需求不存在: {demand_id}")
                if row["status"] == "CANCELLED":
                    raise StateConflict(f"需求 {demand_id} 已是取消状态")
                self._conn.execute(
                    "UPDATE w_demands SET status='CANCELLED', updated_at=?"
                    " WHERE demand_id=?", (ts, demand_id))
                self._conn.execute(
                    "INSERT INTO w_corrections(demand_id, action, old_qty, new_qty,"
                    " note, created_at) VALUES (?, 'CANCEL', ?, NULL, ?, ?)",
                    (demand_id, row["qty"], note, ts))
                # 取消释放出的未发库存重排给其他未满足需求；实物一笔都不动
                made = self._allocate_pass({row["sku"]}, ts)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"demand": self.get_demand(demand_id), "allocations": made}

    def get_demand(self, demand_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT d.*, l.allocated_qty FROM w_demands d"
                " JOIN w_demand_ledgers l ON l.demand_id=d.demand_id"
                " WHERE d.demand_id=?", (demand_id,)).fetchone()
        if row is None:
            raise NotFound(f"需求不存在: {demand_id}")
        out = dict(row)
        out["remaining"] = row["qty"] - row["allocated_qty"]
        return out

    def list_demands(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [r["demand_id"] for r in self._conn.execute(
                "SELECT demand_id FROM w_demands ORDER BY demand_id")]
        return [self.get_demand(i) for i in ids]

    # ============================================================ 到货
    def receive(self, receipt_id: str, commitment_no: str,
                lines: list[dict[str, Any]],
                *, now: str | datetime | None = None) -> dict[str, Any]:
        """登记一张到货并在同一事务内完成优先级分配。

        约束：receipt_id 全仓唯一（重送即拒）；按 SKU 累计到货 ≤ 承诺数量；
        承诺必须已在本仓登记。
        """
        clean = sorted((str(ln["sku"]), int(ln["qty"])) for ln in lines)
        if not clean or any(q <= 0 for _, q in clean):
            raise RuleViolation("到货至少含一行且数量为正")
        ts = _iso(now) if now else datetime.utcnow().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute(
                        "SELECT 1 FROM w_receipts WHERE receipt_id=?",
                        (receipt_id,)).fetchone():
                    raise ReceiptAlreadyProcessed(
                        f"到货身份 {receipt_id} 已处理，一个到货身份只能分配一次",
                        details={"receipt_id": receipt_id})
                commit = self._conn.execute(
                    "SELECT * FROM w_commitments WHERE commitment_no=?",
                    (commitment_no,)).fetchone()
                if commit is None:
                    raise NotFound(f"承诺未登记，不能收货: {commitment_no}")
                self._conn.execute(
                    "INSERT INTO w_receipts(receipt_id, commitment_no, supplier_id,"
                    " received_at, allocated) VALUES (?,?,?,?,0)",
                    (receipt_id, commitment_no, commit["supplier_id"], ts))
                for sku, qty in clean:
                    cl = self._conn.execute(
                        "SELECT qty, COALESCE(received_qty,0) AS received"
                        " FROM w_commitment_lines WHERE commitment_no=? AND sku=?",
                        (commitment_no, sku)).fetchone()
                    if cl is None:
                        raise RuleViolation(
                            f"承诺 {commitment_no} 不含 SKU {sku}，拒收")
                    if cl["received"] + qty > cl["qty"]:
                        raise RuleViolation(
                            f"SKU {sku} 累计到货 {cl['received'] + qty} 超过承诺数量 "
                            f"{cl['qty']}",
                            details={"sku": sku, "commitment_qty": cl["qty"],
                                     "already_received": cl["received"],
                                     "this_receipt": qty})
                    self._conn.execute(
                        "INSERT INTO w_receipt_lines(receipt_id, sku, qty) VALUES (?,?,?)",
                        (receipt_id, sku, qty))
                    self._conn.execute(
                        "UPDATE w_commitment_lines SET received_qty=received_qty+?"
                        " WHERE commitment_no=? AND sku=?",
                        (qty, commitment_no, sku))
                made = self._allocate_pass({s for s, _ in clean}, ts)
                self._conn.execute(
                    "UPDATE w_receipts SET allocated=1 WHERE receipt_id=?",
                    (receipt_id,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"receipt_id": receipt_id,
                "commitment_no": commitment_no,
                "received_at": ts,
                "lines": [{"sku": s, "qty": q} for s, q in clean],
                "allocations": made,
                "leftover": self.receipt_status(receipt_id)["leftover"]}

    def receipt_status(self, receipt_id: str) -> dict[str, Any]:
        with self._lock:
            head = self._conn.execute(
                "SELECT * FROM w_receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()
            if head is None:
                raise NotFound(f"到货不存在: {receipt_id}")
            lines = [dict(r) for r in self._conn.execute(
                "SELECT sku, qty FROM w_receipt_lines WHERE receipt_id=? ORDER BY sku",
                (receipt_id,))]
            alloc_rows = self._conn.execute(
                "SELECT sku, demand_id, qty FROM w_allocations"
                " WHERE receipt_id=? ORDER BY allocation_id", (receipt_id,)).fetchall()
        allocated: dict[str, int] = {}
        for r in alloc_rows:
            allocated[r["sku"]] = allocated.get(r["sku"], 0) + r["qty"]
        for ln in lines:
            ln["allocated_qty"] = allocated.get(ln["sku"], 0)
            ln["leftover_qty"] = ln["qty"] - ln["allocated_qty"]
        return {"receipt_id": receipt_id,
                "commitment_no": head["commitment_no"],
                "supplier_id": head["supplier_id"],
                "received_at": head["received_at"],
                "processed": bool(head["allocated"]),
                "lines": lines,
                "allocations": [dict(r) for r in alloc_rows],
                "leftover": {ln["sku"]: ln["leftover_qty"] for ln in lines}}

    # ============================================================ 分配内核（单事务）
    def _allocate_pass(self, skus: set[str], ts: str) -> list[dict[str, Any]]:
        """对给定 SKU 集合，用当前未发实物按优先级规则补满未满足需求。

        只能在已持有 BEGIN IMMEDIATE 的事务内调用；只追加 w_allocations。
        """
        made: list[dict[str, Any]] = []
        for sku in sorted(skus):
            # 未满足需求：优先级降序、最迟收货日升序、需求 ID
            demands = self._conn.execute(
                "SELECT d.demand_id, d.qty AS want, l.allocated_qty AS got"
                " FROM w_demands d JOIN w_demand_ledgers l ON l.demand_id=d.demand_id"
                " WHERE d.sku=? AND d.status != 'CANCELLED'"
                " AND l.allocated_qty < d.qty"
                " ORDER BY d.priority DESC, d.due_date ASC, d.demand_id ASC",
                (sku,)).fetchall()
            if not demands:
                continue
            # 实物 FIFO：按收货时刻、receipt_id；只取该 SKU 尚有余额的到货
            receipts = self._conn.execute(
                "SELECT receipt_id, arrived, given, arrived - given AS leftover FROM ("
                " SELECT r.receipt_id, r.received_at, rl.qty AS arrived,"
                " COALESCE((SELECT SUM(qty) FROM w_allocations a"
                "            WHERE a.receipt_id=r.receipt_id AND a.sku=rl.sku),0)"
                " AS given"
                " FROM w_receipts r JOIN w_receipt_lines rl ON rl.receipt_id=r.receipt_id"
                " WHERE rl.sku=?) WHERE arrived - given > 0"
                " ORDER BY received_at ASC, receipt_id ASC",
                (sku,)).fetchall()
            pool = [(r["receipt_id"], r["arrived"] - r["given"]) for r in receipts]
            cursor = 0
            for d in demands:
                remaining = d["want"] - d["got"]
                while remaining > 0 and cursor < len(pool):
                    receipt_id, avail = pool[cursor]
                    if avail <= 0:
                        cursor += 1
                        continue
                    take = min(remaining, avail)
                    # 版本条件更新：台账 + 本次 ≤ 需求总量，防并发超发
                    cur = self._conn.execute(
                        "UPDATE w_demand_ledgers SET allocated_qty=allocated_qty+?"
                        " WHERE demand_id=? AND allocated_qty+? <="
                        " (SELECT qty FROM w_demands WHERE demand_id=?)",
                        (take, d["demand_id"], take, d["demand_id"]))
                    if cur.rowcount != 1:
                        raise StateConflict(
                            f"需求 {d['demand_id']} 条件更新失败，疑似并发超发，全部回滚")
                    self._conn.execute(
                        "INSERT INTO w_allocations(receipt_id, sku, demand_id, qty,"
                        " created_at) VALUES (?,?,?,?,?)",
                        (receipt_id, sku, d["demand_id"], take, ts))
                    made.append({"receipt_id": receipt_id, "sku": sku,
                                 "demand_id": d["demand_id"], "qty": take})
                    pool[cursor] = (receipt_id, avail - take)
                    remaining -= take
                    if pool[cursor][1] == 0:
                        cursor += 1
            for d in demands:
                self._refresh_status(d["demand_id"])
        return made

    def _refresh_status(self, demand_id: str) -> None:
        self._conn.execute(
            "UPDATE w_demands SET status = ("
            " SELECT CASE WHEN w_demands.status='CANCELLED' THEN 'CANCELLED'"
            " WHEN l.allocated_qty >= w_demands.qty THEN 'DONE'"
            " WHEN l.allocated_qty > 0 THEN 'PARTIAL' ELSE 'OPEN' END"
            " FROM w_demand_ledgers l WHERE l.demand_id=w_demands.demand_id)"
            " WHERE w_demands.demand_id=?",
            (demand_id,))

    # ============================================================ 库存与解释
    def stock_levels(self) -> dict[str, dict[str, int]]:
        """从原始到货/发放记录独立重算每个 SKU 的库存守恒视图。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rl.sku, SUM(rl.qty) AS received,"
                " COALESCE((SELECT SUM(qty) FROM w_allocations a"
                "           WHERE a.sku=rl.sku),0) AS allocated"
                " FROM w_receipt_lines rl GROUP BY rl.sku").fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out[r["sku"]] = {"received": r["received"],
                             "allocated": r["allocated"],
                             "central": r["received"] - r["allocated"]}
        return out

    def outstanding_demands(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.demand_id, d.sku, d.qty, d.priority, d.due_date, d.status,"
                " l.allocated_qty, (d.qty-l.allocated_qty) AS remaining"
                " FROM w_demands d JOIN w_demand_ledgers l ON l.demand_id=d.demand_id"
                " WHERE d.status != 'CANCELLED' AND l.allocated_qty < d.qty"
                " ORDER BY d.sku, d.priority DESC, d.due_date, d.demand_id").fetchall()
        return [dict(r) for r in rows]

    def allocation_history(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT allocation_id, receipt_id, sku, demand_id, qty, created_at"
                " FROM w_allocations ORDER BY allocation_id").fetchall()
        return [dict(r) for r in rows]

    def explain_receipt(self, receipt_id: str) -> dict[str, Any]:
        """解释一张到货：承诺来源、逐 SKU 发给了谁、多少进中央库存。"""
        status = self.receipt_status(receipt_id)
        return {
            "receipt_id": receipt_id,
            "commitment_no": status["commitment_no"],
            "supplier_id": status["supplier_id"],
            "processed_once": status["processed"],
            "lines": status["lines"],
            "allocations": status["allocations"],
            "to_central_stock": status["leftover"],
            "rule": "优先级降序 → 最迟收货日 → 需求ID；到货内 FIFO；同需求可部分到货",
        }

    def explain_demand(self, demand_id: str) -> dict[str, Any]:
        """解释一个需求：由哪些到货、各发了多少、还欠多少。"""
        demand = self.get_demand(demand_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT receipt_id, sku, qty FROM w_allocations"
                " WHERE demand_id=? ORDER BY allocation_id",
                (demand_id,)).fetchall()
        return {"demand": demand,
                "served_by": [dict(r) for r in rows],
                "served_qty": sum(r["qty"] for r in rows),
                "shortage": demand["remaining"]}

    def conservation_check(self) -> dict[str, Any]:
        """库存守恒独立稽核：对每个 SKU 与每张到货核对 到货=发放+中央余额。"""
        with self._lock:
            sku_rows = self._conn.execute(
                "SELECT sku, SUM(qty) AS q FROM w_receipt_lines GROUP BY sku").fetchall()
            problems: list[str] = []
            details: dict[str, Any] = {}
            for r in sku_rows:
                received = r["q"]
                allocated = self._conn.execute(
                    "SELECT COALESCE(SUM(qty),0) AS q FROM w_allocations WHERE sku=?",
                    (r["sku"],)).fetchone()["q"]
                # 中央余额再用逐到货余额加总交叉验证
                per_receipt = self._conn.execute(
                    "SELECT rl.receipt_id, rl.qty AS arrived,"
                    " COALESCE((SELECT SUM(qty) FROM w_allocations a"
                    "            WHERE a.receipt_id=rl.receipt_id AND a.sku=rl.sku),0)"
                    " AS given FROM w_receipt_lines rl WHERE rl.sku=?",
                    (r["sku"],)).fetchall()
                leftover = sum(x["arrived"] - x["given"] for x in per_receipt)
                details[r["sku"]] = {"received": received, "allocated": allocated,
                                     "central": received - allocated,
                                     "cross_check_leftover": leftover}
                if received != allocated + leftover or leftover != received - allocated:
                    problems.append(r["sku"])
                if any(x["given"] > x["arrived"] for x in per_receipt):
                    problems.append(f"{r['sku']}:receipt-over-issue")
            # 承诺维度：累计到货不得超承诺
            over = self._conn.execute(
                "SELECT commitment_no, sku, qty, received_qty FROM w_commitment_lines"
                " WHERE received_qty > qty").fetchall()
            for o in over:
                problems.append(f"commitment {o['commitment_no']}/{o['sku']} over-received")
        return {"ok": not problems, "problems": problems, "details": details}
