"""可重开的星表服务（SQLite 持久化）。

职责：
  * 条目的新增 / 替换 / 撤回（每次更新一个事务，失败不留半截数据）；
  * 事件审计表 star_events 保留全部历史；
  * 固定版本发布 releases：发布即冻结当时全量状态，旧版本永远可查；
  * 按历元导出方向快照（仅方向 + 来源版本，不含速度），供匹配仓或普通用户使用。

撤回语义：撤回后不进入任何新发布；已发布版本中的历史条目原样保留、仍可查询。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .snapshot import Snapshot, make_snapshot
from .vectors import as_vec3, propagate

CATALOG_SCHEMA_VERSION = "catalog/v1"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class StarRecord:
    star_id: str
    t0: int
    r: np.ndarray
    v: np.ndarray
    status: str
    updated_at: str


class CatalogError(Exception):
    pass


class CatalogService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self.conn:  # 建表自身也是事务
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stars(
                    star_id    TEXT PRIMARY KEY,
                    t0         INTEGER NOT NULL,
                    rx REAL NOT NULL, ry REAL NOT NULL, rz REAL NOT NULL,
                    vx REAL NOT NULL, vy REAL NOT NULL, vz REAL NOT NULL,
                    status     TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS star_events(
                    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    star_id    TEXT NOT NULL,
                    op         TEXT NOT NULL CHECK(op IN ('add','replace','withdraw')),
                    t0         INTEGER,
                    payload    TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases(
                    release_id   TEXT PRIMARY KEY,
                    label        TEXT NOT NULL,
                    note         TEXT,
                    created_at   TEXT NOT NULL,
                    n_entries    INTEGER NOT NULL,
                    n_unavailable_at_release INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS release_entries(
                    release_id TEXT NOT NULL REFERENCES releases(release_id),
                    star_id    TEXT NOT NULL,
                    t0         INTEGER NOT NULL,
                    rx REAL NOT NULL, ry REAL NOT NULL, rz REAL NOT NULL,
                    vx REAL NOT NULL, vy REAL NOT NULL, vz REAL NOT NULL,
                    status     TEXT NOT NULL,
                    PRIMARY KEY(release_id, star_id)
                );
                """
            )
            cur = self.conn.execute("SELECT value FROM meta WHERE key='schema'")
            if cur.fetchone() is None:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema', ?)",
                    (CATALOG_SCHEMA_VERSION,),
                )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CatalogService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- validation
    @staticmethod
    def _validate(star_id: str, t0: int, r: Sequence[float], v: Sequence[float]) -> Tuple[np.ndarray, np.ndarray]:
        if not isinstance(star_id, str) or not star_id:
            raise ValueError("star_id 必须是非空字符串")
        if not isinstance(t0, int) or isinstance(t0, bool):
            raise ValueError("t0 必须是整数日（int）")
        return as_vec3(r, "r"), as_vec3(v, "v")

    # ---------------------------------------------------------------- writes
    def add_star(self, star_id: str, t0: int, r: Sequence[float], v: Sequence[float],
                 *, _commit: bool = True) -> None:
        rvec, vvec = self._validate(star_id, t0, r, v)
        if _commit:
            self.conn.execute("BEGIN")
        try:
            row = self.conn.execute("SELECT status FROM stars WHERE star_id=?", (star_id,)).fetchone()
            if row is not None:
                raise CatalogError(f"star_id 已存在（status={row['status']}），新增冲突: {star_id}")
            now = _utc_now()
            self.conn.execute(
                "INSERT INTO stars(star_id,t0,rx,ry,rz,vx,vy,vz,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'active', ?)",
                (star_id, t0, *rvec, *vvec, now),
            )
            self._audit(star_id, "add", t0, rvec, vvec, now)
        except Exception:
            if _commit:
                self.conn.rollback()
            raise
        if _commit:
            self.conn.commit()

    def replace_star(self, star_id: str, t0: int, r: Sequence[float], v: Sequence[float],
                     *, _commit: bool = True) -> None:
        """替换保持 star_id 不变；仅 active 条目可替换。"""
        rvec, vvec = self._validate(star_id, t0, r, v)
        if _commit:
            self.conn.execute("BEGIN")
        try:
            row = self.conn.execute("SELECT status FROM stars WHERE star_id=?", (star_id,)).fetchone()
            if row is None:
                raise CatalogError(f"star_id 不存在，无法替换: {star_id}")
            if row["status"] != "active":
                raise CatalogError(f"star_id 已撤回，不能替换: {star_id}")
            now = _utc_now()
            self.conn.execute(
                "UPDATE stars SET t0=?, rx=?, ry=?, rz=?, vx=?, vy=?, vz=?, updated_at=? "
                "WHERE star_id=?",
                (t0, *rvec, *vvec, now, star_id),
            )
            self._audit(star_id, "replace", t0, rvec, vvec, now)
        except Exception:
            if _commit:
                self.conn.rollback()
            raise
        if _commit:
            self.conn.commit()

    def withdraw_star(self, star_id: str, *, _commit: bool = True) -> None:
        """撤回：当前表标记 withdrawn；不进入新发布，旧发布不受影响。"""
        if _commit:
            self.conn.execute("BEGIN")
        try:
            row = self.conn.execute("SELECT status FROM stars WHERE star_id=?", (star_id,)).fetchone()
            if row is None:
                raise CatalogError(f"star_id 不存在，无法撤回: {star_id}")
            if row["status"] != "active":
                raise CatalogError(f"star_id 已撤回，不能重复撤回: {star_id}")
            now = _utc_now()
            self.conn.execute(
                "UPDATE stars SET status='withdrawn', updated_at=? WHERE star_id=?", (now, star_id)
            )
            self.conn.execute(
                "INSERT INTO star_events(star_id, op, t0, payload, created_at) VALUES(?,?,?,?,?)",
                (star_id, "withdraw", None, None, now),
            )
        except Exception:
            if _commit:
                self.conn.rollback()
            raise
        if _commit:
            self.conn.commit()

    def _audit(self, star_id: str, op: str, t0: int, r: np.ndarray, v: np.ndarray, now: str) -> None:
        payload = json.dumps(
            {"r": [float(x) for x in r], "v": [float(x) for x in v]}, ensure_ascii=False
        )
        self.conn.execute(
            "INSERT INTO star_events(star_id, op, t0, payload, created_at) VALUES(?,?,?,?,?)",
            (star_id, op, t0, payload, now),
        )

    def import_lines(self, lines: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """批量导入：整个批次一个事务，任一坏行全批回滚（不会留下半条速度）。"""
        n_add = n_replace = 0
        self.conn.execute("BEGIN")
        try:
            for line in lines:
                op = line.get("op", "add")
                if op == "add":
                    self.add_star(line["star_id"], int(line["t0"]), line["r"], line["v"],
                                  _commit=False)
                    n_add += 1
                elif op == "replace":
                    self.replace_star(line["star_id"], int(line["t0"]), line["r"], line["v"],
                                      _commit=False)
                    n_replace += 1
                elif op == "withdraw":
                    self.withdraw_star(line["star_id"], _commit=False)
                else:
                    raise ValueError(f"未知导入操作: {op!r}")
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return {"added": n_add, "replaced": n_replace}

    # ---------------------------------------------------------------- reads
    def get_star(self, star_id: str) -> Optional[StarRecord]:
        row = self.conn.execute("SELECT * FROM stars WHERE star_id=?", (star_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_active_stars(self) -> List[StarRecord]:
        rows = self.conn.execute(
            "SELECT * FROM stars WHERE status='active' ORDER BY star_id"
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_events(self, star_id: Optional[str] = None) -> List[sqlite3.Row]:
        if star_id is None:
            return list(self.conn.execute("SELECT * FROM star_events ORDER BY event_id"))
        return list(
            self.conn.execute(
                "SELECT * FROM star_events WHERE star_id=? ORDER BY event_id", (star_id,)
            )
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> StarRecord:
        keys = row.keys()
        return StarRecord(
            star_id=row["star_id"], t0=row["t0"],
            r=np.array([row["rx"], row["ry"], row["rz"]], dtype=np.float64),
            v=np.array([row["vx"], row["vy"], row["vz"]], dtype=np.float64),
            status=row["status"],
            updated_at=row["updated_at"] if "updated_at" in keys else "",
        )

    # ------------------------------------------------------------- releases
    def release(self, release_id: str, *, label: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        """冻结当前全部 active 条目为固定版本（一个事务）。"""
        label = label or release_id
        with self.conn:
            if self.conn.execute(
                "SELECT 1 FROM releases WHERE release_id=?", (release_id,)
            ).fetchone():
                raise CatalogError(f"发布版本号已存在: {release_id}")
            stars = self.conn.execute(
                "SELECT * FROM stars WHERE status='active' ORDER BY star_id"
            ).fetchall()
            now = _utc_now()
            self.conn.execute(
                "INSERT INTO releases(release_id,label,note,created_at,n_entries) VALUES(?,?,?,?,?)",
                (release_id, label, note, now, len(stars)),
            )
            self.conn.executemany(
                "INSERT INTO release_entries(release_id,star_id,t0,rx,ry,rz,vx,vy,vz,status) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'active')",
                [
                    (release_id, s["star_id"], s["t0"], s["rx"], s["ry"], s["rz"],
                     s["vx"], s["vy"], s["vz"])
                    for s in stars
                ],
            )
        return {"release_id": release_id, "n_entries": len(stars), "created_at": now}

    def list_releases(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM releases ORDER BY created_at, release_id")]

    def get_release_entries(self, release_id: str) -> List[StarRecord]:
        if not self.conn.execute("SELECT 1 FROM releases WHERE release_id=?", (release_id,)).fetchone():
            raise CatalogError(f"发布版本不存在: {release_id}")
        rows = self.conn.execute(
            "SELECT * FROM release_entries WHERE release_id=? ORDER BY star_id", (release_id,)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # -------------------------------------------------------- epoch queries
    def epoch_directions(self, epoch: int, *, release_id: Optional[str] = None) -> Dict[str, Any]:
        """在指定历元计算完整方向表。

        release_id 给定时从冻结版本计算；否则从当前 active 工作集计算。
        返回可用方向与不可用（零向量）star_id 清单。
        """
        records = (
            self.get_release_entries(release_id)
            if release_id is not None
            else self.list_active_stars()
        )
        available, unavailable = [], []
        for rec in records:
            u = propagate(rec.r, rec.v, epoch, rec.t0)
            if u is None:  # propagate 内部按 ZERO_NORM_EPS 判定零向量
                unavailable.append(rec.star_id)
            else:
                available.append({"star_id": rec.star_id, "direction": u})
        return {
            "epoch": epoch,
            "release_id": release_id,
            "available": available,
            "unavailable": unavailable,
        }

    def export_snapshot(
        self, epoch: int, release_id: str, *, unit_tol: float = 1e-9
    ) -> Tuple[Snapshot, List[str]]:
        """从固定版本导出历元快照（只有方向 + 来源版本，无速度）。"""
        table = self.epoch_directions(epoch, release_id=release_id)
        snap = make_snapshot(
            epoch, table["available"],
            source=f"catalog:{release_id}", catalog_release=release_id, unit_tol=unit_tol,
        )
        return snap, table["unavailable"]
