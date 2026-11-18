"""端到端演示与场景测试（python3 demo.py）。

覆盖题面要求的全部场景：
  1. 手算例：冻结 10、盘得 9、冻结后出库 3、提交差异后应为 6；
  2. 移动两侧原子提交（含非法移动不留半次）；
  3. 差异导致当前不足 -> 整单保持待处理，不部分提交；
  4. 重叠盘点整次拒绝；
  5. 录入更正与版本链、录入身份保留；
  6. 提交响应丢失 -> 同请求重试复读同一调整身份；
  7. 两客户端并发提交/并发同请求，不重复调增调减；
  8. 关闭重开后中间盘点与幂等回执仍成立；
  9. 合成事件流 + 独立重放核账，失败前后逐行不变。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from ledger import (
    InvalidOperation,
    InventoryLedger,
)
from workbench import (
    CountWorkbench,
    NegativeAdjustmentError,
    OverlapError,
    SheetStateError,
)
from audit import audit, cross_check_objects
from synthetic import SyntheticWarehouse


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_balances(ledger: InventoryLedger, note: str) -> None:
    print(f"[{note}]")
    for (sku, loc), qty in sorted(ledger.all_balances().items()):
        print(f"  {sku}@{loc} = {qty}")


# ---------------------------------------------------------------------------
# 场景 1：手算例
# ---------------------------------------------------------------------------

def scenario_hand_calc(path: str) -> None:
    banner("场景1 手算例：冻结10 / 盘得9 / 冻结后出库3 / 提交后应为6")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)

    r0 = ledger.inbound("hand-in-1", "SKU-A", "L01", 10)
    print(f"入库 10 件 SKU-A@L01 -> commit_seq={r0.commit_seq}")
    show_balances(ledger, "盘点前")

    opened = wb.open_sheet("hand-open", [("SKU-A", "L01")])
    k = opened["frozen_prefix"]
    sheet_id = opened["sheet_id"]
    print(f"开始盘点单 {sheet_id}，账本实际冻结前缀 k={k}，冻结基准 = "
          f"{opened['lines'][0]['frozen_qty']}")

    # 盘点输入是 k 时刻实数：盘得 9（不是提交时的实物量）
    wb.enter_count("hand-count", sheet_id, "SKU-A", "L01", 9, "counter-甲")
    print("录入盘点实数 9（冻结口径，合成复盘资料提供）")

    # 冻结后仓库照常出库 3 件 -> 当前量降到 7
    ledger.outbound("hand-out-after-freeze", "SKU-A", "L01", 3)
    show_balances(ledger, "冻结后出库3件，提交前当前量")
    assert ledger.balance("SKU-A", "L01") == 7

    # 错误做法对照：若直接覆盖成盘点量 9，会吞掉真实出库（9 != 应有 6）
    naive_overwrite = 9
    print(f"对照：直接覆盖为盘点量会得到 {naive_overwrite}，"
          f"吞掉了冻结后的 3 件真实出库")

    res = wb.submit_sheet("hand-submit", sheet_id)
    delta = res["adjustments"][0]["delta"]
    print(f"提交：差异 = 盘点量9 - 账本as_of({k})10 = {delta}，"
          f"调整事件 commit_seq={res['adjust_commit_seq']}")
    final = ledger.balance("SKU-A", "L01")
    show_balances(ledger, "提交后")
    assert final == 6, f"手算例失败：期望 6，实际 {final}"
    print(f"✓ 最终库存 = 当前7 + 差异({delta}) = {final}，与手算一致")

    # 同请求重试（响应丢失）：复读同一调整身份，库存不再变化
    replay = wb.submit_sheet("hand-submit", sheet_id)
    assert replay["adjust_commit_seq"] == res["adjust_commit_seq"]
    assert replay["adjustments"] == res["adjustments"]
    assert ledger.balance("SKU-A", "L01") == 6
    print(f"✓ 响应丢失重试复读同一调整身份 seq={replay['adjust_commit_seq']}，库存仍为 6")

    # 不同请求号不能再次结算
    try:
        wb.submit_sheet("hand-submit-again", sheet_id)
    except SheetStateError as e:
        print(f"✓ 不同请求再次结算被拒绝: {e}")

    explain = wb.explain_sheet(sheet_id)
    print("逐项解释:")
    print(json.dumps(explain, ensure_ascii=False, indent=2, default=str))
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 2：移动两侧原子提交
# ---------------------------------------------------------------------------

def scenario_transfer(path: str) -> None:
    banner("场景2 同SKU跨库位移动：两侧原子提交，非法移动不留半次")
    ledger = InventoryLedger(path)
    CountWorkbench(ledger)

    ledger.inbound("tr-in", "SKU-B", "L01", 8)
    rec = ledger.transfer("tr-move", "SKU-B", "L01", "L02", 5)
    print(f"移动 5 件 SKU-B L01->L02 -> seq={rec.commit_seq}，两侧同一 commit_seq")
    assert ledger.balance("SKU-B", "L01") == 3
    assert ledger.balance("SKU-B", "L02") == 5

    # 非法移动：源不足，整笔回滚
    before = ledger.all_balances()
    try:
        ledger.transfer("tr-bad", "SKU-B", "L01", "L02", 99)
    except InvalidOperation as e:
        print(f"✓ 超量移动被拒: {e}")
    assert ledger.all_balances() == before, "失败移动留下了半次痕迹"
    assert ledger.balance("SKU-B", "L01") == 3 and ledger.balance("SKU-B", "L02") == 5

    # 同库位移动非法
    try:
        ledger.transfer("tr-same", "SKU-B", "L01", "L01", 1)
    except InvalidOperation as e:
        print(f"✓ 同库位移动被拒: {e}")

    # 非法出库：无负库存、无序号占用
    head_before = ledger.head_seq()
    try:
        ledger.outbound("tr-oversell", "SKU-B", "L02", 999)
    except InvalidOperation as e:
        print(f"✓ 非法出库被拒: {e}")
    assert ledger.head_seq() == head_before, "失败操作占用了 commit_seq"
    assert ledger.balance("SKU-B", "L02") == 5
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 3：差异导致当前不足
# ---------------------------------------------------------------------------

def scenario_negative_block(path: str) -> None:
    banner("场景3 差异会使当前位置为负 -> 整单保持待处理，不部分提交")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)

    ledger.inbound("neg-in1", "SKU-C", "L01", 10)
    ledger.inbound("neg-in2", "SKU-C", "L02", 10)
    opened = wb.open_sheet("neg-open", [("SKU-C", "L01"), ("SKU-C", "L02")])
    sheet_id = opened["sheet_id"]
    k = opened["frozen_prefix"]

    # L01 盘亏 9（差异 -9）；L02 盘盈 2（差异 +2）
    wb.enter_count("neg-c1", sheet_id, "SKU-C", "L01", 1, "counter-甲")
    wb.enter_count("neg-c2", sheet_id, "SKU-C", "L02", 12, "counter-甲")

    # 冻结后 L01 又出库 5 -> 当前只剩 5；差异 -9 会把它打成 -4
    ledger.outbound("neg-out", "SKU-C", "L01", 5)
    assert ledger.balance("SKU-C", "L01") == 5
    before = ledger.all_balances()
    head_before = ledger.head_seq()

    try:
        wb.submit_sheet("neg-submit", sheet_id)
    except NegativeAdjustmentError as e:
        print(f"✓ 提交被拦截: {e}")

    assert wb.get_sheet(sheet_id).status == "open"
    assert ledger.all_balances() == before, "被拦截的提交改动了库存"
    assert ledger.head_seq() == head_before, "被拦截的提交占用了序号"
    print("✓ 整单保持待处理，L02 的盘盈也没有部分入账")

    # 冻结口径不变：补货到当前量足够后，差异仍是按 k 算的 -9/+2
    ledger.inbound("neg-topup", "SKU-C", "L01", 10)
    res = wb.submit_sheet("neg-submit2", sheet_id)
    deltas = {(a["sku"], a["location"]): a["delta"] for a in res["adjustments"]}
    assert deltas[("SKU-C", "L01")] == -9 and deltas[("SKU-C", "L02")] == 2
    # L01: 10 - 5 + 10 - 9 = 6；L02: 10 + 2 = 12
    assert ledger.balance("SKU-C", "L01") == 6
    assert ledger.balance("SKU-C", "L02") == 12
    print(f"✓ 补量后按原冻结口径提交: L01={ledger.balance('SKU-C', 'L01')}, "
          f"L02={ledger.balance('SKU-C', 'L02')}")
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 4：重叠盘点
# ---------------------------------------------------------------------------

def scenario_overlap(path: str) -> None:
    banner("场景4 重叠盘点整次拒绝；关闭/作废后位置可再盘")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)
    ledger.inbound("ov-in", "SKU-D", "L01", 4)
    ledger.inbound("ov-in2", "SKU-D", "L02", 4)

    first = wb.open_sheet("ov-open-1", [("SKU-D", "L01"), ("SKU-D", "L02")])
    print(f"盘点单 {first['sheet_id']} 占用 L01/L02")
    try:
        wb.open_sheet("ov-open-2", [("SKU-D", "L02"), ("SKU-D", "L03")])
    except OverlapError as e:
        print(f"✓ 重叠开单整次拒绝: {e}")

    # 被拒单据不得留下任何半成品
    rows = ledger.conn.execute("SELECT COUNT(*) c FROM count_sheets").fetchone()
    assert rows["c"] == 1
    rows = ledger.conn.execute("SELECT COUNT(*) c FROM active_count_locations").fetchone()
    assert rows["c"] == 2
    print("✓ 被拒开单未留下盘点单/占用半成品")

    # 不重叠的另一 SKU 可以并行盘
    other = wb.open_sheet("ov-open-3", [("SKU-E", "L01")])
    print(f"✓ 不同 SKU 同库位可并行盘点: 单 {other['sheet_id']}")

    # 第一张作废释放位置后，L02 可再开
    wb.void_sheet("ov-void", first["sheet_id"])
    again = wb.open_sheet("ov-open-4", [("SKU-D", "L02")])
    print(f"✓ 作废释放后 L02 重新开单: 单 {again['sheet_id']}")
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 5：录入更正
# ---------------------------------------------------------------------------

def scenario_entry_correction(path: str) -> None:
    banner("场景5 录入可更正，保留录入身份与全部版本")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)
    ledger.inbound("ec-in", "SKU-F", "L01", 5)
    opened = wb.open_sheet("ec-open", [("SKU-F", "L01")])
    sheet_id = opened["sheet_id"]

    wb.enter_count("ec-v1", sheet_id, "SKU-F", "L01", 4, "counter-甲")
    wb.enter_count("ec-v2", sheet_id, "SKU-F", "L01", 6, "counter-乙")
    wb.enter_count("ec-v3", sheet_id, "SKU-F", "L01", 5, "counter-甲")
    history = wb.entry_history(sheet_id, "SKU-F", "L01")
    for v in history:
        print(f"  v{v.version}: 盘得 {v.counted_qty} 录入人 {v.recorder}")
    assert [v.version for v in history] == [1, 2, 3]
    cur = wb.get_sheet(sheet_id).entries[("SKU-F", "L01")]
    assert cur.version == 3 and cur.counted_qty == 5 and cur.recorder == "counter-甲"
    print("✓ 版本链完整，当前版本为最后一次更正，身份保留")

    # 同号同文复读录入，不新增版本
    replay = wb.enter_count("ec-v3", sheet_id, "SKU-F", "L01", 5, "counter-甲")
    assert replay["version"] == 3
    assert len(wb.entry_history(sheet_id, "SKU-F", "L01")) == 3
    print("✓ 录入请求响应丢失重试复读，不产生新版本")
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 6：快照独立核对
# ---------------------------------------------------------------------------

def scenario_snapshot(path: str) -> None:
    banner("场景6 固定前缀快照与逐操作查询，当前量与快照独立核对")
    ledger = InventoryLedger(path)
    CountWorkbench(ledger)
    ledger.inbound("sn-1", "SKU-G", "L01", 10)
    ledger.inbound("sn-2", "SKU-G", "L02", 7)
    snap = ledger.retain_snapshot(ledger.head_seq(), "manual-k2")
    ledger.outbound("sn-3", "SKU-G", "L01", 4)
    ledger.transfer("sn-4", "SKU-G", "L02", "L03", 2)

    assert snap.balances == {("SKU-G", "L01"): 10, ("SKU-G", "L02"): 7}
    assert ledger.as_of(snap.prefix) == snap.balances
    assert ledger.balance("SKU-G", "L01") == 6
    assert ledger.balance("SKU-G", "L02") == 5
    assert ledger.balance("SKU-G", "L03") == 2
    print(f"✓ 快照 k={snap.prefix} 冻结后不受后续收发影响: {snap.balances}")

    ops = ledger.list_operations()
    for op in ops:
        print(f"  seq={op.commit_seq} {op.op_type} {op.request_no} "
              f"lines={len(op.lines)}")
    assert [op.commit_seq for op in ops] == [1, 2, 3, 4]
    assert ledger.get_operation(3).op_type == "outbound"
    print("✓ 逐操作查询与全局递增序号正确")

    # 同标签/同前缀复读
    again = ledger.retain_snapshot(snap.prefix, "manual-k2")
    assert again.balances == snap.balances
    print("✓ 快照物化幂等复读")
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 7：并发
# ---------------------------------------------------------------------------

def scenario_concurrency(path: str) -> None:
    banner("场景7 两客户端并发：同请求不重复调增，不同盘点单不抢先结算")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)
    ledger.inbound("cc-seed", "SKU-H", "L01", 100)

    # 7a. 两个独立连接/线程用同一请求号并发入库，只允许成功一次
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[Exception] = []

    def client_inbound(req_no: str) -> None:
        local = InventoryLedger(path)
        try:
            barrier.wait()
            rec = local.inbound(req_no, "SKU-H", "L01", 10)
            results.append(rec.commit_seq)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            local.close()

    t1 = threading.Thread(target=client_inbound, args=("cc-same-req",))
    t2 = threading.Thread(target=client_inbound, args=("cc-same-req",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ledger.balance("SKU-H", "L01") == 110, ledger.all_balances()
    print(f"✓ 并发同请求入库只生效一次: 成功序号 {results}, 异常 {len(errors)} 个，库存=110")

    # 7b. 两个不同请求并发出库，串行化后各自生效且不为负
    barrier2 = threading.Barrier(2)
    outs: list[int] = []

    def client_outbound(req_no: str) -> None:
        local = InventoryLedger(path)
        try:
            barrier2.wait()
            rec = local.outbound(req_no, "SKU-H", "L01", 30)
            outs.append(rec.commit_seq)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            local.close()

    t1 = threading.Thread(target=client_outbound, args=("cc-out-1",))
    t2 = threading.Thread(target=client_outbound, args=("cc-out-2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ledger.balance("SKU-H", "L01") == 50
    assert len(outs) == 2
    print(f"✓ 并发不同请求各生效一次: 序号 {sorted(outs)}，库存=50")

    # 7c. 两线程并发提交同一张盘点单（同请求号），调整只发布一次
    opened = wb.open_sheet("cc-open", [("SKU-H", "L01")])
    sheet_id = opened["sheet_id"]
    wb.enter_count("cc-count", sheet_id, "SKU-H", "L01", 49, "counter-甲")
    before = ledger.balance("SKU-H", "L01")
    submit_results: list[dict] = []
    barrier3 = threading.Barrier(2)

    def client_submit(req_no: str) -> None:
        local_ledger = InventoryLedger(path)
        local_wb = CountWorkbench(local_ledger)
        try:
            barrier3.wait()
            submit_results.append(local_wb.submit_sheet(req_no, sheet_id))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            local_ledger.close()

    # 7d. 第二张盘点单不能在该位置抢先：冻结期间占用不释放
    try:
        wb.open_sheet("cc-open-race", [("SKU-H", "L01")])
    except OverlapError:
        print("✓ 盘点冻结期间第二张盘点单无法在该位置开单")
    else:
        raise AssertionError("未关闭盘点单的位置不应被第二张单子占用")

    t1 = threading.Thread(target=client_submit, args=("cc-submit",))
    t2 = threading.Thread(target=client_submit, args=("cc-submit",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(submit_results) == 2, errors
    seqs = {r["adjust_commit_seq"] for r in submit_results}
    assert seqs == {submit_results[0]["adjust_commit_seq"]} and len(seqs) == 1
    expected = before + (49 - opened["lines"][0]["frozen_qty"])
    assert ledger.balance("SKU-H", "L01") == expected
    print(f"✓ 并发提交同一盘点单只发布一次调整 seq={seqs.pop()}，库存={expected}")

    # 提交关闭后位置才释放，允许重新开单
    reopened = wb.open_sheet("cc-open-after-close", [("SKU-H", "L01")])
    assert reopened["status"] == "open"
    wb.void_sheet("cc-void-after", reopened["sheet_id"])
    print("✓ 盘点关闭后位置释放，可重新开单")
    ledger.close()


# ---------------------------------------------------------------------------
# 场景 8：关闭重开持久化
# ---------------------------------------------------------------------------

def scenario_persistence(path: str) -> None:
    banner("场景8 关闭重开：中间盘点单与幂等回执仍然成立")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)
    ledger.inbound("ps-in", "SKU-I", "L01", 20)
    opened = wb.open_sheet("ps-open", [("SKU-I", "L01")])
    sheet_id = opened["sheet_id"]
    wb.enter_count("ps-count", sheet_id, "SKU-I", "L01", 18, "counter-甲")
    head_before_close = ledger.head_seq()
    ledger.close()

    # 重新打开：未提交盘点单仍在、位置仍被占用
    ledger2 = InventoryLedger(path)
    wb2 = CountWorkbench(ledger2)
    sheet = wb2.get_sheet(sheet_id)
    assert sheet.status == "open" and sheet.frozen_prefix == opened["frozen_prefix"]
    try:
        wb2.open_sheet("ps-open-overlap", [("SKU-I", "L01")])
    except OverlapError:
        print("✓ 重开后未关闭盘点单继续占用位置")

    # 旧请求号复读（open/enter 的幂等回执持久化）
    r_open = wb2.open_sheet("ps-open", [("SKU-I", "L01")])
    assert r_open["sheet_id"] == sheet_id and r_open["replayed"] is True
    r_enter = wb2.enter_count("ps-count", sheet_id, "SKU-I", "L01", 18, "counter-甲")
    assert r_enter["replayed"] is True and r_enter["version"] == 1
    assert ledger2.head_seq() == head_before_close
    print("✓ 重开后幂等回执可复读，序号未增加")

    res = wb2.submit_sheet("ps-submit", sheet_id)
    assert ledger2.balance("SKU-I", "L01") == 18  # 20 + (18-20)
    print(f"✓ 重开后提交成功，调整 seq={res['adjust_commit_seq']}，库存=18")
    ledger2.close()


# ---------------------------------------------------------------------------
# 场景 9：合成事件流 + 独立核账
# ---------------------------------------------------------------------------

def scenario_workbench_stress(path: str) -> None:
    banner("场景9 工作台确定性压力链路：拦截/复读/补量/作废/差异口径固定")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)

    ledger.inbound("ws-in-1", "SKU-J", "L01", 5)
    ledger.inbound("ws-in-2", "SKU-J", "L02", 8)
    opened = wb.open_sheet("ws-open", [("SKU-J", "L01"), ("SKU-J", "L02")])
    sheet_id = opened["sheet_id"]
    k = opened["frozen_prefix"]
    # 两个位置都盘成 0：差异分别为 -5、-8
    wb.enter_count("ws-c1", sheet_id, "SKU-J", "L01", 0, "counter-甲")
    wb.enter_count("ws-c2", sheet_id, "SKU-J", "L02", 0, "counter-甲")

    # 冻结后出库把 L01 打到 2：差异 -5 会穿零
    ledger.outbound("ws-out", "SKU-J", "L01", 3)
    assert ledger.balance("SKU-J", "L01") == 2
    for attempt in ("ws-submit", "ws-submit"):  # 同请求第二次是复读，仍为拦截态
        try:
            wb.submit_sheet(attempt, sheet_id)
        except NegativeAdjustmentError:
            pass
        else:
            raise AssertionError("应当被非负检查拦截")
    assert wb.get_sheet(sheet_id).status == "open"
    print("✓ 深度盘亏两次提交均被拦截，单据保持待处理且无调整落账")

    # 补 10 件后当前量足够；差异口径仍按冻结前缀 k（-5、-8），不受补量影响
    ledger.inbound("ws-topup", "SKU-J", "L01", 10)
    res = wb.submit_sheet("ws-submit-ok", sheet_id)
    assert {(a["sku"], a["location"]): a["delta"] for a in res["adjustments"]} == {
        ("SKU-J", "L01"): -5,
        ("SKU-J", "L02"): -8,
    }
    # L01: 5 - 3 + 10 - 5 = 7；L02: 8 - 8 = 0
    assert ledger.balance("SKU-J", "L01") == 7 and ledger.balance("SKU-J", "L02") == 0
    print(f"✓ 补量后按 k={k} 原口径提交: L01=7, L02=0")

    # 作废链路：新开一单直接作废，库存与账本序号都不变（无调整落账）
    head = ledger.head_seq()
    before = ledger.all_balances()
    opened2 = wb.open_sheet("ws-open-2", [("SKU-J", "L01")])
    voided = wb.void_sheet("ws-void", opened2["sheet_id"])
    assert voided["replayed"] is False
    assert wb.void_sheet("ws-void", opened2["sheet_id"])["replayed"] is True
    assert ledger.all_balances() == before and ledger.head_seq() == head
    print("✓ 作废释放位置且无调整、无账本序号；作废请求复读稳定")

    # 作废后同位置可立刻再开新单
    opened3 = wb.open_sheet("ws-open-3", [("SKU-J", "L01")])
    assert opened3["sheet_id"] != opened2["sheet_id"]
    print("✓ 作废后位置立即可重新盘点")
    wb.void_sheet("ws-void-2", opened3["sheet_id"])
    ledger.close()


def scenario_synthetic(path: str) -> None:
    banner("场景10 合成业务事件流（上限内）+ 独立重放核账")
    ledger = InventoryLedger(path)
    wb = CountWorkbench(ledger)
    wh = SyntheticWarehouse(ledger, wb, seed=20261118)
    wh.run(phases=6)
    s = wh.stats
    print(f"操作尝试 {s.operations_attempted}（提交 {s.operations_committed} / "
          f"拒绝 {s.operations_rejected}），幂等复读 {s.idempotent_replays}")
    print(f"盘点单 开 {s.sheets_opened} / 提交 {s.sheets_submitted} / "
          f"作废 {s.sheets_voided}，非负拦截 {s.blocked_submits}，"
          f"重叠拒绝 {s.overlaps_rejected}")
    assert s.operations_attempted <= 500 and s.sheets_opened <= 20
    cross_check_objects(ledger, wb)
    print("✓ 应用层 Python 镜像与账本全程一致；对象态快照核对通过")
    ledger.close()

    # 从原始事件独立重放核全账（纯读，失败前后逐行不变）
    report1 = audit(path)
    report2 = audit(path)
    assert json.dumps(report1, sort_keys=True) == json.dumps(report2, sort_keys=True)
    print(f"✓ 独立重放核账通过 {len(report1['checks'])} 项检查，"
          f"快照 {len(report1['snapshots'])} 份，盘点单 {len(report1['sheets'])} 张")
    print(f"  头序号={report1['head_seq']} 期末库存={report1['final_balances']}")


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="freeze-count-")
    paths = {
        name: os.path.join(tmpdir, f"{name}.db")
        for name in [
            "hand", "transfer", "negative", "overlap",
            "entry", "snapshot", "concurrent", "persist", "stress", "synthetic",
        ]
    }
    scenario_hand_calc(paths["hand"])
    scenario_transfer(paths["transfer"])
    scenario_negative_block(paths["negative"])
    scenario_overlap(paths["overlap"])
    scenario_entry_correction(paths["entry"])
    scenario_snapshot(paths["snapshot"])
    scenario_concurrency(paths["concurrent"])
    scenario_persistence(paths["persist"])
    scenario_workbench_stress(paths["stress"])
    scenario_synthetic(paths["synthetic"])

    banner("全部场景通过")
    print(f"数据库文件目录: {tmpdir}")
    for name, p in paths.items():
        print(f"  {name:10s} -> {p}")


if __name__ == "__main__":
    main()
