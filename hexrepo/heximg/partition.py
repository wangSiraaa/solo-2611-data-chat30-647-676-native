"""设备分区目录（不可变、带修订号、禁止重叠）。

每个分区声明 ``start``、``length`` 以及允许读取的填充值策略 ``fill``。
分区目录的每次修订都产生新的修订号；旧差量报告绑定旧修订，
不会因目录后来被修订而改变归属。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import PartitionError
from .image import ADDR_SPACE_SIZE

# 允许的填充值策略：0..255 字节值，或 None 表示该分区不允许物化视图
FillPolicy = int | None


@dataclass(frozen=True)
class Partition:
    """一个设备分区。"""

    name: str
    start: int
    length: int
    fill: FillPolicy = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise PartitionError("分区名必须是非空字符串")
        if not (0 <= self.start < ADDR_SPACE_SIZE):
            raise PartitionError(
                f"分区 {self.name!r} 起始地址越界：{self.start:#010x}"
            )
        if self.length <= 0:
            raise PartitionError(
                f"分区 {self.name!r} 长度必须为正：{self.length}"
            )
        if self.start + self.length > ADDR_SPACE_SIZE:
            raise PartitionError(
                f"分区 {self.name!r} 越过 32 位地址空间上界："
                f"{self.start:#010x}+{self.length}"
            )
        if self.fill is not None and not (0 <= int(self.fill) <= 255):
            raise PartitionError(
                f"分区 {self.name!r} 填充值必须在 0..255：{self.fill}"
            )

    @property
    def end(self) -> int:
        return self.start + self.length

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start": self.start,
            "length": self.length,
            "fill": self.fill,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Partition":
        return cls(name=d["name"], start=int(d["start"]),
                   length=int(d["length"]), fill=d.get("fill"))


class PartitionDirectory:
    """不可变分区目录；修订通过 :meth:`revised` 生成新对象。"""

    def __init__(self, partitions: list[Partition],
                 revision: str = "r1", *, note: str = ""):
        ordered = sorted(partitions, key=lambda p: p.start)
        names = [p.name for p in ordered]
        if len(set(names)) != len(names):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise PartitionError(f"分区名重复：{dup}")
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.start < prev.end:
                raise PartitionError(
                    f"分区 {prev.name!r}[{prev.start:#x}..{prev.end:#x}) 与 "
                    f"{cur.name!r}[{cur.start:#x}..{cur.end:#x}) 重叠"
                )
        self._partitions: tuple[Partition, ...] = tuple(ordered)
        self._revision = revision
        self._note = note

    # ------------------------------------------------------------------ #
    @property
    def partitions(self) -> tuple[Partition, ...]:
        return self._partitions

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def note(self) -> str:
        return self._note

    def get(self, name: str) -> Partition:
        for p in self._partitions:
            if p.name == name:
                return p
        raise KeyError(f"分区 {name!r} 不存在（修订 {self._revision}）")

    def revised(self, partitions: list[Partition], *, revision: str,
                note: str = "") -> "PartitionDirectory":
        """以新分区列表生成下一修订目录；修订号不得与当前相同。"""
        if revision == self._revision:
            raise PartitionError("新目录修订号必须与当前修订不同")
        return PartitionDirectory(partitions, revision=revision, note=note)

    def revision_digest(self) -> str:
        h = hashlib.sha256()
        h.update(f"PARTITION-DIR {self._revision}\n".encode())
        for p in self._partitions:
            h.update(repr(p.to_dict()).encode())
            h.update(b"\n")
        return h.hexdigest()[:16]

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "revision": self._revision,
            "note": self._note,
            "partitions": [p.to_dict() for p in self._partitions],
        }

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str | Path) -> "PartitionDirectory":
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return cls(
            [Partition.from_dict(d) for d in doc["partitions"]],
            revision=doc.get("revision", "r1"),
            note=doc.get("note", ""),
        )
