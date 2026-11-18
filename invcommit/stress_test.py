"""随机事件流压力测试。

在 4 仓 5 SKU 约束内随机生成合法/非法混合操作，非法操作必须被拒绝且
事务回滚；每轮结束后由独立逐事件解释器核验全网守恒与来源占额。
"""

import random

from invcommit import (
    CommitmentError,
    InventoryError,
    InventoryFacade,
    connect,
    transaction,
    verify,
)
from invcommit.constants import MAX_ORDERS, MAX_TRANSFERS

WHS = ["W1", "W2", "W3", "W4"]
SKUS = ["S1", "S2", "S3"]


def run(seed: int, rounds: int = 40) -> None:
    rng = random.Random(seed)
    f = InventoryFacade(connect(":memory:"))
    f.set_clock("2026-11-18")
    with transaction(f.conn):
        for wh in WHS:
            f.commitments.register_transit_days(wh, rng.randint(0, 4))
        for sku in SKUS:
            f.inbound(WHS[0], sku, rng.randint(20, 60))

    transfer_seq = 0
    receipt_seq = 0
    order_seq = 0

    for _ in range(rounds):
        try:
            with transaction(f.conn):
                action = rng.randrange(9)
                sku = rng.choice(SKUS)

                if action == 0:  # 入库
                    f.inbound(rng.choice(WHS), sku, rng.randint(1, 10))

                elif action == 1:  # 建调拨
                    if transfer_seq >= MAX_TRANSFERS:
                        continue
                    src, dst = rng.sample(WHS, 2)
                    f.create_transfer(
                        sku, src, dst, rng.randint(1, 8),
                        f"2026-12-{rng.randint(1, 28):02d}")
                    transfer_seq += 1

                elif action == 2:  # 发出一笔 RESERVED
                    row = f.conn.execute(
                        "SELECT transfer_id FROM led_transfer WHERE status='RESERVED' "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
                    if row:
                        f.ship_transfer(row[0])

                elif action == 3:  # 随机签收（可能超量，应被拒）
                    row = f.conn.execute(
                        "SELECT transfer_id, shipped_qty, received_qty, lost_qty "
                        "FROM led_transfer WHERE status='IN_TRANSIT' "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
                    if row:
                        tid, sh, rc, lo = row
                        receipt_seq += 1
                        f.receive_transfer(
                            tid, rng.randint(1, sh - rc - lo + 2),
                            f"RCP-{receipt_seq:04d}")

                elif action == 4:  # 随机损耗（走门面，触发承诺联动）
                    row = f.conn.execute(
                        "SELECT transfer_id, shipped_qty, received_qty, lost_qty "
                        "FROM led_transfer WHERE status='IN_TRANSIT' "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
                    if row:
                        tid, sh, rc, lo = row
                        receipt_seq += 1
                        f.report_loss(
                            tid, rng.randint(1, sh - rc - lo + 2),
                            f"RCP-{receipt_seq:04d}")

                elif action == 5:  # 改 ETA
                    row = f.conn.execute(
                        "SELECT transfer_id FROM led_transfer WHERE status='IN_TRANSIT' "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
                    if row:
                        f.update_eta(row[0], f"2026-12-{rng.randint(1, 28):02d}")

                elif action == 6:  # 登记订单并立即尝试分配
                    if order_seq >= MAX_ORDERS:
                        continue
                    order_seq += 1
                    k = rng.randint(1, 3)
                    f.commitments.register_order(
                        f"ORD-{order_seq:04d}", sku, rng.randint(1, 12),
                        rng.sample(WHS, k), f"2026-12-{rng.randint(10, 31):02d}",
                        rng.randint(1, 9))
                    f.allocate()

                elif action == 7:  # 尝试客户发货（可能实物不足，应被拒）
                    row = f.conn.execute(
                        "SELECT order_id, warehouse_id, SUM(qty) FROM cmt_portion "
                        "WHERE source_type='SPOT' AND state IN ('HELD','CONVERTED') "
                        "GROUP BY order_id, warehouse_id ORDER BY RANDOM() LIMIT 1"
                    ).fetchone()
                    if row:
                        f.ship_customer_order(
                            row[0], row[1], rng.randint(1, row[2] + 2))

                else:  # 全量重算
                    f.rebuild()

        except (InventoryError, CommitmentError) as e:
            # 业务拒绝即预期行为：事务已整体回滚，状态保持上一轮一致
            assert f.conn.in_transaction is False
            continue

        # 每轮提交后立即独立核验
        verify(f.conn)

    summary = verify(f.conn)
    return summary


if __name__ == "__main__":
    for seed in range(8):
        s = run(seed)
        print(f"seed={seed} OK -> {s}")
    print("\n随机压力测试全部通过：守恒与来源占额在所有事件后成立")
