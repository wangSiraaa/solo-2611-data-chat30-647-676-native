"""精确空间代数测试：差集、并集、不交性。"""

import itertools
import random
import unittest

from netobs.rules import Prefix
from netobs.spatial import (
    Region,
    full_domain,
    intervals_minus,
    normalize_intervals,
    normalize_prefixes,
    prefix_minus_prefix,
    prefix_union_minus,
    subtract_many,
)


def _all_addrs(prefix: Prefix, limit: int = 64) -> list[int]:
    width = 32
    count = min(prefix.size, limit)
    return [prefix.first + i for i in range(count)]


def _region_points(region: Region, per_dim: int = 6) -> list[tuple]:
    src = sorted({p.first for p in region.src} | {p.last for p in region.src})
    dst = sorted({p.first for p in region.dst} | {p.last for p in region.dst})
    sport = sorted({lo for lo, _ in region.sport} | {hi for _, hi in region.sport})
    dport = sorted({lo for lo, _ in region.dport} | {hi for _, hi in region.dport})
    # 加上区间内部点
    sport += [min(PREFIX_LIMIT, lo + 1) for lo, _ in region.sport]
    dport += [min(PREFIX_LIMIT, lo + 1) for lo, _ in region.dport]
    points = []
    for sv, dv, proto, sp, dp in itertools.product(
        src[:per_dim], dst[:per_dim], sorted(region.protocols), sport[:per_dim], dport[:per_dim]
    ):
        if region.contains_point(sv, dv, proto, sp, dp):
            points.append((sv, dv, proto, sp, dp))
    return points


PREFIX_LIMIT = 1 << 32


class PrefixAlgebraTests(unittest.TestCase):
    def test_prefix_minus_disjoint(self):
        a = Prefix.parse("10.0.0.0/8")
        b = Prefix.parse("20.0.0.0/8")
        self.assertEqual(prefix_minus_prefix(a, b), (a,))

    def test_prefix_minus_supernet(self):
        a = Prefix.parse("10.1.0.0/16")
        supernet = Prefix.parse("10.0.0.0/8")
        self.assertEqual(prefix_minus_prefix(a, supernet), ())

    def test_prefix_minus_subnet_is_exact(self):
        parent = Prefix.parse("0.0.0.0/0")
        cut = Prefix.parse("10.1.0.0/16")
        rest = prefix_minus_prefix(parent, cut)
        # 覆盖：rest ∪ cut == parent
        self.assertEqual(prefix_union_minus(rest + (cut,), ()), (Prefix(0, 0),))
        # 不交：任何 rest 前缀都不与 cut 相交
        for piece in rest:
            self.assertFalse(piece.contains(cut.first))
            self.assertFalse(prefix_minus_prefix(piece, cut) != (piece,))

    def test_prefix_union_minus_two_cuts(self):
        owners = [Prefix.parse("10.1.0.0/16")]
        cuts = [Prefix.parse("10.1.0.0/17"), Prefix.parse("10.1.128.0/17")]
        self.assertEqual(prefix_union_minus(owners, cuts), ())

    def test_normalize_merges_siblings(self):
        merged = normalize_prefixes([Prefix.parse("0.0.0.0/1"), Prefix.parse("128.0.0.0/1")])
        self.assertEqual(merged, (Prefix(0, 0),))


class IntervalTests(unittest.TestCase):
    def test_normalize_and_minus(self):
        self.assertEqual(
            normalize_intervals([(5, 10), (1, 3), (4, 6)]), ((1, 10),)
        )
        self.assertEqual(
            intervals_minus([(0, 10)], [(3, 4), (8, 8)]), ((0, 2), (5, 7), (9, 10))
        )
        self.assertEqual(intervals_minus([(0, 10)], [(0, 10)]), ())


class RegionAlgebraTests(unittest.TestCase):
    def _rand_region(self, rng: random.Random) -> Region:
        base = rng.randrange(0, 1 << 32)
        plen = rng.choice([8, 12, 16, 24])
        prefix = Prefix(base & (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF, plen)
        size = 1 << (32 - plen)
        src2 = Prefix((prefix.first + rng.randrange(size)) & ~0xFFFF, 16)
        dst = Prefix(rng.randrange(0, 1 << 24) << 8, 24)
        protos = rng.choice([frozenset(("TCP",)), frozenset(("UDP",)), frozenset(("TCP", "UDP"))])
        sport_lo = rng.randrange(0, 1000)
        dport_lo = rng.randrange(0, 1000)
        return Region(
            (src2,),
            (dst,),
            protos,
            ((sport_lo, sport_lo + rng.randrange(0, 1000)),),
            ((dport_lo, dport_lo + rng.randrange(0, 1000)),),
        )

    def test_subtract_partition_is_exact_and_disjoint(self):
        rng = random.Random(3)
        for _ in range(200):
            a = self._rand_region(rng)
            b = self._rand_region(rng)
            pieces = a.subtract(b)
            inter = a.intersection(b)
            # 每片都不与 b 相交
            for piece in pieces:
                self.assertIsNone(piece.intersection(b), f"piece {piece} intersects cut")
                # 每片都在 a 内
                self.assertFalse(piece.subtract(a), f"piece {piece} not subset of a")
            # 片之间两两不交
            for i in range(len(pieces)):
                for j in range(i + 1, len(pieces)):
                    self.assertIsNone(pieces[i].intersection(pieces[j]))
            # 点级验证：a 中的确定性样本点，要么在 b 内，要么被某片覆盖
            for point in _region_points(a, 5):
                in_b = b.contains_point(*point)
                covered = [p.contains_point(*point) for p in pieces]
                self.assertEqual(
                    sum(covered) + (1 if in_b else 0),
                    1,
                    f"point {point} in_b={in_b} covered={covered}",
                )
            # 空差集 ⇔ a ⊆ b（用覆盖点判断）——只对 b 包含的完整区域做正向断言
            if inter is None:
                self.assertEqual(pieces, (a,))

    def test_subtract_many_union_shadow(self):
        A = Region((Prefix.parse("0.0.0.0/31"),), (Prefix.parse("0.0.0.0/31"),),
                   frozenset(("TCP",)), ((0, 2),), ((1, 2),))
        B = Region((Prefix.parse("0.0.0.0/31"),), (Prefix.parse("0.0.0.0/31"),),
                   frozenset(("TCP",)), ((3, 5),), ((1, 2),))
        S = Region((Prefix.parse("0.0.0.0/31"),), (Prefix.parse("0.0.0.0/31"),),
                   frozenset(("TCP",)), ((0, 5),), ((1, 2),))
        self.assertEqual(subtract_many(S, [A, B]), ())

    def test_full_domain_subtract_returns_complement(self):
        r = Region((Prefix.parse("0.0.0.0/0"),), (Prefix.parse("0.0.0.0/0"),),
                   frozenset(("TCP",)), ((0, 65535),), ((80, 80),))
        pieces = full_domain().subtract(r)
        self.assertEqual(len(pieces), 2)  # UDP 分支 + TCP 端口分支
        self.assertFalse(subtract_many(full_domain(), [r]) == ())
        # 任何不属于 r 的点必须被覆盖
        self.assertTrue(any(p.contains_point(1, 1, "UDP", 0, 80) for p in pieces))
        self.assertTrue(any(p.contains_point(1, 1, "TCP", 0, 81) for p in pieces))


if __name__ == "__main__":
    unittest.main()
