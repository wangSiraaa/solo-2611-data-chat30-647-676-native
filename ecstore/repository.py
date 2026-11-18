"""受损对象恢复仓 (对象层, 构建在独立条带服务之上)。

仓库自身只持久化: 配置登记 (委托 StripeService)、按对象正文摘要寻址的
清单与编码片、名称别名。不保存任何原始对象副本; 恢复只能从目录里的
真实片集计算得到。关闭重开后行为完全一致 (无内存态依赖)。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import manifest as manifest_mod
from . import shard as shard_mod
from .manifest import Manifest, ManifestError, StripeEntry
from .service import FileIssue, StripeService, stripe_identity


class RepositoryError(RuntimeError):
    pass


@dataclass
class StripeStatus:
    index: int
    stripe_id: str
    status: str                       # recovered | insufficient | conflict | failed | empty
    reason: str
    valid_indices: list[int]
    quarantined: list[FileIssue]
    conflicts: list[FileIssue]
    duplicates: list[FileIssue]
    foreign: list[FileIssue]
    manifest_mismatch: list[FileIssue]
    recovered: bytes | None = None
    original_sha256: str | None = None
    pieces: dict[int, bytes] | None = None


@dataclass
class RecoveryResult:
    object_sha256: str
    object_length: int
    complete: bool
    output_path: str | None
    output_sha256: str | None
    exported_length: int
    verified_ranges: list[list[int]]
    stripe_status: list[StripeStatus]
    manifest_error: str | None = None
    reason: str = ""
    unattributed_quarantined: list[FileIssue] | None = None


class ObjectRepository:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.service = StripeService(self.root)  # 共享 configs/ 登记表
        self.objects_dir = self.root / "objects"
        self.aliases_dir = self.root / "aliases"
        self.objects_dir.mkdir(exist_ok=True)
        self.aliases_dir.mkdir(exist_ok=True)

    # ---------------------------------------------------------------- 归档
    def import_object(self, data: bytes, name: str) -> dict:
        """把任意长度对象切成条带并编码入库; 零字节对象只写空清单。

        先在临时暂存目录构建, 成功后原子改名; 任何中途失败都清理半成品,
        仓内不会留下没有清单的孤立片目录。
        """
        object_sha = shard_mod.sha256_hex(data)
        obj_dir = self.objects_dir / object_sha[:16]
        manifest_path = obj_dir / "manifest.json"
        if manifest_path.exists():
            # 内容寻址: 同正文对象天然幂等, 旧清单绝不重写
            self._update_alias(name, object_sha)
            return {"object_sha256": object_sha, "path": str(manifest_path),
                    "immutable": True, "note": "对象已存在, 清单保持不变"}

        staging = self.objects_dir / f".staging_{object_sha[:16]}"
        if staging.exists():
            shutil.rmtree(staging)
        shards_dir = staging / "shards"
        shards_dir.mkdir(parents=True)
        try:
            if len(data) == 0:
                # 零字节对象: 合法空清单, 不生成假条带
                configs = self.service.list_configs()
                if not configs:
                    raise RepositoryError("登记配置后才能导入对象 (零字节对象也需绑定配置)")
                cfg_digest = configs[0]["digest"]
                cfg = self.service.get_config(cfg_digest)
                mani = manifest_mod.build_manifest(data, cfg, [])
            else:
                cfg_digest, cfg = self._single_registered_config()
                stripe_entries: list[StripeEntry] = []
                k, L = cfg.k, cfg.L
                stripe_bytes = k * L
                for s in range(0, len(data), stripe_bytes):
                    chunk = data[s : s + stripe_bytes]
                    if len(chunk) < stripe_bytes:
                        chunk = chunk + b"\x00" * (stripe_bytes - len(chunk))  # 末条带补零
                    tmp_dir = staging / f".tmp_stripe_{s // stripe_bytes}"
                    meta = self.service.encode(cfg_digest, chunk, tmp_dir)
                    for i in range(cfg.n):
                        src = tmp_dir / f"shard_{i:02d}.bin"
                        dst = shards_dir / f"stripe_{s // stripe_bytes:04d}_piece_{i:02d}.bin"
                        os.replace(src, dst)
                    (tmp_dir / "stripe.json").unlink()
                    tmp_dir.rmdir()
                    stripe_entries.append(
                        StripeEntry(meta["stripe_id"], meta["original_sha256"],
                                    {i: meta["piece_sha256"][str(i)] for i in range(cfg.n)})
                    )
                mani = manifest_mod.build_manifest(data, cfg, stripe_entries)

            tmp_manifest = staging / "manifest.json"
            tmp_manifest.write_bytes(manifest_mod.serialize(mani))
            obj_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, obj_dir)  # 整目录原子发布
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._update_alias(name, object_sha)
        return {"object_sha256": object_sha, "path": str(manifest_path),
                "stripe_count": len(mani.stripes), "object_length": len(data)}

    def _single_registered_config(self):
        configs = self.service.list_configs()
        if len(configs) != 1:
            raise RepositoryError(
                f"归档需要恰好登记 1 个配置, 当前 {len(configs)} 个; 请先登记并清理")
        digest = configs[0]["digest"]
        return digest, self.service.get_config(digest)

    def _update_alias(self, name: str, object_sha: str) -> None:
        path = self.aliases_dir / f"{name}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if object_sha not in record.setdefault("history", []):
                record["history"].append(object_sha)
            # primary 保持指向首次导入, 旧清单引用关系不变
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            return
        path.write_text(json.dumps(
            {"name": name, "object_sha256": object_sha, "history": [object_sha]},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def list_objects(self) -> list[dict]:
        out = []
        for obj_dir in sorted(self.objects_dir.iterdir()):
            mp = obj_dir / "manifest.json"
            if not mp.exists():
                continue
            mani = manifest_mod.parse_manifest(mp.read_bytes())
            out.append({"object_sha256": mani.object_sha256,
                        "object_length": mani.object_length,
                        "stripe_count": len(mani.stripes),
                        "manifest": str(mp)})
        return out

    # ---------------------------------------------------------------- 检视
    def inspect_dir(self, recovery_dir: str | os.PathLike) -> dict:
        """逐条带报告可恢复状态与依据, 不写任何恢复正文。"""
        recovery_dir = Path(recovery_dir)
        mani, err = self._load_manifest(recovery_dir)
        if mani is None:
            return {"recovery_dir": str(recovery_dir), "manifest_error": err,
                    "stripes": [], "complete": False}
        statuses = self._evaluate_stripes(recovery_dir, mani)
        complete = all(st.status == "recovered" for st in statuses) if statuses else True
        return {
            "recovery_dir": str(recovery_dir),
            "object_sha256": mani.object_sha256,
            "object_length": mani.object_length,
            "stripe_count": len(mani.stripes),
            "complete": complete,
            "unattributed_quarantined": [
                {"file": i.file, "reason": i.reason}
                for i in getattr(self, "_last_unattributed", [])],
            "stripes": [self._status_to_dict(st) for st in statuses],
        }

    def _load_manifest(self, recovery_dir: Path):
        path = recovery_dir / "manifest.json"
        if not path.exists():
            return None, f"清单不存在: {path}"
        try:
            return manifest_mod.parse_manifest(path.read_bytes()), None
        except ManifestError as exc:
            return None, f"清单不可用: {exc}"

    def _ensure_config(self, mani: Manifest) -> None:
        """消费外部清单: 按清单内嵌的配置规格登记 (摘要一致才接受)。"""
        spec = mani.config_spec
        config = self.service.register(
            k=spec["k"], n=spec["n"], L=spec["L"],
            coding_rows=[list(r) for r in spec["coding_rows"]])
        if config.digest != mani.config_digest:
            raise RepositoryError("清单内嵌配置的摘要与 config_digest 不一致, 拒绝恢复")

    def _evaluate_stripes(self, recovery_dir: Path, mani: Manifest) -> list[StripeStatus]:
        """整个片目录只解析一次, 按 stripe_id 分桶后逐条带解码 (O(文件数))。"""
        self._ensure_config(mani)
        shards_dir = recovery_dir / "shards"

        # 每条带一个桶; 另有未归属桶 (无法解析片头或无法定位条带的文件)
        buckets: dict[str, dict] = {
            entry.stripe_id: {
                "index": s, "entry": entry,
                "pieces": {}, "dup": [], "quar": [], "conflict": [],
                "foreign": [], "mismatch": [],
                # 已见过但因摘要与清单不符而被搁置的片: index -> (文件名, 摘要)
                "seen_mismatch": {},
            }
            for s, entry in enumerate(mani.stripes)
        }
        unattributed: list[FileIssue] = []

        def infer_stripe_index(path: Path) -> int | None:
            # 归档命名 stripe_XXXX_piece_YY.bin; 外部同格式清单沿用该命名
            name = path.name
            if name.startswith("stripe_"):
                try:
                    return int(name.split("_")[1])
                except (IndexError, ValueError):
                    return None
            return None

        if shards_dir.is_dir():
            for path in sorted(p for p in shards_dir.iterdir() if p.is_file()):
                try:
                    sh = shard_mod.parse_shard(path.read_bytes())
                except shard_mod.ShardFormatError as exc:
                    issue = FileIssue(path.name, f"片结构/摘要损坏, 隔离: {exc}")
                    s_idx = infer_stripe_index(path)
                    if s_idx is not None and 0 <= s_idx < len(mani.stripes):
                        buckets[mani.stripes[s_idx].stripe_id]["quar"].append(issue)
                    else:
                        unattributed.append(issue)
                    continue

                if sh.config_digest != mani.config_digest:
                    self._route_foreign(buckets, mani, sh, path.name, unattributed,
                                        f"配置摘要 {sh.config_digest[:12]}... 与清单不符",
                                        infer_stripe_index(path))
                    continue
                bucket = buckets.get(sh.stripe_id)
                if bucket is None:
                    self._route_foreign(buckets, mani, sh, path.name, unattributed,
                                        f"属于清单之外的条带 {sh.stripe_id}",
                                        infer_stripe_index(path))
                    continue

                # 序号越界先隔离
                expected = bucket["entry"].piece_sha256
                if sh.index not in expected:
                    bucket["quar"].append(
                        FileIssue(path.name, f"片序号 {sh.index} 越界 (n={mani.n})"))
                    continue

                # 同序号冲突/重复判定必须与文件到达顺序无关:
                # 同时对照已接受的合法片和已搁置的清单不符片
                if sh.index in bucket["pieces"]:
                    prev = shard_mod.sha256_hex(bucket["pieces"][sh.index])
                    if prev == sh.body_sha256:
                        bucket["dup"].append(
                            FileIssue(path.name, f"序号 {sh.index} 的重复片"))
                    else:
                        bucket["conflict"].append(
                            FileIssue(path.name,
                                      f"序号 {sh.index} 出现不同正文, 与既有片冲突"))
                    continue
                if sh.index in bucket["seen_mismatch"]:
                    prev_name, prev_sha = bucket["seen_mismatch"][sh.index]
                    if prev_sha == sh.body_sha256:
                        bucket["dup"].append(
                            FileIssue(path.name,
                                      f"序号 {sh.index} 与 {prev_name} 为同正文重复片"))
                    else:
                        bucket["conflict"].append(
                            FileIssue(path.name,
                                      f"序号 {sh.index} 出现不同正文, "
                                      f"与 {prev_name} 冲突"))
                    continue

                # 与清单逐片摘要比对 (摘要不符单独标注并搁置, 不参与解码)
                if sh.body_sha256 != expected[sh.index]:
                    bucket["mismatch"].append(
                        FileIssue(path.name,
                                  f"序号 {sh.index} 片摘要与清单不符 "
                                  f"(片={sh.body_sha256[:12]}, 清单="
                                  f"{expected[sh.index][:12]})"))
                    bucket["seen_mismatch"][sh.index] = (path.name, sh.body_sha256)
                    continue
                bucket["pieces"][sh.index] = sh.body

        statuses: list[StripeStatus] = []
        for entry in mani.stripes:
            b = buckets[entry.stripe_id]
            report = self.service.recover_pieces(
                config_digest=mani.config_digest,
                stripe_id=entry.stripe_id,
                pieces=b["pieces"],
                duplicate_files=b["dup"],
                quarantined=b["quar"],
                conflicts=b["conflict"],
                foreign=b["foreign"],
                expected_original_digest=entry.original_sha256,
            )
            statuses.append(StripeStatus(
                index=b["index"],
                stripe_id=entry.stripe_id,
                status=report.status,
                reason=report.reason,
                valid_indices=report.valid_indices,
                quarantined=report.quarantined,
                conflicts=report.conflicts,
                duplicates=report.duplicate_files,
                foreign=report.foreign,
                manifest_mismatch=b["mismatch"],
                recovered=report.recovered_data,
                original_sha256=report.original_digest,
                pieces=b["pieces"],
            ))
        self._last_unattributed = unattributed
        return statuses

    @staticmethod
    def _route_foreign(buckets, mani, sh, filename: str,
                       unattributed: list[FileIssue], reason: str,
                       stripe_hint: int | None = None) -> None:
        issue = FileIssue(filename, reason)
        # 优先按片头 stripe_id 归因; 其次按文件名 stripe_XXXX 归因到同位置条带
        bucket = buckets.get(sh.stripe_id)
        if bucket is None and stripe_hint is not None and 0 <= stripe_hint < len(mani.stripes):
            bucket = buckets[mani.stripes[stripe_hint].stripe_id]
        if bucket is not None:
            bucket["foreign"].append(issue)
        else:
            unattributed.append(issue)

    # ---------------------------------------------------------------- 恢复
    def recover_object(self, recovery_dir: str | os.PathLike,
                       output_path: str | os.PathLike) -> RecoveryResult:
        """从恢复目录的真实片集恢复对象; 失败条带不发布成功。"""
        recovery_dir = Path(recovery_dir)
        mani, err = self._load_manifest(recovery_dir)
        if mani is None:
            return RecoveryResult(
                object_sha256="", object_length=0, complete=False,
                output_path=None, output_sha256=None, exported_length=0,
                verified_ranges=[], stripe_status=[], manifest_error=err,
                reason="清单缺失或损坏, 无法恢复")

        statuses = self._evaluate_stripes(recovery_dir, mani)
        verified_ranges = self._verified_ranges(mani, statuses)
        unattributed = getattr(self, "_last_unattributed", [])

        # 零字节对象: 无条带即完整, 导出空文件
        if len(mani.stripes) == 0:
            if mani.object_length != 0:
                return self._fail(mani, statuses, verified_ranges,
                                  "清单声明非零长度却没有任何条带")
            self._write_output(output_path, b"")
            return RecoveryResult(
                mani.object_sha256, 0, complete=True,
                output_path=str(output_path),
                output_sha256=shard_mod.sha256_hex(b""),
                exported_length=0, verified_ranges=[], stripe_status=statuses)

        all_ok = all(st.status == "recovered" for st in statuses)
        if all_ok:
            data = b"".join(st.recovered for st in statuses)[: mani.object_length]
            digest = shard_mod.sha256_hex(data)
            if digest != mani.object_sha256:
                return self._fail(
                    mani, statuses, verified_ranges,
                    f"整对象正文摘要 {digest[:16]} 与清单 {mani.object_sha256[:16]} 不符")
            self._write_output(output_path, data)
            return RecoveryResult(
                mani.object_sha256, mani.object_length, complete=True,
                output_path=str(output_path), output_sha256=digest,
                exported_length=len(data),
                verified_ranges=[[0, mani.object_length]],
                stripe_status=statuses,
                unattributed_quarantined=unattributed)

        # 部分恢复: 只导出从头开始的连续已验证前缀, 不标 complete
        prefix_len = self._prefix_length(mani, statuses)
        if prefix_len > 0:
            prefix = b"".join(
                st.recovered for st in statuses
                if st.status == "recovered" and self._stripe_end(mani, st.index) <= prefix_len
            )[:prefix_len]
            partial_path = Path(output_path).with_suffix(".partial.bin")
            self._write_output(partial_path, prefix)
            output: str | None = str(partial_path)
            exported = len(prefix)
            out_sha = shard_mod.sha256_hex(prefix)
        else:
            output, exported, out_sha = None, 0, None
        return RecoveryResult(
            mani.object_sha256, mani.object_length, complete=False,
            output_path=output, output_sha256=out_sha, exported_length=exported,
            verified_ranges=verified_ranges, stripe_status=statuses,
            reason="存在未恢复条带, 仅导出已验证前缀, 不标记 complete",
            unattributed_quarantined=unattributed)

    def _fail(self, mani: Manifest, statuses, verified_ranges, reason: str) -> RecoveryResult:
        return RecoveryResult(
            mani.object_sha256, mani.object_length, complete=False,
            output_path=None, output_sha=None, exported_length=0,
            verified_ranges=verified_ranges, stripe_status=statuses, reason=reason)

    def _stripe_end(self, mani: Manifest, s: int) -> int:
        return min((s + 1) * mani.k * mani.L, mani.object_length)

    def _verified_ranges(self, mani: Manifest, statuses: list[StripeStatus]):
        ranges = []
        for st in statuses:
            if st.status == "recovered":
                start = st.index * mani.k * mani.L
                ranges.append([start, self._stripe_end(mani, st.index)])
        return ranges

    def _prefix_length(self, mani: Manifest, statuses: list[StripeStatus]) -> int:
        length = 0
        for st in statuses:
            if st.status != "recovered":
                break
            length = self._stripe_end(mani, st.index)
        return length

    def _write_output(self, path: str | os.PathLike, data: bytes) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # ---------------------------------------------------------------- 范围读
    def read_range(self, recovery_dir: str | os.PathLike,
                   start: int, end: int) -> bytes:
        """按字节半开区间 [start, end) 读取; 跨越的条带必须全部已验证。"""
        recovery_dir = Path(recovery_dir)
        mani, err = self._load_manifest(recovery_dir)
        if mani is None:
            raise RepositoryError(err)
        if not (0 <= start <= end <= mani.object_length):
            raise RepositoryError(
                f"范围 [{start}, {end}) 越界, 对象长度 {mani.object_length}")
        if start == end:
            return b""
        statuses = {st.index: st for st in self._evaluate_stripes(recovery_dir, mani)}
        stripe_bytes = mani.k * mani.L
        first = start // stripe_bytes
        last = (end - 1) // stripe_bytes
        for s in range(first, last + 1):
            if statuses[s].status != "recovered":
                raise RepositoryError(
                    f"范围跨越的条带 {s} 未恢复 ({statuses[s].status}: "
                    f"{statuses[s].reason}), 拒绝读取")
        assembled = b"".join(
            statuses[s].recovered for s in range(first, last + 1))
        offset = start - first * stripe_bytes
        return assembled[offset : offset + (end - start)]

    # ---------------------------------------------------------------- 序列化
    @staticmethod
    def _status_to_dict(st: StripeStatus) -> dict:
        def issues(items):
            return [{"file": i.file, "reason": i.reason} for i in items]
        return {
            "index": st.index,
            "stripe_id": st.stripe_id,
            "status": st.status,
            "reason": st.reason,
            "valid_indices": st.valid_indices,
            "quarantined": issues(st.quarantined),
            "conflicts": issues(st.conflicts),
            "duplicates": issues(st.duplicates),
            "foreign": issues(st.foreign),
            "manifest_mismatch": issues(st.manifest_mismatch),
            "original_sha256": st.original_sha256,
        }

    def result_to_dict(self, result: RecoveryResult) -> dict:
        return {
            "object_sha256": result.object_sha256,
            "object_length": result.object_length,
            "complete": result.complete,
            "output_path": result.output_path,
            "output_sha256": result.output_sha256,
            "exported_length": result.exported_length,
            "verified_ranges": result.verified_ranges,
            "manifest_error": result.manifest_error,
            "reason": result.reason,
            "unattributed_quarantined": (
                [{"file": i.file, "reason": i.reason}
                 for i in (result.unattributed_quarantined or [])]),
            "stripes": [self._status_to_dict(st) for st in result.stripe_status],
        }
