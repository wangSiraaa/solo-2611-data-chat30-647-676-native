"""独立规则解释服务。

设计要点（对应题面约束）：

* 进入一个目录时，先继承全部祖先目录的规则，再按从浅到深追加该目录的
  本地规则；同一目标由规则链中**最后命中**的规则决定；
* 初始默认包含（无任何规则命中即包含）；
* 父目录最终被排除时不进入其子树，子目录内的规则不可能隔空让文件复活
  （:meth:`RuleEngine.check` 先检查祖先目录的最终决定）；
* 重新包含一个文件要求其全部父目录可进入；
* :meth:`check` 只依赖已编译的规则树，不读取任何业务文件正文即可回答，
  并返回真正起作用的规则来源行。

性能：规则按作用域目录建索引，目录的规则链与每路径决定均有缓存；规则热更
（:meth:`reload`）会整体换版本并清空缓存，旧决定不可能污染新选择。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .paths import (
    DIR,
    VALID_KINDS,
    InvalidPath,
    ancestors,
    validate_relpath,
)
from .rules import (
    EXCLUDE,
    IGNORE_FILENAME,
    INCLUDE,
    Rule,
    parse_ignore_text,
)

if TYPE_CHECKING:
    from .fs import AuditedFS

INCLUDE_BOOL = True
EXCLUDE_BOOL = False


@dataclass(frozen=True, slots=True)
class Decision:
    """check 的结构化结果。"""

    path: str
    kind: str
    included: bool
    reason: str
    effective_rule: Rule | None
    blocking_rule: Rule | None
    rule_digest: str

    @property
    def source_line(self) -> str | None:
        """真正起作用的规则来源行，例如 ``src/.ignore:7``。"""
        rule = self.effective_rule or self.blocking_rule
        return rule.source if rule is not None else None

    @property
    def raw_line(self) -> str | None:
        rule = self.effective_rule or self.blocking_rule
        return rule.raw if rule is not None else None

    def to_basis(self) -> dict:
        """归档 manifest 中记录的“规则依据”。"""
        rule = self.effective_rule or self.blocking_rule
        if rule is None:
            return {
                "decision": "include" if self.included else "exclude",
                "reason": self.reason,
                "source": None,
                "line": None,
                "raw": None,
            }
        return {
            "decision": "include" if self.included else "exclude",
            "reason": self.reason,
            "source": rule.source_file,
            "line": rule.line_no,
            "raw": rule.raw,
            "action": rule.action,
        }


class RuleEngine:
    """从已加载的规则文件构建的不可见正文规则解释器。"""

    def __init__(
        self,
        rule_texts: dict[str, str],
        *,
        fs: "AuditedFS | None" = None,
    ):
        """``rule_texts``: 规则文件相对根路径 -> 文本内容。"""
        self._fs = fs
        self._rules_by_dir: dict[str, list[Rule]] = {}
        self._rule_files: dict[str, str] = dict(rule_texts)
        self._chain_cache: dict[str, tuple[Rule, ...]] = {}
        self._decision_cache: dict[tuple[str, str], Decision] = {}
        self._build()

    # ---------------------------------------------------------------- 构建
    @classmethod
    def from_fs(cls, fs: "AuditedFS") -> "RuleEngine":
        """扫描采集根内全部 .ignore 并构建引擎（只产生规则/元数据读取）。"""
        texts = fs.scan_ignore_files()
        return cls(texts, fs=fs)

    def _build(self) -> None:
        # 按目录深度排序，保证从浅到深的稳定构建顺序
        for source_file in sorted(self._rule_files, key=lambda p: (p.count("/"), p)):
            text = self._rule_files[source_file]
            if source_file == IGNORE_FILENAME:
                scope_dir = ""
            elif source_file.endswith("/" + IGNORE_FILENAME):
                scope_dir = source_file[: -(len(IGNORE_FILENAME) + 1)]
            else:  # 防御：非标准命名不进入规则树
                continue
            validate_relpath(source_file)
            rules = parse_ignore_text(text, scope_dir=scope_dir,
                                      source_file=source_file)
            self._rules_by_dir.setdefault(scope_dir, []).extend(rules)
        self._digest = self._compute_digest()

    def _compute_digest(self) -> str:
        h = hashlib.sha256()
        for source_file in sorted(self._rule_files):
            h.update(source_file.encode("utf-8"))
            h.update(b"\0")
            h.update(self._rule_files[source_file].encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    @property
    def rule_digest(self) -> str:
        return self._digest

    @property
    def rule_files(self) -> tuple[str, ...]:
        return tuple(sorted(self._rule_files))

    def rule_text(self, source_file: str) -> str:
        """返回规则文件原文（引擎构建时已读入，不产生新的文件 I/O）。"""
        return self._rule_files[source_file]

    def all_rules(self) -> tuple[Rule, ...]:
        out: list[Rule] = []
        for scope in sorted(self._rules_by_dir, key=lambda p: (p.count("/"), p)):
            out.extend(self._rules_by_dir[scope])
        return tuple(out)

    # ------------------------------------------------------------- 规则链
    def chain_for_directory(self, directory: str) -> tuple[Rule, ...]:
        """目录可见的完整规则链：祖先规则（浅→深）+ 本目录规则。"""
        cached = self._chain_cache.get(directory)
        if cached is not None:
            return cached
        chain: list[Rule] = []
        if self._rules_by_dir.get(""):
            chain.extend(self._rules_by_dir[""])
        for anc in ancestors(directory + "/x") if directory else []:
            chain.extend(self._rules_by_dir.get(anc, ()))
        if directory:
            chain.extend(self._rules_by_dir.get(directory, ()))
        result = tuple(chain)
        self._chain_cache[directory] = result
        return result

    # ------------------------------------------------------------- 核心判定
    def _last_matching(self, path: str, kind: str,
                       chain: tuple[Rule, ...]) -> Rule | None:
        hit: Rule | None = None
        for rule in chain:  # 链本身已是浅→深、同目录按行序
            if rule.matches(path, kind):
                hit = rule
        return hit

    def evaluate(self, path: str, kind: str) -> Decision:
        """只评估路径自身的最终规则命中（不检查祖先）。"""
        key = (path, kind)
        cached = self._decision_cache.get(key)
        if cached is not None:
            return cached

        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        chain = self.chain_for_directory(directory)
        rule = self._last_matching(path, kind, chain)
        if rule is None:
            decision = Decision(
                path=path, kind=kind, included=INCLUDE_BOOL,
                reason="default-include (no rule matched)",
                effective_rule=None, blocking_rule=None,
                rule_digest=self._digest,
            )
        else:
            decision = Decision(
                path=path, kind=kind,
                included=rule.action == INCLUDE,
                reason=("matched include rule"
                        if rule.action == INCLUDE
                        else "matched exclude rule"),
                effective_rule=rule, blocking_rule=None,
                rule_digest=self._digest,
            )
        self._decision_cache[key] = decision
        return decision

    def check(self, path: str, kind: str) -> Decision:
        """对外主入口：返回包含决定与真正起作用的规则来源。

        先从浅到深确认每个祖先目录的最终决定；任一祖先被排除则直接剪枝，
        子树内的规则没有机会覆盖该结论。
        """
        validate_relpath(path)
        if kind not in VALID_KINDS:
            raise InvalidPath(f"unknown kind {kind!r}")

        for anc in ancestors(path):
            anc_decision = self.evaluate(anc, DIR)
            if not anc_decision.included:
                return Decision(
                    path=path, kind=kind, included=EXCLUDE_BOOL,
                    reason="ancestor directory excluded; subtree not entered",
                    effective_rule=None,
                    blocking_rule=anc_decision.effective_rule,
                    rule_digest=self._digest,
                )
        return self.evaluate(path, kind)

    # ------------------------------------------------------------- 热更新
    def reload(self) -> str:
        """重新扫描规则文件并整体换版本；缓存全部失效。

        旧 :class:`Decision` 上保留的 ``rule_digest`` 可用于识别过期选择。
        """
        if self._fs is None:
            raise RuntimeError("engine built without filesystem; reload unavailable")
        texts = self._fs.scan_ignore_files()
        self._rule_files = texts
        self._rules_by_dir = {}
        self._chain_cache.clear()
        self._decision_cache.clear()
        self._build()
        return self._digest
