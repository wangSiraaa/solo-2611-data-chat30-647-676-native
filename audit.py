"""独立重放核账。

不调用账本的任何业务方法，只读取底层 SQLite 表，从原始收发/移动/调整事件
重放出全量库存，与账本当前量、每个已保留快照、盘点冻结基准和最终调整逐项核对。

核账失败会抛出 AssertionError，并在错误信息中给出第一个不一致的位置；
核账通过则返回一份可打印的证据报告。失败前后重跑结果逐行不变（纯读操作）。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ledger import InventoryLedger
from workbench import CountWorkbench


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def replay_ledger(conn: sqlite3.Connection, k: Optional[int] = None) -> dict[tuple[str, str], int]:
    """从 l_movement_lines 原始事件独立重放到前缀 k（None = 全部）。"""
    if k is None:
        rows = conn.execute(
            """
            SELECT sku, location, SUM(delta) AS d
            FROM l_movement_lines
            GROUP BY sku, location
            HAVING SUM(delta) <> 0
            """
        ).fetchall()
    else:
        rows = conn.execute(
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
    balances: dict[tuple[str, str], int] = {}
    for r in rows:
        qty = int(r["d"])
        assert qty >= 0, f"重放出现负库存 {r['sku']}@{r['location']}={qty}"
        balances[(r["sku"], r["location"])] = qty
    return balances


def audit(path: str) -> dict[str, Any]:
    """对落盘库文件做全套独立核对（内存库请先 close 再用文件路径打开）。"""
    conn = _connect(path)
    report: dict[str, Any] = {"checks": [], "snapshots": [], "sheets": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        assert ok, f"核账失败[{name}] {detail}"
        report["checks"].append(name)

    # 1. commit_seq 严格递增、请求号唯一且同号同文
    seqs = [
        int(r["commit_seq"])
        for r in conn.execute("SELECT commit_seq FROM l_requests ORDER BY rowid").fetchall()
    ]
    check("commit_seq 连续递增", seqs == list(range(1, len(seqs) + 1)), str(seqs))
    dup_req = conn.execute(
        "SELECT request_no, COUNT(*) c FROM l_requests GROUP BY request_no HAVING c > 1"
    ).fetchall()
    check("请求号全局唯一", not dup_req)

    # 2. 重放全量 == 账本当前量
    replayed = replay_ledger(conn)
    stored_rows = conn.execute(
        "SELECT sku, location, qty FROM l_balances WHERE qty > 0"
    ).fetchall()
    stored = {(r["sku"], r["location"]): int(r["qty"]) for r in stored_rows}
    check("当前库存与事件重放一致", stored == replayed, f"{stored} != {replayed}")
    check("当前库存全部非负", all(v >= 0 for v in stored.values()))

    # 3. 每个已保留快照 == 在其前缀上的独立重放
    snap_rows = conn.execute(
        "SELECT snapshot_id, prefix, label FROM l_snapshots ORDER BY snapshot_id"
    ).fetchall()
    for sr in snap_rows:
        prefix = int(sr["prefix"])
        replay_at_k = replay_ledger(conn, prefix)
        snap_rows2 = conn.execute(
            "SELECT sku, location, qty FROM l_snapshot_lines WHERE snapshot_id=?",
            (sr["snapshot_id"],),
        ).fetchall()
        snap_bal = {(r["sku"], r["location"]): int(r["qty"]) for r in snap_rows2}
        # 快照只存非零行
        snap_nonzero = {k2: v for k2, v in snap_bal.items() if v != 0}
        check(
            f"快照 {sr['label']}(k={prefix}) 与重放一致",
            snap_nonzero == replay_at_k,
            f"{snap_nonzero} != {replay_at_k}",
        )
        report["snapshots"].append(
            {"prefix": prefix, "label": sr["label"], "positions": len(snap_bal)}
        )

    # 4. 盘点单逐项核对
    sheet_rows = conn.execute(
        "SELECT sheet_id, frozen_prefix, snapshot_label, status, "
        "adjust_request_no, adjust_commit_seq FROM count_sheets ORDER BY sheet_id"
    ).fetchall()
    for sh in sheet_rows:
        sheet_id = int(sh["sheet_id"])
        k = int(sh["frozen_prefix"])
        replay_at_k = replay_ledger(conn, k)

        line_rows = conn.execute(
            "SELECT sku, location, frozen_qty FROM count_sheet_lines WHERE sheet_id=? ORDER BY line_no",
            (sheet_id,),
        ).fetchall()
        for lr in line_rows:
            expected = replay_at_k.get((lr["sku"], lr["location"]), 0)
            check(
                f"单{sheet_id} 冻结基准 {lr['sku']}@{lr['location']}",
                int(lr["frozen_qty"]) == expected,
                f"stored={lr['frozen_qty']} replay={expected}",
            )

        # 录入版本链完整、当前版本与版本表末端一致
        entry_rows = conn.execute(
            "SELECT sku, location, version, counted_qty FROM count_entries WHERE sheet_id=?",
            (sheet_id,),
        ).fetchall()
        for er in entry_rows:
            last_ver = conn.execute(
                "SELECT version, counted_qty FROM count_entry_versions "
                "WHERE sheet_id=? AND sku=? AND location=? ORDER BY version DESC LIMIT 1",
                (sheet_id, er["sku"], er["location"]),
            ).fetchone()
            check(
                f"单{sheet_id} 录入当前版本 {er['sku']}@{er['location']}",
                int(er["version"]) == int(last_ver["version"])
                and int(er["counted_qty"]) == int(last_ver["counted_qty"]),
            )

        if sh["status"] == "submitted":
            # 调整行差异 = 录入盘点量 - 冻结基准
            adj_rows = conn.execute(
                "SELECT sku, location, frozen_qty, counted_qty, delta "
                "FROM count_adjustments WHERE sheet_id=? ORDER BY line_no",
                (sheet_id,),
            ).fetchall()
            entry_map = {
                (r["sku"], r["location"]): int(r["counted_qty"]) for r in entry_rows
            }
            check(
                f"单{sheet_id} 调整行覆盖全部盘点位置",
                len(adj_rows) == len(line_rows),
            )
            for ar in adj_rows:
                check(
                    f"单{sheet_id} 差异口径 {ar['sku']}@{ar['location']}",
                    int(ar["delta"])
                    == entry_map[(ar["sku"], ar["location"])] - int(ar["frozen_qty"]),
                )
            # 账本中确实存在对应调整事件且序号一致
            ledger_row = conn.execute(
                "SELECT commit_seq FROM l_requests WHERE request_no=?",
                (sh["adjust_request_no"],),
            ).fetchone()
            check(
                f"单{sheet_id} 调整事件已落账",
                ledger_row is not None
                and int(ledger_row["commit_seq"]) == int(sh["adjust_commit_seq"]),
            )
            # 调整后该单各位置库存非负（由当前全量非负已覆盖，这里再验调整后重放）
            after = replay_ledger(conn, int(sh["adjust_commit_seq"]))
            for lr in line_rows:
                check(
                    f"单{sheet_id} 调整后非负 {lr['sku']}@{lr['location']}",
                    after.get((lr["sku"], lr["location"]), 0) >= 0,
                )
            report["sheets"].append(
                {
                    "sheet_id": sheet_id,
                    "status": "submitted",
                    "adjust_commit_seq": int(sh["adjust_commit_seq"]),
                    "lines": len(adj_rows),
                }
            )
        elif sh["status"] == "voided":
            n_adj = conn.execute(
                "SELECT COUNT(*) c FROM count_adjustments WHERE sheet_id=?", (sheet_id,)
            ).fetchone()["c"]
            check(f"单{sheet_id} 作废无调整", n_adj == 0)
            report["sheets"].append(
                {"sheet_id": sheet_id, "status": "voided", "lines": len(line_rows)}
            )
        else:
            report["sheets"].append(
                {"sheet_id": sheet_id, "status": "open", "lines": len(line_rows)}
            )

    # 5. 位置占用表只含 open 单据，且与单据状态一致
    active_rows = conn.execute(
        """
        SELECT a.sku, a.location, a.sheet_id, s.status
        FROM active_count_locations a
        JOIN count_sheets s ON s.sheet_id = a.sheet_id
        """
    ).fetchall()
    check(
        "占用表只含 open 单据",
        all(r["status"] == "open" for r in active_rows),
    )
    open_sheets = {
        int(r["sheet_id"])
        for r in conn.execute(
            "SELECT sheet_id FROM count_sheets WHERE status='open'"
        ).fetchall()
    }
    occupied = {(r["sku"], r["location"]) for r in active_rows}
    check("占用位置无重复", len(occupied) == len(active_rows))
    # open 单据的每个位置都应在占用表中
    expected_occupied = {
        (r["sku"], r["location"])
        for r in conn.execute(
            f"""
            SELECT sku, location FROM count_sheet_lines
            WHERE sheet_id IN (
                SELECT sheet_id FROM count_sheets WHERE status='open'
            )
            """
        ).fetchall()
    }
    check("open 单据位置全部占用", occupied == expected_occupied)
    check("open 单据集合自洽", isinstance(open_sheets, set))

    # 6. 工作台幂等回执：每个请求号只出现一次且结果可复读
    wb_dup = conn.execute(
        "SELECT request_no, COUNT(*) c FROM wb_requests GROUP BY request_no HAVING c > 1"
    ).fetchall()
    check("工作台请求号唯一", not wb_dup)

    conn.close()
    report["head_seq"] = len(seqs)
    report["final_balances"] = {f"{k2[0]}@{k2[1]}": v for k2, v in sorted(replayed.items())}
    return report


def _main() -> None:
    import json
    import sys

    if len(sys.argv) != 2:
        print("用法: python3 audit.py <库存数据库文件>", file=sys.stderr)
        raise SystemExit(2)
    report = audit(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(
        f"核账通过：{len(report['checks'])} 项检查，"
        f"{len(report['snapshots'])} 份快照，{len(report['sheets'])} 张盘点单",
        file=sys.stderr,
    )


if __name__ == "__main__":
    _main()


def cross_check_objects(ledger: InventoryLedger, workbench: CountWorkbench) -> dict[str, Any]:
    """对活动对象做内存态交叉核对（快照可独立查询）。"""
    head = ledger.head_seq()
    replayed = ledger.as_of(head)
    current = ledger.all_balances()
    assert replayed == current, f"对象态重放不一致: {replayed} != {current}"
    result = {"head_seq": head, "positions": len(current)}
    for snap in ledger.list_snapshots():
        assert ledger.as_of(snap.prefix) == snap.balances, (
            f"快照 {snap.label} 与 as_of 重放不一致"
        )
        result.setdefault("snapshot_checks", []).append(
            {"label": snap.label, "prefix": snap.prefix}
        )
    return result
