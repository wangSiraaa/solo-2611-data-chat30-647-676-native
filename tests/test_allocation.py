"""纯分解算法测试：三层树余分、并列按 ID、守恒。"""

from __future__ import annotations

import unittest

from orgbudget.budget import WeightedChild, allocate_children, decompose_tree
from orgbudget.errors import WeightError  # noqa: F401  (确保异常可导出)


class AllocateChildrenTest(unittest.TestCase):
    def test_sum_equals_parent_and_non_negative(self) -> None:
        for budget in range(0, 500):
            kids = [WeightedChild("a", 1), WeightedChild("b", 2),
                    WeightedChild("c", 3), WeightedChild("d", 4)]
            result = allocate_children(budget, kids)
            self.assertEqual(sum(result.values()), budget)
            self.assertTrue(all(v >= 0 for v in result.values()))

    def test_floor_then_remainder_order(self) -> None:
        # 100 分按 1:1:1 分三个孩子：各 33，余 1 分；余数完全相同，按 ID 给 A。
        result = allocate_children(
            100, [WeightedChild("C", 1), WeightedChild("A", 1), WeightedChild("B", 1)]
        )
        self.assertEqual(result, {"A": 34, "B": 33, "C": 33})

    def test_remainder_ranking_beats_id(self) -> None:
        # 10 分按 3:3:3:1：floor 分别 3,3,3,1（和 10），无余分。
        result = allocate_children(
            10,
            [WeightedChild("w1", 3), WeightedChild("w2", 3),
             WeightedChild("w3", 3), WeightedChild("w4", 1)],
        )
        self.assertEqual(sum(result.values()), 10)
        # 11 分：floor 3,3,3,1（和 10），余 1；w1/w2/w3 余数相同(=3)，按 ID 给 w1。
        result = allocate_children(
            11,
            [WeightedChild("w1", 3), WeightedChild("w2", 3),
             WeightedChild("w3", 3), WeightedChild("w4", 1)],
        )
        self.assertEqual(result, {"w1": 4, "w2": 3, "w3": 3, "w4": 1})

    def test_distinct_remainders(self) -> None:
        # 10 分权重 6:3:1 -> floor 6,3,1 和为 10；改 11 分：6,3,1 和 10，
        # 余数: 6*11%10=6, 3*11%10=3, 1*11%10=1 -> 补分给权重 6 的孩子。
        result = allocate_children(
            11, [WeightedChild("x", 6), WeightedChild("y", 3), WeightedChild("z", 1)]
        )
        self.assertEqual(result, {"x": 7, "y": 3, "z": 1})

    def test_zero_budget(self) -> None:
        result = allocate_children(
            0, [WeightedChild("a", 1), WeightedChild("b", 2)]
        )
        self.assertEqual(result, {"a": 0, "b": 0})

    def test_bad_weights(self) -> None:
        with self.assertRaises(ValueError):
            allocate_children(10, [WeightedChild("a", 0)])
        with self.assertRaises(ValueError):
            allocate_children(10, [WeightedChild("a", -2)])
        with self.assertRaises(ValueError):
            allocate_children(10, [WeightedChild("a", True)])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            allocate_children(10, [WeightedChild("a", 1), WeightedChild("a", 1)])

    def test_three_level_tree_remainder_conservation(self) -> None:
        """三层树：HQ -> (A,B) -> (A1,A2,B1,B2)，逐级守恒。"""
        children_of = {
            "HQ": ["A", "B"],
            "A": ["A1", "A2"],
            "B": ["B1", "B2"],
        }
        weights = {
            ("HQ", "A"): 2, ("HQ", "B"): 3,
            ("A", "A1"): 1, ("A", "A2"): 1,
            ("B", "B1"): 5, ("B", "B2"): 2,
        }
        alloc = decompose_tree(1000, "HQ", children_of, weights)
        self.assertEqual(alloc["A"] + alloc["B"], 1000)
        self.assertEqual(alloc["A1"] + alloc["A2"], alloc["A"])
        self.assertEqual(alloc["B1"] + alloc["B2"], alloc["B"])
        self.assertEqual(sum(v for k, v in alloc.items() if k not in ("HQ", "A", "B")), 1000)
        # HQ: 1000 * 2/5 = 400, 600。
        self.assertEqual((alloc["A"], alloc["B"]), (400, 600))
        # B 下 5:2 分 600 -> 428.57.../171.42... -> floor 428,171 余 1 给 B1。
        self.assertEqual((alloc["B1"], alloc["B2"]), (429, 171))


if __name__ == "__main__":
    unittest.main()
