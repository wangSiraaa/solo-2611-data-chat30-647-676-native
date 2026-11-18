"""降雨/损失批次：导入、格点修订与显式版本。

关键语义
--------
* 每格存非负有理降雨 ``r`` 与损失 ``l``；模型本地体积为
  ``A * max(r - l, 0)``，由分析引擎计算，本层只保存原始格；
* **未收到数据不等于零**：批次显式声明其完整覆盖的节点集合与
  半开时间区间 ``[lo, hi)``，格点必须在该矩形内齐备，缺格即非法；
  分析请求所需格若落在批次覆盖之外，结果为 INCOMPLETE 而非补零；
* 修订某一格产生**新版本**（内容哈希变化），父版本保留且不可变；
* 批次绑定其目录哈希，目录与数据各自冻结身份。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

from .catalog import Catalog
from .errors import RainError
from .rational import frac, frac_text

MAX_INPUT_STEPS = 200


@dataclass(frozen=True)
class RainBatch:
    """冻结的降雨/损失批次。"""

    catalog_hash: str
    lo: int
    hi: int
    nodes: Tuple[str, ...]
    cells: Mapping[Tuple[str, int], Tuple[Fraction, Fraction]]
    rain_hash: str
    parent_hash: Optional[str] = None

    def cell(self, node_id: str, t: int) -> Tuple[Fraction, Fraction]:
        return self.cells[(node_id, t)]

    def covers(self, node_ids: Iterable[str], lo: int, hi: int) -> bool:
        """该批次是否完整覆盖给定节点集合与时间区间（缺一格即 False）。"""
        node_set: Set[str] = set(self.nodes)
        for nid in node_ids:
            if nid not in node_set:
                return False
        if lo < self.lo or hi > self.hi:
            return False
        for nid in node_ids:
            for t in range(lo, hi):
                if (nid, t) not in self.cells:
                    return False
        return True

    def to_payload(self) -> dict:
        return {
            "format": "hydro-rain/1",
            "catalog_hash": self.catalog_hash,
            "parent_hash": self.parent_hash,
            "lo": self.lo,
            "hi": self.hi,
            "nodes": list(self.nodes),
            "cells": [
                {
                    "node_id": nid,
                    "t": t,
                    "rain": frac_text(r),
                    "loss": frac_text(l),
                }
                for (nid, t), (r, l) in sorted(
                    self.cells.items(), key=lambda x: (x[0][0], x[0][1])
                )
            ],
        }

    def canonical_text(self) -> str:
        return json.dumps(
            self._content_payload(),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    def _content_payload(self) -> dict:
        """身份载荷：只含数据内容；parent_hash 是溯源元数据，不入哈希。

        这样「无变化修订」与原批次同身份，会被显式拒绝；同内容不同
        修订历史的批次也是同一个冻结产品（内容寻址）。
        """
        p = self.to_payload()
        p.pop("parent_hash", None)
        return p

    @staticmethod
    def hash_payload(payload: Mapping) -> str:
        p = dict(payload)
        p.pop("parent_hash", None)  # 与 _content_payload 保持一致
        text = json.dumps(
            p, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping, catalog: Catalog) -> "RainBatch":
        if not isinstance(payload, Mapping) or payload.get("format") != "hydro-rain/1":
            raise RainError("非法降雨产品：缺少 hydro-rain/1 格式标识")
        if payload["catalog_hash"] != catalog.content_hash:
            raise RainError("降雨产品绑定的目录哈希与给定目录不一致")
        cells: Dict[Tuple[str, int], Tuple[Fraction, Fraction]] = {}
        for c in payload["cells"]:
            nid, t = c["node_id"], int(c["t"])
            r, l = frac(c["rain"]), frac(c["loss"])
            if r < 0 or l < 0:
                raise RainError(f"格 ({nid}, t={t}) 降雨/损失必须非负")
            cells[(nid, t)] = (r, l)
        batch = cls.build(
            catalog,
            int(payload["lo"]),
            int(payload["hi"]),
            tuple(payload["nodes"]),
            cells,
            parent_hash=payload.get("parent_hash"),
            expected_hash=cls.hash_payload(payload),
        )
        return batch

    @classmethod
    def build(
        cls,
        catalog: Catalog,
        lo: int,
        hi: int,
        nodes: Sequence[str],
        cells: Mapping[Tuple[str, int], Tuple[Fraction, Fraction]],
        parent_hash: Optional[str] = None,
        expected_hash: Optional[str] = None,
    ) -> "RainBatch":
        """构造并完整校验一个冻结批次。"""
        if not isinstance(lo, int) or not isinstance(hi, int) or lo >= hi:
            raise RainError(f"时间区间非法: [{lo}, {hi})")
        if hi - lo > MAX_INPUT_STEPS:
            raise RainError(
                f"输入步数 {hi - lo} 超过限额 {MAX_INPUT_STEPS}"
            )
        node_set = tuple(dict.fromkeys(nodes))
        for nid in node_set:
            if not catalog.has_node(nid):
                raise RainError(f"降雨节点 {nid!r} 不在目录中")
        normalized: Dict[Tuple[str, int], Tuple[Fraction, Fraction]] = {}
        for (nid, t), v in cells.items():
            if nid not in node_set:
                raise RainError(f"格点节点 {nid!r} 不在批次节点集合内")
            if not (lo <= t < hi):
                raise RainError(f"格点 ({nid}, t={t}) 越出 [{lo}, {hi})")
            r = v[0] if isinstance(v[0], Fraction) else frac(v[0])
            l = v[1] if isinstance(v[1], Fraction) else frac(v[1])
            if r < 0 or l < 0:
                raise RainError(f"格 ({nid}, t={t}) 降雨/损失必须非负")
            normalized[(nid, t)] = (r, l)
        # 完整矩形校验：缺任一必需格即失败（未收到 ≠ 零）
        missing = [
            (nid, t)
            for nid in node_set
            for t in range(lo, hi)
            if (nid, t) not in normalized
        ]
        if missing:
            sample = ", ".join(f"({n},t={t})" for n, t in missing[:5])
            raise RainError(
                f"批次缺 {len(missing)} 个必需格（未收到不等于零）: {sample}"
                + (" …" if len(missing) > 5 else "")
            )
        obj = cls(
            catalog_hash=catalog.content_hash,
            lo=lo,
            hi=hi,
            nodes=tuple(node_set),
            cells=dict(sorted(normalized.items(), key=lambda x: (x[0][0], x[0][1]))),
            rain_hash="",
            parent_hash=parent_hash,
        )
        h = cls.hash_payload(obj.to_payload())
        if expected_hash is not None and h != expected_hash:
            raise RainError("降雨批次内容哈希与期望不符")
        object.__setattr__(obj, "rain_hash", h)
        return obj


class RainRepo:
    """降雨版本仓库，封装事实仓中的批次读写与修订。"""

    def __init__(self, store, catalog: Catalog) -> None:
        self.store = store
        self.catalog = catalog
        # 目录必须已冻结入库
        self.store.save_catalog(catalog)

    def import_batch(
        self,
        lo: int,
        hi: int,
        nodes: Sequence[str],
        cells: Mapping[Tuple[str, int], Tuple],
    ) -> RainBatch:
        """导入一个完整矩形批次并冻结为新版本。"""
        batch = RainBatch.build(self.catalog, lo, hi, list(nodes), cells)
        self.store.save_rain(
            batch.rain_hash,
            batch.catalog_hash,
            None,
            batch.lo,
            batch.hi,
            batch.cells,
        )
        return batch

    def revise_cell(
        self,
        parent: RainBatch,
        node_id: str,
        t: int,
        rain=None,
        loss=None,
    ) -> RainBatch:
        """修订单个格点（仅改给定字段），产生显式新版本。

        旧批次及其分析档案保持不变；新版本通过 ``parent_hash`` 溯源。
        """
        if parent.catalog_hash != self.catalog.content_hash:
            raise RainError("父批次不属于当前目录，不能在此仓库修订")
        if (node_id, t) not in parent.cells:
            raise RainError(f"待修订格 ({node_id}, t={t}) 不在父批次覆盖内")
        old_r, old_l = parent.cells[(node_id, t)]
        new_r = old_r if rain is None else frac(rain)
        new_l = old_l if loss is None else frac(loss)
        if new_r < 0 or new_l < 0:
            raise RainError("修订值必须非负")
        new_cells = dict(parent.cells)
        new_cells[(node_id, t)] = (new_r, new_l)
        child = RainBatch.build(
            self.catalog,
            parent.lo,
            parent.hi,
            parent.nodes,
            new_cells,
            parent_hash=parent.rain_hash,
        )
        if child.rain_hash == parent.rain_hash:
            raise RainError("修订未改变任何格点内容，不产生新版本")
        self.store.save_rain(
            child.rain_hash,
            child.catalog_hash,
            parent.rain_hash,
            child.lo,
            child.hi,
            child.cells,
        )
        return child

    def load(self, rain_hash: str) -> RainBatch:
        rows = self.store.load_rain_cells(rain_hash)
        meta = self.store.load_rain_meta(rain_hash)
        cells = {k: (frac(v[0]), frac(v[1])) for k, v in rows.items()}
        return RainBatch.build(
            self.catalog,
            meta["lo"],
            meta["hi"],
            tuple(
                nid
                for nid in dict.fromkeys(k[0] for k in sorted(cells))
            ),
            cells,
            parent_hash=meta["parent_hash"],
            expected_hash=rain_hash,
        )
