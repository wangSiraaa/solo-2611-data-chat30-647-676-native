"""对抗性边界测试：故意制造各种违规，确认系统拒绝且事务原子回滚。

运行：python -m pytest edge_tests.py -q 或直接 python edge_tests.py
"""

from __future__ import annotations

from invcommit import (
    CommitmentError,
    InventoryError,
    InventoryFacade,
    connect,
    transaction,
    verify,
)
from invcommit.constants import MAX_ORDERS, MAX_SKUS, MAX_TRANSFERS, MAX_WAREHOUSES


def setup() -> InventoryFacade:
    f = InventoryFacade(connect(":memory:"))
    f.set_clock("2026-11-18")
    return f


def test_rollback_is_atomic():
    f = setup()
    try:
        with transaction(f.conn):
            f.inbound("W1", "SKU-A", 10)
            # 超量出库：异常穿透上下文管理器，整个事务回滚（入库也不留）
            f.ledger.outbound("W1", "SKU-A", 99)
    except InventoryError:
        pass
    else:
        raise AssertionError("超量出库应失败")
    assert f.ledger.version() == 0
    assert f.ledger.stock_view() == []


def test_reserved_spot_cannot_be_outbound():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 10)
        f.create_transfer("SKU-A", "W1", "W2", 8)
        # 现货 10 但 8 被预留，只能出 2
        try:
            f.ledger.outbound("W1", "SKU-A", 3)
        except InventoryError:
            pass
        else:
            raise AssertionError("预留冻结量不应被对外出库消费")
        f.ledger.outbound("W1", "SKU-A", 2)  # 未冻结部分可以
    verify(f.conn)


def test_two_transfers_in_transit_are_independent():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 30)
        f.create_transfer("SKU-A", "W1", "W2", 10, "2026-11-22")
        f.create_transfer("SKU-A", "W1", "W3", 20, "2026-11-23")
        f.ship_transfer("TRF-0001")
        f.ship_transfer("TRF-0002")
        # 第一笔签收完不能再碰第二笔的量
        f.ledger.receive_transfer("TRF-0001", 10, "RCP-1")
        try:
            f.ledger.receive_transfer("TRF-0001", 1, "RCP-2")
        except InventoryError:
            pass
        else:
            raise AssertionError("结清调拨不得再签收")
        # 损耗也限定在本调拨未结在途
        try:
            f.ledger.report_loss("TRF-0002", 21, "RCP-3")
        except InventoryError:
            pass
        else:
            raise AssertionError("损耗不得跨调拨")
        f.ledger.report_loss("TRF-0002", 5, "RCP-4")
        f.ledger.receive_transfer("TRF-0002", 15, "RCP-5")
    verify(f.conn)


def test_spot_overcommit_and_all_or_nothing():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 5)
        for wh in ("W1",):
            f.commitments.register_transit_days(wh, 1)
        f.commitments.register_order("O1", "SKU-A", 3, ["W1"], "2026-12-31", 1)
        f.commitments.register_order("O2", "SKU-A", 3, ["W1"], "2026-12-31", 1)
        evs = f.allocate()
        assert len(evs) == 1 and evs[0].payload["order_id"] == "O1"
        # 第二单只剩 2，整单不占
        assert f.commitments._get_order("O2")["status"] == "OPEN"
        held = sum(p["qty"] for p in f.commitments.portions_view()
                   if p["state"] == "HELD")
        assert held == 3  # 绝不能占出 6
    verify(f.conn)


def test_transit_requires_explicit_eta_and_specific_transfer():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 10)
        f.commitments.register_transit_days("W2", 2)
        # 无 ETA 的在途调拨不能成为承诺来源
        f.create_transfer("SKU-A", "W1", "W2", 10)
        f.ledger.ship_transfer("TRF-0001")
        f.commitments.register_order("O1", "SKU-A", 5, ["W2"], "2026-12-31", 1)
        assert f.allocate() == []  # 无明确预计签收日 -> 不分配
        f.ledger.update_eta("TRF-0001", "2026-11-22")
        evs = f.allocate()
        assert len(evs) == 1
        p = evs[0].payload["portions"][0]
        assert p["transfer_id"] == "TRF-0001" and p["source_type"] == "TRANSIT"
    verify(f.conn)


def test_lost_transfer_reopens_order_but_shipped_stays():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 10)
        f.commitments.register_transit_days("W2", 1)
        f.create_transfer("SKU-A", "W1", "W2", 10, "2026-11-22")
        f.ship_transfer("TRF-0001")
        f.commitments.register_order("O1", "SKU-A", 8, ["W2"], "2026-12-31", 1)
        f.allocate()
        # 先签收 3 并转实物、发掉
        f.receive_transfer("TRF-0001", 3, "RCP-1")
        f.ship_customer_order("O1", "W2", 3)
        # 剩余 7 在途报损（订单还占着 5）
        f.report_loss("TRF-0001", 7, "RCP-2")
        states = {p["state"]: p["qty"]
                  for p in f.commitments.portions_view("O1")}
        assert states.get("SHIPPED") == 3       # 已发不可复活
        assert "HELD" not in states and "CONVERTED" not in states
        assert f.commitments._get_order("O1")["status"] == "OPEN"
    verify(f.conn)


def test_capacity_limits_enforced():
    f = setup()
    with transaction(f.conn):
        for i in range(MAX_WAREHOUSES):
            f.inbound(f"W{i+1}", "SKU-X", 1)
        try:
            f.inbound("W5", "SKU-X", 1)
        except InventoryError:
            pass
        else:
            raise AssertionError("仓库上限未强制")
        for i in range(MAX_SKUS - 1):
            f.inbound("W1", f"SKU-{i}", 1)
        try:
            f.inbound("W1", "SKU-TOO-MANY", 1)
        except InventoryError:
            pass
        else:
            raise AssertionError("SKU 上限未强制")
    # 调拨上限
    f2 = setup()
    with transaction(f2.conn):
        f2.inbound("W1", "SKU-A", 1000)
        for i in range(MAX_TRANSFERS):
            f2.create_transfer("SKU-A", "W1", "W2", 1, "2026-12-01")
        try:
            f2.create_transfer("SKU-A", "W1", "W2", 1, "2026-12-01")
        except InventoryError:
            pass
        else:
            raise AssertionError("调拨上限未强制")
    # 订单上限
    f3 = setup()
    with transaction(f3.conn):
        f3.commitments.register_transit_days("W1", 1)
        for i in range(MAX_ORDERS):
            f3.commitments.register_order(
                None, "SKU-A", 1, ["W1"], "2026-12-31", 1)
        try:
            f3.commitments.register_order(
                None, "SKU-A", 1, ["W1"], "2026-12-31", 1)
        except CommitmentError:
            pass
        else:
            raise AssertionError("订单上限未强制")


def test_stale_version_rejected():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 1)
        v = f.ledger.version()
        f.inbound("W1", "SKU-A", 1)  # 账本前进一版
        try:
            f.commitments.register_transit_days(
                "W1", 1, expected_ledger_version=v)
        except CommitmentError:
            pass
        else:
            raise AssertionError("过期版本写入应被拒绝")


def test_rebuild_keeps_lineage_and_history():
    f = setup()
    with transaction(f.conn):
        f.inbound("W1", "SKU-A", 10)
        f.commitments.register_transit_days("W1", 1)
        f.commitments.register_order("O1", "SKU-A", 4, ["W1"], "2026-12-31", 1)
        f.allocate()
        old = [p for p in f.commitments.portions_view() if p["state"] == "HELD"]
        assert len(old) == 1
        f.rebuild()
        superseded = [p for p in f.commitments.portions_view()
                      if p["state"] == "SUPERSEDED"]
        new_held = [p for p in f.commitments.portions_view() if p["state"] == "HELD"]
        assert len(superseded) == 1 and len(new_held) == 1
        # 旧产品仍可追溯，谱系根保留
        assert superseded[0]["portion_id"] == old[0]["portion_id"]
        assert new_held[0]["root_portion_id"] == new_held[0]["portion_id"]
    verify(f.conn)


def test_external_customer_reads_ledger_without_commitment_tables():
    """承诺仓不建表时，纯账本客户仍能查询（模块独立性）。"""
    from invcommit.ledger import TransferLedger
    conn = connect(":memory:")
    led = TransferLedger(conn)   # 建表（executescript）必须在事务外
    with transaction(conn):
        led.inbound("W1", "SKU-A", 5)
        assert led.stock_view()[0]["spot"] == 5
        assert led.version() == 1
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert not any(t.startswith("cmt_") for t in tables)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项边界测试通过")
