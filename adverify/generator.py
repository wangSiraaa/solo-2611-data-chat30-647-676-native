"""合成音频生成器：素材库 + 30 分钟节目流 + 真值。

真值（ground truth）只写入独立 JSON，监测模块不允许 import 本模块、
不读取插入位置；只有 validate 命令用它统计定位误差/漏检/误检。

素材内容为多谐波 + 幅度/频率缓调 + 包络的复合信号：
* 不是单频，每个素材的基频、谐波权重、调制率均独立随机；
* 30 个素材彼此可区分；其中两个素材共享同一段短 jingle（竞争短片段）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from .audioio import Audio, write_wav

SR = 8000
PROGRAM_SECONDS = 30 * 60
N_MATERIALS = 30
RNG_SEED = 20261118


@dataclass
class GenEvent:
    event_id: str
    material_id: Optional[str]          # 共享 jingle 单播时为 None
    kind: str                           # full / truncated_head / truncated_tail / repeated / volume / shared_jingle
    prog_start: int
    prog_end: int
    mat_start: int
    mat_end: int
    gain: float
    note: str = ""


# --------------------------------------------------------------------------- #
# 素材合成
# --------------------------------------------------------------------------- #
def _adsr(n: int, rng: np.random.Generator) -> np.ndarray:
    a = max(1, int(n * 0.04))
    r = max(1, int(n * 0.08))
    env = np.ones(n, dtype=np.float64)
    env[:a] = np.linspace(0.0, 1.0, a)
    env[-r:] = np.linspace(1.0, 0.0, r)
    # 缓慢起伏，避免恒定电平
    env *= 0.85 + 0.15 * np.sin(2 * np.pi * rng.uniform(0.2, 0.7) * np.arange(n) / SR)
    return env


def _tone_material(duration: float, rng: np.random.Generator,
                   base_freq: float, n_harm: int, material_index: int) -> np.ndarray:
    """非平稳复合素材：谐波托底 + 线性扫频 + FM 音 + 独特啁啾序列。

    扫频与啁啾的频率随时间变化，稳态背景无法在整条时间轴上保持高相似度，
    因此真播放与背景伪响应在“连续对角验证”下可分；每个素材使用由
    material_index 派生的独立随机流，啁啾音符取自宽音域且时长独立，
    素材彼此不同、不是单频，也不会出现跨素材的长段雷同音符序列。
    """
    # 独立 RNG：避免多个素材从共享 rng 抽到相近的音符/参数组合
    rng = np.random.default_rng(91001 + material_index * 7919)
    n = int(duration * SR)
    t = np.arange(n, dtype=np.float64) / SR
    out = np.zeros(n, dtype=np.float64)

    # 1) 谐波托底（幅度调制，占小头）
    harm = np.zeros(n, dtype=np.float64)
    for h in range(1, n_harm + 1):
        f = base_freq * h * (1.0 + rng.uniform(-0.004, 0.004))
        amp = rng.uniform(0.1, 0.6) / h
        harm += amp * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))
    out += 0.5 * harm

    # 2) 一条贯穿的线性扫频，方向与速率每素材独立
    f0 = base_freq * rng.uniform(1.2, 2.0)
    f1 = base_freq * rng.uniform(2.5, 5.0)
    if rng.random() < 0.5:
        f0, f1 = f1, f0
    phase = 2 * np.pi * f0 * t + np.pi * (f1 - f0) / duration * t ** 2
    out += 0.9 * np.sin(phase + rng.uniform(0, 2 * np.pi))

    # 3) FM 调制音（调制率每素材独立）
    fm_rate = rng.uniform(5.0, 9.0)
    fm_depth = rng.uniform(20.0, 80.0)
    out += 0.5 * np.sin(2 * np.pi * base_freq * 1.5 * t
                        + fm_depth / fm_rate * np.sin(2 * np.pi * fm_rate * t))

    # 4) 独特啁啾音符序列：宽音域 + 独立时长 + 音符内小扫频
    n_notes = max(5, int(duration / rng.uniform(0.5, 1.1)))
    boundaries = np.sort(rng.uniform(0, duration, size=n_notes - 1))
    bounds = np.concatenate(([0.0], boundaries, [duration]))
    for k in range(n_notes):
        a = int(bounds[k] * SR)
        b = int(bounds[k + 1] * SR)
        nf = rng.uniform(220.0, 2400.0)  # 宽音域随机音符，跨素材撞序列概率极低
        tt = np.arange(b - a) / SR
        target = nf * rng.uniform(0.7, 1.4)
        seg_dur = max((b - a) / SR, 1e-3)
        out[a:b] += 0.7 * np.sin(
            2 * np.pi * nf * tt
            + np.pi * (target - nf) / seg_dur * tt ** 2
            + rng.uniform(0, 2 * np.pi))

    # 5) 很弱的素材专属宽带噪声纹理（确定性种子，登记与播放处一致）
    local_rng = np.random.default_rng(rng.integers(0, 2**31))
    out += 0.03 * local_rng.normal(0, 1, size=n)

    # 6) 素材“签名”标记音：从该素材专属码本中按独立时序放置的短促纯音。
    # 码本频率由 material_index 哈希到分散的窄带，30 个素材的码本两两不同；
    # 标记音能量显著且带时间结构，背景与其它素材不可能在同一 (时刻, 频率)
    # 上复现，因此截断点之后的素材标记缺失时相似度必然跌落。
    marker = _signature_markers(n, material_index, duration)
    out += 0.85 * marker

    am = 1.0 + rng.uniform(0.05, 0.2) * np.sin(2 * np.pi * rng.uniform(1.5, 4.0) * t)
    out *= am * _adsr(n, rng)
    peak = np.max(np.abs(out)) or 1.0
    return out / peak * rng.uniform(0.55, 0.9)


def _signature_markers(n: int, material_index: int, duration: float) -> np.ndarray:
    """生成素材专属的标记音轨：分散窄带 + 独立随机时序。"""
    rng = np.random.default_rng(770001 + material_index * 104729)
    track = np.zeros(n, dtype=np.float64)
    # 每个素材 6 个候选标记频率，按索引散布到 700..3300 Hz
    base = 700 + (material_index * 193) % 2200
    codebook = sorted({int(base + (k * 317 + material_index * 53) % 1900)
                       for k in range(6)})
    # 每 0.4~0.9 秒放一个 90~160 ms 的标记音，时序与频率均独立
    t_cursor = rng.uniform(0.15, 0.5)
    while t_cursor < duration - 0.2:
        f = float(rng.choice(codebook))
        mlen = int(rng.uniform(0.09, 0.16) * SR)
        a = int(t_cursor * SR)
        b = min(n, a + mlen)
        tt = np.arange(b - a) / SR
        env = np.sin(np.pi * np.arange(b - a) / (b - a))  # 淡入淡出
        track[a:b] += env * np.sin(2 * np.pi * f * tt + rng.uniform(0, 6.28))
        t_cursor += rng.uniform(0.4, 0.9)
    return track


def make_jingle(duration: float = 1.6, rng: np.random.Generator | None = None) -> np.ndarray:
    """两个素材共享的短 jingle。"""
    rng = rng or np.random.default_rng(7)
    n = int(duration * SR)
    t = np.arange(n, dtype=np.float64) / SR
    out = np.zeros(n)
    for f in (660.0, 990.0, 1320.0):
        out += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) / 3.0
    out *= _adsr(n, rng)
    return out / (np.max(np.abs(out)) or 1.0) * 0.7


def build_materials(seed: int = RNG_SEED):
    rng = np.random.default_rng(seed)
    materials: dict[str, np.ndarray] = {}
    meta: list[dict] = []
    durations = rng.integers(5, 31, size=N_MATERIALS)  # 5..30 秒
    base_freqs = rng.uniform(140.0, 520.0, size=N_MATERIALS)
    jingle = make_jingle(rng=np.random.default_rng(seed + 99))
    for i in range(N_MATERIALS):
        mid = f"m{i + 1:03d}"
        sig = _tone_material(float(durations[i]), rng, float(base_freqs[i]),
                             n_harm=int(rng.integers(3, 7)), material_index=i)
        rec = {"material_id": mid, "duration_s": float(durations[i]),
               "base_freq": round(float(base_freqs[i]), 2), "shared_jingle": False}
        # m003 与 m017 在不同位置嵌入同一段共享 jingle
        if mid in ("m003", "m017"):
            pos = int(0.3 * sig.shape[0]) if mid == "m003" else int(0.62 * sig.shape[0])
            end = min(sig.shape[0], pos + jingle.shape[0])
            sig[pos:end] = 0.75 * sig[pos:end] + 0.5 * jingle[:end - pos]
            rec["shared_jingle"] = True
            rec["jinkle_pos"] = pos
        materials[mid] = sig
        meta.append(rec)
    return materials, meta, jingle


# --------------------------------------------------------------------------- #
# 节目流合成
# --------------------------------------------------------------------------- #
def _background_bed(n: int, rng: np.random.Generator) -> np.ndarray:
    """与素材频谱不重合的弱背景：低频慢变音 + 很弱的宽带噪声。"""
    t = np.arange(n, dtype=np.float64) / SR
    bed = 0.05 * np.sin(2 * np.pi * 90 * t)
    bed += 0.03 * np.sin(2 * np.pi * 137 * t + 1.3)
    # 缓慢扫频
    sweep_f = 200 + 60 * np.sin(2 * np.pi * t / 45.0)
    bed += 0.02 * np.sin(2 * np.pi * np.cumsum(sweep_f) / SR)
    bed += rng.normal(0.0, 0.006, size=n)   # 少量噪声
    return bed


def build_program(materials: dict[str, np.ndarray], jingle: np.ndarray,
                  seed: int = RNG_SEED + 1):
    rng = np.random.default_rng(seed)
    total = PROGRAM_SECONDS * SR
    prog = _background_bed(total, rng)
    events: list[GenEvent] = []
    cursor = SR * 8  # 开头留 8 秒纯背景

    def place(mat_id, kind, gain, mat_lo, mat_hi, note):
        nonlocal cursor
        seg = materials[mat_id][mat_lo:mat_hi]
        gap = int(rng.integers(SR * 4, SR * 12))
        if cursor + seg.shape[0] + gap > total - SR * 30:
            return False
        prog[cursor:cursor + seg.shape[0]] += seg * gain
        ev = GenEvent(
            event_id=f"e{len(events) + 1:03d}", material_id=mat_id, kind=kind,
            prog_start=cursor, prog_end=cursor + seg.shape[0],
            mat_start=mat_lo, mat_end=mat_hi, gain=round(float(gain), 3), note=note)
        events.append(ev)
        cursor += seg.shape[0] + gap
        return True

    mids = list(materials.keys())
    # 1) 每个素材至少一次完整播放（轮次顺序由固定种子打乱）
    order = rng.permutation(mids)
    for mid in order:
        place(mid, "full", 1.0, 0, materials[mid].shape[0], "基线完整播放")

    # 2) 截断：随机 8 个素材，分别做头截断 / 尾截断
    trunc_pool = rng.choice(mids, size=8, replace=False)
    for k, mid in enumerate(trunc_pool):
        n = materials[mid].shape[0]
        if k % 2 == 0:
            cut = int(rng.integers(int(0.25 * n), int(0.45 * n)))
            place(mid, "truncated_head", 1.0, cut, n, f"头截断 {cut / SR:.1f}s")
        else:
            cut = int(rng.integers(int(0.55 * n), int(0.8 * n)))
            place(mid, "truncated_tail", 1.0, 0, cut, f"尾截断至 {cut / SR:.1f}s")

    # 3) 重复播放：6 个素材再完整播一次（与首次分开计）
    for mid in rng.choice(mids, size=6, replace=False):
        place(mid, "repeated", 1.0, 0, materials[mid].shape[0], "重复完整播放")

    # 4) 音量变化：5 个素材以不同增益完整/近似完整播放
    for k, mid in enumerate(rng.choice(mids, size=5, replace=False)):
        gain = float(rng.choice([0.35, 0.55, 1.6, 2.1]))
        n = materials[mid].shape[0]
        place(mid, "volume", gain, 0, n, f"增益 x{gain}")

    # 5) 共享短 jingle 单独出现一次：任何宿主素材都不得被判为完整播放
    def place_jingle():
        nonlocal cursor
        gap = SR * 7
        if cursor + jingle.shape[0] + gap < total - SR * 20:
            prog[cursor:cursor + jingle.shape[0]] += jingle * 0.9
            events.append(GenEvent(
                event_id=f"e{len(events) + 1:03d}", material_id=None,
                kind="shared_jingle", prog_start=cursor,
                prog_end=cursor + jingle.shape[0], mat_start=0,
                mat_end=jingle.shape[0], gain=0.9,
                note="共享短片段单播，竞争素材均不得确认完整播放"))
            cursor += jingle.shape[0] + gap

    place_jingle()

    # 再加一轮低增益重复，考验弱信号召回
    for mid in rng.choice(mids, size=3, replace=False):
        place(mid, "repeated", 0.45, 0, materials[mid].shape[0], "低增益重复播放")

    peak = np.max(np.abs(prog)) or 1.0
    if peak > 0.99:
        prog = prog / peak * 0.99
    return Audio(prog, SR), events


def generate_all(out_dir: str, seed: int = RNG_SEED) -> dict:
    mat_dir = os.path.join(out_dir, "materials")
    os.makedirs(mat_dir, exist_ok=True)
    materials, meta, jingle = build_materials(seed)
    for mid, sig in materials.items():
        write_wav(os.path.join(mat_dir, f"{mid}.wav"), Audio(sig, SR))
    program, events = build_program(materials, jingle, seed + 1)
    prog_path = os.path.join(out_dir, "program.wav")
    write_wav(prog_path, program)
    gt = {
        "sr": SR,
        "program_samples": program.n,
        "program_seconds": PROGRAM_SECONDS,
        "n_materials": N_MATERIALS,
        "events": [asdict(e) for e in events],
        "material_meta": meta,
        "note": "真值仅供 validate 路径使用，监测模块不得读取本文件",
    }
    with open(os.path.join(out_dir, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=2)
    return {"out_dir": out_dir, "program": prog_path, "material_dir": mat_dir,
            "ground_truth": os.path.join(out_dir, "ground_truth.json"),
            "n_events": len(events)}
