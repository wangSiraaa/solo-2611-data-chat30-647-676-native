"""节目流扫描：在 log-mel 帧序列上检索素材出现位置。

三段式流水线（阈值全部可配置）：

1. 召回 —— FFT 归一化互相关
   素材帧 Q（Lq 行、单位向量）与节目帧 P（Lp 行、单位向量）。先在仅完全
   重叠的滞后上计算整段平均余弦相似度（无边界归一化伪峰），取局部极大值；
   为避免截断播放被素材中未出现的帧稀释，再在每个候选 lag 的邻域内，用
   分块点积求素材任意长度 ``window_frames`` 子段的最高局部均值，过阈值者
   进入验证。
2. 对角验证 —— 沿候选滞后逐帧比对
   逐帧余弦 >= ``verify_threshold`` 记好帧，允许桥接少量坏帧，取最长连续
   好帧段作为真实素材覆盖区间 [mat_lo, mat_hi) 与节目区间。
3. 合并与分级
   同一素材、节目区间重叠或相接触的候选按并集合并（重叠候选合并规则）；
   互不重叠的重复播放保持独立。再按覆盖率/均分区分确定、待核；
   未达确定门槛的片段一律落待核，绝不静默丢弃。

不读取任何插入位置真值，也不使用文件名或整段哈希。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from . import features as F


@dataclass(frozen=True)
class ScanConfig:
    recall_threshold: float = 0.35      # 整段互相关候选峰值门槛
    local_threshold: float = 0.45       # 局部子段平均相似度门槛
    window_frames: int = 60             # 局部子段长度（0.6 秒，兼顾截断）
    verify_threshold: float = 0.35      # 逐帧通过门槛
    min_verify_frames: int = 12         # 验证通过的最少好帧（约 120ms）
    gap_tolerance: int = 4              # 对角验证允许桥接的坏帧数
    confirmed_min_score: float = 0.55   # 确定匹配的平均相似度
    confirmed_min_coverage: float = 0.85  # 确定匹配要求的素材覆盖率
    pending_min_score: float = 0.55     # 待核匹配的平均相似度
    pending_min_coverage: float = 0.55  # 待核匹配要求的覆盖率（截断可低覆盖）
    cross_material_iou_suppress: float = 0.15  # 跨素材 NMS 的 IoU 门槛
    peak_min_gap_frames: int = 6        # 召回峰值之间至少相隔的帧数
    max_candidates_per_material: int = 60

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Match:
    play_id: str
    material_id: str
    prog_start: int
    prog_end: int
    mat_start: int
    mat_end: int
    matched_frames: int
    total_frames: int
    contiguous_frames: int          # 桥接后最长连续（含容差）好帧段
    mean_score: float
    certainty: str                  # confirmed / review
    lag_frame: int
    evidence: dict


def _full_correlation(q: np.ndarray, p: np.ndarray) -> np.ndarray:
    """cc[lag] = 素材全部帧与 P[lag:lag+Lq] 的平均余弦，lag=0..Lp-Lq。

    numpy 约定：irfft(Q * conj(P))[k] = sum_j Q[j] P[j-k]（模 nfft），
    令 lag = -k（模 nfft）即得 sum_j Q[j] P[lag+j]，故
    cc[lag] 取 corr[(-lag) mod nfft]。
    """
    lq, dim = q.shape
    lp = p.shape[0]
    if lp < lq:
        return np.zeros(0, dtype=np.float64)
    nfft = 1
    while nfft < lp + lq:
        nfft <<= 1
    qf = np.fft.rfft(q.astype(np.float64), nfft, axis=0)
    pf = np.fft.rfft(p.astype(np.float64), nfft, axis=0)
    corr = np.zeros(nfft, dtype=np.float64)
    for d in range(dim):
        corr += np.fft.irfft(qf[:, d] * np.conj(pf[:, d]), nfft)
    out_len = lp - lq + 1
    idx = (-np.arange(out_len)) % nfft
    return corr[idx] / lq


def _local_segment_score(q: np.ndarray, p: np.ndarray, lag: int,
                         window: int) -> tuple[float, int]:
    """固定 lag 下，素材侧长度 window 的最佳子段平均相似度及子段起点。"""
    lq = q.shape[0]
    hi = min(lq, p.shape[0] - lag)
    if hi <= 0:
        return 0.0, 0
    w = min(window, hi)
    dots = np.sum(q[:hi] * p[lag:lag + hi], axis=1)
    if hi <= w:
        return float(dots.mean()), 0
    cs = np.concatenate(([0.0], np.cumsum(dots)))
    sums = cs[w:] - cs[:-w]
    a = int(np.argmax(sums))
    return float(sums[a] / w), a


def _peak_lags(cc: np.ndarray, cfg: ScanConfig) -> list[int]:
    """阈值以上的召回峰：按分数降序做非极大抑制，峰间至少隔
    ``peak_min_gap_frames``。不要求严格局部极大，因此相关平台与平顶峰
    （截断/边界常见）不会被漏掉；同分时优先较小滞后，保证确定性。
    """
    idx = np.where(cc >= cfg.recall_threshold)[0]
    cand = sorted(((float(cc[i]), int(i)) for i in idx),
                  key=lambda x: (-x[0], x[1]))
    picked: list[int] = []
    for score, lag in cand:
        if all(abs(lag - j) >= cfg.peak_min_gap_frames for j in picked):
            picked.append(lag)
        if len(picked) >= cfg.max_candidates_per_material:
            break
    picked.sort()
    return picked


def _frame_energy_mask(samples: np.ndarray, fcfg: F.FeatureConfig) -> np.ndarray:
    """每个特征帧的能量是否显著（高于素材中位能量的 15%）。

    截断播放时，素材未播出的尾部若落在节目背景上，低信息帧的余弦相似度
    可能偶然过阈而把覆盖区间错误拉长。用能量掩码把这些帧排除出连续段，
    覆盖区间就停在真实声音结束处。
    """
    starts = F.frame_samples(samples.shape[0], fcfg)
    if starts.size == 0:
        return np.zeros(0, dtype=bool)
    fl = fcfg.frame_len
    energy = np.array([np.mean(samples[s:s + fl] ** 2) for s in starts])
    thresh = max(np.median(energy) * 0.15, 1e-8)
    return energy >= thresh


def _verify_diagonal(q: np.ndarray, p: np.ndarray, lag: int,
                     cfg: ScanConfig, cc: np.ndarray,
                     energy_mask: Optional[np.ndarray] = None) -> Optional[dict]:
    """沿固定滞后逐帧验证，返回素材覆盖区间与统计量。"""
    lq, lp = q.shape[0], p.shape[0]
    lo = max(0, -lag)
    hi = min(lq, lp - lag)
    if hi - lo < cfg.min_verify_frames:
        return None
    sims = np.sum(q[lo:hi] * p[lag + lo:lag + hi], axis=1)
    good = sims >= cfg.verify_threshold
    if energy_mask is not None:
        good = good & energy_mask[lo:hi]
    # 桥接不超过 gap_tolerance 的短坏帧段（含低能量帧）
    bridged = good.copy()
    if (~good).any():
        bad_idx = np.where(~good)[0]
        bad_runs = np.split(bad_idx, np.where(np.diff(bad_idx) > 1)[0] + 1)
        for run in bad_runs:
            if 0 < run[0] and run[-1] < len(good) - 1 and len(run) <= cfg.gap_tolerance:
                bridged[run] = True
    if not bridged.any():
        return None
    idx = np.where(bridged)[0]
    segments = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
    best = max(segments, key=lambda s: (len(s), -s[0]))
    seg_lo, seg_hi = int(best[0]), int(best[-1]) + 1
    mat_lo, mat_hi = lo + seg_lo, lo + seg_hi
    n_good = int(good[seg_lo:seg_hi].sum())
    if n_good < cfg.min_verify_frames:
        return None
    return {
        "lag": int(lag),
        "mat_lo": mat_lo,
        "mat_hi": mat_hi,
        "prog_lo": lag + mat_lo,
        "prog_hi": lag + mat_hi,
        "frames": mat_hi - mat_lo,
        "good_frames": n_good,
        "mean_score": float(sims[seg_lo:seg_hi].mean()),
        "frame_sims": sims[seg_lo:seg_hi].astype(np.float64),
        "recall_score": float(cc[lag]) if lag < len(cc) else None,
    }


def _merge_candidates(parts: list[dict], material_frames: int) -> list[dict]:
    """同素材候选合并：节目区间真正重叠（严格相交）才合并。

    重复播放区间互不相交，保持为独立匹配。相邻滞后峰若只是相接触而不
    重叠，视为两次独立召回，不做并集（避免覆盖区间被拉长到素材之外）。
    """
    parts = sorted(parts, key=lambda d: (d["prog_lo"], d["prog_hi"]))
    merged: list[dict] = []
    for c in parts:
        if merged and c["prog_lo"] < merged[-1]["prog_hi"]:
            m = merged[-1]
            shift = c["lag"] - m["lag"]
            new_mat_lo = max(0, min(m["mat_lo"], c["mat_lo"] - shift))
            new_mat_hi = min(material_frames,
                             max(m["mat_hi"], c["mat_hi"] - shift))
            m["mat_lo"], m["mat_hi"] = new_mat_lo, new_mat_hi
            m["prog_lo"] = m["lag"] + new_mat_lo
            m["prog_hi"] = m["lag"] + new_mat_hi
            m["frames"] = new_mat_hi - new_mat_lo
            m["good_frames"] = max(m["good_frames"], c["good_frames"])
            m["mean_score"] = max(m["mean_score"], c["mean_score"])
            m["merged_with"] = m.get("merged_with", 1) + 1
        else:
            merged.append(dict(c))
    return merged


def scan_material(material_id: str, q: np.ndarray, p: np.ndarray,
                  cfg: ScanConfig, fcfg: F.FeatureConfig,
                  energy_mask: Optional[np.ndarray] = None) -> list[Match]:
    if q.shape[0] == 0 or p.shape[0] < q.shape[0]:
        return []
    cc = _full_correlation(q, p)
    candidates: list[dict] = []
    for lag in _peak_lags(cc, cfg):
        local_score, seg_start = _local_segment_score(
            q, p, lag, min(cfg.window_frames, q.shape[0]))
        if local_score < cfg.local_threshold:
            continue
        v = _verify_diagonal(q, p, lag, cfg, cc, energy_mask)
        if v is not None:
            v["local_score"] = local_score
            v["local_segment_start"] = seg_start
            candidates.append(v)
    parts = _merge_candidates(candidates, q.shape[0])
    matches: list[Match] = []
    for c in parts:
        coverage = c["frames"] / q.shape[0]
        score = c["mean_score"]
        if score >= cfg.confirmed_min_score and coverage >= cfg.confirmed_min_coverage:
            certainty = "confirmed"
        elif score >= cfg.pending_min_score and coverage >= cfg.pending_min_coverage:
            # 截断、弱信号等有实质片段证据但达不到完整播放门槛
            certainty = "review"
        else:
            # 共享短片段、偶发局部相似：证据不足，不作为匹配输出
            continue
        prog_start = F.frame_to_sample(c["prog_lo"], fcfg)
        prog_end = F.frame_to_sample(c["prog_hi"], fcfg)
        mat_start = F.frame_to_sample(c["mat_lo"], fcfg)
        mat_end = F.frame_to_sample(c["mat_hi"], fcfg)
        sims = c["frame_sims"]
        evidence = {
            "lag_frame": c["lag"],
            "recall_score": round(float(cc[c["lag"]]), 5),
            "local_segment_score": round(c["local_score"], 5),
            "verify_mean": round(score, 5),
            "coverage": round(coverage, 4),
            "good_frames": c["good_frames"],
            "merged_candidates": c.get("merged_with", 1),
            "score_quantiles": [round(float(x), 4) for x in
                                np.quantile(sims, [0.0, 0.25, 0.5, 0.75, 1.0])],
            "frame_hop_samples": fcfg.hop_len,
        }
        matches.append(Match(
            play_id="", material_id=material_id,
            prog_start=prog_start, prog_end=prog_end,
            mat_start=mat_start, mat_end=mat_end,
            matched_frames=c["good_frames"], total_frames=c["frames"],
            contiguous_frames=c["frames"],
            mean_score=round(score, 5), certainty=certainty,
            lag_frame=c["lag"], evidence=evidence))
    matches.sort(key=lambda m: (m.prog_start, m.material_id))
    for k, m in enumerate(matches):
        m.play_id = f"{material_id}#p{k + 1:02d}"
    return matches


def _cross_material_nms(matches: list[Match], cfg: ScanConfig) -> list[Match]:
    """跨素材非极大抑制：同一段节目声音被多个素材命中时只保留强者。

    规则（明确、可复查）：按 (确定性, 平均相似度, 覆盖率) 降序逐条处理，
    若候选与已保留候选的节目区间 IoU >= 阈值，则丢弃弱者——共享短片段
    因此不可能让两个不同素材都被判为完整播放。同素材的重复播放区间互不
    相交，不会被这里影响。
    """
    def rank(m: Match):
        return (0 if m.certainty == "confirmed" else 1,
                -m.mean_score, -m.evidence["coverage"])

    ordered = sorted(matches, key=rank)
    kept: list[Match] = []
    for m in ordered:
        suppress = False
        for k in kept:
            lo = max(m.prog_start, k.prog_start)
            hi = min(m.prog_end, k.prog_end)
            inter = max(0, hi - lo)
            union = max(m.prog_end, k.prog_end) - min(m.prog_start, k.prog_start)
            iou = inter / union if union else 0.0
            if iou >= cfg.cross_material_iou_suppress:
                m.evidence = dict(m.evidence)
                m.evidence["suppressed_by"] = k.play_id
                m.evidence["suppression_iou"] = round(iou, 4)
                suppress = True
                break
        if not suppress:
            kept.append(m)
    kept.sort(key=lambda m: (m.prog_start, m.material_id, m.play_id))
    return kept


def scan_program(program_features: np.ndarray,
                 material_features: dict[str, np.ndarray],
                 cfg: ScanConfig, fcfg: F.FeatureConfig,
                 energy_masks: Optional[dict[str, np.ndarray]] = None) -> list[Match]:
    energy_masks = energy_masks or {}
    all_matches: list[Match] = []
    for mid in sorted(material_features):
        all_matches.extend(
            scan_material(mid, material_features[mid], program_features,
                          cfg, fcfg, energy_masks.get(mid)))
    all_matches = _cross_material_nms(all_matches, cfg)
    all_matches.sort(key=lambda m: (m.prog_start, m.material_id, m.play_id))
    return all_matches
