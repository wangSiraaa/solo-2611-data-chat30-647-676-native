"""独立纠删码条带服务。

无网络、无对象仓依赖。持久化内容只有配置登记表 (configs/ 下的 JSON);
不缓存任何条带正文, 每次 encode/recover 都是纯计算 + 读盘。

配置验证结果按配置摘要缓存: 同一配置不重复枚举 C(n,k) 组合。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import codec, shard as shard_mod
from .codec import CodeConfig, ConfigError

STRIPE_ID_PREFIX = "ECSTORE-STRIPE-V1"


class ServiceError(RuntimeError):
    pass


@dataclass
class FileIssue:
    file: str
    reason: str


@dataclass
class StripeReport:
    config_digest: str
    stripe_id: str | None
    status: str                       # recovered | insufficient | conflict | failed | empty
    valid_indices: list[int]
    duplicate_files: list[FileIssue]
    quarantined: list[FileIssue]      # 结构损坏/摘要不符
    conflicts: list[FileIssue]        # 同序号不同正文
    foreign: list[FileIssue]          # 别的配置或别的 stripe 的片
    reason: str = ""
    original_digest: str | None = None
    piece_digests: dict[int, str] = field(default_factory=dict)
    recovered_data: bytes | None = None
    expected_stripe_id: str | None = None
    pieces: dict[int, bytes] = field(default_factory=dict)


def stripe_identity(config_digest: str, original_digest: str) -> str:
    """条带身份 = 配置 + 原始条带正文摘要的复合哈希。

    仅编码参数相同不代表同一次编码: 不同原始正文会得到不同 stripe_id。
    """
    h = hashlib.sha256()
    h.update(STRIPE_ID_PREFIX.encode())
    h.update(config_digest.encode())
    h.update(original_digest.encode())
    return h.hexdigest()[:32]


class StripeService:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.configs_dir = self.root / "configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        # 配置验证缓存: config_digest -> CodeConfig (能加载即已通过 MDS 校验)
        self._cache: dict[str, CodeConfig] = {}
        for path in sorted(self.configs_dir.glob("*.json")):
            with open(path, "rb") as f:
                spec = json.loads(f.read())
            self._cache[spec["digest"]] = _spec_to_config(spec["spec"])

    # ---------- 配置登记 ----------
    def register(self, k: int, n: int, L: int,
                 coding_rows: list[list[int]] | None = None) -> CodeConfig:
        """登记配置; 任意 k 行不可逆时 make_config 抛 ConfigError 拒绝。"""
        config = codec.make_config(k, n, L, coding_rows)  # 内部完成 MDS 枚举
        digest = config.digest
        if digest in self._cache:
            return config  # 已登记, 幂等返回
        path = self.configs_dir / f"{digest[:16]}.json"
        record = {"digest": digest, "spec": config.to_spec()}
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self._cache[digest] = config
        return config

    def get_config(self, digest: str) -> CodeConfig:
        if digest not in self._cache:
            raise ServiceError(f"配置 {digest[:16]}... 未在本服务登记")
        return self._cache[digest]

    def list_configs(self) -> list[dict]:
        return [
            {"digest": d, "k": c.k, "n": c.n, "L": c.L,
             "coding_rows": [list(r) for r in c.coding_rows]}
            for d, c in sorted(self._cache.items())
        ]

    # ---------- 编码 ----------
    def encode(self, config_digest: str, data: bytes,
               out_dir: str | os.PathLike) -> dict:
        config = self.get_config(config_digest)
        pieces = codec.encode_stripe(config, data)
        original_digest = shard_mod.sha256_hex(data)
        sid = stripe_identity(config_digest, original_digest)

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        piece_digests: dict[int, str] = {}
        for i, body in enumerate(pieces):
            raw = shard_mod.build_shard(config_digest, sid, i, body)
            (out / f"shard_{i:02d}.bin").write_bytes(raw)
            piece_digests[i] = shard_mod.sha256_hex(body)

        meta = {
            "config_digest": config_digest,
            "config_summary": config.to_spec(),
            "stripe_id": sid,
            "original_length": len(data),
            "original_sha256": original_digest,
            "piece_sha256": {str(i): piece_digests[i] for i in range(config.n)},
        }
        (out / "stripe.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta

    # ---------- 核验 ----------
    def verify(self, dir_path: str | os.PathLike,
               expected_stripe_id: str | None = None) -> StripeReport:
        """只核验不恢复: 解析目录内全部片并分类报告。"""
        report = self._scan(dir_path, expected_stripe_id)
        if report.status in ("insufficient", "conflict", "failed", "empty"):
            return report
        config = self.get_config(report.config_digest)
        return self._recover_core(config, report,
                                  expected_original_digest=None,
                                  publish_data=False)

    # ---------- 恢复 ----------
    def recover(self, dir_path: str | os.PathLike,
                expected_stripe_id: str | None = None,
                expected_original_digest: str | None = None) -> StripeReport:
        """从目录的真实片集恢复 k 个数据片 (拼成 k*L 字节)。

        闸门顺序: 身份过滤 -> 冲突检查 -> 数量检查 -> 解码 ->
        重编码核对全部有效片 -> 原始摘要/条带身份核对。任一不过不发布成功。
        """
        report = self._scan(dir_path, expected_stripe_id)
        if report.status in ("insufficient", "conflict", "empty"):
            return report
        config = self.get_config(report.config_digest)
        return self._recover_core(config, report, expected_original_digest)

    def recover_pieces(self, config_digest: str, stripe_id: str,
                       pieces: dict[int, bytes],
                       duplicate_files: list[FileIssue] | None = None,
                       quarantined: list[FileIssue] | None = None,
                       conflicts: list[FileIssue] | None = None,
                       foreign: list[FileIssue] | None = None,
                       expected_original_digest: str | None = None
                       ) -> StripeReport:
        """对调用方已按 stripe_id 分桶的内存片集执行恢复。

        恢复仓对整个片目录只解析一次再逐条带调用本方法, 避免 O(条带^2) 扫盘。
        """
        config = self.get_config(config_digest)
        report = StripeReport(
            config_digest=config_digest, stripe_id=stripe_id, status="ready",
            valid_indices=sorted(pieces),
            duplicate_files=list(duplicate_files or []),
            quarantined=list(quarantined or []),
            conflicts=list(conflicts or []),
            foreign=list(foreign or []),
            expected_stripe_id=stripe_id,
            pieces=dict(pieces),
        )
        if report.conflicts:
            report.status = "conflict"
            report.reason = "存在同序号不同正文的冲突片, 该序号不可用"
            return report
        if len(pieces) < config.k:
            report.status = "insufficient"
            report.reason = (f"仅 {len(pieces)} 个有效不同片, "
                             f"少于 k={config.k}, 不靠补零恢复")
            return report
        return self._recover_core(config, report, expected_original_digest)

    def _recover_core(self, config: CodeConfig, report: StripeReport,
                      expected_original_digest: str | None,
                      publish_data: bool = True) -> StripeReport:
        """解码 + 重编码核对 + 条带身份/原始摘要核对的共用闸门。"""
        pieces = report.pieces
        try:
            recovered = codec.recover_stripe(config, pieces)
        except ValueError as exc:
            report.status = "failed"
            report.reason = str(exc)
            return report

        # 恢复后重新编码, 核对所有提供的有效片
        mismatched = codec.reencode_match(config, recovered, pieces)
        if mismatched:
            report.status = "failed"
            report.reason = f"重编码核对不一致的片序号: {mismatched}"
            return report

        data = b"".join(recovered)
        original_digest = shard_mod.sha256_hex(data)
        report.original_digest = original_digest

        # 核对条带身份 (片头里的 stripe_id 必须与恢复正文推导的一致)
        if stripe_identity(report.config_digest, original_digest) != report.stripe_id:
            report.status = "failed"
            report.reason = "恢复正文推导出的 stripe_id 与片头身份不一致"
            return report
        if (expected_original_digest is not None
                and original_digest != expected_original_digest):
            report.status = "failed"
            report.reason = "原始条带摘要与期望值不符"
            return report

        report.status = "recovered"
        report.piece_digests = {idx: shard_mod.sha256_hex(p)
                                for idx, p in pieces.items()}
        if publish_data:
            report.recovered_data = data
        return report

    # ---------- 内部: 目录扫描与分类 ----------
    def _scan(self, dir_path: str | os.PathLike,
              expected_stripe_id: str | None) -> StripeReport:
        directory = Path(dir_path)
        report = StripeReport(
            config_digest="",
            stripe_id=expected_stripe_id,
            status="empty",
            valid_indices=[],
            duplicate_files=[],
            quarantined=[],
            conflicts=[],
            foreign=[],
            expected_stripe_id=expected_stripe_id,
        )
        report.pieces = {}
        files = sorted(p for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []
        if not files:
            report.reason = "目录中没有任何文件"
            return report

        # index -> (body_sha, body); 同序号同正文记重复, 不同正文记冲突
        accepted: dict[int, tuple[str, bytes]] = {}
        seen_digests: set[str] = set()
        seen_sids: set[str] = set()
        for path in files:
            try:
                sh = shard_mod.parse_shard(path.read_bytes())
            except shard_mod.ShardFormatError as exc:
                report.quarantined.append(FileIssue(path.name, str(exc)))
                continue

            if sh.config_digest not in self._cache:
                report.foreign.append(
                    FileIssue(path.name, f"配置 {sh.config_digest[:12]}... 未在本服务登记"))
                continue
            if expected_stripe_id is not None and sh.stripe_id != expected_stripe_id:
                report.foreign.append(
                    FileIssue(path.name,
                              f"属于别的条带 {sh.stripe_id} (期望 {expected_stripe_id})"))
                continue

            seen_digests.add(sh.config_digest)
            seen_sids.add(sh.stripe_id)
            if sh.index in accepted:
                prev_sha, _ = accepted[sh.index]
                if prev_sha == sh.body_sha256:
                    report.duplicate_files.append(FileIssue(path.name, f"序号 {sh.index} 的重复片"))
                else:
                    report.conflicts.append(
                        FileIssue(path.name, f"序号 {sh.index} 出现不同正文, 与既有片冲突"))
                continue
            accepted[sh.index] = (sh.body_sha256, sh.body)
            report.pieces[sh.index] = sh.body

        if not seen_digests:
            report.status = "empty"
            report.reason = "没有任何片通过身份检查"
            return report
        # 通过身份检查的片应共享同一配置与同一条带 (不同者已进 foreign)
        report.config_digest = next(iter(seen_digests))
        report.stripe_id = next(iter(seen_sids))
        report.valid_indices = sorted(accepted)

        if report.conflicts:
            report.status = "conflict"
            report.reason = "存在同序号不同正文的冲突片, 该序号不可用"
            return report
        config = self._cache[report.config_digest]
        if len(accepted) < config.k:
            report.status = "insufficient"
            report.reason = (f"仅 {len(accepted)} 个有效不同片, "
                             f"少于 k={config.k}, 不靠补零恢复")
            return report
        report.status = "ready"
        return report

def _spec_to_config(spec: dict) -> CodeConfig:
    return codec.make_config(
        k=int(spec["k"]), n=int(spec["n"]), L=int(spec["L"]),
        coding_rows=[list(r) for r in spec["coding_rows"]],
    )
