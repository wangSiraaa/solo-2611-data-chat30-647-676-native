"""独立库存守恒稽核：只用裸 SQL 读原始表，不调用仓库类任何方法。

守恒恒等式（逐 SKU、逐到货、逐承诺三个维度交叉验证）：
    累计到货 = 累计发放 + 中央库存余额
    单张到货：arrived >= 该到货已发放
    单条承诺：received_qty <= 承诺数量
退出码 0 表示全部通过，非 0 表示发现破口。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict


def audit(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    problems: list[str] = []
    detail: dict = {}

    # 维度 1：逐 SKU，到货合计 vs 发放合计 vs 逐到货余额加总
    receipts = defaultdict(int)
    for r in conn.execute(
            "SELECT sku, SUM(qty) q FROM w_receipt_lines GROUP BY sku"):
        receipts[r["sku"]] = r["q"]
    allocations = defaultdict(int)
    per_receipt_given = defaultdict(int)
    for r in conn.execute(
            "SELECT receipt_id, sku, SUM(qty) q FROM w_allocations"
            " GROUP BY receipt_id, sku"):
        allocations[r["sku"]] += r["q"]
        per_receipt_given[(r["receipt_id"], r["sku"])] = r["q"]

    for sku, arrived in sorted(receipts.items()):
        given = allocations.get(sku, 0)
        leftover_rows = conn.execute(
            "SELECT receipt_id, qty FROM w_receipt_lines WHERE sku=?", (sku,)).fetchall()
        leftover_sum = 0
        for lr in leftover_rows:
            g = per_receipt_given.get((lr["receipt_id"], sku), 0)
            if g > lr["qty"]:  # 维度 2：单张到货超发
                problems.append(f"{sku} 到货 {lr['receipt_id']} 超发: {g}>{lr['qty']}")
            leftover_sum += lr["qty"] - g
        central = arrived - given
        if leftover_sum != central:
            problems.append(f"{sku} 中央库存两种算法不一致: {central} vs {leftover_sum}")
        detail[sku] = {"received": arrived, "allocated": given,
                       "central": central, "cross_check_leftover": leftover_sum}

    # 维度 3：承诺累计到货不超承诺量；台账不超需求量
    for r in conn.execute(
            "SELECT commitment_no, sku, qty, received_qty FROM w_commitment_lines"):
        if r["received_qty"] > r["qty"]:
            problems.append(
                f"承诺 {r['commitment_no']}/{r['sku']} 累计到货 {r['received_qty']}"
                f" > 承诺 {r['qty']}")
    for r in conn.execute(
            "SELECT d.demand_id, d.qty want, l.allocated_qty got FROM w_demands d"
            " JOIN w_demand_ledgers l ON l.demand_id=d.demand_id"):
        if r["got"] > r["want"]:
            problems.append(f"需求 {r['demand_id']} 超发: {r['got']}>{r['want']}")

    # 维度 4：发放引用的到货身份必须真实存在且只处理一次
    seen_receipts = {r["receipt_id"] for r in conn.execute(
        "SELECT DISTINCT receipt_id FROM w_receipts")}
    for r in conn.execute("SELECT DISTINCT receipt_id FROM w_allocations"):
        if r["receipt_id"] not in seen_receipts:
            problems.append(f"发放引用了不存在的到货 {r['receipt_id']}")
    for r in conn.execute(
            "SELECT receipt_id, COUNT(*) c FROM w_receipts GROUP BY receipt_id HAVING c>1"):
        problems.append(f"到货身份重复登记 {r['receipt_id']}")

    conn.close()
    return {"ok": not problems, "problems": problems, "by_sku": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="独立库存守恒稽核（裸 SQL，不经仓库类）")
    parser.add_argument("db", help="warehouse.db 路径")
    args = parser.parse_args()
    result = audit(args.db)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
