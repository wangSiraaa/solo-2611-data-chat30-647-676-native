"""稀疏映像产品：段区间存储、显式 overlay 层次、范围读取与来源回查。

存储结构
~~~~~~~~
地址空间用**有序、互不相交、相邻即合并的段（segment）**表示，绝不按
4 GiB 地址空间分配全量数组。每个段内：

* ``data`` 为该连续已定义区间的字节（不可变 ``bytes``）；
* ``runs`` 为“来源游程”列表，连续同来源的字节共享一条游程，
  记录每个字节由哪些 :class:`Source` 提供。

同值重复写入会归并并保留全部来源；不同值在同层冲突时拒绝整份产品，
在显式 overlay 时由最高层胜出（与到达顺序无关）。
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable

from .errors import OverlayConflict, ReadRangeError

ADDR_SPACE_SIZE = 1 << 32
MAX_READ_SIZE = 1 << 20  # 范围读取单次上限 1 MiB

# 来源游程：(长度, 来源元组)
SourceRun = tuple[int, tuple["Source", ...]]


@dataclass(frozen=True)
class Source:
    """一个有效字节的原始出处。

    ``byte_range`` 为该来源记录在物理文件中的半开字节偏移区间。
    """

    file: str
    line_no: int
    byte_range: tuple[int, int]

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line_no": self.line_no,
            "byte_range": list(self.byte_range),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        return cls(file=d["file"], line_no=d["line_no"],
                   byte_range=(d["byte_range"][0], d["byte_range"][1]))


@dataclass(frozen=True)
class Span:
    """范围读取结果中的一段。

    * ``data is None`` 表示未定义空洞，空洞段 ``runs`` 为 ``((长度, ()),)``；
    * 已定义段的 ``runs`` 给出段内逐字节的来源游程（长度之和等于数据长度）。
    """

    address: int
    data: bytes | None
    runs: tuple[SourceRun, ...]

    @classmethod
    def hole(cls, address: int, length: int) -> "Span":
        if length <= 0:
            raise ValueError("空洞长度必须为正")
        return cls(address, None, ((length, ()),))

    @classmethod
    def defined(cls, address: int, data: bytes,
                runs: Iterable[SourceRun]) -> "Span":
        runs_t = tuple(runs)
        if sum(length for length, _ in runs_t) != len(data):
            raise ValueError("来源游程长度之和必须等于数据长度")
        return cls(address, data, runs_t)

    @property
    def length(self) -> int:
        return sum(length for length, _ in self.runs)

    @property
    def is_hole(self) -> bool:
        return self.data is None

    @property
    def sources(self) -> tuple[Source, ...]:
        """段内出现过的全部来源（去重并保持稳定顺序）。"""
        out: list[Source] = []
        for _, srcs in self.runs:
            for src in srcs:
                if src not in out:
                    out.append(src)
        return tuple(out)


@dataclass
class _Run:
    """段内可变来源游程（构建期使用）。"""

    length: int
    sources: tuple[Source, ...]


@dataclass
class _Segment:
    start: int
    data: bytes
    runs: list[_Run]

    @property
    def end(self) -> int:
        return self.start + len(self.data)


@dataclass(frozen=True)
class ConflictPoint:
    """同层冲突的一个字节点。"""

    address: int
    old_value: int
    new_value: int
    layer: int
    old_sources: tuple[Source, ...]
    new_source: Source

    def __str__(self) -> str:
        old_files = [f"{s.file}:{s.line_no}" for s in self.old_sources]
        return (
            f"地址 {self.address:#010x} 已有 {self.old_value:#04x}"
            f"（{old_files}），层 {self.layer} 又写入 {self.new_value:#04x}"
            f"（{self.new_source.file}:{self.new_source.line_no}）"
        )


def _slice_runs(runs: list[_Run], lo: int, hi: int) -> list[_Run]:
    """按段内偏移 ``[lo, hi)`` 切出来源游程。"""
    if hi <= lo:
        return []
    out: list[_Run] = []
    pos = 0
    for run in runs:
        rlo, rhi = pos, pos + run.length
        if rhi <= lo:
            pos = rhi
            continue
        if rlo >= hi:
            break
        nlo, nhi = max(rlo, lo), min(rhi, hi)
        if nhi > nlo:
            out.append(_Run(nhi - nlo, run.sources))
        pos = rhi
    return out


def _coalesce_runs(runs: list[_Run]) -> list[_Run]:
    out: list[_Run] = []
    for run in runs:
        if out and out[-1].sources == run.sources:
            out[-1] = _Run(out[-1].length + run.length, run.sources)
        else:
            out.append(_Run(run.length, run.sources))
    return out


def _union_sources(old: tuple[Source, ...],
                   new: tuple[Source, ...]) -> tuple[Source, ...]:
    """保留全部来源并去重，保持稳定顺序。"""
    out = list(old)
    for src in new:
        if src not in out:
            out.append(src)
    return tuple(out)


class SparseImage:
    """不可变的稀疏映像产品（构建完成后冻结）。

    调用方应通过 :meth:`from_writes` 或 :meth:`overlay` 构造；
    分析器只允许使用公开读取 API，不得重新解析 HEX 文件。
    """

    def __init__(self, name: str):
        self.name = name
        self._segments: list[_Segment] = []
        self._starts: list[int] = []
        self._frozen = False
        self._digest: str | None = None
        self._layer_max: int = 0

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    def _assert_mutable(self) -> None:
        if self._frozen:
            raise TypeError(f"映像产品 {self.name!r} 已冻结，不可修改")

    def _put(self, address: int, data: bytes, runs: list[_Run], *,
             reject_conflicts: bool, layer: int,
             conflicts: list[ConflictPoint] | None,
             merge_same_value: bool = False) -> None:
        """向可变映像写入一段带来源游程的数据。

        ``reject_conflicts=True`` 时同地址异值收集到 ``conflicts``（同层模式）；
        同值位置在 ``merge_same_value=True`` 时归并并保留全部来源。
        ``reject_conflicts=False`` 时新数据覆盖旧数据（显式 overlay 模式）。
        """
        self._assert_mutable()
        if not data:
            return
        end = address + len(data)
        if address < 0 or end > ADDR_SPACE_SIZE:
            raise OverflowError(
                f"写入 {address:#010x}..{end - 1:#010x} 超出 32 位地址空间"
            )
        if sum(r.length for r in runs) != len(data):
            raise ValueError("来源游程长度之和必须等于数据长度")

        lo = bisect_left(self._starts, address)
        if lo > 0 and self._segments[lo - 1].end > address:
            lo -= 1
        hi = lo
        while hi < len(self._segments) and self._segments[hi].start < end:
            hi += 1
        affected = self._segments[lo:hi]

        leftovers: list[_Segment] = []
        old_pieces: list[tuple[int, int, bytes, list[_Run]]] = []
        for seg in affected:
            if seg.start < address:
                cut = address - seg.start
                leftovers.append(_Segment(
                    seg.start, seg.data[:cut], _slice_runs(seg.runs, 0, cut)))
            if seg.end > end:
                cut = end - seg.start
                leftovers.append(_Segment(
                    end, seg.data[cut:],
                    _slice_runs(seg.runs, cut, len(seg.data))))
            ov_lo = max(seg.start, address)
            ov_hi = min(seg.end, end)
            if ov_hi > ov_lo:
                sl_lo, sl_hi = ov_lo - seg.start, ov_hi - seg.start
                old_pieces.append((
                    ov_lo - address, ov_hi - address,
                    seg.data[sl_lo:sl_hi],
                    _slice_runs(seg.runs, sl_lo, sl_hi),
                ))

        merged_data, merged_runs = self._build_region(
            address, data, runs, old_pieces,
            reject_conflicts=reject_conflicts, layer=layer,
            conflicts=conflicts, merge_same_value=merge_same_value,
        )

        del self._segments[lo:hi]
        new_segments = leftovers + [_Segment(address, merged_data, merged_runs)]
        self._segments[lo:lo] = sorted(new_segments, key=lambda s: s.start)
        self._merge_adjacent()
        self._starts = [s.start for s in self._segments]
        self._layer_max = max(self._layer_max, layer)

    @staticmethod
    def _build_region(base_address, new_data, new_runs, old_pieces, *,
                      reject_conflicts, layer, conflicts,
                      merge_same_value=False):
        """对新区间内的旧字节与新字节按游程边界逐段合并。

        冲突点地址补回写入区间基址 ``base_address``，输出绝对地址。
        """
        length = len(new_data)
        bounds = {0, length}
        pos = 0
        for run in new_runs:
            bounds.add(pos)
            bounds.add(pos + run.length)
            pos += run.length
        for lo, hi, _data, runs in old_pieces:
            bounds.add(lo)
            bounds.add(hi)
            rpos = lo
            for run in runs:
                bounds.add(rpos)
                bounds.add(rpos + run.length)
                rpos += run.length
        ordered = sorted(bounds)

        old_pieces = sorted(old_pieces, key=lambda p: p[0])
        piece_idx = 0

        out_data = bytearray()
        out_runs: list[_Run] = []
        npos = 0
        for run in new_runs:
            nlo, nhi = npos, npos + run.length
            new_sources = run.sources
            npos = nhi

            sub_bounds = [b for b in ordered if nlo < b < nhi]
            edges = [nlo] + sub_bounds + [nhi]
            for k in range(len(edges) - 1):
                a, b = edges[k], edges[k + 1]
                new_val = new_data[a:b]

                while piece_idx < len(old_pieces) and old_pieces[piece_idx][1] <= a:
                    piece_idx += 1
                old_val = None
                old_sources: tuple[Source, ...] = ()
                if piece_idx < len(old_pieces):
                    plo, phi, pdata, pruns = old_pieces[piece_idx]
                    if plo <= a and phi >= b:
                        old_val = pdata[a - plo:b - plo]
                        rpos = plo
                        for rr in pruns:
                            if rpos <= a < rpos + rr.length:
                                old_sources = rr.sources
                                break
                            rpos += rr.length

                if old_val is None:
                    final_val, final_sources = new_val, new_sources
                elif reject_conflicts:
                    # 同层：逐字节比较；同值可归并保留全部来源，异值即冲突
                    for off in range(b - a):
                        if old_val[off] != new_val[off]:
                            conflicts.append(ConflictPoint(
                                address=base_address + a + off,
                                old_value=old_val[off],
                                new_value=new_val[off],
                                layer=layer,
                                old_sources=old_sources,
                                new_source=new_sources[0],
                            ))
                    if merge_same_value and old_val == new_val:
                        final_val = new_val
                        final_sources = _union_sources(old_sources,
                                                       new_sources)
                    else:
                        final_val, final_sources = new_val, new_sources
                elif old_val == new_val:
                    # 同值归并，保留全部来源
                    final_val = new_val
                    final_sources = _union_sources(old_sources, new_sources)
                else:
                    # 显式 overlay：高层新值胜出，来源随高层替换
                    final_val, final_sources = new_val, new_sources

                out_data.extend(final_val)
                out_runs.append(_Run(b - a, final_sources))

        return bytes(out_data), _coalesce_runs(out_runs)

    def _merge_adjacent(self) -> None:
        """相邻段合并为最大连续已定义区间（含来源游程衔接）。"""
        if len(self._segments) < 2:
            return
        merged: list[_Segment] = [self._segments[0]]
        for seg in self._segments[1:]:
            prev = merged[-1]
            if prev.end == seg.start:
                data = prev.data + seg.data
                runs = _coalesce_runs(prev.runs + seg.runs)
                merged[-1] = _Segment(prev.start, data, runs)
            else:
                merged.append(seg)
        self._segments = merged

    @classmethod
    def from_writes(cls, name: str, writes: Iterable, *,
                    layer: int = 0) -> "SparseImage":
        """把一个层次内的 :class:`~heximg.records.DataWrite` 构造成产品。

        同层任意同地址异值都会拒绝整份产品（一次性报出全部冲突点）。
        """
        img = cls(name)
        conflicts: list[ConflictPoint] = []
        for w in writes:
            img._put(
                w.address, bytes(w.data),
                [_Run(len(w.data), (w.source,))],
                reject_conflicts=True, layer=layer, conflicts=conflicts,
                merge_same_value=True,
            )
        if conflicts:
            raise OverlayConflict(conflicts, layer=layer)
        img._layer_max = layer
        img._frozen = True
        return img

    def overlay(self, higher: "SparseImage", *, layer: int) -> "SparseImage":
        """以 ``higher`` 为更高显式层次覆盖本映像，返回新产品。

        * 高层未定义处保持本映像原值；
        * 同地址同值归并并保留两层全部来源；
        * 同地址异值由高层胜出——只看层次，不看到达顺序。
        """
        result = SparseImage(self.name)
        result._segments = [
            _Segment(s.start, s.data,
                     [_Run(r.length, r.sources) for r in s.runs])
            for s in self._segments
        ]
        result._starts = [s.start for s in result._segments]
        result._layer_max = max(self._layer_max, layer)
        for seg in higher._segments:
            offset = 0
            for run in seg.runs:
                chunk = seg.data[offset:offset + run.length]
                result._put(
                    seg.start + offset, chunk,
                    [_Run(run.length, run.sources)],
                    reject_conflicts=False, layer=layer, conflicts=None,
                )
                offset += run.length
        result._frozen = True
        return result

    # ------------------------------------------------------------------ #
    # 公开读取 API（分析器只能消费这些，禁止重新解析文件）
    # ------------------------------------------------------------------ #
    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def layer_count(self) -> int:
        return self._layer_max + 1

    @property
    def defined_byte_count(self) -> int:
        return sum(len(s.data) for s in self._segments)

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    def address_span(self) -> tuple[int, int] | None:
        """返回 ``(最小地址, 最大地址+1)``；空映像返回 ``None``。"""
        if not self._segments:
            return None
        return self._segments[0].start, self._segments[-1].end

    def _locate(self, address: int) -> int:
        idx = bisect_left(self._starts, address)
        if idx < len(self._segments) and self._segments[idx].start == address:
            return idx
        if idx > 0 and self._segments[idx - 1].end > address:
            return idx - 1
        return -1

    def byte_at(self, address: int) -> int | None:
        """已定义返回字节值；空洞或越界返回 ``None``（绝不把空洞当 0x00）。"""
        idx = self._locate(address)
        if idx < 0:
            return None
        seg = self._segments[idx]
        return seg.data[address - seg.start]

    def sources_at(self, address: int) -> tuple[Source, ...]:
        """回查某有效字节的全部来源；空洞返回空元组。"""
        idx = self._locate(address)
        if idx < 0:
            return ()
        seg = self._segments[idx]
        off = address - seg.start
        pos = 0
        for run in seg.runs:
            if pos <= off < pos + run.length:
                return run.sources
            pos += run.length
        return ()

    def is_defined(self, address: int) -> bool:
        return self._locate(address) >= 0

    def read_spans(self, start: int, length: int) -> list[Span]:
        """范围读取：逐段区分已定义字节与空洞。

        * 单次最多返回 1 MiB（按请求长度计）；
        * 空洞以 :meth:`Span.hole` 表示，``data is None``；
        * 读取窗口越过 32 位空间上界时，越界部分按空洞处理。
        """
        if length < 0:
            raise ReadRangeError(f"读取长度不能为负：{length}")
        if length > MAX_READ_SIZE:
            raise ReadRangeError(
                f"单次范围读取上限 1 MiB，请求 {length} 字节"
            )
        if start < 0 or start >= ADDR_SPACE_SIZE:
            raise ReadRangeError(f"读取起始地址越界：{start:#010x}")
        if length == 0:
            return []

        req_end = start + length
        end = min(req_end, ADDR_SPACE_SIZE)
        out: list[Span] = []
        cursor = start
        for seg in self._segments:
            if seg.end <= start:
                continue
            if seg.start >= end:
                break
            if seg.start > cursor:
                out.append(Span.hole(cursor, seg.start - cursor))
            rlo = max(seg.start, start)
            rhi = min(seg.end, end)
            sl_lo, sl_hi = rlo - seg.start, rhi - seg.start
            sliced = _slice_runs(seg.runs, sl_lo, sl_hi)
            out.append(Span.defined(
                rlo, seg.data[sl_lo:sl_hi],
                ((r.length, r.sources) for r in sliced),
            ))
            cursor = rhi
        if cursor < req_end:
            # 含越过 32 位上界的部分：统一按空洞返回，总长度等于请求长度
            out.append(Span.hole(cursor, req_end - cursor))
        return out

    def defined_spans(self) -> list[Span]:
        """全部已定义段（无空洞），供分析器做稀疏扫描。"""
        return [
            Span.defined(
                seg.start, seg.data,
                ((r.length, r.sources) for r in seg.runs),
            )
            for seg in self._segments
        ]

    def digest(self) -> str:
        """内容摘要：段布局 + 字节 + 来源全部参与哈希（结果缓存）。"""
        if self._digest is not None:
            return self._digest
        h = hashlib.sha256()
        h.update(b"SPARSE-IMAGE-V1\n")
        h.update(self.name.encode("utf-8"))
        h.update(b"\n")
        for seg in self._segments:
            h.update(f"SEG {seg.start} {len(seg.data)}\n".encode())
            h.update(seg.data)
            h.update(b"\nRUNS\n")
            for run in seg.runs:
                h.update(f"{run.length} ".encode())
                for src in run.sources:
                    h.update(repr(src).encode("utf-8"))
                    h.update(b";")
                h.update(b"\n")
        self._digest = h.hexdigest()
        return self._digest

    def iter_bytes(self, start: int, end: int):
        """供分析器分页扫描的字节迭代器，空洞产出 ``None``。"""
        for span in self.read_spans(start, end - start):
            if span.is_hole:
                for _ in range(span.length):
                    yield None
            else:
                for b in span.data:
                    yield b
