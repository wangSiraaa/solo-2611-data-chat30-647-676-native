"""plan：只读目录元数据与规则文件的规划阶段。

本阶段不允许打开任何业务文件正文。:class:`~ignarc.fs.AuditedFS` 的审计日志
可在 plan 结束后立即核验：日志里出现 ``body-read`` 即属实现缺陷。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .fs import AuditedFS
from .traversal import Selection, is_control_file, select
from .engine import RuleEngine


@dataclass(slots=True)
class Plan:
    rule_digest: str
    selection: Selection
    rule_files: tuple[str, ...]
    audit_snapshot: list[dict] = field(default_factory=list)

    @property
    def stats(self) -> dict:
        return self.selection.stats

    def to_dict(self) -> dict:
        sel = self.selection

        def node_brief(node) -> dict:
            d = node.decision
            return {
                "path": node.path,
                "kind": node.kind,
                "included": d.included,
                "basis": d.to_basis(),
                "control_file": node.is_control_file,
                **({"link": node.link_info} if node.link_info else {}),
            }

        return {
            "rule_digest": self.rule_digest,
            "rule_files": list(self.rule_files),
            "stats": self.stats,
            "included_files": [n.path for n in sel.included_files],
            "included_dirs": [n.path for n in sel.included_dirs],
            "included_links": [node_brief(n) for n in sel.included_links],
            "excluded": [node_brief(n) for n in sel.excluded],
            "pruned_dirs": sel.pruned_dirs,
            "control_files": sel.control_files,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def assert_metadata_only(self) -> None:
        """断言规划阶段没有任何正文读取（测试与 CLI 自检共用）。"""
        bad = [e for e in self.audit_snapshot if e["op"] == "body-read"]
        if bad:
            raise AssertionError(
                f"plan illegally opened business file bodies: {[e['path'] for e in bad]}"
            )


def build_plan(fs: AuditedFS, engine: RuleEngine) -> Plan:
    # 传入的 fs 可能已承担规则预扫描；plan 的审计证据只统计自身这次剪枝遍历，
    # 因此用同一采集根的全新审计实例执行选择，杜绝把预扫描混进“规划未读正文”
    # 的证据里（预扫描本身同样不读正文，但证据必须干净、可解释）。
    plan_fs = AuditedFS(fs.root)
    selection = select(plan_fs, engine)
    plan = Plan(
        rule_digest=engine.rule_digest,
        selection=selection,
        rule_files=engine.rule_files,
        audit_snapshot=plan_fs.snapshot_audit(),
    )
    plan.assert_metadata_only()
    return plan
