"""编码片的自描述文件格式。

每个片单独成文件, 头部为单行 JSON + '\\n', 其后紧跟 L 字节正文:

    ECSTORE-SHARD-V1\\n
    {"config_digest":...,"stripe_id":...,"index":i,"length":L,"body_sha256":...}\\n
    <L 字节正文>

片身份 = (配置摘要, stripe_id, 序号)。同序号不同正文会在服务层判冲突;
stripe_id 不同的片属于别的条带, 恢复仓逐条带扫描时不会混入。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAGIC = "ECSTORE-SHARD-V1"


class ShardFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Shard:
    config_digest: str
    stripe_id: str
    index: int
    length: int
    body_sha256: str
    body: bytes


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_shard(config_digest: str, stripe_id: str, index: int, body: bytes) -> bytes:
    header = {
        "magic": MAGIC,
        "config_digest": config_digest,
        "stripe_id": stripe_id,
        "index": index,
        "length": len(body),
        "body_sha256": sha256_hex(body),
    }
    return (json.dumps(header, separators=(",", ":")).encode() + b"\n" + body)


def parse_shard(raw: bytes) -> Shard:
    """解析片文件; 结构、magic、长度或正文摘要不符一律抛 ShardFormatError。"""
    nl1 = raw.find(b"\n")
    if nl1 < 0:
        raise ShardFormatError("缺少头部换行")
    try:
        header = json.loads(raw[:nl1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShardFormatError(f"头部不是合法 JSON: {exc}") from exc
    if not isinstance(header, dict) or header.get("magic") != MAGIC:
        raise ShardFormatError(f"magic 不匹配, 期望 {MAGIC}")
    try:
        cfg_d = str(header["config_digest"])
        stripe_id = str(header["stripe_id"])
        index = int(header["index"])
        length = int(header["length"])
        body_sha = str(header["body_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShardFormatError(f"头部字段缺失或类型错误: {exc}") from exc

    body = raw[nl1 + 1 :]
    if len(body) != length:
        raise ShardFormatError(f"正文长度 {len(body)} 与头部声明 {length} 不符")
    if sha256_hex(body) != body_sha:
        raise ShardFormatError("正文摘要不匹配: 片已损坏或被篡改, 隔离处理")
    return Shard(cfg_d, stripe_id, index, length, body_sha, body)
