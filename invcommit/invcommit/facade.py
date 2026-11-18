"""两模块协同门面。

事务纪律
========
- 一个 SQLite 连接即一个共享事务：账本写 led_* 表、承诺仓写 cmt_* 表，
  要么一起提交，要么一起回滚，杜绝“账本已扣、占额未转”的中间态。
- 每次承诺仓写入都锚定当时账本版本（cmt_event.ledger_anchor_seq）；
  调用方也可传入期望版本做乐观并发重验，版本不符直接失败。
- 账本反应（签收转身份、报损/延迟失效）在同一事务内完成，随后对
  重新开放的订单再跑一次分配；反应事件与分配事件同进退。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .commitment import CommitmentStore
from .errors import InventoryError
from .ledger import TransferLedger


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """打开库。isolation_level=None 让我们显式管理 BEGIN/COMMIT。"""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式共享事务：异常即回滚，正常结束才提交。

    注意：sqlite3 的 executescript 会先隐式 COMMIT，因此建表（模块构造）
    必须在事务外完成；本上下文只包裹数据变更。
    """
    conn.execute("BEGIN")
    try:
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class InventoryFacade:
    """对外唯一编排入口。账本仍是库存唯一事实源。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ledger = TransferLedger(conn)
        self.commitments = CommitmentStore(conn, self.ledger)

    def set_clock(self, today_iso: str) -> None:
        self.commitments.set_clock(today_iso)

    # ---- 纯账本动作（无需承诺联动） ---------------------------------- #
    def inbound(self, wh: str, sku: str, qty: int):
        return self.ledger.inbound(wh, sku, qty)

    def create_transfer(self, sku, source_wh, target_wh, qty,
                        expected_receipt_date=None):
        """协同校验：调拨预留只能来自未被交付承诺占用的真实现货。

        账本模块本身不知道承诺仓（可独立使用）；跨模块的容量纪律在门面
        统一强制执行，保证 spot >= 调拨预留 + 承诺占额。
        """
        spot, reserved = self.ledger._bal(source_wh, sku)
        occupied = self.commitments.spot_occupancy(source_wh, sku)
        free_for_reserve = spot - reserved - occupied
        if free_for_reserve < qty:
            raise InventoryError(
                f"源仓 {source_wh}/{sku} 可预留现货不足：需 {qty}，"
                f"未冻结且未承诺 {free_for_reserve}"
                f"（现货 {spot} / 调拨预留 {reserved} / 承诺占额 {occupied}）"
            )
        return self.ledger.create_transfer(
            sku, source_wh, target_wh, qty, expected_receipt_date
        )

    def cancel_transfer(self, transfer_id: str):
        return self.ledger.cancel_transfer(transfer_id)

    # ---- 发出：无承诺反应，但锚定版本 ------------------------------- #
    def ship_transfer(self, transfer_id: str, expected_receipt_date=None):
        return self.ledger.ship_transfer(transfer_id, expected_receipt_date)

    # ---- 带承诺联动的账本动作 --------------------------------------- #
    def receive_transfer(self, transfer_id: str, qty: int, receipt_id: str):
        led_ev = self.ledger.receive_transfer(transfer_id, qty, receipt_id)
        cmt_ev = self.commitments.on_transfer_received(
            transfer_id, qty, anchor=led_ev.seq
        )
        # 身份转换可能让某订单具备可发实物；对仍 OPEN 的订单重排一次
        realloc = self.commitments.run_allocation(
            expected_ledger_version=led_ev.seq
        )
        return led_ev, cmt_ev, realloc

    def report_loss(self, transfer_id: str, qty: int, receipt_id: str):
        led_ev = self.ledger.report_loss(transfer_id, qty, receipt_id)
        cmt_ev, reopened = self.commitments.on_transfer_lost(
            transfer_id, qty, anchor=led_ev.seq
        )
        realloc = self.commitments.run_allocation(
            expected_ledger_version=led_ev.seq
        )
        return led_ev, cmt_ev, realloc

    def update_eta(self, transfer_id: str, new_expected_date: str):
        led_ev = self.ledger.update_eta(transfer_id, new_expected_date)
        cmt_ev, reopened = self.commitments.on_eta_changed(
            transfer_id, new_expected_date, anchor=led_ev.seq
        )
        realloc = self.commitments.run_allocation(
            expected_ledger_version=led_ev.seq
        )
        return led_ev, cmt_ev, realloc

    def ship_customer_order(self, order_id: str, warehouse_id: str, qty: int):
        return self.commitments.ship_customer_order(
            order_id, warehouse_id, qty, self.ledger
        )

    def allocate(self):
        return self.commitments.run_allocation()

    def rebuild(self):
        return self.commitments.rebuild()
