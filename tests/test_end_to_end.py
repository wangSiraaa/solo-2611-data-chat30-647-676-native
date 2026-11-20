"""端到端：计划 → 真实报价/承诺 → 分配仓只消费真实事实；拒绝从报价造实物。"""

import unittest

from ordercommit.execution import ProcurementExecutor
from ordercommit.planner import PurchasePlanner, SupplierCatalogEntry
from ordercommit.pricing import SkuRule
from ordercommit.scenario import build_catalog, build_supplier_service, demand_lines
from ordercommit.supplier import SupplierService
from ordercommit.synthetic_data import OVERBUY_CAPS, T_MAIN
from ordercommit.warehouse import Warehouse


class EndToEndTest(unittest.TestCase):
    def test_actual_cost_comes_from_real_quotes_not_rebuilt_prices(self) -> None:
        svc = build_supplier_service(":memory:")
        wh = Warehouse(":memory:")
        catalog = build_catalog(svc, T_MAIN)
        plan = PurchasePlanner(catalog, OVERBUY_CAPS).plan(demand_lines())
        result = ProcurementExecutor(svc, wh).execute(plan, "E2E", T_MAIN)

        # 真实成本与穷举试算一致，但来源是承诺表
        self.assertTrue(result.cost_matches_estimate)
        for c in result.commitments:
            stored = svc.get_commitment(c["commitment_no"])
            self.assertEqual(stored["total_price"], c["total_price"])
            whc = wh.get_commitment(c["commitment_no"])
            # 分配仓侧承诺与供应商侧逐行一致
            self.assertEqual(
                [(l["sku"], l["qty"]) for l in whc["lines"]],
                [(l["sku"], l["qty"]) for l in stored["lines"]])
        svc.close(); wh.close()

    def test_warehouse_has_no_goods_before_real_receipt(self) -> None:
        svc = build_supplier_service(":memory:")
        wh = Warehouse(":memory:")
        catalog = build_catalog(svc, T_MAIN)
        plan = PurchasePlanner(catalog, OVERBUY_CAPS).plan(demand_lines())
        ProcurementExecutor(svc, wh).execute(plan, "E2E2", T_MAIN)
        # 承诺已登记但未到货：中央库存必须为 0，需求全部欠着
        for d in demand_lines():
            wh.add_demand(d.demand_id, d.sku, d.qty, d.priority, d.due_date, now=T_MAIN)
        self.assertEqual(wh.stock_levels(), {})
        self.assertEqual(
            sum(x["remaining"] for x in wh.outstanding_demands()),
            sum(d.qty for d in demand_lines()))
        svc.close(); wh.close()

    def test_re_executing_same_plan_is_idempotent(self) -> None:
        svc = build_supplier_service(":memory:")
        wh = Warehouse(":memory:")
        catalog = build_catalog(svc, T_MAIN)
        plan = PurchasePlanner(catalog, OVERBUY_CAPS).plan(demand_lines())
        ex = ProcurementExecutor(svc, wh)
        r1 = ex.execute(plan, "IDEM", T_MAIN)
        r2 = ex.execute(plan, "IDEM", T_MAIN)  # 同请求号复读 + 已接受承诺幂等
        self.assertEqual(
            [c["commitment_no"] for c in r1.commitments],
            [c["commitment_no"] for c in r2.commitments])
        self.assertEqual(r1.actual_cost, r2.actual_cost)
        svc.close(); wh.close()


if __name__ == "__main__":
    unittest.main()
