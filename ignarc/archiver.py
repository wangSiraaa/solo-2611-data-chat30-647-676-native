"""可追溯目录归档器。

collect 真正遍历目录、读取被选文件正文，生成自包含包（ZIP）与 manifest：

* manifest 逐文件记录相对路径、字节长度、SHA256 与规则依据（来源文件、行号、
  原始规则行），并单列 .ignore 控制文件与全部排除/剪枝决定；
* 符号链接只保存链接文本与词法分析结果，不跟随；指向根外或成环也不会让
  采集读取根外正文；
* 审计日志随包保存，可证明被排除文件从未出现 ``body-read``；
* 规则修订后生成的是**全新选择 + 全新包**：collect 不接受外部传入的旧选择
  缓存，旧包作为独立产物保留，inspect 仍可离线查其来源。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .engine import RuleEngine
from .fs import AuditedFS
from .paths import InvalidPath, validate_relpath
from .traversal import Selection, select

MANIFEST_NAME = "MANIFEST.json"
AUDIT_NAME = "AUDIT.json"
PAYLOAD_PREFIX = "payload/"
RULES_PREFIX = "rules/"
SYMLINK_ATTR = "ignarc.link-target"
CHUNK = 1 << 16


@dataclass(slots=True)
class PackageResult:
    package_path: str
    manifest_path: str
    package_id: str
    rule_digest: str
    selection: Selection
    manifest: dict


def _hash_stream(fh) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = fh.read(CHUNK)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
    return h.hexdigest(), total


def _safe_zip_name(relpath: str) -> str:
    """保证包内路径既不绝对化也不逃逸（zip-slip 防御）。"""
    validate_relpath(relpath)
    return PAYLOAD_PREFIX + relpath


def collect(
    fs: AuditedFS,
    engine: RuleEngine,
    output_dir: str | os.PathLike[str],
    *,
    package_name: str | None = None,
    note: str = "",
) -> PackageResult:
    """执行一次完整采集并落盘自包含包。"""
    # 每次 collect 都重新选择，杜绝沿用旧选择缓存多带文件
    selection = select(fs, engine)
    rule_digest = engine.rule_digest

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    package_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
    package_name = package_name or f"collection-{package_id}.pack.zip"
    package_path = out_dir / package_name
    manifest_path = out_dir / (package_name.removesuffix(".zip") + ".manifest.json")

    # 规则清单以引擎已知的全部 .ignore 为准：位于剪枝子树内的规则文件不会被
    # 选择遍历枚举到，但其文本在引擎构建时已读入，需原样保留以保证可追溯。
    all_rule_files = list(engine.rule_files)

    entries: list[dict] = []
    links: list[dict] = []
    dirs: list[dict] = []

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1) 规则控制输入原样单列保存，使包完全自包含、可离线重放
        for rule_file in all_rule_files:
            zf.writestr(
                RULES_PREFIX + rule_file,
                engine.rule_text(rule_file).encode("utf-8"),
            )

        # 2) 入选目录（零字节目录项，仅为保留结构与目录级规则依据）
        for node in selection.included_dirs:
            dirs.append({
                "path": node.path,
                "basis": node.decision.to_basis(),
            })
            zi = zipfile.ZipInfo(_safe_zip_name(node.path) + "/")
            zi.external_attr = (0o755 << 16) | 0x10  # MS-DOS directory flag
            zf.writestr(zi, b"")

        # 3) 入选符号链接：只写链接文本，不 open、不跟随
        for node in selection.included_links:
            info = node.link_info or {}
            target = info["target"]
            links.append({
                "path": node.path,
                "target": target,
                "link_info": info,
                "basis": node.decision.to_basis(),
            })
            zi = zipfile.ZipInfo(_safe_zip_name(node.path))
            zi.create_system = 3  # Unix
            zi.external_attr = (0o777 << 16) | 0o120000  # S_IFLNK
            zi.comment = json.dumps({"link": True}, ensure_ascii=False).encode()
            zf.writestr(zi, target.encode("utf-8"))

        # 4) 入选业务文件：每个文件只 open 一次，边哈希边落临时文件，再入包
        for node in selection.included_files:
            with fs.open_body(node.path) as fh:
                hasher = hashlib.sha256()
                size = 0
                # 先写到包旁的临时文件，避免把整个正文驻留内存
                tmp = tempfile.SpooledTemporaryFile(max_size=1 << 20, mode="w+b")
                try:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                        tmp.write(chunk)
                    digest = hasher.hexdigest()

                    zi = zipfile.ZipInfo(_safe_zip_name(node.path))
                    zi.create_system = 3
                    zi.external_attr = (0o644 << 16)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    tmp.seek(0)
                    with zf.open(zi, "w") as sink:
                        while True:
                            chunk = tmp.read(CHUNK)
                            if not chunk:
                                break
                            sink.write(chunk)
                finally:
                    tmp.close()
            entries.append({
                "path": node.path,
                "size": size,
                "sha256": digest,
                "basis": node.decision.to_basis(),
            })

        manifest = {
            "manifest_version": 1,
            "package_id": package_id,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "collection_root": str(fs.root),
            "rule_digest": rule_digest,
            "note": note,
            "stats": selection.stats,
            "rule_files": [
                {
                    "path": p,
                    "stored_as": RULES_PREFIX + p,
                    "inside_pruned_subtree": any(
                        p == d + "/.ignore" or p.startswith(d + "/")
                        for d in selection.pruned_dirs
                    ),
                }
                for p in all_rule_files
            ],
            "files": entries,
            "directories": dirs,
            "symlinks": links,
            "excluded": [
                {
                    "path": n.path,
                    "kind": n.kind,
                    "basis": n.decision.to_basis(),
                }
                for n in selection.excluded
            ],
            "pruned_dirs": selection.pruned_dirs,
        }

        # 5) manifest 与审计日志同时内嵌并另存旁车文件
        zf.writestr(MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        zf.writestr(AUDIT_NAME,
                    json.dumps(fs.snapshot_audit(), ensure_ascii=False,
                               indent=2).encode("utf-8"))

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6) 即时自检：审计日志中绝不能出现被排除文件的正文读取
    excluded_paths = {n.path for n in selection.excluded}
    for record in fs.body_reads():
        if record.relpath in excluded_paths:
            raise AssertionError(
                f"excluded path {record.relpath!r} was opened during collect"
            )

    return PackageResult(
        package_path=str(package_path),
        manifest_path=str(manifest_path),
        package_id=package_id,
        rule_digest=rule_digest,
        selection=selection,
        manifest=manifest,
    )


# --------------------------------------------------------------------- inspect
def inspect_package(package_path: str | os.PathLike[str]) -> dict:
    """离线读取包内 manifest（不接触采集根，不校验当前磁盘状态）。"""
    with zipfile.ZipFile(package_path, "r") as zf:
        with zf.open(MANIFEST_NAME) as fh:
            return json.loads(fh.read().decode("utf-8"))


def _safe_extract_member_name(name: str) -> str:
    # ZIP 内全部成员都应位于 payload/、rules/ 下或是顶层元数据文件
    if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
        raise InvalidPath(f"unsafe member in package: {name!r}")
    return name


# ---------------------------------------------------------------------- verify
def verify_package(
    package_path: str | os.PathLike[str],
    fs: AuditedFS | None = None,
    engine: RuleEngine | None = None,
    *,
    extract_root: str | os.PathLike[str] | None = None,
) -> dict:
    """校验包。始终做内部哈希校验；提供 fs/engine 时追加实时来源对照。

    报告字段 ``ok`` 为总体结论；任何单项失败都会给出明细而不是静默通过。
    """
    report: dict = {"package": str(package_path), "checks": {}, "errors": []}
    with zipfile.ZipFile(package_path, "r") as zf:
        bad_members = [n for n in zf.namelist()
                       if n.startswith("/") or ".." in n.split("/")]
        report["checks"]["zip_safe_paths"] = not bad_members
        if bad_members:
            report["errors"].append(f"unsafe zip members: {bad_members}")

        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        audit = json.loads(zf.read(AUDIT_NAME).decode("utf-8"))
        names = set(zf.namelist())

        # 1) 逐文件重算 SHA256 / 长度，与 manifest 对照（不只数文件个数）
        hash_errors: list[str] = []
        for entry in manifest["files"]:
            member = PAYLOAD_PREFIX + entry["path"]
            if member not in names:
                hash_errors.append(f"missing member: {entry['path']}")
                continue
            with zf.open(member) as fh:
                digest, size = _hash_stream(fh)
            if digest != entry["sha256"] or size != entry["size"]:
                hash_errors.append(
                    f"hash/size mismatch: {entry['path']} "
                    f"got {digest}/{size} want {entry['sha256']}/{entry['size']}"
                )
        report["checks"]["manifest_hashes"] = not hash_errors
        report["checks"]["file_count"] = len(manifest["files"])
        report["errors"].extend(hash_errors)

        # 2) 链接文本一致，且包内没有跟随链接产生的正文副本
        link_errors: list[str] = []
        for link in manifest["symlinks"]:
            member = PAYLOAD_PREFIX + link["path"]
            stored = zf.read(member).decode("utf-8")
            if stored != link["target"]:
                link_errors.append(f"link text mismatch: {link['path']}")
        report["checks"]["symlink_text"] = not link_errors
        report["errors"].extend(link_errors)

        # 3) 审计证明：排除文件从未被打开
        excluded_paths = {e["path"] for e in manifest["excluded"]}
        leaked = sorted({
            rec["path"] for rec in audit
            if rec["op"] == "body-read" and rec["path"] in excluded_paths
        })
        report["checks"]["no_excluded_body_read"] = not leaked
        if leaked:
            report["errors"].append(f"excluded files were opened: {leaked}")

        # 4) 规则控制输入完整随包
        rule_missing = [
            rf["path"] for rf in manifest["rule_files"]
            if rf["stored_as"] not in names
        ]
        report["checks"]["rule_files_present"] = not rule_missing
        if rule_missing:
            report["errors"].append(f"missing rule files: {rule_missing}")

        extracted: dict[str, bytes] = {}
        if extract_root is not None:
            root = Path(extract_root)
            root.mkdir(parents=True, exist_ok=True)
            for name in zf.namelist():
                _safe_extract_member_name(name)
                if name.endswith("/"):
                    continue
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                # 符号链接按“链接文本文件 + 元数据”落地，绝不创建真实 symlink
                target.write_bytes(zf.read(name))
                if name.startswith(PAYLOAD_PREFIX):
                    extracted[name[len(PAYLOAD_PREFIX):]] = zf.read(name)

    # 5) 实时来源字节对照：解包后的每个业务文件与采集根现盘逐字节相等
    if fs is not None and engine is not None:
        live_errors: list[str] = []
        if engine.rule_digest != manifest["rule_digest"]:
            live_errors.append(
                f"rule digest drifted: package={manifest['rule_digest'][:12]} "
                f"live={engine.rule_digest[:12]}"
            )
        # 用当前规则重新选择，确认选择集合与包记录一致
        live = select(fs, engine)
        live_paths = {n.path for n in live.included_files}
        pkg_paths = {e["path"] for e in manifest["files"]}
        if live_paths != pkg_paths:
            live_errors.append(
                "selection set differs: "
                f"only-live={sorted(live_paths - pkg_paths)[:5]} "
                f"only-package={sorted(pkg_paths - live_paths)[:5]}"
            )
        for entry in manifest["files"]:
            actual = fs.read_body(entry["path"])
            if hashlib.sha256(actual).hexdigest() != entry["sha256"]:
                live_errors.append(f"live byte mismatch: {entry['path']}")
            if extract_root is not None:
                unpacked = extracted.get(entry["path"])
                if unpacked != actual:
                    live_errors.append(f"unpacked byte mismatch: {entry['path']}")
        # 链接来源对照（只读链接文本）
        for link in manifest["symlinks"]:
            if fs.read_link(link["path"]) != link["target"]:
                live_errors.append(f"live link text mismatch: {link['path']}")
        report["checks"]["live_source_match"] = not live_errors
        report["errors"].extend(live_errors)

    report["ok"] = not report["errors"]
    return report
