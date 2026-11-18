"""独立逐事件解释器 / 不变量核验器。

独立性体现在：
- 不 import 账本与承诺仓的任何业务类，只读取事件日志 led_event / cmt_event；
- 用自己的内存结构逐事件重放，重放结果与库内物化表逐项比对；
- 每个事件后立即检查全网守恒与占额不等式，定位到具体事件序号。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict

from .errors import VerificationError


def _load_events(conn: sqlite3.Connection, table: str):
    return [
        (r[0], r[1], r[2], json.loads(r[3]))
        for r in conn.execute(
            f"SELECT seq, event_id, kind, payload FROM {table} ORDER BY seq"
        )
    ]


def _receipt_hash(namespace: str, transfer_id: str, qty: int) -> str:
    return hashlib.sha256(
        f"{namespace}|{transfer_id}|{qty}".encode()
    ).hexdigest()


class _LedgerReplay:
    """账本事件的独立重放机。"""

    def __init__(self):
        self.spot: dict[tuple[str, str], int] = defaultdict(int)
        self.reserved: dict[tuple[str, str], int] = defaultdict(int)
        self.flow_in = defaultdict(int)
        self.flow_out = defaultdict(int)
        self.flow_lost = defaultdict(int)
        self.transfers: dict[str, dict] = {}
        self.receipts: dict[str, tuple[str, str]] = {}
        self.skus: set[str] = set()

    def apply(self, seq: int, kind: str, p: dict) -> None:
        sku = p.get("sku")
        if sku:
            self.skus.add(sku)

        if kind == "INBOUND":
            self.spot[(p["warehouse_id"], sku)] += p["qty"]
            self.flow_in[sku] += p["qty"]

        elif kind == "OUTBOUND":
            key = (p["warehouse_id"], sku)
            if self.spot[key] - self.reserved[key] < p["qty"]:
                raise VerificationError(f"LEV#{seq} 出库击穿未冻结现货")
            self.spot[key] -= p["qty"]
            self.flow_out[sku] += p["qty"]

        elif kind == "TRANSFER_CREATED":
            tid = p["transfer_id"]
            if tid in self.transfers:
                raise VerificationError(f"LEV#{seq} 调拨 ID 重复 {tid}")
            key = (p["source_wh"], sku)
            if self.spot[key] - self.reserved[key] < p["qty"]:
                raise VerificationError(f"LEV#{seq} 预留击穿现货")
            self.reserved[key] += p["qty"]
            self.transfers[tid] = {
                "sku": sku, "source": p["source_wh"], "target": p["target_wh"],
                "qty": p["qty"], "shipped": 0, "received": 0, "lost": 0,
                "status": "RESERVED", "eta": p.get("expected_receipt_date"),
            }

        elif kind == "TRANSFER_SHIPPED":
            t = self.transfers[p["transfer_id"]]
            if t["status"] != "RESERVED":
                raise VerificationError(f"LEV#{seq} 非法发出状态 {t['status']}")
            key = (t["source"], t["sku"])
            self.reserved[key] -= t["qty"]
            self.spot[key] -= t["qty"]
            t["shipped"] = t["qty"]
            t["status"] = "IN_TRANSIT"
            if p.get("expected_receipt_date"):
                t["eta"] = p["expected_receipt_date"]

        elif kind in ("TRANSFER_RECEIVED", "TRANSFER_LOST"):
            tid = p["transfer_id"]
            t = self.transfers[tid]
            qty = p["qty"]
            if t["status"] != "IN_TRANSIT":
                raise VerificationError(f"LEV#{seq} 非在途调拨发生 {kind}")
            outstanding = t["shipped"] - t["received"] - t["lost"]
            if qty > outstanding:
                raise VerificationError(
                    f"LEV#{seq} {kind} {qty} 超过本调拨未结在途 {outstanding}"
                )
            # 独立复核凭据幂等：同文指纹、同 ID 不得异文
            ns = "RECEIPT" if kind == "TRANSFER_RECEIVED" else "LOSS"
            digest = _receipt_hash(ns, tid, qty)
            rid = p["receipt_id"]
            if rid in self.receipts:
                if self.receipts[rid] != (tid, digest):
                    raise VerificationError(
                        f"LEV#{seq} 凭据 {rid} 异文重放（必须被业务层拒绝）"
                    )
                raise VerificationError(
                    f"LEV#{seq} 同文凭据 {rid} 产生了第二条事件（幂等失效）"
                )
            self.receipts[rid] = (tid, digest)
            if kind == "TRANSFER_RECEIVED":
                self.spot[(t["target"], t["sku"])] += qty
                t["received"] += qty
            else:
                self.flow_lost[t["sku"]] += qty
                t["lost"] += qty
            if t["received"] + t["lost"] == t["shipped"]:
                t["status"] = "CLOSED"

        elif kind == "TRANSFER_CANCELLED":
            t = self.transfers[p["transfer_id"]]
            if t["status"] != "RESERVED":
                raise VerificationError(f"LEV#{seq} 发出后取消（必须被拒绝）")
            self.reserved[(t["source"], t["sku"])] -= t["qty"]
            t["status"] = "CANCELLED"

        elif kind == "TRANSFER_ETA_CHANGED":
            t = self.transfers[p["transfer_id"]]
            if t["status"] != "IN_TRANSIT":
                raise VerificationError(f"LEV#{seq} 非在途调拨改 ETA")
            t["eta"] = p["new_expected_receipt_date"]

        else:
            raise VerificationError(f"LEV#{seq} 未知事件类型 {kind}")

        self._check_invariants(seq)

    def _check_invariants(self, seq: int) -> None:
        # 1) 非负且预留是现货子集
        for key, spot in self.spot.items():
            if spot < 0 or self.reserved[key] < 0:
                raise VerificationError(f"LEV#{seq} 出现负库存 {key}")
            if self.reserved[key] > spot:
                raise VerificationError(
                    f"LEV#{seq} 预留 {self.reserved[key]} 超过现货 {spot}"
                )
        # 2) 逐 SKU 全网守恒（题目口径）：
        #    期初+入库 = 现货 + 在途 + 累计损耗 + 累计对外出库
        #    其中预留是现货的子集，绝不再加进总量
        for sku in self.skus:
            spot_sum = sum(v for (_, s), v in self.spot.items() if s == sku)
            res_sum = sum(v for (_, s), v in self.reserved.items() if s == sku)
            if res_sum > spot_sum:  # 子集关系已在上面逐仓检查，这里仅备忘
                raise VerificationError(f"LEV#{seq} 预留非现货子集：{sku}")
            transit_sum = sum(
                t["shipped"] - t["received"] - t["lost"]
                for t in self.transfers.values() if t["sku"] == sku
            )
            lhs = self.flow_in[sku]
            rhs = (spot_sum + transit_sum
                   + self.flow_lost[sku] + self.flow_out[sku])
            if lhs != rhs:
                raise VerificationError(
                    f"LEV#{seq} SKU {sku} 守恒断裂：入库 {lhs} != "
                    f"现货 {spot_sum}(含预留 {res_sum}) + 在途 {transit_sum} "
                    f"+ 损耗 {self.flow_lost[sku]} + 出库 {self.flow_out[sku]}"
                )


class _CommitmentReplay:
    """承诺仓事件的独立重放机（份额状态机自己实现一份）。"""

    HELD, CONVERTED, SHIPPED, INVALID, SUPERSEDED = (
        "HELD", "CONVERTED", "SHIPPED", "INVALID", "SUPERSEDED",
    )

    def __init__(self):
        self.transit_days: dict[str, int] = {}
        self.orders: dict[str, dict] = {}
        self.portions: dict[str, dict] = {}

    def _require_held(self, ref_id: str, need_qty: int, ctx: str, seq: int) -> dict:
        """事件载荷引用的源份额在事件发生时点必须是有足量余额的 HELD。"""
        pt = self.portions.get(ref_id)
        if pt is None or pt["state"] != self.HELD or pt["qty"] < need_qty:
            raise VerificationError(f"CEV#{seq} {ctx} 来源状态/数量非法：{ref_id}")
        return pt

    def apply(self, seq: int, kind: str, p: dict) -> None:
        if kind == "TRANSIT_DAYS_SET":
            self.transit_days[p["warehouse_id"]] = p["transit_days"]

        elif kind == "ORDER_REGISTERED":
            self.orders[p["order_id"]] = {
                "sku": p["sku"], "qty": p["qty"],
                "whs": set(p["accepted_whs"]),
                "latest": p["latest_date"], "priority": p["priority"],
            }

        elif kind == "ALLOCATION":
            oid = p["order_id"]
            total = sum(x["qty"] for x in p["portions"])
            # 全有全无按净缺口：订单量 - 已发 - 事件前存活的未发份额
            already_shipped = sum(
                pt["qty"] for pt in self.portions.values()
                if pt["order"] == oid and pt["state"] == self.SHIPPED
            )
            already_live = sum(
                pt["qty"] for pt in self.portions.values()
                if pt["order"] == oid
                and pt["state"] in (self.HELD, self.CONVERTED)
            )
            want_total = self.orders[oid]["qty"] - already_shipped - already_live
            if total != want_total or total <= 0:
                raise VerificationError(
                    f"CEV#{seq} 订单 {oid} 违反全有全无/净缺口规则：占 {total} / "
                    f"应占 {want_total}（已发 {already_shipped}，存活 {already_live}）"
                )
            for x in p["portions"]:
                if x["portion_id"] in self.portions:
                    raise VerificationError(f"CEV#{seq} 份额 ID 重复")
                self.portions[x["portion_id"]] = {
                    "order": oid, "sku": p["sku"],
                    "type": x["source_type"], "wh": x["warehouse_id"],
                    "transfer": x["transfer_id"], "qty": x["qty"],
                    "expected": x["expected_date"], "state": self.HELD,
                    "root": x["portion_id"],
                }

        elif kind == "PORTIONS_CONVERTED":
            for c in p["conversions"]:
                if "split_from" in c:
                    src = self._require_held(
                        c["split_from"], c["qty"], "转换拆分", seq)
                    src["qty"] -= c["qty"]
                    self.portions[c["portion_id"]] = {
                        "order": c["order_id"], "sku": src["sku"],
                        "type": "SPOT", "wh": c["warehouse_id"],
                        "transfer": c["transfer_id"], "qty": c["qty"],
                        "expected": src["expected"],
                        "state": self.CONVERTED, "root": c["portion_id"],
                    }
                else:
                    pt = self.portions[c["portion_id"]]
                    if pt["state"] != self.HELD or pt["qty"] != c["qty"]:
                        raise VerificationError(
                            f"CEV#{seq} 整份转换状态/数量非法"
                        )
                    pt["state"] = self.CONVERTED
                    pt["type"] = "SPOT"

        elif kind == "PORTIONS_INVALIDATED":
            reason = p.get("reason", "LOSS")
            for x in p["invalid_portions"]:
                if "split_from" in x:
                    # 部分失效（部分报损）：旧份额缩减保留 HELD，另立 INVALID 同谱系
                    src = self._require_held(
                        x["split_from"], x["qty"], "失效拆分", seq)
                    src["qty"] -= x["qty"]
                    self.portions[x["portion_id"]] = {
                        **src, "qty": x["qty"], "state": self.INVALID,
                        "root": x["portion_id"],
                    }
                else:
                    pt = self.portions[x["portion_id"]]
                    if pt["state"] != self.HELD:
                        raise VerificationError(
                            f"CEV#{seq} 失效份额 {x['portion_id']} 非 HELD"
                        )
                    if reason == "LOSS" and x["qty"] != pt["qty"]:
                        raise VerificationError(
                            f"CEV#{seq} 报损整份失效数量与份额不符"
                        )
                    pt["state"] = self.INVALID

        elif kind == "CUSTOMER_SHIPPED":
            for x in p["portions"]:
                if "split_from" in x:
                    src = self._require_held(
                        x["split_from"], x["qty"], "发货拆分", seq)
                    src["qty"] -= x["qty"]
                    self.portions[x["portion_id"]] = {
                        **src, "qty": x["qty"], "state": self.SHIPPED,
                        "root": x["portion_id"],
                    }
                else:
                    pt = self.portions[x["portion_id"]]
                    if pt["type"] != "SPOT":
                        raise VerificationError(
                            f"CEV#{seq} 从在途份额直接发货（必须被拒绝）"
                        )
                    if pt["state"] not in (self.HELD, self.CONVERTED):
                        raise VerificationError(
                            f"CEV#{seq} 对 {pt['state']} 份额发货"
                        )
                    pt["state"] = self.SHIPPED

        elif kind == "REBUILD_STARTED":
            for pid in p["superseded_portions"]:
                pt = self.portions[pid]
                if pt["state"] not in (self.HELD, self.CONVERTED):
                    raise VerificationError(f"CEV#{seq} 重算取代了 {pt['state']} 份额")
                pt["state"] = self.SUPERSEDED

        else:
            raise VerificationError(f"CEV#{seq} 未知承诺事件 {kind}")


def verify(conn: sqlite3.Connection) -> dict:
    """全量核验。通过返回统计摘要；任何不变量断裂抛 VerificationError。"""
    led = _LedgerReplay()
    for seq, _eid, kind, payload in _load_events(conn, "led_event"):
        led.apply(seq, kind, payload)

    # ---- 重放结果 vs 账本物化表逐项比对 ----
    for wh, sku, spot, reserved in conn.execute(
        "SELECT warehouse_id, sku, spot, reserved FROM led_balance"
    ):
        if led.spot[(wh, sku)] != spot or led.reserved[(wh, sku)] != reserved:
            raise VerificationError(
                f"余额表与事件重放不符：{wh}/{sku} "
                f"表=({spot},{reserved}) 重放=({led.spot[(wh, sku)]},"
                f"{led.reserved[(wh, sku)]})"
            )
    for r in conn.execute(
        """SELECT transfer_id, shipped_qty, received_qty, lost_qty, status,
                  expected_receipt_date FROM led_transfer"""
    ):
        t = led.transfers[r[0]]
        if (t["shipped"], t["received"], t["lost"], t["status"], t["eta"]) != \
                (r[1], r[2], r[3], r[4], r[5]):
            raise VerificationError(f"调拨表与事件重放不符：{r[0]}")
    for sku, ib, ob, lq in conn.execute(
        "SELECT sku, inbound_qty, outbound_qty, lost_qty FROM led_sku_flow"
    ):
        if (led.flow_in[sku], led.flow_out[sku], led.flow_lost[sku]) != (ib, ob, lq):
            raise VerificationError(f"汇总索引与事件重放不符：{sku}")

    # ---- 承诺仓事件独立重放 ----
    cmt = _CommitmentReplay()
    cmt_events = _load_events(conn, "cmt_event")
    last_anchor = 0
    max_led_seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0) FROM led_event"
    ).fetchone()[0]
    for seq, _eid, kind, payload in cmt_events:
        anchor = conn.execute(
            "SELECT ledger_anchor_seq FROM cmt_event WHERE seq=?", (seq,)
        ).fetchone()[0]
        if anchor < last_anchor or anchor > max_led_seq:
            raise VerificationError(
                f"CEV#{seq} 账本锚点异常：{anchor}（上一锚点 {last_anchor}）"
            )
        last_anchor = anchor
        cmt.apply(seq, kind, payload)

    # ---- 重放份额 vs cmt_portion 物化表比对 ----
    db_parts = {
        r[0]: r for r in conn.execute(
            """SELECT portion_id, order_id, sku, source_type, warehouse_id,
                      transfer_id, qty, expected_date, state, root_portion_id
                 FROM cmt_portion"""
        )
    }
    if set(db_parts) != set(cmt.portions):
        raise VerificationError("份额集合：事件重放与物化表不一致")
    for pid, pt in cmt.portions.items():
        r = db_parts[pid]
        actual = (r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])
        expect = (pt["order"], pt["sku"], pt["type"], pt["wh"],
                  pt["transfer"], pt["qty"], pt["expected"], pt["state"])
        if actual != expect:
            raise VerificationError(f"份额 {pid} 重放与物化表不符：{actual} != {expect}")

    # ---- 来源占额不等式（对账本事实表现场计算）----
    # 1) 现货占额 <= 账本未冻结现货
    spot_occ = defaultdict(int)
    for pid, pt in cmt.portions.items():
        if pt["type"] == "SPOT" and pt["state"] in (
            _CommitmentReplay.HELD, _CommitmentReplay.CONVERTED
        ):
            spot_occ[(pt["wh"], pt["sku"])] += pt["qty"]
    for (wh, sku), occ in spot_occ.items():
        row = conn.execute(
            "SELECT spot, reserved FROM led_balance WHERE warehouse_id=? AND sku=?",
            (wh, sku),
        ).fetchone()
        spot, reserved = row if row else (0, 0)
        if occ > spot - reserved:
            raise VerificationError(
                f"现货超占：{wh}/{sku} 占 {occ} > 未冻结 {spot - reserved}"
            )

    # 2) 在途占额 <= 该特定调拨未结在途（不能消费别的调拨）
    tr_occ = defaultdict(int)
    for pt in cmt.portions.values():
        if pt["type"] == "TRANSIT" and pt["state"] == _CommitmentReplay.HELD:
            tr_occ[pt["transfer"]] += pt["qty"]
    for tid, occ in tr_occ.items():
        r = conn.execute(
            "SELECT shipped_qty, received_qty, lost_qty FROM led_transfer WHERE transfer_id=?",
            (tid,),
        ).fetchone()
        outstanding = r[0] - r[1] - r[2]
        if occ > outstanding:
            raise VerificationError(
                f"在途超占：{tid} 占 {occ} > 未结在途 {outstanding}"
            )

    # 3) 已 CLOSED 调拨不得残留 HELD 在途份额（签收后身份必须转换）
    for tid, in conn.execute(
        "SELECT transfer_id FROM led_transfer WHERE status='CLOSED'"
    ):
        held = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM cmt_portion WHERE transfer_id=? "
            "AND source_type='TRANSIT' AND state='HELD'",
            (tid,),
        ).fetchone()[0]
        if held:
            raise VerificationError(f"{tid} 已结清却残留 HELD 在途承诺 {held}")

    # 4) 转换总量不超过该调拨实际签收量（不额外释放在途额度）
    conv = defaultdict(int)
    for pt in cmt.portions.values():
        if pt["state"] in (_CommitmentReplay.CONVERTED, _CommitmentReplay.SHIPPED) \
                and pt["transfer"] and pt["type"] == "SPOT":
            conv[pt["transfer"]] += pt["qty"]
    for tid, q in conv.items():
        received = conn.execute(
            "SELECT received_qty FROM led_transfer WHERE transfer_id=?", (tid,)
        ).fetchone()[0]
        if q > received:
            raise VerificationError(
                f"{tid} 转换份额 {q} 超过实际签收 {received}"
            )

    # 5) 订单级份额闭合 + 状态机一致 + 已发量不可复活
    #    状态只由两类活份额决定：HELD/CONVERTED（未发）与 SHIPPED（已发）；
    #    INVALID/SUPERSEDED 是已终结的历史份额，不参与状态推导
    for oid, o in cmt.orders.items():
        states = defaultdict(int)
        for pt in cmt.portions.values():
            if pt["order"] == oid:
                states[pt["state"]] += pt["qty"]
        live = states[_CommitmentReplay.HELD] + states[_CommitmentReplay.CONVERTED]
        shipped = states[_CommitmentReplay.SHIPPED]
        if live + shipped > o["qty"]:
            raise VerificationError(f"{oid} 有效份额超过订单量")
        db_status = conn.execute(
            "SELECT status FROM cmt_order WHERE order_id=?", (oid,)
        ).fetchone()[0]
        if shipped == o["qty"]:
            want = "SHIPPED"
        elif live == 0:
            # 未发缺口裸露（失效/被取代后尚未补回）：统一 OPEN，等待重新分配
            want = "OPEN"
        elif live + shipped == o["qty"]:
            # 缺口已被活份额覆盖：整单未发是 ALLOCATED，发过一部分则 PARTIALLY_SHIPPED
            want = "ALLOCATED" if shipped == 0 else "PARTIALLY_SHIPPED"
        else:
            want = "OPEN"
        if db_status != want:
            raise VerificationError(
                f"{oid} 状态 {db_status} 与份额重放推导 {want} 不符"
            )

    # 6) 每笔客户发货都能在账本对外出库事件中找到对应扣减
    led_kinds = {
        r[1]: r[2] for r in conn.execute(
            "SELECT seq, event_id, kind FROM led_event"
        )
    }
    for _seq, _eid, kind, p in cmt_events:
        if kind == "CUSTOMER_SHIPPED":
            row = conn.execute(
                "SELECT payload FROM led_event WHERE event_id=?",
                (p["ledger_event_id"],),
            ).fetchone()
            if row is None or led_kinds.get(p["ledger_event_id"]) != "OUTBOUND":
                raise VerificationError(
                    f"客户发货 {p['order_id']} 缺少账本 OUTBOUND 事件"
                )
            lp = json.loads(row[0])
            if lp.get("warehouse_id") != p["warehouse_id"] or \
                    lp.get("qty") != p["qty"]:
                raise VerificationError(
                    f"客户发货 {p['order_id']} 与账本出库事件内容不符"
                )

    return {
        "ledger_events": len(_load_events(conn, "led_event")),
        "commitment_events": len(cmt_events),
        "transfers": len(led.transfers),
        "orders": len(cmt.orders),
        "portions": len(cmt.portions),
        "skus_checked": sorted(led.skus),
    }
