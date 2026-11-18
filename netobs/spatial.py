"""精确的五维空间代数：前缀集合、闭区间集合与乘积区域。

所有运算都在整数集合上精确进行，不使用采样近似：

* ``normalize_prefixes`` 把前缀列表化为“不相交、无包含、可合并已合并”的规范形；
* ``normalize_intervals`` 把闭区间列表化为不相交且不相邻的规范形；
* :class:`Region` 是五个维度的笛卡尔积，``subtract`` 利用
  ``R \\ (A1×…×A5) = ∪_i {p ∈ R : p_i ∉ A_i}`` 精确分解差集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .rules import PORT_MAX, PORT_MIN, Prefix, format_ipv4

UINT32_BITS = 32


class ResourceLimitError(RuntimeError):
    """精确集合运算超出预算；调用方必须把它与“规则等价”区分开。"""


def _remove_contained(prefixes: Iterable[Prefix]) -> list[Prefix]:
    kept: list[Prefix] = []
    for prefix in sorted(set(prefixes), key=lambda p: (p.plen, p.addr)):
        if not any(
            other.plen <= prefix.plen
            and other.first <= prefix.first
            and prefix.last <= other.last
            for other in kept
        ):
            kept.append(prefix)
    return kept


def normalize_prefixes(prefixes: Iterable[Prefix]) -> tuple[Prefix, ...]:
    """精确规范化前缀并集（集合语义）。"""
    pool = _remove_contained(prefixes)
    changed = True
    while changed:
        changed = False
        as_set = set(pool)
        for prefix in list(pool):
            if prefix.plen == 0 or prefix not in as_set:
                continue
            sibling = Prefix(prefix.addr ^ (1 << (32 - prefix.plen)), prefix.plen)
            if sibling in as_set:
                mask = 0 if prefix.plen == 1 else (0xFFFFFFFF << (33 - prefix.plen)) & 0xFFFFFFFF
                parent = Prefix(prefix.addr & mask, prefix.plen - 1)
                as_set.discard(prefix)
                as_set.discard(sibling)
                as_set.add(parent)
                changed = True
        pool = _remove_contained(as_set)
    return tuple(sorted(pool, key=lambda p: (p.first, p.plen)))


def prefix_minus_prefix(prefix: Prefix, other: Prefix) -> tuple[Prefix, ...]:
    """精确的 ``prefix \\ other``。"""
    if not _prefixes_overlap(prefix, other):
        return (prefix,)
    if other.plen <= prefix.plen and other.contains(prefix.first):
        # other 包含整个 prefix
        return ()
    # prefix 严格包含 other：自 prefix 逐层下行，收集每层的兄弟前缀。
    result: list[Prefix] = []
    node = prefix
    while node.plen < other.plen:
        left, right = node.split()  # type: ignore[misc]
        child, sibling = (left, right) if left.contains(other.first) else (right, left)
        result.append(sibling)
        node = child
    return tuple(result)


def _prefixes_overlap(a: Prefix, b: Prefix) -> bool:
    # 两个 CIDR 有交集 ⇔ 其中一个包含另一个的网络地址
    return a.contains(b.first) or b.contains(a.first)


def prefix_union_minus(
    owners: Sequence[Prefix], excluded: Sequence[Prefix]
) -> tuple[Prefix, ...]:
    """精确的 ``∪owners \\ ∪excluded``。"""
    current: list[Prefix] = list(owners)
    for cut in excluded:
        nxt: list[Prefix] = []
        for prefix in current:
            nxt.extend(prefix_minus_prefix(prefix, cut))
        current = list(normalize_prefixes(nxt))
    return normalize_prefixes(current)


def normalize_intervals(
    intervals: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    items = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for lo, hi in items:
        if lo > hi:
            raise ValueError(f"非法区间 {lo}..{hi}")
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return tuple(merged)


def intervals_intersect(
    a: Sequence[tuple[int, int]], b: Sequence[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            result.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return normalize_intervals(result)


def intervals_minus(
    owners: Sequence[tuple[int, int]], excluded: Sequence[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    current = normalize_intervals(owners)
    for cut_lo, cut_hi in exclude_normalized(excluded):
        nxt: list[tuple[int, int]] = []
        for lo, hi in current:
            if cut_hi < lo or cut_lo > hi:
                nxt.append((lo, hi))
                continue
            if lo < cut_lo:
                nxt.append((lo, cut_lo - 1))
            if cut_hi < hi:
                nxt.append((cut_hi + 1, hi))
        current = normalize_intervals(nxt)
    return current


def exclude_normalized(intervals: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    return normalize_intervals(intervals)


def prefixes_to_intervals(prefixes: Sequence[Prefix]) -> tuple[tuple[int, int], ...]:
    return normalize_intervals((p.first, p.last) for p in prefixes)


@dataclass(frozen=True)
class Region:
    """五维乘积区域：源/目前缀并集、协议集合、源/目端口区间并集。"""

    src: tuple[Prefix, ...]
    dst: tuple[Prefix, ...]
    protocols: frozenset[str]
    sport: tuple[tuple[int, int], ...]
    dport: tuple[tuple[int, int], ...]

    def is_empty(self) -> bool:
        return not (self.src and self.dst and self.protocols and self.sport and self.dport)

    def key(self) -> tuple:
        return (
            tuple((p.addr, p.plen) for p in self.src),
            tuple((p.addr, p.plen) for p in self.dst),
            tuple(sorted(self.protocols)),
            self.sport,
            self.dport,
        )

    def contains_point(self, src: int, dst: int, proto: str, sport: int, dport: int) -> bool:
        if proto not in self.protocols:
            return False
        if not any(p.contains(src) for p in self.src):
            return False
        if not any(p.contains(dst) for p in self.dst):
            return False
        if not any(lo <= sport <= hi for lo, hi in self.sport):
            return False
        return any(lo <= dport <= hi for lo, hi in self.dport)

    def witness(self) -> tuple[int, int, str, int, int]:
        """返回区域中的一个具体合法五元组（确定性最小取值）。"""
        if self.is_empty():
            raise ValueError("空区域没有见证")
        return (
            min(p.first for p in self.src),
            min(p.first for p in self.dst),
            sorted(self.protocols)[0],
            self.sport[0][0],
            self.dport[0][0],
        )

    def sample_points(self, limit: int = 8) -> list[tuple[int, int, str, int, int]]:
        """在区域边界上取确定性的少量代表点（仅用于诊断展示）。"""
        points: list[tuple[int, int, str, int, int]] = []
        src_vals = sorted({p.first for p in self.src} | {p.last for p in self.src})
        dst_vals = sorted({p.first for p in self.dst} | {p.last for p in self.dst})
        sport_vals = sorted({lo for lo, _ in self.sport} | {hi for _, hi in self.sport})
        dport_vals = sorted({lo for lo, _ in self.dport} | {hi for _, hi in self.dport})
        for proto in sorted(self.protocols):
            for sv in src_vals:
                for dv in dst_vals:
                    for sp in sport_vals:
                        for dp in dport_vals:
                            points.append((sv, dv, proto, sp, dp))
                            if len(points) >= limit:
                                return points
        return points

    def intersection(self, other: "Region") -> "Region | None":
        if not (self.protocols & other.protocols):
            return None
        src = _prefix_intersection(self.src, other.src)
        if not src:
            return None
        dst = _prefix_intersection(self.dst, other.dst)
        if not dst:
            return None
        sport = intervals_intersect(self.sport, other.sport)
        if not sport:
            return None
        dport = intervals_intersect(self.dport, other.dport)
        if not dport:
            return None
        return Region(src, dst, self.protocols & other.protocols, sport, dport)

    def subtract(self, other: "Region") -> tuple["Region", ...]:
        """精确差集，返回互不相交的乘积区域。

        分解式（每一片都加上前面维度的“落在 other 内”约束，保证两两不交）::

            (S\\Os) × D × P × SP × DP
            (S∩Os) × (D\\Od) × P × SP × DP
            (S∩Os) × (D∩Od) × (P\\Op) × SP × DP
            (S∩Os) × (D∩Od) × (P∩Op) × (SP\\SPo) × DP
            (S∩Os) × (D∩Od) × (P∩Op) × (SP∩SPo) × (DP\\DPo)
        """
        if self.intersection(other) is None:
            return (self,)

        inter_src = _prefix_intersection(self.src, other.src)
        inter_dst = _prefix_intersection(self.dst, other.dst)
        inter_proto = self.protocols & other.protocols
        inter_sport = intervals_intersect(self.sport, other.sport)
        inter_dport = intervals_intersect(self.dport, other.dport)

        pieces: list[Region] = []

        src_rest = prefix_union_minus(self.src, other.src)
        if src_rest:
            pieces.append(Region(src_rest, self.dst, self.protocols, self.sport, self.dport))

        if inter_src and inter_dst:
            dst_rest = prefix_union_minus(self.dst, other.dst)
            if dst_rest:
                pieces.append(
                    Region(inter_src, dst_rest, self.protocols, self.sport, self.dport)
                )

        if inter_src and inter_dst and inter_proto:
            proto_rest = self.protocols - other.protocols
            if proto_rest:
                pieces.append(
                    Region(inter_src, inter_dst, frozenset(proto_rest), self.sport, self.dport)
                )

        if inter_src and inter_dst and inter_proto and inter_sport:
            sport_rest = intervals_minus(self.sport, other.sport)
            if sport_rest:
                pieces.append(
                    Region(inter_src, inter_dst, inter_proto, sport_rest, self.dport)
                )

        if inter_src and inter_dst and inter_proto and inter_sport and inter_dport:
            dport_rest = intervals_minus(self.dport, other.dport)
            if dport_rest:
                pieces.append(
                    Region(inter_src, inter_dst, inter_proto, inter_sport, dport_rest)
                )

        return tuple(pieces)

    def to_dict(self) -> dict:
        return {
            "src": [str(p) for p in self.src],
            "dst": [str(p) for p in self.dst],
            "protocols": sorted(self.protocols),
            "sport": [list(iv) for iv in self.sport],
            "dport": [list(iv) for iv in self.dport],
        }


def _prefix_intersection(a: Sequence[Prefix], b: Sequence[Prefix]) -> tuple[Prefix, ...]:
    out: list[Prefix] = []
    for left in a:
        for right in b:
            if left.plen <= right.plen and left.contains(right.first):
                out.append(right)
            elif right.plen <= left.plen and right.contains(left.first):
                out.append(left)
    return normalize_prefixes(out)


def subtract_many(region: Region, others: Sequence[Region]) -> tuple[Region, ...]:
    """精确计算 ``region \\ ∪others``，中途空集即停止。"""
    current: list[Region] = [region]
    for other in others:
        nxt: list[Region] = []
        for piece in current:
            nxt.extend(piece.subtract(other))
        current = dedupe_regions(nxt)
        if not current:
            return ()
    return tuple(current)


def dedupe_regions(regions: Iterable[Region]) -> list[Region]:
    seen: set[tuple] = set()
    out: list[Region] = []
    for region in regions:
        if region.is_empty():
            continue
        key = region.key()
        if key not in seen:
            seen.add(key)
            out.append(region)
    return out


def region_from_rule(rule) -> Region:  # avoids importing Rule for typing cycle
    protocols = frozenset(("TCP", "UDP")) if rule.protocol == "ANY" else frozenset((rule.protocol,))
    return Region(
        src=(rule.src,),
        dst=(rule.dst,),
        protocols=protocols,
        sport=(rule.sport,),
        dport=(rule.dport,),
    )


def full_domain() -> Region:
    return Region(
        src=(Prefix(0, 0),),
        dst=(Prefix(0, 0),),
        protocols=frozenset(("TCP", "UDP")),
        sport=((PORT_MIN, PORT_MAX),),
        dport=((PORT_MIN, PORT_MAX),),
    )


def format_point(point: tuple[int, int, str, int, int]) -> dict:
    src, dst, proto, sport, dport = point
    return {
        "src": format_ipv4(src),
        "dst": format_ipv4(dst),
        "protocol": proto,
        "sport": sport,
        "dport": dport,
    }


def point_key(point: tuple[int, int, str, int, int]) -> tuple:
    return point
