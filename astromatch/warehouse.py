"""独立曝光匹配仓（SQLite 持久化）。

与星表服务解耦：
  * 只消费历元方向快照（本星表导出或外部同格式 JSON），永不读取速度表；
  * 每次匹配生成不可变 run，绑定当时封存的快照；星表更正后同曝光重跑产生新 run，
    旧 run 仍绑定旧快照与旧分配，不会只改显示坐标；
  * 原始输入文件删除后，run、快照、亮点、候选边、轨迹全部可从库内复读。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .matcher import (
    build_candidates, prepare_detections,
    prepare_stars, solve_matching,
)
from .snapshot import Snapshot, snapshot_from_jsonable

WAREHOUSE_SCHEMA_VERSION = "warehouse/v1"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class WarehouseError(Exception):
    pass


@dataclass
class ExposureInput:
    exposure_id: str
    epoch: int
    detections: List[Dict[str, Any]]   # detection_id / direction / brightness
    unit_tol: float = 1e-9


class MatchWarehouse:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "MatchWarehouse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots(
                    snapshot_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    catalog_release TEXT,
                    unit_tol REAL NOT NULL,
                    blob TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_stars(
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
                    star_id TEXT NOT NULL,
                    dx REAL NOT NULL, dy REAL NOT NULL, dz REAL NOT NULL,
                    PRIMARY KEY(snapshot_id, star_id)
                );
                CREATE TABLE IF NOT EXISTS exposures(
                    exposure_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL,
                    unit_tol REAL NOT NULL,
                    brightness_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS detections(
                    exposure_id TEXT NOT NULL REFERENCES exposures(exposure_id),
                    detection_id TEXT NOT NULL,
                    dx REAL NOT NULL, dy REAL NOT NULL, dz REAL NOT NULL,
                    brightness INTEGER NOT NULL,
                    PRIMARY KEY(exposure_id, detection_id)
                );
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY,
                    exposure_id TEXT NOT NULL REFERENCES exposures(exposure_id),
                    seq INTEGER NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
                    threshold REAL NOT NULL,
                    eps REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    n_matches INTEGER NOT NULL,
                    n_candidates INTEGER NOT NULL,
                    min_cost REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    trace_json TEXT NOT NULL,
                    UNIQUE(exposure_id, seq)
                );
                CREATE TABLE IF NOT EXISTS run_matches(
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    star_id TEXT NOT NULL,
                    detection_id TEXT NOT NULL,
                    angle REAL NOT NULL,
                    cost REAL NOT NULL,
                    PRIMARY KEY(run_id, star_id, detection_id)
                );
                CREATE TABLE IF NOT EXISTS run_candidates(
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    star_id TEXT NOT NULL,
                    detection_id TEXT NOT NULL,
                    angle REAL NOT NULL,
                    cost REAL NOT NULL,
                    selected INTEGER NOT NULL,
                    PRIMARY KEY(run_id, star_id, detection_id)
                );
                """
            )
            if self.conn.execute("SELECT 1 FROM meta WHERE key='schema'").fetchone() is None:
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema',?)", (WAREHOUSE_SCHEMA_VERSION,))

    # ------------------------------------------------------------- snapshots
    def _store_snapshot(self, snap: Snapshot) -> None:
        existing = self.conn.execute(
            "SELECT 1 FROM snapshots WHERE snapshot_id=?", (snap.snapshot_id,)).fetchone()
        if existing:
            return
        blob = snap.to_json()
        self.conn.execute(
            "INSERT INTO snapshots(snapshot_id,epoch,source,catalog_release,unit_tol,blob,imported_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (snap.snapshot_id, snap.epoch, snap.source, snap.catalog_release,
             snap.unit_tol, blob, _utc_now()),
        )
        self.conn.executemany(
            "INSERT INTO snapshot_stars(snapshot_id,star_id,dx,dy,dz) VALUES(?,?,?,?,?)",
            [(snap.snapshot_id, e.star_id, float(e.direction[0]),
              float(e.direction[1]), float(e.direction[2])) for e in snap.entries],
        )

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        row = self.conn.execute(
            "SELECT blob FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise WarehouseError(f"快照不存在: {snapshot_id}")
        return snapshot_from_jsonable(json.loads(row["blob"]))

    # ------------------------------------------------------------- exposures
    def import_exposure(self, exp: ExposureInput) -> None:
        """校验并封存曝光输入（方向校验在事务内先做，坏数据不入库）。"""
        if not isinstance(exp.exposure_id, str) or not exp.exposure_id:
            raise ValueError("exposure_id 必须是非空字符串")
        if not isinstance(exp.epoch, int) or isinstance(exp.epoch, bool):
            raise ValueError("曝光整数日 epoch 必须是 int")
        # 先全量校验，再开事务写入
        det_ids, det_dirs, brights = prepare_detections(exp.detections, unit_tol=exp.unit_tol)
        bright_map = {did: b for did, b in zip(det_ids, brights)}
        with self.conn:
            if self.conn.execute(
                "SELECT 1 FROM exposures WHERE exposure_id=?", (exp.exposure_id,)
            ).fetchone():
                raise WarehouseError(f"曝光已存在: {exp.exposure_id}")
            now = _utc_now()
            self.conn.execute(
                "INSERT INTO exposures(exposure_id,epoch,unit_tol,brightness_json,imported_at)"
                " VALUES(?,?,?,?,?)",
                (exp.exposure_id, exp.epoch, exp.unit_tol,
                 json.dumps(bright_map, ensure_ascii=False, sort_keys=True), now),
            )
            self.conn.executemany(
                "INSERT INTO detections(exposure_id,detection_id,dx,dy,dz,brightness)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (exp.exposure_id, did, float(u[0]), float(u[1]), float(u[2]), b)
                    for did, u, b in zip(det_ids, det_dirs, brights)
                ],
            )

    def _load_exposure(self, exposure_id: str) -> ExposureInput:
        row = self.conn.execute(
            "SELECT * FROM exposures WHERE exposure_id=?", (exposure_id,)).fetchone()
        if row is None:
            raise WarehouseError(f"曝光不存在: {exposure_id}")
        drows = self.conn.execute(
            "SELECT * FROM detections WHERE exposure_id=? ORDER BY detection_id", (exposure_id,)
        ).fetchall()
        detections = [
            {
                "detection_id": d["detection_id"],
                "direction": [d["dx"], d["dy"], d["dz"]],
                "brightness": d["brightness"],
            }
            for d in drows
        ]
        return ExposureInput(
            exposure_id=row["exposure_id"], epoch=row["epoch"],
            detections=detections, unit_tol=row["unit_tol"],
        )

    # ------------------------------------------------------------------ run
    def run_matching(
        self, exposure_id: str, snapshot: Snapshot, *,
        threshold: float, eps: float,
    ) -> Dict[str, Any]:
        """消费快照对已入库曝光做匹配，生成不可变 run（整过程一个事务）。"""
        if threshold < 0 or eps < 0:
            raise ValueError("threshold 与 eps 必须非负")
        if snapshot.epoch != self._require_epoch(exposure_id):
            raise WarehouseError(
                f"快照历元 {snapshot.epoch} 与曝光历元 {self._require_epoch(exposure_id)} 不一致")
        exp = self._load_exposure(exposure_id)
        det_ids, det_dirs, _ = prepare_detections(exp.detections, unit_tol=exp.unit_tol)
        star_rows = [
            {"star_id": e.star_id, "direction": e.direction} for e in snapshot.entries
        ]
        star_ids, star_dirs = prepare_stars(star_rows, unit_tol=snapshot.unit_tol)
        candidates = build_candidates(star_ids, star_dirs, det_ids, det_dirs, threshold)
        outcome = solve_matching(
            exposure_id, star_ids, det_ids, candidates, eps=eps, threshold=threshold)

        with self.conn:
            self._store_snapshot(snapshot)
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM runs WHERE exposure_id=?",
                (exposure_id,),
            ).fetchone()["n"]
            run_id = f"R-{exposure_id}-{seq:03d}"
            now = _utc_now()
            selected = set(outcome.match_pairs)
            self.conn.execute(
                "INSERT INTO runs(run_id,exposure_id,seq,snapshot_id,threshold,eps,created_at,"
                "n_matches,n_candidates,min_cost,total_cost,trace_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, exposure_id, seq, snapshot.snapshot_id, threshold, eps, now,
                 len(outcome.matches), len(candidates), outcome.min_cost,
                 outcome.total_cost, json.dumps(outcome.trace, ensure_ascii=False)),
            )
            self.conn.executemany(
                "INSERT INTO run_matches(run_id,star_id,detection_id,angle,cost) VALUES(?,?,?,?,?)",
                [(run_id, s, d, a, a * a) for s, d, a in outcome.matches],
            )
            self.conn.executemany(
                "INSERT INTO run_candidates(run_id,star_id,detection_id,angle,cost,selected)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (run_id, c.star_id, c.detection_id, c.angle, c.cost,
                     1 if c.as_pair() in selected else 0)
                    for c in candidates
                ],
            )
        return self.get_run(run_id)

    def _require_epoch(self, exposure_id: str) -> int:
        row = self.conn.execute(
            "SELECT epoch FROM exposures WHERE exposure_id=?", (exposure_id,)).fetchone()
        if row is None:
            raise WarehouseError(f"曝光不存在: {exposure_id}")
        return row["epoch"]

    # ------------------------------------------------------------- queries
    def list_runs(self, exposure_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if exposure_id is None:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY exposure_id, seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE exposure_id=? ORDER BY seq", (exposure_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_run_id(self, exposure_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT run_id FROM runs WHERE exposure_id=? ORDER BY seq DESC LIMIT 1",
            (exposure_id,),
        ).fetchone()
        return row["run_id"] if row else None

    def get_run(self, run_id: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise WarehouseError(f"run 不存在: {run_id}")
        out = dict(row)
        out["matches"] = [dict(r) for r in self.conn.execute(
            "SELECT star_id,detection_id,angle,cost FROM run_matches WHERE run_id=? "
            "ORDER BY star_id,detection_id", (run_id,))]
        out["candidates"] = [dict(r) for r in self.conn.execute(
            "SELECT star_id,detection_id,angle,cost,selected FROM run_candidates "
            "WHERE run_id=? ORDER BY star_id,detection_id", (run_id,))]
        out["trace"] = json.loads(out.pop("trace_json"))
        exp = self._load_exposure(out["exposure_id"])
        matched_d = {m["detection_id"] for m in out["matches"]}
        matched_s = {m["star_id"] for m in out["matches"]}
        out["unmatched_detections"] = [
            d["detection_id"] for d in exp.detections if d["detection_id"] not in matched_d]
        snap = self.load_snapshot(out["snapshot_id"])
        out["unmatched_stars"] = [
            e.star_id for e in snap.entries if e.star_id not in matched_s]
        return out

    def explain_match(self, run_id: str, star_id: str, detection_id: str) -> Dict[str, Any]:
        """解释某条匹配为何成立 / 某候选为何落选（读库内轨迹，原始文件可已删除）。"""
        run = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise WarehouseError(f"run 不存在: {run_id}")
        cand = self.conn.execute(
            "SELECT * FROM run_candidates WHERE run_id=? AND star_id=? AND detection_id=?",
            (run_id, star_id, detection_id),
        ).fetchone()
        if cand is None:
            return {
                "run_id": run_id, "star_id": star_id, "detection_id": detection_id,
                "status": "not_a_candidate",
                "reason": "该 (星,亮点) 夹角超过阈值或某侧不可用，从未成为候选边",
            }
        trace = json.loads(run["trace_json"])
        event = next(
            (t for t in trace if tuple(t["edge"]) == (star_id, detection_id)), None)
        # 该星/亮点上最终入选的边（竞争对照）
        competitors = [dict(r) for r in self.conn.execute(
            "SELECT * FROM run_candidates WHERE run_id=? AND "
            "(star_id=? OR detection_id=?) ORDER BY star_id,detection_id",
            (run_id, star_id, detection_id))]
        return {
            "run_id": run_id,
            "star_id": star_id,
            "detection_id": detection_id,
            "angle": cand["angle"],
            "cost": cand["cost"],
            "status": "selected" if cand["selected"] else "rejected",
            "trace_event": event,
            "competitors": competitors,
            "min_cost": run["min_cost"],
            "eps": run["eps"],
            "budget": run["min_cost"] + run["eps"],
        }

    def explain_run(self, run_id: str) -> Dict[str, Any]:
        """整条 run 的裁决叙述 + 未匹配星/亮点原因。"""
        run = self.get_run(run_id)
        rejected = [
            {
                "edge": [c["star_id"], c["detection_id"]],
                "angle": c["angle"], "cost": c["cost"],
                "reason": next(
                    (t["reason"] for t in run["trace"]
                     if tuple(t["edge"]) == (c["star_id"], c["detection_id"])),
                    "未进入裁决（在达到 K 条边后停止）"),
            }
            for c in run["candidates"] if not c["selected"]
        ]
        return {
            "run_id": run_id,
            "exposure_id": run["exposure_id"],
            "snapshot_id": run["snapshot_id"],
            "max_cardinality": run["n_matches"],
            "min_cost": run["min_cost"],
            "total_cost": run["total_cost"],
            "selected": run["matches"],
            "rejected_candidates": rejected,
            "unmatched_detections": run["unmatched_detections"],
            "unmatched_stars": run["unmatched_stars"],
        }
