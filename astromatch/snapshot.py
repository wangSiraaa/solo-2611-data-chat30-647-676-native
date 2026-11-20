"""历元方向快照：匹配仓消费的唯一接口，不含速度等星表私有信息。

快照是星表服务在某一历元的“完整方向表 + 来源版本”的不可变导出物，
也允许外部系统按同一 JSON 格式自行制作（见 make_snapshot）。
匹配仓只依赖本模块的结构，不读取星表的 r/v 私有表。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .vectors import as_vec3, unit_length_error, validate_unit_direction

SNAPSHOT_SCHEMA = "astromatch.snapshot/v1"


@dataclass(frozen=True)
class SnapshotEntry:
    star_id: str
    direction: np.ndarray  # 单位方向；零向量历元不会出现在快照中
    available: bool = True

    def __post_init__(self) -> None:
        # frozen dataclass 里用 object.__setattr__ 固化为不可变数组
        object.__setattr__(self, "direction", np.array(self.direction, dtype=np.float64, copy=True))

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "star_id": self.star_id,
            "available": bool(self.available),
            "direction": [float(x) for x in self.direction],
        }


@dataclass(frozen=True)
class Snapshot:
    epoch: int                                  # 整数历元日
    entries: List[SnapshotEntry]
    source: str = "external"                    # 来源：catalog 版本号 或 external
    catalog_release: Optional[str] = None       # 星表发布版本（外部快照可为 None）
    unit_tol: float = 1e-9                      # 方向单位长度声明容差
    schema: str = SNAPSHOT_SCHEMA
    snapshot_id: str = field(default="")        # 由规范化字节哈希派生

    @property
    def star_ids(self) -> List[str]:
        return [e.star_id for e in self.entries]

    def direction_matrix(self) -> np.ndarray:
        if not self.entries:
            return np.zeros((0, 3), dtype=np.float64)
        return np.vstack([e.direction for e in self.entries])

    def canonical_bytes(self) -> bytes:
        """规范化 JSON 字节；snapshot_id 即其 sha256 前 16 位。"""
        payload = {
            "schema": self.schema,
            "epoch": int(self.epoch),
            "source": self.source,
            "catalog_release": self.catalog_release,
            "unit_tol": float(self.unit_tol),
            "entries": [e.to_jsonable() for e in self.entries],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def to_jsonable(self) -> Dict[str, Any]:
        return json.loads(self.canonical_bytes().decode("utf-8"))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_jsonable(), ensure_ascii=False, indent=indent)


def make_snapshot(
    epoch: int,
    rows: Sequence[Dict[str, Any]],
    *,
    source: str = "external",
    catalog_release: Optional[str] = None,
    unit_tol: float = 1e-9,
) -> Snapshot:
    """从 {star_id, direction(单位向量)} 行构造并校验快照（外部同格式入口）。

    零向量/不可用条目不进入快照（星表服务侧在导出时已剔除）。
    """
    entries: List[SnapshotEntry] = []
    seen = set()
    for row in rows:
        sid = str(row["star_id"])
        if sid in seen:
            raise ValueError(f"快照内 star_id 重复: {sid}")
        seen.add(sid)
        u = validate_unit_direction(row["direction"], unit_tol, name=f"direction[{sid}]")
        entries.append(SnapshotEntry(star_id=sid, direction=u))
    snap = Snapshot(
        epoch=int(epoch), entries=entries, source=source,
        catalog_release=catalog_release, unit_tol=float(unit_tol),
    )
    sid_hash = hashlib.sha256(snap.canonical_bytes()).hexdigest()[:16]
    object.__setattr__(snap, "snapshot_id", sid_hash)
    return snap


def snapshot_from_jsonable(payload: Dict[str, Any], *, unit_tol_override: Optional[float] = None) -> Snapshot:
    """读取并校验外部同格式快照；不假设其来自本星表。"""
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"不支持的快照 schema: {payload.get('schema')!r}，期望 {SNAPSHOT_SCHEMA}")
    tol = float(payload["unit_tol"]) if unit_tol_override is None else float(unit_tol_override)
    rows = []
    for e in payload["entries"]:
        u = as_vec3(e["direction"], name=f"direction[{e['star_id']}]")
        if not e.get("available", True):
            continue  # 不可用条目不参与匹配
        if unit_length_error(u) > tol:
            raise ValueError(
                f"快照方向 {e['star_id']} 单位长度误差 {unit_length_error(u):.3e} 超过容差 {tol:.3e}")
        rows.append({"star_id": e["star_id"], "direction": u})
    return make_snapshot(
        int(payload["epoch"]), rows,
        source=str(payload.get("source", "external")),
        catalog_release=payload.get("catalog_release"),
        unit_tol=tol,
    )
