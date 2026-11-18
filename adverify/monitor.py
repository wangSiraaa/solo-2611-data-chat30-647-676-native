"""素材与监测模块：登记、删除、查询、扫描，全部持久化到 SQLite。

不可变批次与更正
----------------
* 每次 scan 产生一个新 ``scan`` 批次，matches 只追加、不更新；
* 删除素材是软删除（``deleted_at``），旧批次的匹配证据原样保留；
* ``correct_match`` 不修改旧批次，而是复制出一个新批次（``supersedes``
  指回旧批次），在新批次里写入更正后的匹配，履约端重开新批次即重算分配；
* play_id 在批次内由 (素材ID, 出现序号) 确定性生成，重跑同输入可复现。

索引失效
--------
特征按 (material_id, feature_config_hash) 存盘。扫描时若当前 FeatureConfig
的哈希与库中索引不匹配，抛 :class:`IncompatibleIndexError`，绝不静默复用；
素材源文件变化（sha256 不同）重新登记时重建该素材索引，旧批次仍记录其
创建时使用的 feature_hash。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from . import features as F
from . import matcher as M
from .audioio import read_wav, rms_db
MONITOR_EXPORT_FORMAT = "adverify-monitor-export/1"
CERTAINTIES = ("confirmed", "review", "unmatched")


class IncompatibleIndexError(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blob_to_array(blob: bytes) -> np.ndarray:
    return np.load(io.BytesIO(blob), allow_pickle=False)


def _array_to_blob(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr.astype(np.float64), allow_pickle=False)
    return buf.getvalue()


def _feature_hash(arr: np.ndarray) -> str:
    return hashlib.sha256(arr.astype(np.float64).tobytes()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sr INTEGER NOT NULL,
    n_samples INTEGER NOT NULL,
    duration_s REAL NOT NULL,
    rms_db REAL NOT NULL,
    source_sha256 TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    feature_config_hash TEXT NOT NULL,
    n_frames INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS feature_index (
    material_id TEXT NOT NULL,
    feature_config_hash TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    n_frames INTEGER NOT NULL,
    blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (material_id, feature_config_hash)
);
CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    feature_config_hash TEXT NOT NULL,
    scan_config_json TEXT NOT NULL,
    scan_config_hash TEXT NOT NULL,
    program_path TEXT NOT NULL,
    program_sha256 TEXT NOT NULL,
    program_n_samples INTEGER NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    supersedes_scan TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    scan_id TEXT NOT NULL,
    play_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    certainty TEXT NOT NULL,
    prog_start INTEGER NOT NULL,
    prog_end INTEGER NOT NULL,
    mat_start INTEGER NOT NULL,
    mat_end INTEGER NOT NULL,
    matched_frames INTEGER NOT NULL,
    total_frames INTEGER NOT NULL,
    contiguous_frames INTEGER NOT NULL,
    mean_score REAL NOT NULL,
    lag_frame INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    correction_of TEXT,
    PRIMARY KEY (scan_id, play_id)
);
CREATE TABLE IF NOT EXISTS scan_unmatched (
    scan_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (scan_id, material_id)
);
"""


@dataclass
class MaterialRecord:
    material_id: str
    path: str
    sr: int
    n_samples: int
    duration_s: float
    rms_db: float
    source_sha256: str
    feature_hash: str
    feature_config_hash: str
    n_frames: int
    registered_at: str
    deleted_at: Optional[str]


class MonitorStore:
    def __init__(self, db_path: str, fcfg: Optional[F.FeatureConfig] = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.fcfg = fcfg or F.FeatureConfig()
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(k,v) VALUES('feature_config_json',?)",
            (self.fcfg.stable_json(),))
        self.conn.commit()
        self._white_stats: Optional[tuple[np.ndarray, np.ndarray]] = None

    def _get_white_stats(self) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """读取建库时估计并持久化的全局白化统计。"""
        if self._white_stats is not None:
            return self._white_stats
        r = self.conn.execute(
            "SELECT v FROM meta WHERE k='white_stats_blob'").fetchone()
        if r is None:
            return None
        self._white_stats = _blob_to_array(r["v"])
        mean, std = self._white_stats
        return mean, std

    def _save_white_stats(self, stats: tuple[np.ndarray, np.ndarray]) -> None:
        blob = _array_to_blob(np.stack(stats, axis=0))
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(k,v) VALUES('white_stats_blob',?)", (blob,))
        self.conn.commit()
        self._white_stats = stats

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ #
    # 登记 / 删除 / 查询
    # ------------------------------------------------------------------ #
    def register(self, material_id: str, wav_path: str,
                 overwrite: bool = False) -> MaterialRecord:
        audio = read_wav(wav_path)
        if audio.sr != self.fcfg.sr:
            raise ValueError(f"采样率 {audio.sr} 与配置 {self.fcfg.sr} 不一致: {wav_path}")
        sha = _sha256_file(wav_path)
        row = self.conn.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
        if row is not None and row["deleted_at"] is None and not overwrite:
            raise ValueError(f"素材 {material_id} 已登记；如需替换请显式 overwrite")
        stats = self._get_white_stats()
        feats = F.extract(audio.samples, self.fcfg, white_stats=stats)
        fh = _feature_hash(feats)
        fch = self.fcfg.config_hash()
        now = _utcnow()
        if row is None:
            self.conn.execute(
                """INSERT INTO materials(material_id,path,sr,n_samples,duration_s,
                   rms_db,source_sha256,feature_hash,feature_config_hash,n_frames,
                   registered_at,deleted_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (material_id, os.path.abspath(wav_path), audio.sr, audio.n,
                 audio.n / audio.sr, rms_db(audio.samples), sha, fh, fch,
                 feats.shape[0], now))
        else:
            self.conn.execute(
                """UPDATE materials SET path=?,sr=?,n_samples=?,duration_s=?,rms_db=?,
                   source_sha256=?,feature_hash=?,feature_config_hash=?,n_frames=?,
                   registered_at=?,deleted_at=NULL WHERE material_id=?""",
                (os.path.abspath(wav_path), audio.sr, audio.n, audio.n / audio.sr,
                 rms_db(audio.samples), sha, fh, fch, feats.shape[0], now, material_id))
        self.conn.execute(
            """INSERT OR REPLACE INTO feature_index(material_id,feature_config_hash,
               feature_hash,n_frames,blob,created_at) VALUES(?,?,?,?,?,?)""",
            (material_id, fch, fh, feats.shape[0], _array_to_blob(feats), now))
        self.conn.commit()
        return self.get_material(material_id)

    def delete(self, material_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM materials WHERE material_id=? AND deleted_at IS NULL",
            (material_id,)).fetchone()
        if row is None:
            raise ValueError(f"素材 {material_id} 不存在或已删除")
        self.conn.execute(
            "UPDATE materials SET deleted_at=? WHERE material_id=?",
            (_utcnow(), material_id))
        self.conn.commit()

    def rebuild_whitening(self) -> None:
        """按当前特征配置重建全部在库素材的检索索引。

        命名沿用早期方案；当前稀疏峰指纹表示不需要全局白化统计，本方法
        的实际职责是统一重建索引，保证素材与节目特征出自同一特征版本。
        旧扫描批次只记录其创建时的 feature_config_hash，不会被改写。
        """
        rows = self.conn.execute(
            "SELECT path FROM materials WHERE deleted_at IS NULL").fetchall()
        now = _utcnow()
        fch = self.fcfg.config_hash()
        for r in rows:
            audio = read_wav(r["path"])
            feats = F.extract(audio.samples, self.fcfg)
            mid = self.conn.execute(
                "SELECT material_id FROM materials WHERE path=?",
                (r["path"],)).fetchone()["material_id"]
            fh = _feature_hash(feats)
            self.conn.execute(
                """UPDATE materials SET feature_hash=?,feature_config_hash=?,n_frames=?
                   WHERE material_id=?""", (fh, fch, feats.shape[0], mid))
            self.conn.execute(
                """INSERT OR REPLACE INTO feature_index(material_id,feature_config_hash,
                   feature_hash,n_frames,blob,created_at) VALUES(?,?,?,?,?,?)""",
                (mid, fch, fh, feats.shape[0], _array_to_blob(feats), now))
        self.conn.commit()

    def list_materials(self, include_deleted: bool = False) -> list[MaterialRecord]:
        sql = "SELECT * FROM materials"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY material_id"
        return [MaterialRecord(**dict(r)) for r in self.conn.execute(sql)]

    def get_material(self, material_id: str) -> MaterialRecord:
        r = self.conn.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
        if r is None:
            raise KeyError(material_id)
        return MaterialRecord(**dict(r))

    def load_features(self, material_id: str) -> np.ndarray:
        fch = self.fcfg.config_hash()
        r = self.conn.execute(
            "SELECT blob FROM feature_index WHERE material_id=? AND feature_config_hash=?",
            (material_id, fch)).fetchone()
        if r is None:
            raise IncompatibleIndexError(
                f"素材 {material_id} 缺少配置 {fch} 的索引，需要重新登记/reindex")
        return _blob_to_array(r["blob"])

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def _check_index_compatibility(self, scan_cfg: M.ScanConfig) -> None:
        fch = self.fcfg.config_hash()
        rows = self.conn.execute(
            "SELECT material_id, feature_config_hash FROM materials "
            "WHERE deleted_at IS NULL ORDER BY material_id").fetchall()
        missing = [r["material_id"] for r in rows
                   if not self.conn.execute(
                       "SELECT 1 FROM feature_index WHERE material_id=? "
                       "AND feature_config_hash=?", (r["material_id"], fch)).fetchone()]
        if missing:
            raise IncompatibleIndexError(
                "当前特征配置与已存索引不兼容，拒绝静默复用。"
                f"缺失索引素材 {len(missing)} 个（示例 {missing[:5]}），请先 reindex。")
        cfg_h = hashlib.sha256(
            json.dumps(scan_cfg.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
        return None

    def scan(self, program_wav: str, scan_cfg: Optional[M.ScanConfig] = None,
             note: str = "", supersedes: Optional[str] = None,
             overrides: Optional[dict] = None) -> str:
        scan_cfg = scan_cfg or M.ScanConfig()
        self._check_index_compatibility(scan_cfg)
        audio = read_wav(program_wav)
        if audio.sr != self.fcfg.sr:
            raise ValueError(f"节目采样率 {audio.sr} 与配置 {self.fcfg.sr} 不一致")
        stats = self._get_white_stats()
        prog_feat = F.extract(audio.samples, self.fcfg, white_stats=stats)
        materials = self.list_materials()
        mat_feats = {m.material_id: self.load_features(m.material_id) for m in materials}
        # 能量掩码来自素材真实音频，用于截断处的覆盖区间收敛
        energy_masks = {}
        for rec in materials:
            mat_audio = read_wav(rec.path)
            energy_masks[rec.material_id] = M._frame_energy_mask(
                mat_audio.samples, self.fcfg)
        matches = M.scan_program(prog_feat, mat_feats, scan_cfg,
                                 self.fcfg, energy_masks)

        # 更正批次：在写入前应用覆盖指令
        correction_of: dict[str, str] = {}
        if overrides:
            by_play = {m.play_id: m for m in matches}
            for ov in overrides:
                pid = ov["play_id"]
                if pid not in by_play:
                    raise KeyError(f"更正目标 {pid} 不在扫描结果中")
                m = by_play[pid]
                for key in ("certainty", "mat_start", "mat_end", "prog_start", "prog_end"):
                    if key in ov:
                        setattr(m, key, ov[key])
                m.evidence = dict(m.evidence)
                m.evidence["manual_correction"] = ov
                correction_of[pid] = ov.get("reason", "manual_correction")

        scan_id = "scan-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + \
            "-" + hashlib.sha1(os.urandom(6)).hexdigest()[:6]
        cfg_h = hashlib.sha256(
            json.dumps(scan_cfg.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
        self.conn.execute(
            """INSERT INTO scans(scan_id,created_at,feature_config_hash,scan_config_json,
               scan_config_hash,program_path,program_sha256,program_n_samples,status,
               note,supersedes_scan) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, _utcnow(), self.fcfg.config_hash(),
             json.dumps(scan_cfg.to_dict(), sort_keys=True), cfg_h,
             os.path.abspath(program_wav), _sha256_file(program_wav), audio.n,
             "corrected" if overrides else "ok", note, supersedes))
        matched_ids = {m.material_id for m in matches}
        for m in matches:
            self.conn.execute(
                """INSERT INTO matches(scan_id,play_id,material_id,certainty,prog_start,
                   prog_end,mat_start,mat_end,matched_frames,total_frames,
                   contiguous_frames,mean_score,lag_frame,evidence_json,correction_of)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scan_id, m.play_id, m.material_id, m.certainty, m.prog_start,
                 m.prog_end, m.mat_start, m.mat_end, m.matched_frames, m.total_frames,
                 m.contiguous_frames, m.mean_score, m.lag_frame,
                 json.dumps(m.evidence, ensure_ascii=False),
                 correction_of.get(m.play_id)))
        for rec in materials:
            if rec.material_id not in matched_ids:
                self.conn.execute(
                    "INSERT INTO scan_unmatched VALUES(?,?,?)",
                    (scan_id, rec.material_id, "no_candidate_above_threshold"))
        self.conn.commit()
        return scan_id

    def correct_match(self, old_scan_id: str, play_id: str, overrides: dict,
                      reason: str = "manual_correction") -> str:
        """以旧批次为基线重扫并应用一条更正，产生新批次（旧批次不动）。"""
        old = self.get_scan(old_scan_id)
        ov = dict(overrides)
        ov["play_id"] = play_id
        ov["reason"] = reason
        return self.scan(old["program_path"],
                         M.ScanConfig(**json.loads(old["scan_config_json"])),
                         note=f"correction of {old_scan_id}: {play_id} {reason}",
                         supersedes=old_scan_id, overrides=[ov])

    # ------------------------------------------------------------------ #
    # 批次查询
    # ------------------------------------------------------------------ #
    def list_scans(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT scan_id,created_at,status,note,supersedes_scan,"
            "program_n_samples FROM scans ORDER BY created_at")]

    def latest_scan_id(self) -> Optional[str]:
        r = self.conn.execute(
            "SELECT scan_id FROM scans ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return r["scan_id"] if r else None

    def get_scan(self, scan_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if r is None:
            raise KeyError(scan_id)
        return dict(r)

    def get_matches(self, scan_id: str, certainty: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM matches WHERE scan_id=?"
        params: list = [scan_id]
        if certainty:
            sql += " AND certainty=?"
            params.append(certainty)
        sql += " ORDER BY prog_start, material_id, play_id"
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["evidence"] = json.loads(d["evidence_json"])
            del d["evidence_json"]
            out.append(d)
        return out

    def get_play(self, scan_id: str, play_id: str) -> dict:
        r = self.conn.execute(
            "SELECT * FROM matches WHERE scan_id=? AND play_id=?",
            (scan_id, play_id)).fetchone()
        if r is None:
            raise KeyError(play_id)
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    def get_unmatched(self, scan_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM scan_unmatched WHERE scan_id=? ORDER BY material_id",
            (scan_id,))]

    def export_scan(self, scan_id: str) -> dict:
        scan = self.get_scan(scan_id)
        return {
            "format": MONITOR_EXPORT_FORMAT,
            "scan_id": scan_id,
            "created_at": scan["created_at"],
            "feature_config_hash": scan["feature_config_hash"],
            "scan_config_hash": scan["scan_config_hash"],
            "program_sha256": scan["program_sha256"],
            "program_n_samples": scan["program_n_samples"],
            "matches": self.get_matches(scan_id),
            "unmatched": self.get_unmatched(scan_id),
        }


# --------------------------------------------------------------------------- #
# 外部监测表校验（履约端可直接消费，无需原音频）
# --------------------------------------------------------------------------- #
def validate_monitor_table(data: dict) -> list[dict]:
    if not isinstance(data, dict) or data.get("format") != MONITOR_EXPORT_FORMAT:
        raise ValueError("监测表 format 字段必须为 " + MONITOR_EXPORT_FORMAT)
    required = {"play_id", "material_id", "certainty", "prog_start", "prog_end",
                "mat_start", "mat_end", "contiguous_frames"}
    seen: set[str] = set()
    normalized: list[dict] = []
    for i, m in enumerate(data.get("matches", [])):
        missing = required - set(m)
        if missing:
            raise ValueError(f"第 {i} 条匹配缺少字段: {sorted(missing)}")
        if m["certainty"] not in CERTAINTIES:
            raise ValueError(f"第 {i} 条匹配 certainty 非法: {m['certainty']}")
        for k in ("prog_start", "prog_end", "mat_start", "mat_end", "contiguous_frames"):
            if not isinstance(m[k], int) or m[k] < 0:
                raise ValueError(f"第 {i} 条匹配 {k} 必须为非负整数")
        if not (m["prog_start"] < m["prog_end"] and m["mat_start"] < m["mat_end"]):
            raise ValueError(f"第 {i} 条匹配区间必须为半开且非空")
        if m["play_id"] in seen:
            raise ValueError(f"play_id 重复: {m['play_id']}")
        seen.add(m["play_id"])
        normalized.append(dict(m))
    normalized.sort(key=lambda x: (x["prog_start"], x["material_id"], x["play_id"]))
    return normalized
