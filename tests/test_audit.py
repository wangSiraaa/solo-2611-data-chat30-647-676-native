"""独立稽核测试：正常库通过；被篡改的库必须被裸 SQL 稽核抓到。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ordercommit.audit import audit
from ordercommit.warehouse import Warehouse

T = "2026-11-15T10:00:00"


class AuditTest(unittest.TestCase):
    def _build(self, db: str) -> None:
        wh = Warehouse(db)
        wh.register_commitment("C1", "S1", "V1",
                               [{"sku": "A", "qty": 6, "unit_price": 1}], now=T)
        wh.add_demand("d1", "A", 6, 5, "2026-11-20", now=T)
        wh.receive("R1", "C1", [{"sku": "A", "qty": 6}], now=T)
        wh.close()

    def test_clean_database_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "ok.db")
            self._build(db)
            result = audit(db)
            self.assertTrue(result["ok"], result["problems"])
            self.assertEqual(result["by_sku"]["A"],
                             {"received": 6, "allocated": 6,
                              "central": 0, "cross_check_leftover": 0})

    def test_tampered_over_issue_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "bad.db")
            self._build(db)
            # 绕过仓库类，直接插入一笔不可能的超发记录
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO w_allocations(receipt_id, sku, demand_id, qty, created_at)"
                " VALUES ('R1','A','d1', 99, ?)", (T,))
            conn.commit()
            conn.close()
            result = audit(db)
            self.assertFalse(result["ok"])
            self.assertTrue(any("超发" in p for p in result["problems"]))

    def test_tampered_over_receipt_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "bad2.db")
            self._build(db)
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE w_commitment_lines SET received_qty=99 WHERE commitment_no='C1'")
            conn.commit()
            conn.close()
            result = audit(db)
            self.assertFalse(result["ok"])
            self.assertTrue(any("承诺" in p for p in result["problems"]))


if __name__ == "__main__":
    unittest.main()
