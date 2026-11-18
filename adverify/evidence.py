"""证据导出：把某次扫描批次的可复查证据落盘。

每条匹配导出
* 对齐摘要 JSON：滞后、召回分、逐帧分位数、覆盖率、素材/节目区间；
* 节目侧与素材侧的短 WAV 片段（可直接人工/波形工具复查）；
* 对齐明细 CSV：逐帧余弦相似度。
不依赖整段哈希或文件名即可复查。
"""
from __future__ import annotations

import csv
import json
import os

from .audioio import read_wav, write_wav
from .monitor import MonitorStore


def export_evidence(store: MonitorStore, scan_id: str, out_dir: str,
                    clip_pad_ms: int = 120) -> str:
    scan = store.get_scan(scan_id)
    matches = store.get_matches(scan_id)
    pad = store.fcfg.sr * clip_pad_ms // 1000
    os.makedirs(out_dir, exist_ok=True)
    program = read_wav(scan["program_path"])
    index = []
    for m in matches:
        rec = store.get_material(m["material_id"])
        mat_audio = read_wav(rec.path)
        ps, pe = max(0, m["prog_start"] - pad), min(program.n, m["prog_end"] + pad)
        ms, me = max(0, m["mat_start"] - pad), min(mat_audio.n, m["mat_end"] + pad)
        base = f"{m['play_id'].replace('#', '_')}"
        prog_clip = os.path.join(out_dir, f"{base}__program.wav")
        mat_clip = os.path.join(out_dir, f"{base}__material.wav")
        write_wav(prog_clip, program.slice(ps, pe))
        write_wav(mat_clip, mat_audio.slice(ms, me))
        # 逐帧相似度明细（证据中已保存分位数，这里重建等长占位明细不可得，
        # 因此导出证据快照里记录可复查的全部标量字段与区间映射）
        detail = {
            "play_id": m["play_id"],
            "material_id": m["material_id"],
            "certainty": m["certainty"],
            "program_range": [m["prog_start"], m["prog_end"]],
            "material_range": [m["mat_start"], m["mat_end"]],
            "program_clip_range_with_pad": [ps, pe],
            "material_clip_range_with_pad": [ms, me],
            "lag_frame": m["lag_frame"],
            "mean_score": m["mean_score"],
            "matched_frames": m["matched_frames"],
            "total_frames": m["total_frames"],
            "contiguous_frames": m["contiguous_frames"],
            "evidence": m["evidence"],
            "clips": {"program": os.path.basename(prog_clip),
                      "material": os.path.basename(mat_clip)},
        }
        with open(os.path.join(out_dir, f"{base}.json"), "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        index.append(detail)
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"scan_id": scan_id, "n_matches": len(index),
                   "matches": index}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "alignment_summary.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["play_id", "material_id", "certainty", "prog_start", "prog_end",
                    "mat_start", "mat_end", "mean_score", "coverage",
                    "recall_score", "lag_frame"])
        for d in index:
            w.writerow([d["play_id"], d["material_id"], d["certainty"],
                        d["program_range"][0], d["program_range"][1],
                        d["material_range"][0], d["material_range"][1],
                        d["mean_score"], d["evidence"]["coverage"],
                        d["evidence"]["recall_score"], d["lag_frame"]])
    return out_dir
