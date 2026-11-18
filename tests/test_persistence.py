"""持久化测试：关闭重开后旧生产修订与旧成本切面仍可查。"""

import os
import tempfile
import unittest

from ledger_cost.cost import CostWarehouse
from ledger_cost.production import ProductionLedger
from ledger_cost.scenario import (
    B9_CORRECTION,
    build_cost_inputs_v1,
    build_cost_inputs_v2,
    build_ledger,
    open_systems,
)
from ledger_cost.storage import Storage


class TestPersistence(unittest.TestCase):
    def test_reopen_keeps_revisions_and_cuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plant.db")
            storage = Storage(path)
            ledger = build_ledger(storage)
            warehouse = CostWarehouse(storage, ledger)
            build_cost_inputs_v1(warehouse)
            warehouse.compute_cut("cut-v1", "v1", "root-v1", "fee-v1", "ref-v1")
            build_cost_inputs_v2(warehouse)
            warehouse.compute_cut("cut-v2", "v2 全量", "root-v1", "fee-v1", "ref-v2")
            warehouse.compute_cut("cut-v2i", "v2 增量", "root-v1", "fee-v1", "ref-v2",
                                  mode="incremental")
            # B9 更正产生修订 2
            ledger.correct_batch("B9", **B9_CORRECTION)
            storage.close()

            storage2, ledger2, wh2 = open_systems(path)
            # 生产修订历史保留
            revs = ledger2.list_revisions("B9")
            self.assertEqual([r["revision_no"] for r in revs], [1, 2])
            self.assertEqual(revs[0]["status"], "superseded")
            # 旧成本切面与产品仍可查；L2A 改价经 B3 传导使 L3A 单位成本变化
            cuts = [c["cut_id"] for c in wh2.list_cuts()]
            self.assertEqual(cuts, ["cut-v1", "cut-v2", "cut-v2i"])
            p_old = wh2.get_product("cut-v1", "L3A")
            p_new = wh2.get_product("cut-v2", "L3A")
            self.assertNotEqual(p_old.exact_unit_cost, p_new.exact_unit_cost)
            # 重开后还能继续发布新切面
            wh2.compute_cut("cut-v3", "更正后", "root-v1", "fee-v1", "ref-v2")
            self.assertTrue(wh2.get_product("cut-v3", "L9A").priceable)
            storage2.close()


if __name__ == "__main__":
    unittest.main()
