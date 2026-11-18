"""合成业务事件生成器。

用固定随机种子产生确定性的收发/移动事件流与盘点复盘资料，
驱动账本和工作台跑在约束上限内（≤10 SKU、≤10 库位、≤500 操作、≤20 盘点单）。

生成器同时维护一份纯 Python 镜像库存，作为应用层的第二独立事实来源；
每个阶段都可与账本核对。失败的非法操作（库存不足等）不推进镜像，
用于验证“失败前后逐行不变”。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from ledger import (
    IdempotencyConflict,
    InvalidOperation,
    InventoryLedger,
    OperationReceipt,
)
from workbench import (
    CountWorkbench,
    NegativeAdjustmentError,
    OverlapError,
    SheetStateError,
)

MAX_SKUS = 10
MAX_LOCATIONS = 10
MAX_OPERATIONS = 500
MAX_SHEETS = 20


@dataclass
class GeneratorStats:
    operations_attempted: int = 0
    operations_committed: int = 0
    operations_rejected: int = 0
    idempotent_replays: int = 0
    sheets_opened: int = 0
    sheets_submitted: int = 0
    sheets_voided: int = 0
    blocked_submits: int = 0
    overlaps_rejected: int = 0
    events: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.events.append(msg)


class SyntheticWarehouse:
    def __init__(self, ledger: InventoryLedger, workbench: CountWorkbench, seed: int = 20261118):
        self.ledger = ledger
        self.wb = workbench
        self.rng = random.Random(seed)
        self.skus = [f"SKU{i:02d}" for i in range(MAX_SKUS)]
        self.locations = [f"L{i:02d}" for i in range(MAX_LOCATIONS)]
        # 纯 Python 镜像：独立于账本表的第二事实来源
        self.mirror: dict[tuple[str, str], int] = {}
        self.stats = GeneratorStats()
        self._req_counter = 0
        self._open_sheets: dict[int, list[tuple[str, str]]] = {}

    def _req(self, prefix: str) -> str:
        self._req_counter += 1
        return f"{prefix}-{self._req_counter:05d}"

    def _budget_left(self) -> int:
        return MAX_OPERATIONS - self.stats.operations_attempted

    def _topup(self, sku: str, location: str, qty: int = 50) -> OperationReceipt:
        """受控补量：同样计入 500 操作预算。"""
        if self._budget_left() <= 0:
            raise RuntimeError("操作预算已用尽")
        req = self._req("biz-topup")
        self.stats.operations_attempted += 1
        receipt = self.ledger.inbound(req, sku, location, qty)
        self._mirror_apply((sku, location), qty)
        self.stats.operations_committed += 1
        return receipt

    def _mirror_balance(self, key: tuple[str, str]) -> int:
        return self.mirror.get(key, 0)

    def _mirror_apply(self, key: tuple[str, str], delta: int) -> None:
        new_qty = self.mirror.get(key, 0) + delta
        assert new_qty >= 0
        if new_qty == 0:
            self.mirror.pop(key, None)
        else:
            self.mirror[key] = new_qty

    # -- 普通收发移动 -----------------------------------------------------

    def random_business_event(self) -> None:
        if self.stats.operations_attempted >= MAX_OPERATIONS:
            return
        kind = self.rng.choices(
            ["inbound", "outbound", "transfer"], weights=[45, 35, 20], k=1
        )[0]
        sku = self.rng.choice(self.skus)
        req = self._req(f"biz-{kind}")
        self.stats.operations_attempted += 1

        if kind == "inbound":
            loc = self.rng.choice(self.locations)
            qty = self.rng.randint(1, 40)
            receipt = self.ledger.inbound(req, sku, loc, qty)
            self._mirror_apply((sku, loc), qty)
            self.stats.operations_committed += 1
            self.stats.log(f"入库 {qty} {sku}@{loc} -> seq {receipt.commit_seq}")

        elif kind == "outbound":
            loc = self.rng.choice(self.locations)
            # 30% 概率故意要一个可能超过库存的量，制造非法出库样本
            qty = (
                self.rng.randint(1, 60)
                if self.rng.random() < 0.3
                else self.rng.randint(1, max(1, self._mirror_balance((sku, loc))))
            )
            before = dict(self.mirror)
            try:
                receipt = self.ledger.outbound(req, sku, loc, qty)
            except InvalidOperation:
                self.stats.operations_rejected += 1
                self.stats.log(f"非法出库被拒 {qty} {sku}@{loc}（库存 {self._mirror_balance((sku, loc))}）")
                assert self.mirror == before, "失败出库改动了镜像"
                return
            self._mirror_apply((sku, loc), -qty)
            self.stats.operations_committed += 1
            self.stats.log(f"出库 {qty} {sku}@{loc} -> seq {receipt.commit_seq}")

        else:
            src, dst = self.rng.sample(self.locations, 2)
            qty = self.rng.randint(1, max(1, self._mirror_balance((sku, src)) + 5))
            before = dict(self.mirror)
            try:
                receipt = self.ledger.transfer(req, sku, src, dst, qty)
            except InvalidOperation:
                self.stats.operations_rejected += 1
                self.stats.log(f"非法移动被拒 {qty} {sku} {src}->{dst}")
                assert self.mirror == before, "失败移动改动了镜像"
                return
            self._mirror_apply((sku, src), -qty)
            self._mirror_apply((sku, dst), qty)
            self.stats.operations_committed += 1
            self.stats.log(f"移动 {qty} {sku} {src}->{dst} -> seq {receipt.commit_seq}")

    def verify_mirror(self) -> None:
        actual = self.ledger.all_balances()
        assert actual == self.mirror, (
            f"镜像与账本不一致:\n账本={actual}\n镜像={self.mirror}"
        )

    # -- 幂等故障注入 -----------------------------------------------------

    def inject_idempotency_probe(self) -> None:
        """同号同文复读、同号异文拒绝；复读不新增序号。"""
        sku, loc = self.rng.choice(self.skus), self.rng.choice(self.locations)
        req = self._req("idem")
        head_before = self.ledger.head_seq()
        r1 = self.ledger.inbound(req, sku, loc, 7)
        self._mirror_apply((sku, loc), 7)
        r2 = self.ledger.inbound(req, sku, loc, 7)  # 响应丢失后重试
        assert r1.commit_seq == r2.commit_seq and len(r2.lines) == len(r1.lines)
        assert self.ledger.head_seq() == head_before + 1
        self.stats.idempotent_replays += 1
        self.stats.log(f"幂等复读 {req} 命中 seq {r2.commit_seq}")

        try:
            self.ledger.inbound(req, sku, loc, 8)  # 同号异文
        except IdempotencyConflict:
            self.stats.log(f"同号异文拒绝 {req}")
        else:
            raise AssertionError("同号异文应当被拒绝")
        assert self.ledger.head_seq() == head_before + 1

    # -- 盘点 -------------------------------------------------------------

    def open_random_sheet(self, size: int = 3) -> Optional[int]:
        if self.stats.sheets_opened >= MAX_SHEETS:
            return None
        # 按冻结时刻（= 当前头序号）有库存的位置来选目标，
        # 保证盘点单里至少存在正冻结基准，能构造真实盘亏差异
        head = self.ledger.head_seq()
        frozen_balances = self.ledger.as_of(head)
        stocked_positions = [pos for pos, qty in frozen_balances.items() if qty > 0]
        if len(stocked_positions) >= size:
            chosen = self.rng.sample(stocked_positions, size)
        elif stocked_positions:
            chosen = list(stocked_positions)
            # 补足到 size：同 SKU 下挑空闲库位
            sku = chosen[0][0]
            for loc in self.locations:
                if len(chosen) >= size:
                    break
                if (sku, loc) not in chosen:
                    chosen.append((sku, loc))
        else:
            sku = self.rng.choice(self.skus)
            chosen = [(sku, loc) for loc in self.rng.sample(self.locations, size)]
        targets = sorted(set(chosen))
        req = self._req("wb-open")
        try:
            res = self.wb.open_sheet(req, targets)
        except OverlapError:
            self.stats.overlaps_rejected += 1
            self.stats.log(f"重叠盘点整次拒绝: {targets}")
            return None
        sheet_id = int(res["sheet_id"])
        self._open_sheets[sheet_id] = targets
        self.stats.sheets_opened += 1
        self.stats.log(
            f"开盘点单 {sheet_id} 冻结前缀 k={res['frozen_prefix']} 位置={targets}"
        )
        return sheet_id

    def enter_sheet_counts(self, sheet_id: int, recorder: str = "recorder-A") -> None:
        info = self.wb.get_sheet(sheet_id)
        k = info.frozen_prefix
        baseline = self.ledger.as_of(k)
        for line in info.lines:
            key = (line.sku, line.location)
            frozen = baseline.get(key, 0)
            # 合成复盘资料：以冻结口径实数为准，制造盘盈/盘亏/相符三类
            roll = self.rng.random()
            if roll < 0.35:
                counted = max(0, frozen + self.rng.choice([-2, -1, 1, 2, 3]))
            elif roll < 0.55 and frozen > 0:
                # 故意盘成 0 的深度盘亏：冻结后若发生出库，提交时当前量必为负，
                # 确定性驱动非负检查分支
                counted = 0
            else:
                counted = frozen
            req = self._req("wb-enter")
            self.wb.enter_count(req, sheet_id, line.sku, line.location, counted, recorder)
            self.stats.log(
                f"录入 单{sheet_id} {line.sku}@{line.location} 冻结={frozen} 盘={counted}"
            )

    def correct_one_entry(self, sheet_id: int) -> None:
        """录入更正：保留身份与版本链。"""
        info = self.wb.get_sheet(sheet_id)
        line = self.rng.choice(list(info.lines))
        key = (line.sku, line.location)
        cur = info.entries[key].counted_qty
        new_qty = max(0, cur + self.rng.choice([-1, 1]))
        req = self._req("wb-correct")
        res = self.wb.enter_count(
            req, sheet_id, line.sku, line.location, new_qty, "recorder-B"
        )
        assert res["version"] >= 2
        history = self.wb.entry_history(sheet_id, line.sku, line.location)
        assert history[-1].version == res["version"] and history[-1].counted_qty == new_qty
        self.stats.log(
            f"更正 单{sheet_id} {line.sku}@{line.location}: {cur} -> {new_qty} (v{res['version']})"
        )

    def submit_or_keep_open(self, sheet_id: int) -> str:
        req = self._req("wb-submit")
        try:
            res = self.wb.submit_sheet(req, sheet_id)
        except NegativeAdjustmentError as exc:
            self.stats.blocked_submits += 1
            self.stats.log(f"提交被非负检查拦截，单据保持待处理: {exc}")
            # 单据仍为 open，占用未释放
            assert self.wb.get_sheet(sheet_id).status == "open"
            return "blocked"
        # 镜像应用同一批差异
        for adj in res["adjustments"]:
            if adj["delta"] != 0:
                self._mirror_apply((adj["sku"], adj["location"]), adj["delta"])
        self.stats.sheets_submitted += 1
        self._open_sheets.pop(sheet_id, None)
        self.stats.log(
            f"提交盘点单 {sheet_id} 调整事件 seq={res['adjust_commit_seq']} "
            f"行数={len(res['adjustments'])}"
        )
        return "submitted"

    def replay_submit(self, sheet_id: int, request_no: str) -> dict[str, Any]:
        return self.wb.submit_sheet(request_no, sheet_id)

    def resettle_with_new_request(self, sheet_id: int) -> None:
        """不同请求号不得再次结算已关闭单据。"""
        try:
            self.wb.submit_sheet(self._req("wb-resettle"), sheet_id)
        except SheetStateError:
            self.stats.log(f"已关闭单据 {sheet_id} 拒绝再次结算")
            return
        raise AssertionError("已关闭盘点单不应允许再次结算")

    def void_sheet(self, sheet_id: int) -> None:
        before_mirror = dict(self.mirror)
        req = self._req("wb-void")
        res = self.wb.void_sheet(req, sheet_id)
        assert res["status"] == "voided" and not res["adjustments"]
        assert self.mirror == before_mirror, "作废不应改动库存"
        self.stats.sheets_voided += 1
        self._open_sheets.pop(sheet_id, None)
        self.stats.log(f"作废盘点单 {sheet_id}，位置已释放，无调整")

    # -- 整厂跑批 ---------------------------------------------------------

    def _attempt_overlap(self) -> None:
        """对一张在途盘点单发起重叠开单，预期整次拒绝。"""
        if not self._open_sheets:
            return
        busy_sheet = next(iter(self._open_sheets))
        targets = self._open_sheets[busy_sheet][:2]
        req = self._req("wb-overlap")
        try:
            self.wb.open_sheet(req, targets)
        except OverlapError:
            self.stats.overlaps_rejected += 1
            self.stats.log(f"重叠开单拒绝 {targets}（被单 {busy_sheet} 占用）")
        else:
            raise AssertionError("重叠开单应当被拒绝")

    def _force_block_then_resubmit(self, sheet_id: int) -> None:
        """确定性制造一次非负拦截，再补量按原冻结口径提交。"""
        info = self.wb.get_sheet(sheet_id)
        # 找一个冻结基准为正的位置，把盘点量录成 0 -> 差异必为负
        target = next((ln for ln in info.lines if ln.frozen_qty > 0), info.lines[0])
        self.wb.enter_count(
            self._req("wb-force-zero"),
            sheet_id,
            target.sku,
            target.location,
            0,
            "recorder-force",
        )
        # 把该位置当前量出库到 0（已为 0 则跳过），负差异提交必穿零
        cur = self.ledger.balance(target.sku, target.location)
        if cur > 0 and self._budget_left() > 0:
            self.stats.operations_attempted += 1
            rec = self.ledger.outbound(
                self._req("biz-force-out"), target.sku, target.location, cur
            )
            self._mirror_apply((target.sku, target.location), -cur)
            self.stats.operations_committed += 1
            self.stats.log(f"制造穿零出库 seq={rec.commit_seq}")
        assert self.ledger.balance(target.sku, target.location) == 0
        # 当前量为 0 而差异为负，提交必然被非负检查拦截
        try:
            self.wb.submit_sheet(self._req("wb-blocked-submit"), sheet_id)
        except NegativeAdjustmentError:
            self.stats.blocked_submits += 1
            self.stats.log(f"单 {sheet_id} 确定性非负拦截成功")
        else:
            raise AssertionError("该提交应当被非负检查拦截")
        assert self.wb.get_sheet(sheet_id).status == "open"

        info2 = self.wb.get_sheet(sheet_id)
        for line in info2.lines:
            # 补到“当前量 + 补量 >= 盘亏量”，保证整单非负检查通过
            cur = self.ledger.balance(line.sku, line.location)
            entry = info2.entries[(line.sku, line.location)]
            shortage = max(0, line.frozen_qty - entry.counted_qty - cur)
            if shortage > 0:
                receipt = self._topup(line.sku, line.location, shortage)
                self.stats.log(
                    f"补入库 {shortage} 后重提 {line.sku}@{line.location} "
                    f"seq={receipt.commit_seq}"
                )
        res = self.wb.submit_sheet(self._req("wb-submit2"), sheet_id)
        for adj in res["adjustments"]:
            if adj["delta"] != 0:
                self._mirror_apply((adj["sku"], adj["location"]), adj["delta"])
        self.stats.sheets_submitted += 1
        self._open_sheets.pop(sheet_id, None)
        self.stats.log(f"补量后提交盘点单 {sheet_id} seq={res['adjust_commit_seq']}")

    def run(self, phases: int = 6) -> None:
        for phase in range(phases):
            for _ in range(20):
                if self._budget_left() <= 0:
                    break
                self.random_business_event()
            self.verify_mirror()

            if self._budget_left() <= 0:
                break
            self.inject_idempotency_probe()
            self.verify_mirror()

            if self.stats.sheets_opened < MAX_SHEETS and self._budget_left() > 12:
                sheet_id = self.open_random_sheet(size=3)
                if sheet_id is not None:
                    self.enter_sheet_counts(sheet_id)
                    if self.rng.random() < 0.5:
                        self.correct_one_entry(sheet_id)
                    # 冻结后继续收发，驱动提交时的当前量非负检查
                    for _ in range(5):
                        if self._budget_left() <= 0:
                            break
                        self.random_business_event()

                    # 确定性重叠尝试：在途位置不允许第二张盘点单
                    self._attempt_overlap()

                    if phase == 1:
                        # 第二阶段确定性作废一张已录入单据，覆盖作废分支
                        self.void_sheet(sheet_id)
                    elif phase % 2 == 0:
                        # 一半单据走“拦截 -> 补量 -> 原口径重提”
                        self._force_block_then_resubmit(sheet_id)
                    else:
                        outcome = self.submit_or_keep_open(sheet_id)
                        if outcome == "submitted":
                            self.resettle_with_new_request(sheet_id)
                        else:
                            self._force_block_then_resubmit(sheet_id)
                    self.verify_mirror()

        # 收尾：先确定性作废一张残留单据，保证作废分支每次复跑都被覆盖；
        # 其余 open 单据按剩余预算补量提交或作废
        if self._open_sheets:
            self.void_sheet(next(iter(self._open_sheets)))
        for sheet_id in list(self._open_sheets):
            info = self.wb.get_sheet(sheet_id)
            needed = 0
            for line in info.lines:
                cur = self.ledger.balance(line.sku, line.location)
                entry = info.entries.get((line.sku, line.location))
                counted = entry.counted_qty if entry else line.frozen_qty
                needed += max(0, line.frozen_qty - counted - cur)
            if self._budget_left() < needed:
                self.void_sheet(sheet_id)
                continue
            for line in info.lines:
                cur = self.ledger.balance(line.sku, line.location)
                entry = info.entries[(line.sku, line.location)]
                shortage = max(0, line.frozen_qty - entry.counted_qty - cur)
                if shortage > 0:
                    receipt = self._topup(line.sku, line.location, shortage)
                    self.stats.log(
                        f"收尾补量 {shortage} {line.sku}@{line.location} seq={receipt.commit_seq}"
                    )
            req = self._req("wb-submit3")
            res = self.wb.submit_sheet(req, sheet_id)
            for adj in res["adjustments"]:
                if adj["delta"] != 0:
                    self._mirror_apply((adj["sku"], adj["location"]), adj["delta"])
            self.stats.sheets_submitted += 1
            self._open_sheets.pop(sheet_id, None)
            self.verify_mirror()

        self.verify_mirror()
        assert self.stats.operations_attempted <= MAX_OPERATIONS
        assert self.stats.sheets_opened <= MAX_SHEETS
        # 关键分支必须在每次复跑中都被实际覆盖，而不是靠随机碰巧
        assert self.stats.blocked_submits >= 1, "未覆盖非负拦截分支"
        assert self.stats.overlaps_rejected >= 1, "未覆盖重叠拒绝分支"
        assert self.stats.sheets_voided >= 1, "未覆盖作废分支"
        assert self.stats.idempotent_replays >= 1, "未覆盖幂等复读分支"
