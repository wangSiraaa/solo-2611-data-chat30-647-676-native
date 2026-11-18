"""接入 / overlay 目录（manifest）。

清单按顺序列出多个源文件，并为每个源文件声明显式 overlay 层次。
层次号越小越靠近底层；同层文件之间的任何同地址异值冲突都会拒绝整份产品，
高层同地址异值由高层胜出——**层次是唯一裁决依据，与文件出现顺序无关**。

每个文件的扩展地址基址作用域严格限定在该文件内（解析器每次从零状态开始），
不会泄漏到另一个文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .image import SparseImage
from .records import parse_hex_file


@dataclass(frozen=True)
class ManifestEntry:
    """清单中的一个源文件条目。"""

    path: str
    layer: int

    def to_dict(self) -> dict:
        return {"path": self.path, "layer": self.layer}

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestEntry":
        return cls(path=d["path"], layer=int(d["layer"]))


@dataclass(frozen=True)
class Manifest:
    """不可变接入清单。"""

    entries: tuple[ManifestEntry, ...]
    name: str = "product"

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return cls(
            entries=tuple(ManifestEntry.from_dict(e) for e in doc["entries"]),
            name=doc.get("name", Path(path).stem),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_files(cls, files: list[str], *, name: str = "product",
                   layers: list[int] | None = None) -> "Manifest":
        """便捷构造：默认全部放在第 0 层（同层冲突即拒绝）。"""
        if layers is None:
            layers = [0] * len(files)
        if len(layers) != len(files):
            raise ValueError("layers 与 files 长度必须一致")
        return cls(
            entries=tuple(ManifestEntry(p, int(layer))
                          for p, layer in zip(files, layers)),
            name=name,
        )


def build_product(manifest: Manifest, *, base_dir: str | Path | None = None
                  ) -> SparseImage:
    """按清单构建冻结的 :class:`SparseImage` 产品。

    1. 逐文件独立解析（扩展地址作用域不跨文件）；
    2. 同层文件合并，任何冲突抛出
       :class:`~heximg.errors.OverlayConflict`，拒绝整份产品；
    3. 按层次号由低到高显式 overlay，高层裁决。
    """
    base = Path(base_dir) if base_dir is not None else Path(".")

    # 按层收集，保留层内文件顺序
    layer_files: dict[int, list[str]] = {}
    for entry in manifest.entries:
        layer_files.setdefault(entry.layer, []).append(entry.path)

    layer_images: dict[int, SparseImage] = {}
    for layer, paths in layer_files.items():
        writes = []
        for rel in paths:
            p = Path(rel)
            full = p if p.is_absolute() else base / p
            parsed = parse_hex_file(str(full))
            writes.extend(parsed.writes)
        # 同层：冲突直接拒绝，不做覆盖
        layer_images[layer] = SparseImage.from_writes(
            f"{manifest.name}:layer{layer}", writes, layer=layer
        )

    ordered_layers = sorted(layer_images)
    product = SparseImage(manifest.name)
    product._layer_max = 0
    product._frozen = True  # 空基底；overlay 内部会创建新对象
    first = True
    for layer in ordered_layers:
        if first:
            product = layer_images[layer]
            first = False
        else:
            product = product.overlay(layer_images[layer], layer=layer)
    product.name = manifest.name
    return product
