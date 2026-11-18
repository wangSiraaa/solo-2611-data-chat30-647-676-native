"""目录遍历与规则选择。

plan 与 collect 共用本模块，确保“规划看到的选择集合”与“实际打包的选择
集合”是同一条代码路径产生的。遍历只做元数据读取；正文读取仅发生在
collect 阶段。被最终排除的目录不会再被 ``scandir``——子树根本不进入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import Decision, RuleEngine
from .fs import AuditedFS
from .paths import DIR, FILE, LINK
from .rules import IGNORE_FILENAME


@dataclass(slots=True)
class SelectedNode:
    path: str
    kind: str
    decision: Decision
    is_control_file: bool          # .ignore 控制输入
    link_info: dict | None = None  # 仅符号链接


@dataclass(slots=True)
class Selection:
    nodes: list[SelectedNode] = field(default_factory=list)
    included_files: list[SelectedNode] = field(default_factory=list)
    included_dirs: list[SelectedNode] = field(default_factory=list)
    included_links: list[SelectedNode] = field(default_factory=list)
    excluded: list[SelectedNode] = field(default_factory=list)
    control_files: list[str] = field(default_factory=list)
    pruned_dirs: list[str] = field(default_factory=list)
    rule_digest: str = ""

    @property
    def stats(self) -> dict:
        return {
            "total_seen": len(self.nodes),
            "included_files": len(self.included_files),
            "included_dirs": len(self.included_dirs),
            "included_links": len(self.included_links),
            "excluded_nodes": len(self.excluded),
            "pruned_subtrees": len(self.pruned_dirs),
            "control_files": len(self.control_files),
        }


def is_control_file(relpath: str) -> bool:
    return relpath.rsplit("/", 1)[-1] == IGNORE_FILENAME


def select(fs: AuditedFS, engine: RuleEngine) -> Selection:
    """遍历采集根并应用规则，产出完整选择记录（含排除依据与剪枝记录）。"""
    sel = Selection(rule_digest=engine.rule_digest)
    # 待进入的目录栈；根始终可进入。逆序压栈以维持字典序 DFS。
    pending: list[str] = [""]

    while pending:
        directory = pending.pop()
        children = fs.iter_directory(directory)
        for name, kind in reversed(children):
            rel = name if directory == "" else f"{directory}/{name}"
            decision = engine.check(rel, kind)
            node = SelectedNode(
                path=rel,
                kind=kind,
                decision=decision,
                is_control_file=is_control_file(rel),
            )

            if kind == LINK and decision.included:
                info = fs.analyze_link(rel)
                node.link_info = {
                    "target": info.raw_target,
                    "is_absolute": info.is_absolute,
                    "escapes_root": info.escapes_root,
                    "internal_path": info.internal_path,
                    "is_cycle": info.is_cycle,
                    "chain": list(info.chain),
                }

            sel.nodes.append(node)
            if node.is_control_file:
                sel.control_files.append(rel)

            if not decision.included:
                sel.excluded.append(node)
                if kind == DIR:
                    # 父目录最终排除：不入栈 → 子树不会被 scandir，
                    # 其内的任何规则都没有机会隔空复活文件
                    sel.pruned_dirs.append(rel)
                continue

            if kind == DIR:
                sel.included_dirs.append(node)
                pending.append(rel)
            elif kind == LINK:
                sel.included_links.append(node)
            elif not node.is_control_file:
                sel.included_files.append(node)

    # 回到字典序，便于 manifest 稳定输出
    order = {n.path: i for i, n in enumerate(sorted(sel.nodes, key=lambda n: n.path))}
    for bucket in (sel.included_files, sel.included_dirs,
                   sel.included_links, sel.excluded):
        bucket.sort(key=lambda n: order[n.path])
    sel.control_files.sort()
    sel.pruned_dirs.sort()
    return sel
