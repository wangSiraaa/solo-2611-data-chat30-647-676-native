"""独立批次成本仓服务。

与生产台账的边界：成本仓通过 :meth:`ProductionLedger.build_snapshot` 取得只读
生产切面后才能计算，内部没有任何写产量的路径，也不能从成本表反推产量。
所有切面与产品持久化保留，关闭重开后可按 ``cut_id`` 查询历史版本。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .engine import (
    closure,
    changed_batches,
    explain_unit_change,
    full_compute,
    incremental_compute,
)
from .errors import NotFound, VersionError
from .models import BatchCostView, CostProduct, Snapshot
from .production import ProductionLedger
from .storage import Storage, int_to_frac

ZERO = Fraction(0)


class CostWarehouse:
    def __init__(self, storage: Storage, ledger: ProductionLedger):
        self.db = storage
        self.ledger = ledger

    # ================================================================ 版本化输入

    def set_opening_cost(self, version: str, lot_id: str, total_cost_cents: int) -> None:
        """登记某版本下根原料的整数分总成本。"""
        if not isinstance(total_cost_cents, int) or isinstance(total_cost_cents, bool):
            raise VersionError("期初总成本必须为整数分")
        if total_cost_cents < 0:
            raise VersionError("期初总成本不能为负")
        if not self.db.get_root_lot(lot_id):
            raise NotFound(f"根批号 {lot_id} 未在生产台账登记")
        self.db.upsert_root_cost(version, lot_id, Fraction(total_cost_cents))
        self.db.commit()

    def set_processing_fee(self, version: str, batch_id: str, fee_cents: int) -> None:
        """登记某版本下生产批的额外加工费（整数分，允许为 0）。"""
        if not isinstance(fee_cents, int) or isinstance(fee_cents, bool) or fee_cents < 0:
            raise VersionError("加工费必须为非负整数分")
        if not self.db.batch_exists(batch_id):
            raise NotFound(f"生产批 {batch_id} 未在生产台账登记")
        self.db.upsert_fee(version, batch_id, Fraction(fee_cents))
        self.db.commit()

    def set_reference_value(self, version: str, lot_id: str, unit_value_cents: int) -> None:
        """登记某版本下产出批的单位参考价值（整数分/克，允许为零）。"""
        if (
            not isinstance(unit_value_cents, int)
            or isinstance(unit_value_cents, bool)
            or unit_value_cents < 0
        ):
            raise VersionError("参考单位价值必须为非负整数分/克")
        if not self.db.lot_exists(lot_id):
            raise NotFound(f"批号 {lot_id} 未在生产台账登记")
        self.db.upsert_reference_value(version, lot_id, Fraction(unit_value_cents))
        self.db.commit()

    def list_versions(self) -> dict[str, list[str]]:
        return {
            "root_cost": self.db.list_root_cost_versions(),
            "processing_fee": self.db.list_fee_versions(),
            "reference_value": self.db.list_ref_versions(),
        }

    # ================================================================ 切面计算

    def compute_cut(
        self,
        cut_id: str,
        name: str,
        root_version: str,
        fee_version: str,
        ref_version: str,
        mode: str = "full",
        note: Optional[str] = None,
    ) -> str:
        """对当前生产切面发布一个成本切面。

        ``mode='full'`` 全量拓扑重算；``mode='incremental'`` 基于上一切面，
        用依赖索引只重算受影响批闭包，结果与全量口径一致（demo 中有对拍核验）。
        """
        if mode not in ("full", "incremental"):
            raise VersionError("mode 必须是 'full' 或 'incremental'")
        if self.db.get_cut(cut_id) is not None:
            raise VersionError(f"成本切面 {cut_id} 已存在")
        self._require_versions(root_version, fee_version, ref_version)

        snapshot = self.ledger.build_snapshot()
        root_costs = self.db.get_root_costs(root_version)
        fees = self.db.get_fees(fee_version)
        refs = self.db.get_reference_values(ref_version)

        if mode == "full":
            products, batch_views = full_compute(snapshot, root_costs, fees, refs)
        else:
            prev = self.db.latest_cut()
            if prev is None:
                raise VersionError("增量更新前必须先存在一个已发布切面")
            old_products = self._load_products(prev["cut_id"])
            affected = self._affected_batches(
                snapshot, prev, root_costs, fees, refs, old_products
            )
            products, batch_views = incremental_compute(
                snapshot, root_costs, fees, refs, old_products, affected
            )

        self.db.insert_cut(
            cut_id, name, root_version, fee_version, ref_version, mode, note
        )
        for p in products.values():
            self.db.replace_product(cut_id, CostProduct(
                lot_id=p.lot_id, exact_total_cost=p.exact_total_cost,
                published_total_cost=p.published_total_cost,
                exact_unit_cost=p.exact_unit_cost,
                reference_unit_value=p.reference_unit_value, grams=p.grams,
                priceable=p.priceable, reason=p.reason, origin_batch=p.origin_batch,
                cut_id=cut_id,
            ))
        for view in batch_views.values():
            self.db.replace_cut_batch(cut_id, BatchCostView(
                batch_id=view.batch_id, cut_id=cut_id,
                input_lot_id=view.input_lot_id, input_grams=view.input_grams,
                input_exact_cost=view.input_exact_cost,
                processing_fee=view.processing_fee, pool_exact=view.pool_exact,
                pool_published=view.pool_published, weight_total=view.weight_total,
                priceable=view.priceable, reason=view.reason,
                products=view.products,
            ))
        self.db.commit()
        return cut_id

    def _require_versions(self, root_version: str, fee_version: str, ref_version: str) -> None:
        if root_version not in self.db.list_root_cost_versions():
            raise VersionError(f"期初成本版本 {root_version} 不存在")
        if fee_version not in self.db.list_fee_versions():
            raise VersionError(f"加工费版本 {fee_version} 不存在")
        if ref_version not in self.db.list_ref_versions():
            raise VersionError(f"参考价值版本 {ref_version} 不存在")

    def _affected_batches(
        self, snapshot: Snapshot, prev_cut, root_costs, fees, refs, old_products
    ) -> set[str]:
        """汇总价格输入变化与生产修订变化两类种子，再取后代闭包。"""
        old_root = self.db.get_root_costs(prev_cut["root_version"])
        old_fees = self.db.get_fees(prev_cut["fee_version"])
        old_refs = self.db.get_reference_values(prev_cut["ref_version"])
        seeds = changed_batches(
            snapshot, old_root, root_costs, old_fees, fees, old_refs, refs
        )
        # 生产修订：与上一切面的批次/产品行逐批比对
        prev_batch_rows = {r["batch_id"]: r for r in self.db.get_cut_batches(prev_cut["cut_id"])}
        prev_products = self.db.get_products(prev_cut["cut_id"])
        prev_by_origin: dict[Optional[str], dict] = {}
        for r in prev_products:
            prev_by_origin.setdefault(r["origin_batch"], {})[r["lot_id"]] = r["produced_grams"]
        for bid, b in snapshot.batches.items():
            old_b = prev_batch_rows.get(bid)
            if old_b is None:
                seeds.add(bid)  # 上一切面后新增的生产批
                continue
            if old_b["input_lot_id"] != b.input_lot_id or old_b["input_grams"] != b.input_grams:
                seeds.add(bid)
                continue
            cur_outs = {o.lot_id: o.grams for o in b.outputs}
            if cur_outs != prev_by_origin.get(bid, {}):
                seeds.add(bid)
        return closure(snapshot, seeds)

    # ================================================================ 查询

    def list_cuts(self) -> list[dict]:
        return [dict(r) for r in self.db.list_cuts()]

    def get_product(self, cut_id: str, lot_id: str) -> CostProduct:
        rows = {r["lot_id"]: r for r in self.db.get_products(cut_id)}
        if lot_id not in rows:
            raise NotFound(f"切面 {cut_id} 中不存在批号 {lot_id}")
        return self._row_to_product(rows[lot_id], cut_id)

    def list_products(self, cut_id: str) -> list[CostProduct]:
        return [self._row_to_product(r, cut_id) for r in self.db.get_products(cut_id)]

    def priceable_products(self, cut_id: str) -> list[CostProduct]:
        return [p for p in self.list_products(cut_id) if p.priceable]

    def get_batch_cost(self, cut_id: str, batch_id: str) -> BatchCostView:
        rows = {r["batch_id"]: r for r in self.db.get_cut_batches(cut_id)}
        if batch_id not in rows:
            raise NotFound(f"切面 {cut_id} 中不存在生产批 {batch_id}")
        r = rows[batch_id]
        products = tuple(
            p for p in self.list_products(cut_id) if p.origin_batch == batch_id
        )
        return BatchCostView(
            batch_id=r["batch_id"], cut_id=cut_id, input_lot_id=r["input_lot_id"],
            input_grams=r["input_grams"],
            input_exact_cost=int_to_frac(r["input_cost_num"], r["input_cost_den"]),
            processing_fee=Fraction(r["fee_num"], r["fee_den"]),
            pool_exact=int_to_frac(r["pool_num"], r["pool_den"]),
            pool_published=r["pool_published"],
            weight_total=Fraction(r["weight_num"], r["weight_den"]),
            priceable=bool(r["priceable"]), reason=r["reason"], products=products,
        )

    def retained_cost(self, cut_id: str, root_lot_id: str) -> dict:
        """部分投入时根批号的留存成本（精确值与发布值都保留）。"""
        snapshot = self.ledger.build_snapshot()
        if root_lot_id not in snapshot.roots:
            raise NotFound(f"{root_lot_id} 不是根批号")
        lot = snapshot.lots[root_lot_id]
        product = self.get_product(cut_id, root_lot_id)
        retained_grams = lot.produced_grams - lot.consumed_grams
        if not product.priceable:
            return {
                "root_lot_id": root_lot_id, "retained_grams": retained_grams,
                "exact_retained_cost": None, "published_retained_cost": None,
                "priceable": False, "reason": product.reason,
            }
        exact_retained = product.exact_unit_cost * retained_grams
        from .engine import round_half_up
        return {
            "root_lot_id": root_lot_id,
            "produced_grams": lot.produced_grams,
            "consumed_grams": lot.consumed_grams,
            "retained_grams": retained_grams,
            "exact_unit_cost": product.exact_unit_cost,
            "exact_retained_cost": exact_retained,
            "published_retained_cost": round_half_up(exact_retained),
            "priceable": True,
        }

    def explain_unit_change(self, new_cut_id: str, lot_id: str, old_cut_id: str) -> dict:
        """说明未直接改动批号的单位成本为何在两个切面间变化。"""
        snapshot = self.ledger.build_snapshot()
        old_products = self._load_products(old_cut_id)
        new_products = self._load_products(new_cut_id)
        return explain_unit_change(snapshot, old_products, new_products, lot_id)

    # ================================================================ 行映射

    def _load_products(self, cut_id: str) -> dict[str, CostProduct]:
        return {r["lot_id"]: self._row_to_product(r, cut_id) for r in self.db.get_products(cut_id)}

    @staticmethod
    def _row_to_product(r, cut_id: str) -> CostProduct:
        return CostProduct(
            lot_id=r["lot_id"],
            exact_total_cost=int_to_frac(r["exact_num"], r["exact_den"]),
            published_total_cost=r["published"],
            exact_unit_cost=(
                Fraction(r["exact_num"], r["exact_den"]) / r["produced_grams"]
                if r["priceable"] else None
            ),
            reference_unit_value=Fraction(r["ref_num"], r["ref_den"]),
            grams=r["produced_grams"],
            priceable=bool(r["priceable"]),
            reason=r["reason"],
            origin_batch=r["origin_batch"],
            cut_id=cut_id,
        )
