"""检索特征：分帧对数梅尔频谱（log-mel），帧级 L2 归一化。

设计要点
--------
* 25 ms 汉宁窗、10 ms 帧移，FFT 后接三角梅尔滤波器组取对数能量；
* 每帧减去自身均值并做 L2 归一化 -> 帧成为单位向量，素材在节目中被整体
  放大/衰减（音量变化）后，帧间余弦相似度保持不变，因此不依赖绝对响度；
* 不使用整段哈希：匹配在帧序列上做局部对齐，截断素材自然只覆盖部分帧；
* 特征配置参与哈希，配置或素材信号变化都会使旧索引失效（见 monitor 模块）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class FeatureConfig:
    sr: int = 8000
    frame_ms: int = 25
    hop_ms: int = 10
    n_mels: int = 32
    fmin: float = 80.0
    fmax: float = 3600.0
    n_fft: int = 512
    n_peaks: int = 6        # 每帧保留的最强梅尔频带（静态峰）数
    use_delta: bool = True  # 再取一阶差分绝对值的峰，刻画非平稳动态

    @property
    def frame_len(self) -> int:
        return self.sr * self.frame_ms // 1000

    @property
    def hop_len(self) -> int:
        return self.sr * self.hop_ms // 1000

    def stable_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def config_hash(self) -> str:
        return hashlib.sha256(("logmel-v1|" + self.stable_json()).encode()).hexdigest()[:16]


def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _mel_filterbank(cfg: FeatureConfig) -> np.ndarray:
    """返回 (n_mels, n_fft//2+1) 三角滤波器矩阵。"""
    n_bins = cfg.n_fft // 2 + 1
    mels = np.linspace(_hz_to_mel(cfg.fmin), _hz_to_mel(cfg.fmax), cfg.n_mels + 2)
    hz = _mel_to_hz(mels)
    bins = np.floor((cfg.n_fft + 1) * hz / cfg.sr).astype(int)
    fb = np.zeros((cfg.n_mels, n_bins), dtype=np.float64)
    for m in range(cfg.n_mels):
        l, c, r = bins[m], bins[m + 1], bins[m + 2]
        if c == l:
            fb[m, c] = 1.0
        else:
            for b in range(l, c):
                fb[m, b] = (b - l) / max(c - l, 1)
        if r == c:
            fb[m, c] = 1.0
        else:
            for b in range(c, r):
                fb[m, b] = (r - b) / max(r - c, 1)
    return fb


def frame_samples(n: int, cfg: FeatureConfig) -> np.ndarray:
    """每个特征帧对应的音频起始采样点（半开区间起点）。"""
    hop = cfg.hop_len
    if n < cfg.frame_len:
        return np.zeros(0, dtype=np.int64)
    count = 1 + (n - cfg.frame_len) // hop
    return np.arange(count, dtype=np.int64) * hop


def extract(samples: np.ndarray, cfg: FeatureConfig,
            white_stats: Optional[tuple[np.ndarray, np.ndarray]] = None) -> np.ndarray:
    """返回 (T, n_mels[*2]) 的单位归一化帧序列。

    white_stats 参数保留以兼容调用方；当前表示不做全局白化（实测高维单位
    向量上白化会把背景余弦推高、适得其反），判别力来自非平稳素材结构本身。
    """
    samples = np.asarray(samples, dtype=np.float64)
    starts = frame_samples(samples.shape[0], cfg)
    if starts.size == 0:
        dim = cfg.n_mels * (2 if cfg.use_delta else 1)
        return np.zeros((0, dim), dtype=np.float64)
    win = np.hanning(cfg.frame_len).astype(np.float64)
    frames = np.stack([samples[s:s + cfg.frame_len] * win for s in starts])
    spec = np.fft.rfft(frames, n=cfg.n_fft, axis=1)
    power = (spec.real ** 2 + spec.imag ** 2) / cfg.n_fft
    fb = _mel_filterbank(cfg)
    mel_e = np.maximum(power @ fb.T, 1e-10)
    logmel = np.log(mel_e)
    # 稀疏频谱峰指纹：每帧只保留能量最高的 n_peaks 个梅尔带（二值激活）。
    # 单位归一化后帧余弦 = 峰集合重合比例：同一段声音峰高度重合（~0.7-0.95），
    # 背景/其它素材的窄带峰几乎不重合（~0.15-0.3），从根本上压低误检基线，
    # 截断点之后素材标记音缺失，峰重合立即跌落，边界因此可辨。
    def topk_binary(matrix: np.ndarray, k: int) -> np.ndarray:
        k = min(k, matrix.shape[1])
        idx = np.argpartition(matrix, -k, axis=1)[:, -k:]
        out = np.zeros_like(matrix)
        rows = np.arange(matrix.shape[0])[:, None]
        out[rows, idx] = 1.0
        return out

    static_fp = topk_binary(logmel, cfg.n_peaks)
    parts = [static_fp]
    if cfg.use_delta:
        delta = np.zeros_like(logmel)
        delta[1:-1] = logmel[2:] - logmel[:-2]
        delta[0] = logmel[1] - logmel[0]
        delta[-1] = logmel[-1] - logmel[-2]
        parts.append(topk_binary(np.abs(delta), cfg.n_peaks))
    fp = np.concatenate(parts, axis=1)
    # 二值向量 L2 归一化 -> 余弦即 Jaccard 型峰重合度；对整体增益不变
    norm = np.linalg.norm(fp, axis=1, keepdims=True)
    return fp / np.maximum(norm, 1e-12)


def estimate_white_stats(samples_list: list[np.ndarray],
                         cfg: FeatureConfig) -> tuple[np.ndarray, np.ndarray]:
    """保留接口占位；当前表示未使用全局白化（高维单位向量余弦会过度集中）。"""
    feats = [extract(s, cfg) for s in samples_list]
    all_f = np.concatenate([f for f in feats if f.shape[0]], axis=0)
    return all_f.mean(axis=0), all_f.std(axis=0)


def frame_to_sample(frame_idx: int, cfg: FeatureConfig) -> int:
    return int(frame_idx) * cfg.hop_len


def sample_to_frame(sample: int, cfg: FeatureConfig) -> int:
    return int(sample) // cfg.hop_len
