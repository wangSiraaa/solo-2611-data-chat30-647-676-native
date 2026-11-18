"""测试共用工具：独立逐字节参考模型与随机合成场景。

参考模型不使用库内任何稀疏结构，仅用 dict 逐字节建模，
作为来源保留、overlay 裁决与差量分类的独立预言机。
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass
from pathlib import Path

from heximg.image import Source
from heximg.partition import Partition
from heximg.synth import (
    data_record,
    eof_record,
    extended_linear_record,
    write_text,
)


class ReferenceModel:
    """逐字节 dict 模型：addr -> (value, tuple[Source,...])。"""

    def __init__(self) -> None:
        self.cells: dict[int, tuple[int, tuple[Source, ...]]] = {}
        self.conflicts: list[int] = []

    def apply_layer_writes(self, writes) -> None:
        """模拟同层接入：同值合并来源，异值记录冲突地址。"""
        for w in writes:
            for i, val in enumerate(w.data):
                addr = w.address + i
                if addr not in self.cells:
                    self.cells[addr] = (val, (w.source,))
                else:
                    old_val, old_srcs = self.cells[addr]
                    if old_val == val:
                        if w.source not in old_srcs:
                            self.cells[addr] = (
                                old_val, old_srcs + (w.source,))
                    else:
                        if addr not in self.conflicts:
                            self.conflicts.append(addr)

    def overlay(self, higher: "ReferenceModel") -> None:
        """模拟显式高层覆盖（只看层次，不看到达顺序）。"""
        for addr, (val, srcs) in higher.cells.items():
            if addr not in self.cells:
                self.cells[addr] = (val, srcs)
            else:
                old_val, old_srcs = self.cells[addr]
                if old_val == val:
                    merged = old_srcs + tuple(s for s in srcs if s not in old_srcs)
                    self.cells[addr] = (old_val, merged)
                else:
                    self.cells[addr] = (val, srcs)

    def value(self, addr: int):
        return None if addr not in self.cells else self.cells[addr][0]

    def sources(self, addr: int):
        return () if addr not in self.cells else self.cells[addr][1]


@dataclass
class Scenario:
    files: list[str]
    layers: list[int]
    partitions: list[Partition]
    ref: ReferenceModel
    conflict_addresses: list[int]
    tmpdir: str


def make_random_scenario(seed: int, *, domain_end: int = 0x20000,
                         n_files: int = 6, max_layers: int = 3,
                         n_partitions: int = 5) -> Scenario:
    """生成一个随机小地址域场景（HEX 文件 + 分区 + 参考模型）。"""
    rng = random.Random(seed)
    tmpdir = tempfile.mkdtemp(prefix=f"heximg_scn_{seed}_")
    files: list[str] = []
    layers: list[int] = []
    layer_writes: dict[int, list] = {}

    from heximg.records import parse_hex_file

    for fi in range(n_files):
        upper = rng.choice([0x0000, 0x0001])
        lines = [extended_linear_record(upper)]
        n_recs = rng.randint(2, 8)
        for _ in range(n_recs):
            addr16 = rng.randrange(0, 0x10000 - 16)
            length = rng.randint(1, 12)
            payload = bytes(rng.randrange(0, 256) for _ in range(length))
            lines.append(data_record(addr16, payload))
        lines.append(eof_record())
        path = write_text(Path(tmpdir) / f"f{fi}.hex", lines)
        files.append(str(path))
        layer = rng.randrange(0, max_layers)
        layers.append(layer)
        parsed = parse_hex_file(str(path))
        layer_writes.setdefault(layer, []).extend(parsed.writes)

    # 参考模型：先逐层（含同层冲突检测），再按层次 overlay
    refs: dict[int, ReferenceModel] = {}
    conflict_addresses: list[int] = []
    for layer, writes in layer_writes.items():
        rm = ReferenceModel()
        rm.apply_layer_writes(writes)
        refs[layer] = rm
        conflict_addresses.extend(rm.conflicts)
    final_ref = ReferenceModel()
    for layer in sorted(refs):
        final_ref.overlay(refs[layer])

    # 随机不重叠分区，覆盖部分地址域（制造分区内外边界）
    partitions: list[Partition] = []
    cursor = rng.randrange(0, 0x100)
    for pi in range(n_partitions):
        if cursor >= domain_end - 16:
            break
        length = rng.randint(0x40, min(0x2000, domain_end - cursor))
        fill = None if rng.random() < 0.3 else rng.randrange(0, 256)
        partitions.append(Partition(f"part{pi}", cursor, length, fill))
        cursor += length + rng.randint(0, 0x400)

    return Scenario(files, layers, partitions, final_ref,
                    sorted(set(conflict_addresses)), tmpdir)


def expected_byte_classes(ref_a: ReferenceModel, ref_b: ReferenceModel,
                          part: Partition, view: str):
    """参考模型逐字节分类，返回 (classes, flags, hole_stats)。"""
    classes: dict[int, str] = {}
    flags: dict[int, tuple[bool, bool]] = {}
    stats = {"both_hole": 0, "hole_to_defined": 0, "defined_to_hole": 0}
    for addr in range(part.start, part.end):
        va, vb = ref_a.value(addr), ref_b.value(addr)
        ah, bh = va is None, vb is None
        if ah and bh:
            stats["both_hole"] += 1
            if view == "materialized":
                classes[addr] = "unchanged"
                flags[addr] = (True, True)
        elif ah:
            stats["hole_to_defined"] += 1
            classes[addr] = "added"
            flags[addr] = (True, False)
        elif bh:
            stats["defined_to_hole"] += 1
            classes[addr] = "removed"
            flags[addr] = (False, True)
        else:
            classes[addr] = "unchanged" if va == vb else "changed"
            flags[addr] = (False, False)
    return classes, flags, stats


def cleanup(scn: Scenario) -> None:
    import shutil
    shutil.rmtree(scn.tmpdir, ignore_errors=True)
