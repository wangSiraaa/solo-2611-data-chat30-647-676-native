"""履约模块持久化：排期库 + 不可变履约运行批次。

与监测库物理分离（独立 SQLite 文件）。运行时只需要一份监测表——可以来自
本项目监测库导出，也可以是合法的外部监测表 JSON，全程不读取音频。

更正兼容性
----------
监测端把一次 confirmed 匹配更正为截断（review）后，对新监测批次重新
:meth:`FulfillStore.run` 即可得到全新运行：求解器整体重算，而不是删一个
成功标记；旧运行批次原样保留，可固定批次重开查询与对比。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .monitor import validate_monitor_table
from .solver import Schedule, solve

SCHEDULE_FORMAT = "adverify-schedules/1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    win_start INTEGER NOT NULL,
    win_end INTEGER NOT NULL,
    min_contiguous INTEGER NOT NULL,
    required_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    monitor_hash TEXT NOT NULL,
    schedule_hash TEXT NOT NULL,
    supersedes_run TEXT,
    note TEXT,
    result_json TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FulfillStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ #
    # 排期导入
    # ------------------------------------------------------------------ #
    def import_schedules(self, path: str, replace: bool = False) -> int:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") != SCHEDULE_FORMAT:
            raise ValueError(f"排期表 format 必须为 {SCHEDULE_FORMAT}")
        rows = data["schedules"]
        parsed: list[Schedule] = []
        seen: set[str] = set()
        for i, r in enumerate(rows):
            req = {"schedule_id", "material_id", "win_start", "win_end",
                   "min_contiguous", "required_count"}
            if req - set(r):
                raise ValueError(f"第 {i} 条排期缺字段: {sorted(req - set(r))}")
            if r["schedule_id"] in seen:
                raise ValueError(f"schedule_id 重复: {r['schedule_id']}")
            seen.add(r["schedule_id"])
            s = Schedule(str(r["schedule_id"]), str(r["material_id"]),
                         int(r["win_start"]), int(r["win_end"]),
                         int(r["min_contiguous"]), int(r["required_count"]))
            if not (1 <= s.required_count <= 3):
                raise ValueError(f"{s.schedule_id}: required_count 必须在 1..3")
            if not (s.win_start < s.win_end):
                raise ValueError(f"{s.schedule_id}: 窗口必须为非空半开区间")
            if s.min_contiguous < 0:
                raise ValueError(f"{s.schedule_id}: min_contiguous 不能为负")
            parsed.append(s)
        if replace:
            self.conn.execute("DELETE FROM schedules")
        now = _utcnow()
        for s in parsed:
            self.conn.execute(
                """INSERT OR REPLACE INTO schedules(schedule_id,material_id,win_start,
                   win_end,min_contiguous,required_count,imported_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (s.schedule_id, s.material_id, s.win_start, s.win_end,
                 s.min_contiguous, s.required_count, now))
        self.conn.commit()
        return len(parsed)

    def load_schedules(self) -> list[Schedule]:
        return [Schedule(r["schedule_id"], r["material_id"], r["win_start"],
                         r["win_end"], r["min_contiguous"], r["required_count"])
                for r in self.conn.execute(
                    "SELECT * FROM schedules ORDER BY schedule_id")]

    def schedules_hash(self) -> str:
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT schedule_id,material_id,win_start,win_end,min_contiguous,"
            "required_count FROM schedules ORDER BY schedule_id")]
        return hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ #
    # 运行
    # ------------------------------------------------------------------ #
    def run(self, monitor_table: dict, source_kind: str = "monitor_db",
            source_ref: str = "", note: str = "",
            supersedes_run: Optional[str] = None) -> str:
        plays = validate_monitor_table(monitor_table)
        confirmed = [p for p in plays if p["certainty"] == "confirmed"]
        schedules = self.load_schedules()
        if not schedules:
            raise RuntimeError("排期库为空，先 import-schedules")
        result = solve(schedules, confirmed)
        run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + \
            "-" + hashlib.sha1(os.urandom(6)).hexdigest()[:6]
        monitor_hash = hashlib.sha256(
            json.dumps(monitor_table, sort_keys=True).encode()).hexdigest()[:16]
        self.conn.execute(
            """INSERT INTO runs(run_id,created_at,source_kind,source_ref,monitor_hash,
               schedule_hash,supersedes_run,note,result_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_id, _utcnow(), source_kind, source_ref, monitor_hash,
             self.schedules_hash(), supersedes_run, note,
             json.dumps(result, ensure_ascii=False)))
        self.conn.commit()
        return run_id

    def list_runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT run_id,created_at,source_kind,source_ref,monitor_hash,"
            "schedule_hash,supersedes_run,note FROM runs ORDER BY created_at")]

    def latest_run_id(self) -> Optional[str]:
        r = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return r["run_id"] if r else None

    def get_run(self, run_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            raise KeyError(run_id)
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json"))
        return d


def generate_schedule_file(materials_meta: list[dict], ground_truth: dict,
                           out_path: str, n_schedules: int = 100,
                           sr: int = 8000, seed: int = 4242) -> str:
    """按真值事件构造 100 条排期（生成器真值只用于造数据与验证）。"""
    rng = np_random(seed)
    events = [e for e in ground_truth["events"] if e["material_id"]]
    schedules = []
    sid = 0

    def add(mat_id, center, half_win, min_contig, req):
        nonlocal sid
        sid += 1
        schedules.append({
            "schedule_id": f"s{sid:04d}",
            "material_id": mat_id,
            "win_start": int(max(0, center - half_win)),
            "win_end": int(center + half_win),
            "min_contiguous": int(min_contig),
            "required_count": int(req),
        })

    # ~70 条锚定真实事件：窗口包住开始点，部分要求 2~3 次（需重复播放）
    anchor = events[:70] if len(events) >= 70 else events
    for e in anchor:
        dur = e["prog_end"] - e["prog_start"]
        half = int(rng.integers(sr // 2, sr * 4))
        req = int(rng.choice([1, 1, 1, 2, 2, 3])) if e["kind"] in (
            "repeated", "full") else 1
        min_contig = int(dur * rng.uniform(0.5, 0.95))
        add(e["material_id"], e["prog_start"], half, min_contig, req)
    # 其余随机排期，部分窗口内根本无播放（制造未满足/部分履约）
    mids = [m["material_id"] for m in materials_meta]
    total_n = ground_truth["program_samples"]
    while len(schedules) < n_schedules:
        mid = str(rng.choice(mids))
        center = int(rng.integers(0, total_n))
        half = int(rng.integers(sr, sr * 6))
        min_contig = int(rng.integers(sr * 2, sr * 12))
        req = int(rng.integers(1, 4))
        add(mid, center, half, min_contig, req)
    payload = {"format": SCHEDULE_FORMAT, "sr": sr, "schedules": schedules}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def np_random(seed: int):
    import numpy as np
    return np.random.default_rng(seed)
