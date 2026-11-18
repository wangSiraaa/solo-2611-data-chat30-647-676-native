"""组织编制服务测试：单根、子树移动、停用、不可变快照、树闭包。"""

from __future__ import annotations

from tests.support import ServiceTestCase
from orgbudget.errors import (
    CycleError,
    DuplicateDepartment,
    InactiveDepartment,
    NotALeaf,
    RootError,
)


class OrgServiceTest(ServiceTestCase):
    def _build(self) -> int:
        #       HQ
        #      /  \
        #     A    B
        #    / \    \
        #  A1  A2    B1
        return self.build_tree(
            [
                ("HQ", None),
                ("A", "HQ"),
                ("B", "HQ"),
                ("A1", "A"),
                ("A2", "A"),
                ("B1", "B"),
            ]
        )

    def test_single_root_and_duplicate(self) -> None:
        self.org.create_department("HQ", None)
        with self.assertRaises(RootError):
            self.org.create_department("HQ2", None)
        with self.assertRaises(DuplicateDepartment):
            self.org.create_department("HQ", None)

    def test_published_product_is_immutable(self) -> None:
        v1 = self._build()
        p1 = self.org.get_product(v1)
        self.assertEqual(p1.ancestors("A1"), ("HQ", "A"))
        self.assertEqual(p1.descendants("HQ"),
                         ("A", "A1", "A2", "B", "B1"))
        self.assertEqual(p1.descendants("A"), ("A1", "A2"))

        # 工作副本移动 A 整棵子树到 B 下，再发布 v2。
        self.org.move_subtree("A", "B")
        v2 = self.org.publish("move A under B").version
        p2 = self.org.get_product(v2)

        # 旧产品完全不变。
        self.assertEqual(self.org.get_product(v1).parent_of["A"], "HQ")
        self.assertEqual(self.org.get_product(v1).ancestors("A1"), ("HQ", "A"))
        # 新产品沿新树解释。
        self.assertEqual(p2.parent_of["A"], "B")
        self.assertEqual(p2.ancestors("A1"), ("HQ", "B", "A"))
        self.assertEqual(p2.descendants("B"), ("A", "A1", "A2", "B1"))
        self.assertEqual(self.org.versions()[0][0], v1)
        self.assertEqual(self.org.versions()[1][0], v2)

    def test_move_whole_subtree_never_partial(self) -> None:
        self._build()
        self.org.move_subtree("A", "B")
        product = self.org.publish("move")
        # A 的两个孩子随 A 一起走，父边不变，没有节点被留在旧父下。
        self.assertEqual(product.parent_of["A1"], "A")
        self.assertEqual(product.parent_of["A2"], "A")
        self.assertEqual(product.children("A"), ("A1", "A2"))
        self.assertEqual(product.children("HQ"), ("B",))

    def test_cannot_move_root_or_into_descendant(self) -> None:
        self._build()
        with self.assertRaises(RootError):
            self.org.move_subtree("HQ", "B")
        with self.assertRaises(CycleError):
            self.org.move_subtree("A", "A1")
        with self.assertRaises(CycleError):
            self.org.move_subtree("A", "A")

    def test_deactivate_leaf_only(self) -> None:
        version = self._build()
        with self.assertRaises(NotALeaf):
            self.org.deactivate_leaf("A")
        with self.assertRaises(RootError):
            self.org.deactivate_leaf("HQ")
        self.org.deactivate_leaf("A1")
        product = self.org.publish("deactivate A1")
        v2 = product.version
        self.assertFalse(product.is_active("A1"))
        self.assertTrue(product.is_active("A2"))
        # 停用叶仍在树中、仍是叶，旧版本仍为 active。
        self.assertTrue(self.org.get_product(version).is_active("A1"))
        self.assertTrue(product.is_leaf("A1"))
        with self.assertRaises(InactiveDepartment):
            self.org.create_department("A1x", "A1")

    def test_working_tree_queries(self) -> None:
        self._build()
        working = self.org.working_product()
        self.assertEqual(working.root, "HQ")
        self.assertEqual(working.depth("A1"), 2)
        self.assertEqual(working.max_depth(), 2)


if __name__ == "__main__":
    unittest.main()
