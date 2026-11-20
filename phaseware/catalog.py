"""独立相位约束目录（生产侧）。

职责边界（题面要求）：

* 管理有向观测边的新增、按来源撤回、边界/余数更正，全部落在 SQLite 编辑表；
* 每次发布生成**不可变**新产品（JSON 可导出），产品之间通过 product_id 区分；
* 提供有限候选差值、同向边交集、路径总差范围等只读查询；
* 不管理锚点、不管理位移尺度 scale，也不做全局可行性判定（那是分析仓的职责）。

冲突观测（同向交集为空、回路残差不为零）不会被自动删除，目录照实发布，
只在 :meth:`Catalog.lint` 与分析报告中解释。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .exact import (
    BinaryConstraint,
    cycle_basis,
    minkowski_sum,
    orient_cycle,
    signed_step_set,
)

MAX_NODES = 12
MAX_EDGES = 30
MAX_M = 16
MAX_CANDIDATES = 5


# ---------------------------------------------------------------------------
# 纯函数：候选差值
# ---------------------------------------------------------------------------


def candidates_for(r: int, m: int, lo: int, hi: int) -> tuple[int, ...]:
    """边 (u,v) 的有限候选真实差值集合。

    含义：x_v - x_u ∈ {r + M*k | lo <= r+M*k <= hi}，全部为整数。
    """
    if not (0 <= r < m):
        raise ValueError(f"余数 r={r} 必须满足 0 <= r < M={m}")
    if lo > hi:
        return ()
    # lo <= r + M*k <= hi，M>0：k_lo = ceil((lo-r)/M)，k_hi = floor((hi-r)/M)。
    # 整数 ceil(a/b)（b>0）= -floor(-a/b) = -((-a)//b)。
    k_lo = -((-(lo - r)) // m)
    k_hi = (hi - r) // m
    if k_lo > k_hi:
        return ()
    vals = tuple(r + m * k for k in range(k_lo, k_hi + 1))
    if len(vals) > MAX_CANDIDATES:
        raise ValueError(
            f"候选差值 {len(vals)} 个超过上限 {MAX_CANDIDATES}，请收紧 [lo, hi]"
        )
    return vals


# ---------------------------------------------------------------------------
# 数据传输对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeRecord:
    edge_id: int
    source: str
    u: str
    v: str
    r: int
    lo: int
    hi: int
    active: bool
    retract_reason: Optional[str]

    def candidates(self, m: int) -> tuple[int, ...]:
        return candidates_for(self.r, m, self.lo, self.hi)

    def direction_allowed(self, m: int, tail: str, head: str) -> frozenset[int]:
        """沿 tail -> head 方向解释的真实差值集合。"""
        cands = frozenset(self.candidates(m))
        if (tail, head) == (self.u, self.v):
            return cands
        if (tail, head) == (self.v, self.u):
            return frozenset(-d for d in cands)
        raise ValueError(f"边 {self.edge_id} 不连接 {tail} -> {head}")


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------


class Catalog:
    def __init__(self, path: str, m: Optional[int] = None) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        row = self.conn.execute("SELECT value FROM meta WHERE key='M'").fetchone()
        if row is None:
            if m is None:
                raise ValueError("新目录必须指定模数 M")
            if not (1 <= m <= MAX_M):
                raise ValueError(f"M 必须为 1..{MAX_M} 的正整数")
            self.conn.execute("INSERT INTO meta(key, value) VALUES('M', ?)", (m,))
            self.conn.commit()
            self.m = m
        else:
            self.m = int(row[0])
            if m is not None and m != self.m:
                raise ValueError(f"目录已固定 M={self.m}，不能改用 M={m}")

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nodes(
                node_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges(
                edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                u TEXT NOT NULL,
                v TEXT NOT NULL,
                r INTEGER NOT NULL,
                lo INTEGER NOT NULL,
                hi INTEGER NOT NULL,
                active INTEGER NOT NULL,
                retract_reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products(
                product_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                note TEXT
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- 节点 ---------------------------------------------------------------

    def add_node(self, node_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone():
            return
        if self.count_nodes() >= MAX_NODES:
            raise ValueError(f"节点数超过上限 {MAX_NODES}")
        self.conn.execute(
            "INSERT INTO nodes(node_id, created_at) VALUES(?, ?)",
            (node_id, _now()),
        )
        self.conn.commit()

    def count_nodes(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def nodes(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT node_id FROM nodes ORDER BY node_id"
            ).fetchall()
        ]

    def _require_node(self, node_id: str) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone():
            raise ValueError(f"未知节点 {node_id!r}：拒绝发布/查询")

    # -- 边的生产 ------------------------------------------------------------

    def add_edge(
        self, source: str, u: str, v: str, r: int, lo: int, hi: int
    ) -> int:
        """新增一条有向观测边；同节点对可多次新增，约束同时成立互不覆盖。"""
        if not source:
            raise ValueError("source 来源标识不能为空")
        self._require_node(u)
        self._require_node(v)
        cands = candidates_for(r, self.m, lo, hi)  # 含范围与候选数校验
        if not cands:
            raise ValueError(
                "该边界内没有任何 r+M*k 候选；目录拒绝写入空候选边（不自动删除观测，请更正边界）"
            )
        if self.count_active_edges() >= MAX_EDGES:
            raise ValueError(f"活跃边数超过上限 {MAX_EDGES}")
        cur = self.conn.execute(
            """INSERT INTO edges(source, u, v, r, lo, hi, active, created_at)
               VALUES(?, ?, ?, ?, ?, ?, 1, ?)""",
            (source, u, v, r, lo, hi, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def retract_edge(self, edge_id: int, reason: str) -> None:
        """按 edge_id 撤回某来源边；撤回不抹历史，仅令 active=0。"""
        row = self._get_edge_row(edge_id)
        if not row["active"]:
            raise ValueError(f"边 {edge_id} 已撤回，不能重复撤回")
        self.conn.execute(
            "UPDATE edges SET active=0, retract_reason=? WHERE edge_id=?",
            (reason, edge_id),
        )
        self.conn.commit()

    def retract_source(self, source: str, reason: str) -> list[int]:
        """撤回某来源的全部活跃边，返回受影响 edge_id。"""
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT edge_id FROM edges WHERE source=? AND active=1 ORDER BY edge_id",
                (source,),
            ).fetchall()
        ]
        for eid in ids:
            self.retract_edge(eid, reason)
        return ids

    def correct_edge(
        self,
        edge_id: int,
        *,
        r: Optional[int] = None,
        lo: Optional[int] = None,
        hi: Optional[int] = None,
        reason: str = "correction",
    ) -> int:
        """边界/余数更正：旧边撤回（保留审计），新边继承来源与方向，返回新 edge_id。"""
        old = self.get_edge(edge_id)
        new_r = old.r if r is None else r
        new_lo = old.lo if lo is None else lo
        new_hi = old.hi if hi is None else hi
        self.retract_edge(edge_id, f"{reason}: 被更正替代")
        return self.add_edge(old.source, old.u, old.v, new_r, new_lo, new_hi)

    def count_active_edges(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE active=1"
        ).fetchone()[0]

    def _get_edge_row(self, edge_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM edges WHERE edge_id=?", (edge_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知边 {edge_id}")
        return row

    def get_edge(self, edge_id: int) -> EdgeRecord:
        return _to_record(self._get_edge_row(edge_id))

    def active_edges(self) -> list[EdgeRecord]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE active=1 ORDER BY edge_id"
        ).fetchall()
        return [_to_record(r) for r in rows]

    # -- 只读查询 ------------------------------------------------------------

    def edge_candidates(self, edge_id: int) -> tuple[int, ...]:
        """任意边（含已撤回）的有限候选差值；空候选明确抛错而非静默删除。"""
        rec = self.get_edge(edge_id)
        cands = rec.candidates(self.m)
        if not cands:
            raise ValueError(f"边 {edge_id} 候选为空，拒绝发布该观测")
        return cands

    def same_direction_intersection(
        self, u: str, v: str
    ) -> dict[str, object]:
        """同一有向节点对 u->v 上全部活跃边的候选交集。

        多条来源边必须同时满足；交集为空即来源冲突，照实报告，不覆盖不删除。
        """
        self._require_node(u)
        self._require_node(v)
        edges = [
            _to_record(r)
            for r in self.conn.execute(
                "SELECT * FROM edges WHERE active=1 AND u=? AND v=? ORDER BY edge_id",
                (u, v),
            ).fetchall()
        ]
        if not edges:
            raise ValueError(f"u->v 方向没有活跃边：拒绝发布空交集")
        per_edge = {str(e.edge_id): e.direction_allowed(self.m, u, v) for e in edges}
        inter: Optional[frozenset[int]] = None
        for allowed in per_edge.values():
            inter = allowed if inter is None else inter & allowed
        assert inter is not None
        return {
            "u": u,
            "v": v,
            "edge_ids": [e.edge_id for e in edges],
            "per_edge": {k: sorted(v) for k, v in per_edge.items()},
            "intersection": sorted(inter),
            "conflict": len(inter) == 0,
        }

    def path_range(self, node_path: list[str]) -> dict[str, object]:
        """给定节点路径的总差范围（路径局部松弛，非全局可行范围）。

        每一步取该有向节点对上**全部活跃边的交集**作为允许步长；
        任一节点未知、某跳无活跃边或交集为空，都明确拒绝。
        """
        if len(node_path) < 2:
            raise ValueError("路径至少包含两个节点")
        for n in node_path:
            self._require_node(n)
        steps: list[dict[str, object]] = []
        step_sets: list[frozenset[int]] = []
        for a, b in zip(node_path, node_path[1:]):
            info = self.same_direction_intersection(a, b)
            inter = frozenset(info["intersection"])  # type: ignore[arg-type]
            if not inter:
                raise ValueError(f"跳 {a}->{b} 同向交集为空，路径不可发布")
            step_sets.append(inter)
            steps.append(
                {
                    "tail": a,
                    "head": b,
                    "edge_ids": info["edge_ids"],
                    "step_values": sorted(inter),
                }
            )
        totals = minkowski_sum(step_sets)
        return {
            "node_path": node_path,
            "steps": steps,
            "possible_totals": sorted(totals),
            "min": min(totals),
            "max": max(totals),
            "note": "路径局部闵可夫斯基和；不代表全图可行域内的真实可达范围",
        }

    def path_range_by_edge_ids(self, edge_id_path: list[int]) -> dict[str, object]:
        """按 edge_id 序列行走；反向经过的边按自身反向解释。"""
        if len(edge_id_path) < 1:
            raise ValueError("边路径至少包含一条边")
        recs = [self.get_edge(eid) for eid in edge_id_path]
        node_path: list[str] = []
        step_sets: list[frozenset[int]] = []
        steps: list[dict[str, object]] = []
        current: Optional[str] = None
        for rec in recs:
            if current is None:
                tail, head, forward = rec.u, rec.v, True
            elif rec.u == current:
                tail, head, forward = rec.u, rec.v, True
            elif rec.v == current:
                tail, head, forward = rec.v, rec.u, False
            else:
                raise ValueError(
                    f"边 {rec.edge_id} 与当前路径端点 {current!r} 不首尾相接"
                )
            allowed = rec.direction_allowed(self.m, tail, head)
            if not node_path:
                node_path.append(tail)
            node_path.append(head)
            step_sets.append(allowed)
            steps.append(
                {
                    "edge_id": rec.edge_id,
                    "tail": tail,
                    "head": head,
                    "forward": forward,
                    "step_values": sorted(allowed),
                }
            )
            current = head
        totals = minkowski_sum(step_sets)
        return {
            "edge_id_path": edge_id_path,
            "node_path": node_path,
            "steps": steps,
            "possible_totals": sorted(totals),
            "min": min(totals),
            "max": max(totals),
            "note": "路径局部闵可夫斯基和；不代表全图可行域内的真实可达范围",
        }

    # -- 冲突与回路解释（生产侧只读诊断，不阻断发布） -------------------------

    def lint(self) -> dict[str, object]:
        edges = self.active_edges()
        problems: list[dict[str, object]] = []

        # 1) 同节点对同向交集为空
        pairs = sorted({(e.u, e.v) for e in edges})
        for u, v in pairs:
            info = self.same_direction_intersection(u, v)
            if info["conflict"]:
                problems.append(
                    {
                        "kind": "same_direction_conflict",
                        "u": u,
                        "v": v,
                        "edge_ids": info["edge_ids"],
                        "detail": info["per_edge"],
                        "message": f"边 {info['edge_ids']} 在 {u}->{v} 方向候选无公共值",
                    }
                )

        # 2) 回路残差：基本回路的步长闵可夫斯基和不含 0
        constraints = [
            BinaryConstraint(
                cid=str(e.edge_id),
                u=e.u,
                v=e.v,
                allowed=frozenset(e.candidates(self.m)),
            )
            for e in edges
        ]
        nodes = self.nodes()
        for ci, cyc in enumerate(cycle_basis(nodes, constraints)):
            oriented = orient_cycle(cyc)
            if oriented is None:
                continue
            step_sets = [signed_step_set(e, forward) for e, forward in oriented]
            totals = minkowski_sum(step_sets)
            if 0 not in totals:
                problems.append(
                    {
                        "kind": "cycle_residual",
                        "cycle_index": ci,
                        "edge_ids": [e.cid for e, _ in oriented],
                        "residual_values": sorted(totals),
                        "message": (
                            f"回路 {[e.cid for e, _ in oriented]} 闭合残差候选 "
                            f"{sorted(totals)} 不含 0，任何整数势都无法同时满足"
                        ),
                    }
                )
        return {"ok": not problems, "problems": problems}

    # -- 发布不可变产品 ------------------------------------------------------

    def publish(self, note: str = "") -> dict[str, object]:
        """把当前活跃边快照固化为不可变产品并入库，返回产品对象。"""
        edges = self.active_edges()
        if self.count_nodes() > MAX_NODES or len(edges) > MAX_EDGES:
            raise ValueError("产品超出节点/边规模上限")
        for e in edges:
            cands = e.candidates(self.m)
            if not cands:
                raise ValueError(f"边 {e.edge_id} 候选为空，拒绝发布")
        payload = {
            "format": "phaseware-product/1",
            "m": self.m,
            "nodes": self.nodes(),
            "edges": [
                {
                    "edge_id": e.edge_id,
                    "source": e.source,
                    "u": e.u,
                    "v": e.v,
                    "r": e.r,
                    "lo": e.lo,
                    "hi": e.hi,
                    "candidates": list(e.candidates(self.m)),
                }
                for e in edges
            ],
        }
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        product_id = "P" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        if not self.conn.execute(
            "SELECT 1 FROM products WHERE product_id=?", (product_id,)
        ).fetchone():
            self.conn.execute(
                "INSERT INTO products(product_id, created_at, payload, note) VALUES(?,?,?,?)",
                (product_id, _now(), body, note),
            )
            self.conn.commit()
        return {"product_id": product_id, "payload": payload}

    def list_products(self) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT product_id, created_at, note FROM products ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_product(self, product_id: str) -> dict[str, object]:
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知产品 {product_id}")
        return json.loads(row[0])

    def export_product(self, product_id: str, out_path: str) -> None:
        payload = self.get_product(product_id)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {"product_id": product_id, "payload": payload},
                f,
                ensure_ascii=False,
                indent=2,
            )


# ---------------------------------------------------------------------------
# 产品校验（分析仓导入外部同格式产品时复用）
# ---------------------------------------------------------------------------


def validate_product(payload: dict[str, object]) -> None:
    """对公开产品做结构性校验，失败抛 ValueError。"""
    if payload.get("format") != "phaseware-product/1":
        raise ValueError("产品 format 必须为 phaseware-product/1")
    m = payload["m"]
    if not isinstance(m, int) or not (1 <= m <= MAX_M):
        raise ValueError(f"产品 M 非法或超过 {MAX_M}")
    nodes = payload["nodes"]
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES or len(nodes) != len(set(nodes)):
        raise ValueError(f"节点列表非法或超过 {MAX_NODES}")
    node_set = set(nodes)
    edges = payload["edges"]
    if len(edges) > MAX_EDGES:
        raise ValueError(f"边数超过 {MAX_EDGES}")
    seen_ids: set[int] = set()
    for e in edges:
        for key in ("edge_id", "source", "u", "v", "r", "lo", "hi", "candidates"):
            if key not in e:
                raise ValueError(f"边缺少字段 {key}")
        if e["u"] not in node_set or e["v"] not in node_set:
            raise ValueError(f"边 {e['edge_id']} 引用未知节点")
        if not (0 <= e["r"] < m):
            raise ValueError(f"边 {e['edge_id']} 余数越界")
        expected = candidates_for(e["r"], m, e["lo"], e["hi"])
        if tuple(e["candidates"]) != expected:
            raise ValueError(
                f"边 {e['edge_id']} 候选与 r/M/[lo,hi] 不一致，拒绝外部产品"
            )
        if not expected:
            raise ValueError(f"边 {e['edge_id']} 候选为空")
        if e["edge_id"] in seen_ids:
            raise ValueError(f"edge_id {e['edge_id']} 重复")
        seen_ids.add(e["edge_id"])


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _to_record(row: sqlite3.Row) -> EdgeRecord:
    return EdgeRecord(
        edge_id=row["edge_id"],
        source=row["source"],
        u=row["u"],
        v=row["v"],
        r=row["r"],
        lo=row["lo"],
        hi=row["hi"],
        active=bool(row["active"]),
        retract_reason=row["retract_reason"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
