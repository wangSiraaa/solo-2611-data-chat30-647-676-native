"""带审计与根目录约束的文件系统访问层。

采集器的一切真实 I/O 都经过 :class:`AuditedFS`：

* 每次打开正文、读取规则、读取元数据都写入审计日志，可据此证明被排除的
  文件从未被打开；
* 所有业务路径都必须是相对采集根的安全相对路径（见 :mod:`ignarc.paths`），
  打开前再做一次解析后根目录包含校验，符号链接指向根外也无法借此读到正文；
* 符号链接只通过 ``readlink`` 取链接文本，绝不 ``open``/跟随。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .paths import DIR, FILE, LINK, InvalidPath, validate_relpath

# 审计操作类型
OP_BODY_READ = "body-read"        # 读取业务文件正文
OP_RULE_READ = "rule-read"        # 读取 .ignore 控制文件
OP_METADATA = "metadata"          # 目录项/类型/链接目标等元数据
OP_RULE_SCAN = "rule-scan"        # 遍历目录树寻找 .ignore（仅元数据）


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    op: str
    relpath: str
    detail: str = ""
    bytes_read: int = 0


@dataclass(frozen=True, slots=True)
class LinkTargetInfo:
    """符号链接目标的纯词法分析结果（不触碰目标正文）。"""

    raw_target: str
    is_absolute: bool
    escapes_root: bool
    internal_path: str | None     # 不逃逸时的根内相对路径
    is_cycle: bool = False
    chain: tuple[str, ...] = ()


class RootViolation(InvalidPath):
    """解析后的真实路径逃出了采集根。"""


class AuditedFS:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(f"collection root is not a directory: {self.root}")
        self._audit: list[AuditEntry] = []
        self._seq = 0

    # ------------------------------------------------------------------ 审计
    def _log(self, op: str, relpath: str, detail: str = "", bytes_read: int = 0) -> None:
        self._seq += 1
        self._audit.append(
            AuditEntry(seq=self._seq, op=op, relpath=relpath, detail=detail,
                       bytes_read=bytes_read)
        )

    @property
    def audit_log(self) -> tuple[AuditEntry, ...]:
        return tuple(self._audit)

    def body_reads(self) -> tuple[AuditEntry, ...]:
        return tuple(e for e in self._audit if e.op == OP_BODY_READ)

    def was_body_opened(self, relpath: str) -> bool:
        return any(
            e.op == OP_BODY_READ and e.relpath == relpath for e in self._audit
        )

    def snapshot_audit(self) -> list[dict]:
        return [
            {"seq": e.seq, "op": e.op, "path": e.relpath,
             "detail": e.detail, "bytes_read": e.bytes_read}
            for e in self._audit
        ]

    # ------------------------------------------------------------- 路径安全
    def _physical(self, relpath: str) -> Path:
        validate_relpath(relpath)
        physical = self.root / relpath
        # 解析全部中间分量（含中间符号链接），确保真实位置仍在根内
        resolved = physical.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise RootViolation(
                f"{relpath!r} resolves outside collection root"
            ) from exc
        return physical

    # --------------------------------------------------------------- 元数据
    def kind_of(self, relpath: str) -> str:
        validate_relpath(relpath)
        st = os.lstat(self.root / relpath)
        self._log(OP_METADATA, relpath, "lstat")
        if os.path.islink(self.root / relpath):
            return LINK
        if os.path.isdir(self.root / relpath):
            return DIR
        return FILE

    def read_link(self, relpath: str) -> str:
        """只取链接文本，不跟随。"""
        validate_relpath(relpath)
        target = os.readlink(self.root / relpath)
        self._log(OP_METADATA, relpath, f"readlink->{target}")
        return target

    def analyze_link(self, relpath: str) -> LinkTargetInfo:
        """词法分析链接目标，必要时沿链接链继续 readlink（仍是元数据操作）。"""
        raw = self.read_link(relpath)
        is_abs = os.path.isabs(raw)
        internal: str | None = None
        escapes = is_abs

        if not is_abs:
            base = os.path.dirname(relpath)
            candidate = os.path.normpath(os.path.join(base, raw)) if base else \
                os.path.normpath(raw)
            if candidate.startswith("..") or os.path.isabs(candidate):
                escapes = True
            else:
                internal = candidate

        chain: list[str] = [relpath]
        is_cycle = False
        if internal is not None:
            seen = {relpath}
            cursor = internal
            for _ in range(64):  # 有界追踪，防止环/超长链
                physical_cursor = self.root / cursor
                if not os.path.islink(physical_cursor):
                    break
                chain.append(cursor)
                if cursor in seen:
                    is_cycle = True
                    break
                seen.add(cursor)
                nxt = os.readlink(physical_cursor)
                self._log(OP_METADATA, cursor, f"readlink->{nxt}")
                if os.path.isabs(nxt):
                    escapes = True
                    internal = None
                    break
                cursor = os.path.normpath(os.path.join(os.path.dirname(cursor), nxt))
                if cursor.startswith(".."):
                    escapes = True
                    internal = None
                    break
            else:
                is_cycle = True  # 超过有界步数视为环

        return LinkTargetInfo(
            raw_target=raw,
            is_absolute=is_abs,
            escapes_root=escapes,
            internal_path=internal,
            is_cycle=is_cycle,
            chain=tuple(chain),
        )

    # ----------------------------------------------------------------- 读取
    def open_body(self, relpath: str):
        """打开业务文件正文（二进制流）。符号链接路径一律拒绝。"""
        physical = self._physical(relpath)
        if physical.is_symlink():
            raise RootViolation(f"refusing to follow symlink for body read: {relpath}")
        self._log(OP_BODY_READ, relpath, "open")
        return physical.open("rb")

    def read_body(self, relpath: str) -> bytes:
        with self.open_body(relpath) as fh:
            data = fh.read()
        # 补记真实字节数（open 条目保留，便于核对“从未 open”）
        self._log(OP_BODY_READ, relpath, "read", len(data))
        return data

    def read_rule_text(self, relpath: str) -> str:
        physical = self._physical(relpath)
        data = physical.read_bytes()
        self._log(OP_RULE_READ, relpath, "read", len(data))
        return data.decode("utf-8", errors="strict")

    # --------------------------------------------------------------- 遍历
    def iter_directory(self, directory: str = "", *, scan_tag: str = "scandir"):
        """枚举单层目录，返回排序后的 ``(name, kind)``，不跟随符号链接。

        由调用方决定是否继续深入子目录，被规则排除的目录不会被再次枚举。
        ``scan_tag`` 用于在审计日志中区分“规则预扫描”与“采集剪枝遍历”。
        """
        validate_relpath(directory) if directory else None
        physical_dir = self.root if directory == "" else self.root / directory
        with os.scandir(physical_dir) as it:
            entries = list(it)
        self._log(OP_METADATA, directory or ".", scan_tag)
        out = []
        for entry in entries:
            if entry.is_symlink():
                kind = LINK
            elif entry.is_dir(follow_symlinks=False):
                kind = DIR
            else:
                kind = FILE
            out.append((entry.name, kind))
        out.sort(key=lambda x: x[0])
        return out

    def walk(self, *, scan_tag: str = "scandir"):
        """自顶向下产出 ``(relpath, kind)``，不跟随符号链接（无规则剪枝）。"""
        stack: list[str] = [""]
        while stack:
            directory = stack.pop()
            for name, kind in self.iter_directory(directory, scan_tag=scan_tag):
                rel = name if directory == "" else f"{directory}/{name}"
                yield rel, kind
                if kind == DIR:
                    stack.append(rel)

    def scan_ignore_files(self) -> dict[str, str]:
        """全量扫描 .ignore 控制文件（只读元数据 + 规则文本）。"""
        found: dict[str, str] = {}
        for rel, kind in self.walk(scan_tag="rule-prescan"):
            if kind != FILE:
                continue  # 符号链接形式的 .ignore 不跟随
            if rel == ".ignore" or rel.endswith("/.ignore"):
                found[rel] = self.read_rule_text(rel)
        self._log(OP_RULE_SCAN, ".", f"{len(found)} ignore files")
        return found
