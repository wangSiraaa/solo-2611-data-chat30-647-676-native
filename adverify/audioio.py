"""单声道 PCM WAV 读写与基础信号处理。

题面约定输入采样率统一（8000 Hz、单声道、16-bit PCM），因此这里不做
重采样/变调/变速，只负责磁盘 PCM 与浮点信号之间的转换。
"""
from __future__ import annotations

import wave
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Audio:
    samples: np.ndarray  # float64, 取值约在 [-1, 1]
    sr: int

    @property
    def n(self) -> int:
        return int(self.samples.shape[0])

    def slice(self, start: int, end: int) -> "Audio":
        start = max(0, int(start))
        end = min(self.n, int(end))
        return Audio(self.samples[start:end].copy(), self.sr)


def read_wav(path: str) -> Audio:
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"仅支持 16-bit PCM，文件 {path} 的 sampwidth={sw}")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if nch > 1:
        data = data.reshape(-1, nch).mean(axis=1)
    return Audio(data, sr)


def write_wav(path: str, audio: Audio) -> None:
    x = np.clip(audio.samples, -1.0, 1.0)
    pcm = np.round(x * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.sr)
        w.writeframes(pcm.tobytes())


def rms_db(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2) + 1e-12))
    return 20.0 * np.log10(max(rms, 1e-9))


def apply_gain(samples: np.ndarray, linear_gain: float) -> np.ndarray:
    return samples.astype(np.float64) * float(linear_gain)


def add_noise(samples: np.ndarray, rng: np.random.Generator, noise_rms: float) -> np.ndarray:
    out = samples.astype(np.float64).copy()
    n = rng.normal(0.0, noise_rms, size=out.shape)
    return out + n
