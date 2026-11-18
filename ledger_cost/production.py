"""生产台账：批次登记、可用量、多级 DAG 谱系与发布后的修订/撤销。

业务规则（全部由合成数据定义，不模拟任何现实财务准则）：

* 台账按 ``batch_id`` 登记投入批号、投入整数克、各产出 ``lot_id`` 与整数克、
  不可回收损耗克；``Σ 产出克 + 损耗克 == 实际投入克``，零克产出禁止登记。
* 一个批号至多作为一次生产的投入来源；允许部分投入，累计消耗不得超过可用量。
* 谱系为多级有向无环图：禁止消费自己（未来）的产出，深度与规模受常量限制。
* 已发布批次不原地改写：更正产生新修订并置旧修订为 superseded；撤销置 cancelled。
  若下游已实际消耗被减掉的数量，整次更正/撤销拒绝，台账保持原样。
* 本模块不依赖任何成本数据即可独立登记与查询物料去向。
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .errors import (
    AlreadyExists,
    ConstraintViolation,
    NotFound,
    RejectedByConsumption,
)
from .models import BatchView, LotView, OutputView, Snapshot
from .storage import Storage

MAX_BATCHES = 20       # 最多生产批
MAX_OUTPUTS = 5        # 每批最多产出
MAX_DEPTH = 6          # 谱系最大深度（根批号深度 0，消费根的批深度 1）


class ProductionLedger:
    """生产台账服务。所有写操作在单个 SQLite 事务内完成校验与落库。"""

    def __init__(self, storage: Storage):
        self.db = storage

    # ================================================================ 期初原料

    def register_root_lot(self, lot_id: str, grams: int) -> None:
        """登记一笔期初原料批号（整数克，>0）。"""
        if not isinstance(lot_id, str) or not lot_id:
            raise ConstraintViolation("lot_id 必须为非空字符串")
        self._require_int(grams)
        if grams <= 0:
            raise ConstraintViolation(f"根批号 {lot_id} 期初克数必须为正整数")
        if self.db.lot_exists(lot_id):
            raise AlreadyExists(f"批号 {lot_id} 已存在")
        self.db.insert_root_lot(lot_id, grams)
        self.db.commit()

    # ================================================================ 批次登记

    def register_batch(
        self,
        batch_id: str,
        input_lot_id: str,
        input_grams: int,
        outputs: list[tuple[str, int]],
        loss_grams: int,
    ) -> int:
        """登记一个新生产批，返回修订号（首版恒为 1）。"""
        self._validate_shape(batch_id, input_lot_id, input_grams, outputs, loss_grams)
        if self.db.batch_exists(batch_id):
            raise AlreadyExists(f"生产批 {batch_id} 已存在")
        if len(self.db.all_batches()) >= MAX_BATCHES:
            raise ConstraintViolation(f"生产批数量上限 {MAX_BATCHES}")

        snapshot = self.build_snapshot()
        self._validate_input_availability(snapshot, batch_id, input_lot_id, input_grams)
        self._validate_outputs_new(snapshot, batch_id, outputs)
        depth = self._depth_for(snapshot, input_lot_id)
        if depth > MAX_DEPTH:
            raise ConstraintViolation(
                f"批 {batch_id} 深度 {depth} 超过谱系深度上限 {MAX_DEPTH}"
            )
        self._assert_no_cycle(snapshot, batch_id, input_lot_id, outputs)

        self.db.insert_batch(batch_id)
        revision_id = self.db.insert_revision(
            batch_id, 1, input_lot_id, input_grams, loss_grams
        )
        self.db.insert_outputs(revision_id, outputs)
        for lot_id, _ in outputs:
            self.db.insert_output_lot(lot_id, batch_id, revision_id)
        self.db.commit()
        return 1

    # ================================================================ 更正修订

    def correct_batch(
        self,
        batch_id: str,
        input_lot_id: Optional[str] = None,
        input_grams: Optional[int] = None,
        outputs: Optional[list[tuple[str, int]]] = None,
        loss_grams: Optional[int] = None,
    ) -> int:
        """对已发布批次建立新修订。

        参数为 ``None`` 表示沿用上一修订对应字段；``outputs`` 一旦给出即整体替换
        （保留的批号需重新列出并给出更正后克数，未列出即视为归零移除）。
        下游消耗拦截不通过时抛 :class:`RejectedByConsumption`，不写任何数据。
        """
        old = self._require_active_batch(batch_id)
        old_outputs = [
            (r["lot_id"], r["grams"]) for r in self.db.outputs_of(old["revision_id"])
        ]
        new_input_lot = input_lot_id if input_lot_id is not None else old["input_lot_id"]
        new_input_grams = input_grams if input_grams is not None else old["input_grams"]
        new_outputs = list(outputs) if outputs is not None else old_outputs
        new_loss = loss_grams if loss_grams is not None else old["loss_grams"]

        self._validate_shape(batch_id, new_input_lot, new_input_grams, new_outputs, new_loss)

        snapshot = self.build_snapshot()
        # 1) 下游实际消耗拦截：拟保留/新克数不得小于已消耗量
        blocking: list[tuple[str, int, int]] = []
        new_grams_map = dict(new_outputs)
        for lot_id, _old_grams in old_outputs:
            lot = snapshot.lots.get(lot_id)
            consumed = lot.consumed_grams if lot else 0
            kept = new_grams_map.get(lot_id, 0)
            if kept < consumed:
                blocking.append((lot_id, kept, consumed))
        if blocking:
            raise RejectedByConsumption(
                f"批 {batch_id} 的更正被下游实际消耗阻止: {blocking}", blocking
            )

        # 2) 投入侧校验（排除本批旧修订后的假设可用量）
        self._validate_input_availability(
            snapshot, batch_id, new_input_lot, new_input_grams
        )
        # 3) 产出批号：新批号不得与任何现存批号冲突，旧批号必须归属本批
        self._validate_outputs_correction(snapshot, batch_id, new_outputs)
        # 4) 深度与环
        depth = self._depth_for(snapshot, new_input_lot)
        if depth > MAX_DEPTH:
            raise ConstraintViolation(
                f"批 {batch_id} 更正后深度 {depth} 超过上限 {MAX_DEPTH}"
            )
        self._assert_no_cycle(snapshot, batch_id, new_input_lot, new_outputs)

        new_revision_no = int(old["revision_no"]) + 1
        new_revision_id = self.db.insert_revision(
            batch_id, new_revision_no, new_input_lot, new_input_grams, new_loss
        )
        self.db.insert_outputs(new_revision_id, new_outputs)
        old_lot_ids = {lot_id for lot_id, _ in old_outputs}
        for lot_id, _ in new_outputs:
            if lot_id not in old_lot_ids:
                self.db.insert_output_lot(lot_id, batch_id, new_revision_id)
        self.db.supersede_revision(old["revision_id"], new_revision_id)
        self.db.commit()
        return new_revision_no

    # ================================================================ 撤销

    def cancel_batch(self, batch_id: str) -> None:
        """撤销批次；任一出产批号已被下游实际消耗则整次拒绝。"""
        old = self._require_active_batch(batch_id)
        snapshot = self.build_snapshot()
        blocking = []
        for r in self.db.outputs_of(old["revision_id"]):
            lot = snapshot.lots.get(r["lot_id"])
            consumed = lot.consumed_grams if lot else 0
            if consumed > 0:
                blocking.append((r["lot_id"], 0, consumed))
        if blocking:
            raise RejectedByConsumption(
                f"批 {batch_id} 的撤销被下游实际消耗阻止: {blocking}", blocking
            )
        self.db.cancel_revision(old["revision_id"])
        self.db.commit()

    # ================================================================ 查询

    def available(self, lot_id: str) -> int:
        """批号当前可继续投入的整数克。"""
        snapshot = self.build_snapshot()
        lot = snapshot.lots.get(lot_id)
        if lot is None:
            raise NotFound(f"批号 {lot_id} 在当前发布切面不存在（可能已归零移除）")
        return lot.available_grams

    def get_lot(self, lot_id: str) -> LotView:
        snapshot = self.build_snapshot()
        if lot_id not in snapshot.lots:
            raise NotFound(f"批号 {lot_id} 在当前发布切面不存在")
        return snapshot.lots[lot_id]

    def get_batch(self, batch_id: str) -> BatchView:
        snapshot = self.build_snapshot()
        if batch_id not in snapshot.batches:
            raise NotFound(f"生产批 {batch_id} 不存在或已撤销")
        return snapshot.batches[batch_id]

    def list_revisions(self, batch_id: str) -> list[dict]:
        """返回批次全部历史修订（含 superseded/cancelled），旧记录持久保留。"""
        if not self.db.batch_exists(batch_id):
            raise NotFound(f"生产批 {batch_id} 不存在")
        result = []
        for rev in self.db.list_revisions(batch_id):
            result.append(
                {
                    "revision_no": rev["revision_no"],
                    "status": rev["status"],
                    "input_lot_id": rev["input_lot_id"],
                    "input_grams": rev["input_grams"],
                    "loss_grams": rev["loss_grams"],
                    "outputs": [
                        {"lot_id": r["lot_id"], "grams": r["grams"]}
                        for r in self.db.outputs_of(rev["revision_id"])
                    ],
                }
            )
        return result

    def trace_lot(self, lot_id: str) -> dict:
        """物料去向查询：来源、当前产量、被谁消耗多少、可用量、历史修订。"""
        lot_row = self.db.get_lot_row(lot_id)
        if lot_row is None:
            raise NotFound(f"批号 {lot_id} 从未登记")
        snapshot = self.build_snapshot()
        view = snapshot.lots.get(lot_id)

        consumers = [
            {
                "batch_id": b.batch_id,
                "revision_no": b.revision_no,
                "input_grams": b.input_grams,
                "status": "cancelled" if b.cancelled else "active",
            }
            for b in snapshot.batches.values()
            if b.input_lot_id == lot_id
        ]

        if lot_row["kind"] == "root":
            origin = {"kind": "root", "initial_grams": self.db.get_root_lot(lot_id)["grams"]}
        else:
            origin = {
                "kind": "output",
                "origin_batch": lot_row["origin_batch"],
                "revisions": self.list_revisions(lot_row["origin_batch"]),
            }
        return {
            "lot_id": lot_id,
            "origin": origin,
            "produced_grams": view.produced_grams if view else 0,
            "consumed_grams": view.consumed_grams if view else 0,
            "available_grams": view.available_grams if view else 0,
            "cancelled": view.cancelled if view else True,
            "consumers": consumers,
        }

    def descendants(self, batch_id: str) -> set[str]:
        """返回批次的全部后代批号（沿当前有效修订的产出-消耗边）。"""
        snapshot = self.build_snapshot()
        result: set[str] = set()
        queue: deque[str] = deque([batch_id])
        while queue:
            cur = queue.popleft()
            b = snapshot.batches.get(cur)
            if b is None:
                continue
            for out in b.output_lot_ids:
                if out in result:
                    continue
                result.add(out)
                child_lot = snapshot.lots.get(out)
                if child_lot and child_lot.origin_batch and child_lot.origin_batch != cur:
                    queue.append(child_lot.origin_batch)
        return result

    # ================================================================ 切面构建

    def build_snapshot(self) -> Snapshot:
        """构建当前发布切面的只读生产快照（成本仓唯一的数据入口）。"""
        active = self.db.all_active_revisions()
        batches: dict[str, BatchView] = {}
        produced: dict[str, int] = {}
        origin: dict[str, str] = {}

        for root in self.db.all_root_lots():
            produced[root["lot_id"]] = root["grams"]

        for rev in active:
            outputs = tuple(
                OutputView(r["lot_id"], r["grams"])
                for r in self.db.outputs_of(rev["revision_id"])
            )
            for o in outputs:
                # 同一批号只可能由一个批次出产，修订间直接覆盖为最新克数
                produced[o.lot_id] = o.grams
                origin[o.lot_id] = rev["batch_id"]
            batches[rev["batch_id"]] = BatchView(
                batch_id=rev["batch_id"],
                revision_no=rev["revision_no"],
                input_lot_id=rev["input_lot_id"],
                input_grams=rev["input_grams"],
                outputs=outputs,
                loss_grams=rev["loss_grams"],
                depth=0,  # 下面统一计算
                cancelled=False,
                superseded_by=None,
            )

        # 深度（沿投入边）；环由登记/更正拦截，这里可直接递归
        def depth_of(batch_id: str, stack: frozenset[str] = frozenset()) -> int:
            b = batches[batch_id]
            in_lot = b.input_lot_id
            parent = origin.get(in_lot)
            if parent is None:
                return 1  # 投入根批号
            if parent in stack:  # 防御性：理论上登记侧已拦截
                raise ConstraintViolation(f"检测到谱系环: {parent}")
            return depth_of(parent, stack | {batch_id}) + 1

        for bid in list(batches):
            b = batches[bid]
            batches[bid] = BatchView(
                batch_id=b.batch_id,
                revision_no=b.revision_no,
                input_lot_id=b.input_lot_id,
                input_grams=b.input_grams,
                outputs=b.outputs,
                loss_grams=b.loss_grams,
                depth=depth_of(bid),
                cancelled=False,
                superseded_by=None,
            )

        consumed: dict[str, int] = {}
        for b in batches.values():
            consumed[b.input_lot_id] = consumed.get(b.input_lot_id, 0) + b.input_grams

        roots = tuple(r["lot_id"] for r in self.db.all_root_lots())
        lots: dict[str, LotView] = {}
        for lot_id, grams in produced.items():
            lot_row = self.db.get_lot_row(lot_id)
            ob = origin.get(lot_id)
            lots[lot_id] = LotView(
                lot_id=lot_id,
                produced_grams=grams,
                consumed_grams=consumed.get(lot_id, 0),
                is_root=lot_row["kind"] == "root",
                origin_batch=ob,
                cancelled=False,
            )
        max_depth = max((b.depth for b in batches.values()), default=0)
        return Snapshot(batches=batches, lots=lots, roots=roots, max_depth=max_depth)

    # ================================================================ 内部校验

    @staticmethod
    def _require_int(*values: object) -> None:
        for v in values:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConstraintViolation(f"克数必须为整数，收到 {v!r}")

    def _validate_shape(
        self, batch_id: str, input_lot_id: str, input_grams: int,
        outputs: list[tuple[str, int]], loss_grams: int,
    ) -> None:
        if not isinstance(batch_id, str) or not batch_id:
            raise ConstraintViolation("batch_id 必须为非空字符串")
        if not isinstance(input_lot_id, str) or not input_lot_id:
            raise ConstraintViolation("input_lot_id 必须为非空字符串")
        if isinstance(input_grams, bool) or not isinstance(input_grams, int) or input_grams <= 0:
            raise ConstraintViolation("投入克数必须为正整数")
        if isinstance(loss_grams, bool) or not isinstance(loss_grams, int) or loss_grams < 0:
            raise ConstraintViolation("损耗克数必须为非负整数")
        if not outputs:
            raise ConstraintViolation(f"批 {batch_id} 至少需要一个非零产出")
        if len(outputs) > MAX_OUTPUTS:
            raise ConstraintViolation(f"每批产出不得超过 {MAX_OUTPUTS} 个")
        lot_ids = set()
        out_total = 0
        for lot_id, grams in outputs:
            if not isinstance(lot_id, str) or not lot_id:
                raise ConstraintViolation("产出 lot_id 必须为非空字符串")
            if lot_id in lot_ids:
                raise ConstraintViolation(f"批 {batch_id} 产出批号重复: {lot_id}")
            lot_ids.add(lot_id)
            if isinstance(grams, bool) or not isinstance(grams, int) or grams <= 0:
                raise ConstraintViolation(f"产出 {lot_id} 克数必须为正整数（禁止零克登记）")
            out_total += grams
        if out_total + loss_grams != input_grams:
            raise ConstraintViolation(
                f"批 {batch_id} 质量不平衡: 投入 {input_grams} != 产出 {out_total} + 损耗 {loss_grams}"
            )

    def _validate_input_availability(
        self, snapshot: Snapshot, batch_id: str, input_lot_id: str, input_grams: int,
    ) -> None:
        if input_lot_id not in snapshot.lots:
            raise NotFound(f"投入批号 {input_lot_id} 不存在")
        target_lot = snapshot.lots[input_lot_id]
        # “一个批号至多作为一次生产的投入来源”：排除本批自身的旧修订占用
        other_consumers = [
            b.batch_id
            for b in snapshot.batches.values()
            if b.input_lot_id == input_lot_id and b.batch_id != batch_id
        ]
        if other_consumers:
            raise ConstraintViolation(
                f"批号 {input_lot_id} 已被生产批 {other_consumers[0]} 用作投入，"
                "一个批号至多作为一次生产的投入来源"
            )
        own_old = snapshot.batches.get(batch_id)
        own_hold = own_old.input_grams if own_old and own_old.input_lot_id == input_lot_id else 0
        available = target_lot.produced_grams - target_lot.consumed_grams + own_hold
        if input_grams > available:
            raise ConstraintViolation(
                f"批号 {input_lot_id} 可用量不足: 需要 {input_grams}，"
                f"可用 {available}（产量 {target_lot.produced_grams} - "
                f"已耗 {target_lot.consumed_grams} + 本批原占用 {own_hold}）"
            )

    def _validate_outputs_new(
        self, snapshot: Snapshot, batch_id: str, outputs: list[tuple[str, int]]
    ) -> None:
        for lot_id, _ in outputs:
            if lot_id in snapshot.lots or self.db.lot_exists(lot_id):
                raise AlreadyExists(f"产出批号 {lot_id} 已存在，必须使用新批号")

    def _validate_outputs_correction(
        self, snapshot: Snapshot, batch_id: str, outputs: list[tuple[str, int]]
    ) -> None:
        own_old = snapshot.batches[batch_id]
        owned = set(own_old.output_lot_ids)
        for lot_id, _ in outputs:
            if lot_id in owned:
                continue
            if lot_id in snapshot.lots or self.db.lot_exists(lot_id):
                raise AlreadyExists(f"产出批号 {lot_id} 已被其他批次/根批号占用")

    def _depth_for(self, snapshot: Snapshot, input_lot_id: str) -> int:
        lot = snapshot.lots[input_lot_id]
        if lot.origin_batch is None:
            return 1
        return snapshot.batches[lot.origin_batch].depth + 1

    def _assert_no_cycle(
        self,
        snapshot: Snapshot,
        batch_id: str,
        input_lot_id: str,
        outputs: list[tuple[str, int]],
    ) -> None:
        # 新投入批号不得是本批既有后代（消费自己的产出即形成环）
        if batch_id in snapshot.batches:
            descendants = self.descendants(batch_id)
            if input_lot_id in descendants:
                raise ConstraintViolation(
                    f"批 {batch_id} 不能消费自己的后代产出 {input_lot_id}"
                )
        # 新产出批号也不得是本批祖先链上已存在的批号
        ancestor_lots: set[str] = set()
        cur_lot = input_lot_id
        guard = 0
        while cur_lot is not None and guard <= MAX_DEPTH + 2:
            ancestor_lots.add(cur_lot)
            lot = snapshot.lots.get(cur_lot)
            if lot is None or lot.origin_batch is None:
                break
            cur_batch = snapshot.batches[lot.origin_batch]
            cur_lot = cur_batch.input_lot_id
            guard += 1
        for lot_id, _ in outputs:
            if lot_id in ancestor_lots:
                raise ConstraintViolation(
                    f"产出批号 {lot_id} 位于批 {batch_id} 的祖先链上，禁止成环"
                )

    def _require_active_batch(self, batch_id: str):
        if not self.db.batch_exists(batch_id):
            raise NotFound(f"生产批 {batch_id} 不存在")
        rev = self.db.latest_revision(batch_id)
        if rev is None or rev["status"] != "active":
            raise ConstraintViolation(f"生产批 {batch_id} 已撤销，不能再修订")
        return rev
