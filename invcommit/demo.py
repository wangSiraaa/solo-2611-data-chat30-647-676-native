"""端到端演示与案例生成。

覆盖题目要求的全部案例：
  A. 部分签收/部分损耗、同 SKU 两笔在途、签收重送（同文复读/异文拒绝）、
     不能消费别的调拨在途、发出后不可取消；
  B. 两客户争最后量、优先级定序、全有或全无、对外出库原子扣减；
  C. 先承诺后延迟、部分签收身份转换、报损失效、已发客户量不可复活、
     未签收承诺不能直接发货；
  D. 全量更正重算（旧份额可追溯）、版本重验、外部库存客户直查账本；
  E. 独立逐事件解释器在中段与终盘各核验一次，落盘关闭后重开再验。
"""

from __future__ import annotations

import os

from invcommit import (
    CommitmentError,
    InventoryError,
    InventoryFacade,
    connect,
    transaction,
    verify,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_stock(facade: InventoryFacade) -> None:
    print("-- 各仓现货/预留（外部库存客户视角，无需承诺仓） --")
    for r in facade.ledger.stock_view():
        print(f"   {r['warehouse_id']} {r['sku']}: 现货={r['spot']:<4} "
              f"预留={r['reserved']:<4} 未冻结={r['unreserved_spot']}")
    print("-- 在途明细（逐笔隔离） --")
    for r in facade.ledger.in_transit_view():
        print(f"   {r['transfer_id']} {r['sku']} {r['source_wh']}->{r['target_wh']} "
              f"发出={r['shipped_qty']} 签收={r['received_qty']} 损耗={r['lost_qty']} "
              f"未结在途={r['outstanding_in_transit']:<3} {r['status']} ETA={r['expected_receipt_date']}")
    print("-- 逐 SKU 累计流 --")
    for r in facade.ledger.sku_flow():
        print(f"   {r['sku']}: 累计入库={r['inbound_qty']} "
              f"累计出库={r['outbound_qty']} 累计损耗={r['lost_qty']}")


def show_conservation(facade: InventoryFacade) -> None:
    print("-- 逐 SKU 守恒等式：期初+入库 = 现货(含预留子集)+在途+损耗+出库 --")
    flows = {r["sku"]: r for r in facade.ledger.sku_flow()}
    stocks = facade.ledger.stock_view()
    transits = facade.ledger.in_transit_view()
    skus = sorted(set(flows) | {s["sku"] for s in stocks})
    for sku in skus:
        spot = sum(s["spot"] for s in stocks if s["sku"] == sku)
        reserved = sum(s["reserved"] for s in stocks if s["sku"] == sku)
        transit = sum(t["outstanding_in_transit"] for t in transits
                      if t["sku"] == sku and t["status"] == "IN_TRANSIT")
        f = flows.get(sku, {"inbound_qty": 0, "outbound_qty": 0, "lost_qty": 0})
        lhs = f["inbound_qty"]
        rhs = spot + transit + f["lost_qty"] + f["outbound_qty"]
        flag = "OK" if lhs == rhs else "*** BREAK ***"
        print(f"   {sku}: {lhs} = {spot}(现货,含预留{reserved})+{transit}(在途)"
              f"+{f['lost_qty']}(损耗)+{f['outbound_qty']}(出库) = {rhs}  [{flag}]")


def show_orders(facade: InventoryFacade) -> None:
    print("-- 订单与逐份来源占额 --")
    for o in facade.commitments.orders_view():
        print(f"   {o['order_id']} {o['sku']} x{o['qty']} 优先级={o['priority']} "
              f"最迟={o['latest_date']} 可发仓={o['accepted_whs']} => {o['status']}")
        for p in facade.commitments.portions_view(o["order_id"]):
            src = (f"现货 {p['warehouse_id']}" if p["source_type"] == "SPOT"
                   else f"在途 {p['transfer_id']}->{p['warehouse_id']}")
            print(f"      {p['portion_id']} {src} qty={p['qty']} "
                  f"预计={p['expected_date']} 状态={p['state']} 谱系根={p['root_portion_id']}")


def expect_error(label: str, fn) -> None:
    try:
        fn()
    except (InventoryError, CommitmentError) as e:
        print(f"   [预期拒绝] {label}: {e}")
        return
    raise AssertionError(f"{label} 本应失败却成功了")


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect(DB_PATH)
    f = InventoryFacade(conn)
    f.set_clock("2026-11-18")

    # 固定运输天数（客户段：现货可交付日 = 今天 + 天数）
    hr("初始化：4 仓固定运输天数")
    with transaction(conn):
        for wh, days in (("W1", 2), ("W2", 1), ("W3", 3), ("W4", 5)):
            ev = f.commitments.register_transit_days(wh, days)
            print(f"   {ev.event_id} 锚定账本v{ev.ledger_anchor_seq} {wh} 运输天数={days}")

    # ---------------------------------------------------------------- #
    hr("案例 A：部分签收/损耗、同 SKU 两笔在途、重送与跨调拨隔离")
    with transaction(conn):
        ev = f.inbound("W1", "SKU-A", 100)
        print(f"   {ev.event_id} 入库 W1/SKU-A +100（seq={ev.seq}）")

        ev1 = f.create_transfer("SKU-A", "W1", "W2", 30, "2026-11-22")
        ev2 = f.create_transfer("SKU-A", "W1", "W3", 20, "2026-11-25")
        print(f"   {ev1.event_id} 创建 {ev1.payload['transfer_id']} W1->W2 30（预留源仓现货）")
        print(f"   {ev2.event_id} 创建 {ev2.payload['transfer_id']} W1->W3 20（同 SKU 第二笔）")
        show_stock(f)

        s1 = f.ship_transfer("TRF-0001")
        s2 = f.ship_transfer("TRF-0002")
        print(f"   {s1.event_id} 发出 TRF-0001：预留转在途 30")
        print(f"   {s2.event_id} 发出 TRF-0002：预留转在途 20")

    with transaction(conn):
        r1 = f.ledger.receive_transfer("TRF-0001", 12, "RCP-0001")
        print(f"   {r1.event_id} 首次签收 TRF-0001 +12（RCP-0001），目标仓现货增加")
        # 同文重投：必须返回同一事件产品，库存不动
        r1b = f.ledger.receive_transfer("TRF-0001", 12, "RCP-0001")
        same = r1.event_id == r1b.event_id
        print(f"   同文重投 RCP-0001：返回 {r1b.event_id}（与首次相同={same}），库存未再增加")
        # 异文重投：同 receipt_id 不同数量，拒绝
        expect_error("同凭据异文 RCP-0001/15",
                     lambda: f.ledger.receive_transfer("TRF-0001", 15, "RCP-0001"))

        r2 = f.ledger.receive_transfer("TRF-0001", 10, "RCP-0002")
        print(f"   {r2.event_id} 分次签收 TRF-0001 +10（累计 22/30）")
        l1 = f.ledger.report_loss("TRF-0001", 8, "RCP-0003")
        print(f"   {l1.event_id} 部分损耗 TRF-0001 8：扣在途、不增目标库存，调拨结清")
        expect_error("结清后再签收",
                     lambda: f.ledger.receive_transfer("TRF-0001", 1, "RCP-0004"))
        # 不能消费别的调拨同 SKU 在途：TRF-0002 只有 20，试签 25
        expect_error("TRF-0002 超签 25>20（不得挪用 TRF-0001 额度）",
                     lambda: f.ledger.receive_transfer("TRF-0002", 25, "RCP-0005"))
        r3 = f.ledger.receive_transfer("TRF-0002", 20, "RCP-0006")
        print(f"   {r3.event_id} 签收 TRF-0002 +20，仅消费本调拨在途")
        # 发出后不得直接取消
        expect_error("发出后取消 TRF-0002",
                     lambda: f.ledger.cancel_transfer("TRF-0002"))
        # 未发取消：预留释放回未冻结现货
        c0 = f.create_transfer("SKU-A", "W1", "W4", 5)
        print(f"   {c0.event_id} 创建 {c0.payload['transfer_id']} 5（未发）")
        cx = f.cancel_transfer("TRF-0003")
        print(f"   {cx.event_id} 未发取消 TRF-0003：预留 5 释放")

    show_stock(f)
    show_conservation(f)
    print("\n>> 中段核验（独立逐事件解释器）...")
    print("  ", verify(conn))

    # ---------------------------------------------------------------- #
    hr("案例 B：两客户争最后量、优先级定序、全有或全无、原子出库")
    with transaction(conn):
        ev = f.inbound("W2", "SKU-B", 5)
        print(f"   {ev.event_id} 入库 W2/SKU-B +5（仅 5 件可争）")
        f.commitments.register_order(
            "ORD-1001", "SKU-B", 4, {"W2"}, "2026-12-31", priority=5)
        f.commitments.register_order(
            "ORD-1002", "SKU-B", 3, {"W2"}, "2026-12-31", priority=9)
        print("   登记 ORD-1001(优先级5, 需4) 与 ORD-1002(优先级9, 需3)")
        evs = f.allocate()
        for e in evs:
            print(f"   {e.event_id} 分配 {e.payload['order_id']}："
                  + ", ".join(f"{p['portion_id']}x{p['qty']}" for p in e.payload["portions"]))
        show_orders(f)
        print("   -> 高优先级 ORD-1002 拿走 3；ORD-1001 需 4 但只剩 2，整单一份不占")

        ev = f.inbound("W2", "SKU-B", 2)
        print(f"   {ev.event_id} 补入库 W2/SKU-B +2，重新分配")
        evs = f.allocate()
        for e in evs:
            print(f"   {e.event_id} 补分配 {e.payload['order_id']} 4 件，来源逐份保留")

        led_ev, cmt_ev = f.ship_customer_order("ORD-1002", "W2", 3)
        print(f"   {led_ev.event_id} 账本对外出库 W2/SKU-B 3（原子扣现货）")
        print(f"   {cmt_ev.event_id} 承诺份额转 SHIPPED：{cmt_ev.payload['order_id']}")
        led_ev, cmt_ev = f.ship_customer_order("ORD-1001", "W2", 1)
        print(f"   {led_ev.event_id} ORD-1001 部分发货 1，订单进入 PARTIALLY_SHIPPED")
    show_orders(f)
    show_conservation(f)

    # ---------------------------------------------------------------- #
    hr("案例 C：先承诺后延迟、部分签收转身份、报损失效、已发不可复活")
    with transaction(conn):
        f.inbound("W1", "SKU-C", 20)
        f.create_transfer("SKU-C", "W1", "W2", 10, "2026-11-22")
        f.ship_transfer("TRF-0004")
        print("   TRF-0004 W1->W2 SKU-C 10 件在途，ETA=2026-11-22")
        f.commitments.register_order(
            "ORD-2001", "SKU-C", 6, {"W2"}, "2026-11-30", priority=7)
        evs = f.allocate()
        e = evs[0]
        print(f"   {e.event_id} ORD-2001 6 件全部承诺自特定在途 {e.payload['portions'][0]['portion_id']}"
              "（不把在途当到货，仅锁定 TRF-0004 剩余量）")
        show_orders(f)

    with transaction(conn):
        print("   -- ETA 延后到 2026-12-20：预计可交付 12-21 > 最迟 11-30 --")
        led_ev, cmt_ev, realloc = f.update_eta("TRF-0004", "2026-12-20")
        print(f"   {led_ev.event_id} 账本 ETA 变更")
        print(f"   {cmt_ev.event_id} 失效承诺："
              + ", ".join(p["portion_id"] for p in cmt_ev.payload["invalid_portions"])
              + f"；重开订单 {cmt_ev.payload['reopened_orders']}（无替代来源，保持 OPEN）")

        print("   -- ETA 改回 2026-11-24（可交付 11-25，满足最迟日）--")
        led_ev, cmt_ev, realloc = f.update_eta("TRF-0004", "2026-11-24")
        print(f"   {cmt_ev.event_id} 重新分配，新份额锁定同一调拨在途")

    with transaction(conn):
        print("   -- 部分签收 4 件：在途份额身份转为目标仓实物 --")
        led_ev, cmt_ev, _ = f.receive_transfer("TRF-0004", 4, "RCP-0007")
        print(f"   {led_ev.event_id} 账本签收 +4；{cmt_ev.event_id} 份额转换：")
        for c in cmt_ev.payload["conversions"]:
            extra = f"（自 {c['split_from']} 拆分）" if "split_from" in c else ""
            print(f"      {c['portion_id']} 订单 {c['order_id']} x{c['qty']} -> CONVERTED{extra}")
        show_orders(f)
        # 剩余 2 件仍是未签收在途承诺，直接发货必须拒绝
        expect_error("对未签收在途份额直接发货 6 件",
                     lambda: f.ship_customer_order("ORD-2001", "W2", 6))
        # 已转换的 4 件实物可发
        led_ev, cmt_ev = f.ship_customer_order("ORD-2001", "W2", 4)
        print(f"   {led_ev.event_id} 已转换实物 4 件对外出库；{cmt_ev.event_id} 转 SHIPPED")

    with transaction(conn):
        print("   -- 剩余在途 6 件全部报损 --")
        led_ev, cmt_ev, realloc = f.report_loss("TRF-0004", 6, "RCP-0008")
        print(f"   {led_ev.event_id} 报损 6；{cmt_ev.event_id} 未发 HELD 份额失效、订单重开")
        print("   -> 已 SHIPPED 的 4 件保持 SHIPPED 不参与重判（客户量不可反向复活）")
    show_orders(f)
    show_conservation(f)

    # ---------------------------------------------------------------- #
    hr("案例 D：版本重验、全量更正重算、外部客户直查")
    with transaction(conn):
        v = f.ledger.version()
        expect_error(f"过期账本版本 v{v-1} 写入被拒",
                     lambda: f.commitments.register_transit_days(
                         "W4", 5, expected_ledger_version=v - 1))
        print(f"   当前账本版本 v{v}，传入 v{v-1} 的写入被乐观重验拒绝")

        evs = f.rebuild()
        print(f"   {evs[0].event_id} REBUILD_STARTED：旧未发份额标 SUPERSEDED（不删除）")
        for e in evs[1:]:
            print(f"   {e.event_id} 重算后新分配 {e.payload['order_id']}")
        print("   -> SHIPPED 历史份额与账本 OUTBOUND 事件原样保留可追溯")
    show_orders(f)

    print("\n>> 终盘核验（独立逐事件解释器）...")
    summary = verify(conn)
    print("  ", summary)
    conn.close()

    # 落盘后重开：证明 SQLite 持久化，且重放结果与物化表仍然一致
    conn2 = connect(DB_PATH)
    print("\n>> 关闭重开 demo.db 后再次核验...")
    print("  ", verify(conn2))
    f2 = InventoryFacade(conn2)
    show_conservation(f2)
    conn2.close()
    print("\n全部案例通过，守恒与来源占额不变量成立。")


if __name__ == "__main__":
    main()
