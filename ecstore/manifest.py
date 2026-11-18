"""对象仓清单 (manifest) 格式。

清单是对象恢复的唯一依据, 记录真实对象长度 (末条带补零不丢失该信息)、
条带顺序、配置、各片摘要与原始条带摘要。零字节对象有合法空清单,
不生成假条带。清单按对象正文摘要寻址, 一经写入不可变。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MANIFEST_MAGIC = "ECSTORE-MANIFEST-V1"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class StripeEntry:
    stripe_id: str
    original_sha256: str
    piece_sha256: dict[int, str]

    def to_json(self) -> dict:
        return {
            "stripe_id": self.stripe_id,
            "original_sha256": self.original_sha256,
            "piece_sha256": {str(k): v for k, v in sorted(self.piece_sha256.items())},
        }


@dataclass(frozen=True)
class Manifest:
    object_sha256: str
    object_length: int
    config_digest: str
    config_spec: dict
    k: int
    n: int
    L: int
    stripes: tuple[StripeEntry, ...]

    def to_json(self) -> dict:
        return {
            "magic": MANIFEST_MAGIC,
            "object_sha256": self.object_sha256,
            "object_length": self.object_length,
            "config_digest": self.config_digest,
            "config_spec": self.config_spec,
            "k": self.k,
            "n": self.n,
            "L": self.L,
            "stripe_count": len(self.stripes),
            "stripes": [s.to_json() for s in self.stripes],
        }


def build_manifest(object_data: bytes, config, stripe_entries: list[StripeEntry]) -> Manifest:
    return Manifest(
        object_sha256=hashlib.sha256(object_data).hexdigest(),
        object_length=len(object_data),
        config_digest=config.digest,
        config_spec=config.to_spec(),
        k=config.k,
        n=config.n,
        L=config.L,
        stripes=tuple(stripe_entries),
    )


def serialize(manifest: Manifest) -> bytes:
    return (json.dumps(manifest.to_json(), ensure_ascii=False, indent=2) + "\n").encode()


def parse_manifest(raw: bytes) -> Manifest:
    """解析并严格校验清单; 截断/缺字段/类型错误一律 ManifestError。"""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"清单不是合法 JSON (可能被截断): {exc}") from exc
    if not isinstance(doc, dict) or doc.get("magic") != MANIFEST_MAGIC:
        raise ManifestError(f"magic 不匹配, 期望 {MANIFEST_MAGIC}")
    try:
        object_sha = str(doc["object_sha256"])
        object_length = int(doc["object_length"])
        config_digest = str(doc["config_digest"])
        config_spec = dict(doc["config_spec"])
        k, n, L = int(doc["k"]), int(doc["n"]), int(doc["L"])
        stripe_count = int(doc["stripe_count"])
        raw_stripes = list(doc["stripes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"清单顶层字段缺失或类型错误: {exc}") from exc

    if object_length < 0 or stripe_count != len(raw_stripes):
        raise ManifestError("stripe_count 与条带条目数不一致")
    if len(object_sha) != 64:
        raise ManifestError("object_sha256 长度非法")

    entries: list[StripeEntry] = []
    for pos, item in enumerate(raw_stripes):
        try:
            sid = str(item["stripe_id"])
            orig_sha = str(item["original_sha256"])
            piece_map = {int(idx): str(sha) for idx, sha in dict(item["piece_sha256"]).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"第 {pos} 个条带条目损坏: {exc}") from exc
        if set(piece_map) != set(range(n)):
            raise ManifestError(f"第 {pos} 个条带的片摘要应恰好覆盖序号 0..{n - 1}")
        entries.append(StripeEntry(sid, orig_sha, piece_map))
    return Manifest(object_sha, object_length, config_digest, config_spec,
                    k, n, L, tuple(entries))
