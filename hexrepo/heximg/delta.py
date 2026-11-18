"""分区差量分析器。

硬约束：本模块**只能消费** :class:`~heximg.image.SparseImage` 的公开范围读取
与来源 API（``read_spans`` / ``byte_at`` / ``sources_at`` /
``defined_spans``），禁止导入或调用 :mod:`heximg.records` 重新解析文件，
因此 overlay 冲突结论对分析器不可绕过。

两种比较视图（结果不可混用，报告内显式记录视图并在合并时校验）：

``unknown``      空洞保持未知。空洞与任何字节都不做值比较；
                 hole↔hole 只进空洞统计，不进四类段。
``materialized`` 空洞按所属分区的填充值物化成具体字节后再比较；
                 分区未声明填充策略时拒绝该视图。

每分区输出新增定义 / 撤去定义 / 值变化 / 不变四类**最大连续段**，
空洞变化单独统计。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterator

from .errors import PartitionError, ViewMismatch
from .image import MAX_READ_SIZE, Span, SparseImage
from .partition import Partition, PartitionDirectory

VIEW_UNKNOWN = "unknown"
VIEW_MATERIALIZED = "materialized"
VIEWS = frozenset({VIEW_UNKNOWN, VIEW_MATERIALIZED})

KIND_ADDED = "added"          # 新增定义（A 空洞 → B 已定义）
KIND_REMOVED = "removed"      # 撤去定义（A 已定义 → B 空洞）
KIND_CHANGED = "changed"      # 值变化（两侧均已定义且有效值不同）
KIND_UNCHANGED = "unchanged"  # 不变
KINDS = (KIND_ADDED, KIND_REMOVED, KIND_CHANGED, KIND_UNCHANGED)

# 分页窗口；不得超过 read_spans 的 1 MiB 上限
_PAGE_SIZE = 1 << 18  # 256 KiB


@dataclass(frozen=True)
class DeltaRun:
    """分区内一段最大连续差量分类。

    ``a_hole`` / ``b_hole`` 标记该段两端的定义状态，物化视图下
    “双空洞但填充值相等”的不变段可借此与普通不变段区分。
    """

    partition: str
    kind: str
    start: int
    length: int
    a_hole: bool
    b_hole: bool

    @property
    def end(self) -> int:
        return self.start + self.length

    def to_dict(self) -> dict:
        return {
            "partition": self.partition,
            "kind": self.kind,
            "start": self.start,
            "length": self.length,
            "end": self.end,
            "a_hole": self.a_hole,
            "b_hole": self.b_hole,
        }


@dataclass(frozen=True)
class HoleStats:
    """单分区空洞变化统计（字节数）。"""

    both_hole: int = 0              # 两侧皆空洞
    hole_to_defined: int = 0        # A 空洞 → B 已定义（新增定义的定义侧）
    defined_to_hole: int = 0        # A 已定义 → B 空洞（撤去定义的定义侧）
    materialized_reads_a: int = 0   # 物化视图中 A 侧按填充值读取的空洞字节
    materialized_reads_b: int = 0   # 物化视图中 B 侧按填充值读取的空洞字节

    @property
    def net_hole_change(self) -> int:
        """B 相对 A 的空洞净变化（正=空洞增多）。"""
        return self.defined_to_hole - self.hole_to_defined

    def to_dict(self) -> dict:
        d = {
            "both_hole": self.both_hole,
            "hole_to_defined": self.hole_to_defined,
            "defined_to_hole": self.defined_to_hole,
            "net_hole_change": self.net_hole_change,
        }
        if self.materialized_reads_a or self.materialized_reads_b:
            d["materialized_reads_a"] = self.materialized_reads_a
            d["materialized_reads_b"] = self.materialized_reads_b
        return d


@dataclass
class _RunBuilder:
    """跨分页边界拼接最大连续段。"""

    partition: str
    runs: list[DeltaRun] = field(default_factory=list)
    _state: tuple[str, bool, bool] | None = None
    _start: int = 0
    _cursor: int = 0

    def emit(self, kind: str, a_hole: bool, b_hole: bool,
             start: int, length: int) -> None:
        state = (kind, a_hole, b_hole)
        if self._state == state and start == self._cursor:
            prev = self.runs[-1]
            self.runs[-1] = DeltaRun(
                self.partition, kind, prev.start,
                prev.length + length, a_hole, b_hole,
            )
        else:
            self.runs.append(
                DeltaRun(self.partition, kind, start, length, a_hole, b_hole)
            )
        self._state = state
        self._start = start
        self._cursor = start + length

    def flush(self) -> None:
        self._state = None


@dataclass(frozen=True)
class PartitionDelta:
    partition: str
    start: int
    length: int
    view: str
    runs: tuple[DeltaRun, ...]
    counts: dict[str, int]
    hole_stats: HoleStats
    defined_a: int
    defined_b: int

    def to_dict(self) -> dict:
        return {
            "partition": self.partition,
            "start": self.start,
            "length": self.length,
            "view": self.view,
            "defined_a": self.defined_a,
            "defined_b": self.defined_b,
            "counts": dict(self.counts),
            "hole_stats": self.hole_stats.to_dict(),
            "runs": [r.to_dict() for r in self.runs],
        }


@dataclass(frozen=True)
class DeltaReport:
    """两个不可变映像产品在某一目录修订、某一视图下的完整差量报告。"""

    image_a: str
    image_b: str
    digest_a: str
    digest_b: str
    view: str
    directory_revision: str
    directory_digest: str
    directory_snapshot: tuple[dict, ...]
    partitions: tuple[PartitionDelta, ...]
    report_digest: str

    def to_dict(self) -> dict:
        return {
            "image_a": self.image_a,
            "image_b": self.image_b,
            "digest_a": self.digest_a,
            "digest_b": self.digest_b,
            "view": self.view,
            "directory_revision": self.directory_revision,
            "directory_digest": self.directory_digest,
            "directory_snapshot": list(self.directory_snapshot),
            "summary": self.summary(),
            "partitions": [p.to_dict() for p in self.partitions],
            "report_digest": self.report_digest,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def summary(self) -> dict:
        agg = {k: 0 for k in KINDS}
        holes = {"both_hole": 0, "hole_to_defined": 0,
                 "defined_to_hole": 0}
        for pd in self.partitions:
            for k in KINDS:
                agg[k] += pd.counts[k]
            hs = pd.hole_stats
            holes["both_hole"] += hs.both_hole
            holes["hole_to_defined"] += hs.hole_to_defined
            holes["defined_to_hole"] += hs.defined_to_hole
        return {"byte_counts": agg, "hole_stats": holes,
                "partition_count": len(self.partitions)}

    @classmethod
    def from_dict(cls, doc: dict) -> "DeltaReport":
        """从 JSON 结构重建报告（旧报告因此可重开核对）。"""
        parts = []
        for pd in doc["partitions"]:
            runs = tuple(
                DeltaRun(r["partition"], r["kind"], r["start"],
                         r["length"], r["a_hole"], r["b_hole"])
                for r in pd["runs"]
            )
            hs_doc = pd["hole_stats"]
            hs = HoleStats(
                both_hole=hs_doc.get("both_hole", 0),
                hole_to_defined=hs_doc.get("hole_to_defined", 0),
                defined_to_hole=hs_doc.get("defined_to_hole", 0),
                materialized_reads_a=hs_doc.get("materialized_reads_a", 0),
                materialized_reads_b=hs_doc.get("materialized_reads_b", 0),
            )
            parts.append(PartitionDelta(
                partition=pd["partition"], start=pd["start"],
                length=pd["length"], view=pd["view"], runs=runs,
                counts=dict(pd["counts"]), hole_stats=hs,
                defined_a=pd["defined_a"], defined_b=pd["defined_b"],
            ))
        return cls(
            image_a=doc["image_a"], image_b=doc["image_b"],
            digest_a=doc["digest_a"], digest_b=doc["digest_b"],
            view=doc["view"],
            directory_revision=doc["directory_revision"],
            directory_digest=doc["directory_digest"],
            directory_snapshot=tuple(
                dict(d) for d in doc.get("directory_snapshot", ())),
            partitions=tuple(parts),
            report_digest=doc["report_digest"],
        )


# ---------------------------------------------------------------------- #
# 扫描辅助：只通过公开 API 拿 Span 流
# ---------------------------------------------------------------------- #
def _paged_spans(image: SparseImage, start: int, length: int
                 ) -> Iterator[Span]:
    """把分区窗口按 ≤1 MiB 分页展开为 Span 流（分析器侧不持有更大缓冲）。"""
    page = min(_PAGE_SIZE, MAX_READ_SIZE)
    remaining = length
    cursor = start
    while remaining > 0:
        n = min(page, remaining)
        for span in image.read_spans(cursor, n):
            yield span
        cursor += n
        remaining -= n


def _diff_partition(image_a: SparseImage, image_b: SparseImage,
                    part: Partition, view: str) -> PartitionDelta:
    if view not in VIEWS:
        raise ViewMismatch(f"未知比较视图：{view!r}")
    fill = part.fill
    if view == VIEW_MATERIALIZED and fill is None:
        raise PartitionError(
            f"分区 {part.name!r} 未声明填充值策略，不支持 materialized 视图；"
            "请改用 unknown 视图或为分区配置 fill"
        )

    builder = _RunBuilder(part.name)
    counts = {k: 0 for k in KINDS}
    stats = HoleStats()
    defined_a = defined_b = 0
    mat_a = mat_b = 0
    both_hole = h2d = d2h = 0

    spans_a = _paged_spans(image_a, part.start, part.length)
    spans_b = _paged_spans(image_b, part.start, part.length)
    sa = next(spans_a, None)
    sb = next(spans_b, None)
    off_a = 0  # sa 在分区窗口内的起始偏移
    off_b = 0  # sb 在分区窗口内的起始偏移

    while sa is not None or sb is not None:
        a_start = off_a
        a_end = off_a + sa.length if sa is not None else part.length
        b_start = off_b
        b_end = off_b + sb.length if sb is not None else part.length
        lo = max(a_start, b_start)
        hi = min(a_end, b_end)
        seg_len = hi - lo
        base_addr = part.start + lo
        a_span_off = lo - a_start  # 重叠区间在 sa 内的偏移
        b_span_off = lo - b_start
        cur_a_hole = sa is None or sa.is_hole
        cur_b_hole = sb is None or sb.is_hole

        # 空洞统计（与视图无关的原始定义状态）
        if cur_a_hole and cur_b_hole:
            both_hole += seg_len
        elif cur_a_hole:
            h2d += seg_len
            defined_b += seg_len
        elif cur_b_hole:
            d2h += seg_len
            defined_a += seg_len
        else:
            defined_a += seg_len
            defined_b += seg_len
        if view == VIEW_MATERIALIZED:
            if cur_a_hole:
                mat_a += seg_len
            if cur_b_hole:
                mat_b += seg_len

        if cur_a_hole and cur_b_hole:
            # unknown：双空洞只进空洞统计；materialized：填充值相等→不变段
            if view == VIEW_MATERIALIZED:
                builder.emit(KIND_UNCHANGED, True, True, base_addr, seg_len)
                counts[KIND_UNCHANGED] += seg_len
        elif cur_a_hole:
            _emit_bytewise_defined_side(
                builder, counts, a_hole=True, b_hole=False,
                defined_span=sb, off=b_span_off, length=seg_len,
                view=view, const_is_a=True,
            )
        elif cur_b_hole:
            _emit_bytewise_defined_side(
                builder, counts, a_hole=False, b_hole=True,
                defined_span=sa, off=a_span_off, length=seg_len,
                view=view, const_is_a=False,
            )
        else:
            _emit_defined_defined(
                builder, counts, sa, sb, a_span_off, b_span_off,
                seg_len, base_addr, fill, view,
            )

        # 区间归并：谁先结束谁前进（同时结束则同时前进）
        if a_end <= hi:
            sa = next(spans_a, None)
            off_a = hi
        if b_end <= hi:
            sb = next(spans_b, None)
            off_b = hi

    stats = HoleStats(
        both_hole=both_hole, hole_to_defined=h2d, defined_to_hole=d2h,
        materialized_reads_a=mat_a if view == VIEW_MATERIALIZED else 0,
        materialized_reads_b=mat_b if view == VIEW_MATERIALIZED else 0,
    )
    return PartitionDelta(
        partition=part.name, start=part.start, length=part.length,
        view=view, runs=tuple(builder.runs), counts=counts,
        hole_stats=stats, defined_a=defined_a, defined_b=defined_b,
    )


def _emit_defined_defined(builder, counts, sa: Span, sb: Span,
                          a_off: int, b_off: int, length: int,
                          base_addr: int, fill, view: str) -> None:
    """两侧皆已定义：逐字节比较并合并最大连续段。"""
    run_kind: str | None = None
    run_start = base_addr
    run_len = 0
    da = sa.data
    db = sb.data
    for i in range(length):
        kind = KIND_UNCHANGED if da[a_off + i] == db[b_off + i] else KIND_CHANGED
        if kind != run_kind:
            if run_kind is not None:
                builder.emit(run_kind, False, False, run_start, run_len)
                counts[run_kind] += run_len
            run_kind, run_start, run_len = kind, base_addr + i, 1
        else:
            run_len += 1
    if run_kind is not None:
        builder.emit(run_kind, False, False, run_start, run_len)
        counts[run_kind] += run_len


def _emit_bytewise_defined_side(builder, counts, *, a_hole: bool,
                                b_hole: bool, defined_span: Span,
                                off: int, length: int,
                                view: str, const_is_a: bool) -> None:
    """一侧空洞、另一侧已定义：定义状态变化决定整段类别。

    类别在重叠区间内恒定（新增或撤去），逐字节只是为了与相邻段
    正确合并成最大连续段。
    """
    kind = KIND_ADDED if const_is_a else KIND_REMOVED
    base = defined_span.address
    builder.emit(kind, a_hole, b_hole, base + off, length)
    counts[kind] += length


# ---------------------------------------------------------------------- #
# 公开入口
# ---------------------------------------------------------------------- #
def compare_images(image_a: SparseImage, image_b: SparseImage,
                   directory: PartitionDirectory, *,
                   view: str = VIEW_UNKNOWN) -> DeltaReport:
    """在给定分区目录修订下比较两个不可变映像产品。"""
    if view not in VIEWS:
        raise ViewMismatch(
            f"非法视图 {view!r}，允许：{sorted(VIEWS)}"
        )
    part_deltas = tuple(
        _diff_partition(image_a, image_b, part, view)
        for part in directory.partitions
    )
    snapshot = tuple(p.to_dict() for p in directory.partitions)
    report = DeltaReport(
        image_a=image_a.name,
        image_b=image_b.name,
        digest_a=image_a.digest(),
        digest_b=image_b.digest(),
        view=view,
        directory_revision=directory.revision,
        directory_digest=directory.revision_digest(),
        directory_snapshot=snapshot,
        partitions=part_deltas,
        report_digest="",
    )
    digest = hashlib.sha256(
        report.to_json().encode("utf-8")
    ).hexdigest()
    object.__setattr__(report, "report_digest", digest)
    return report


def assert_same_view(reports: Iterator[DeltaReport]) -> None:
    """拒绝跨视图混用报告。"""
    views = {r.view for r in reports}
    if len(views) > 1:
        raise ViewMismatch(
            f"报告视图不一致，禁止混用：{sorted(views)}"
        )
