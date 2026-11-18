"""独立朴素规则解释器——刻意不 import 任何 ignarc 生产模块。

用途：用一棵小树、最直白的递归实现独立核算选择集合，与生产引擎
:class:`ignarc.engine.RuleEngine` 的结果逐路径对照。两者若同时错成一样的
概率远低于单一实现出错的概率。

本模块自带：
* 规则行解析；
* 不依赖 ``re`` 的 ``?`` / ``*`` 单分量匹配；
* 显式递归的 ``**`` 多分量匹配；
* 祖先剪枝与“最后命中”规则链求值。
"""

from __future__ import annotations

FILE, DIR, LINK = "file", "dir", "link"


# ---------------------------------------------------------------- 模式匹配
def segment_match(pattern: str, text: str) -> bool:
    """单分量匹配：* 吃任意个非斜杠字符，? 吃一个，其余码点精确相等。"""

    def rec(pi: int, ti: int) -> bool:
        while pi < len(pattern):
            ch = pattern[pi]
            if ch == "*":
                while pi < len(pattern) and pattern[pi] == "*":
                    pi += 1
                if pi == len(pattern):
                    return "/" not in text[ti:]
                for k in range(ti, len(text) + 1):
                    if "/" in text[ti:k]:
                        break
                    if rec(pi, k):
                        return True
                return False
            if ti >= len(text):
                return False
            if ch == "?":
                if text[ti] == "/":
                    return False
                pi += 1
                ti += 1
            else:
                if ch != text[ti]:
                    return False
                pi += 1
                ti += 1
        return ti == len(text)

    return rec(0, 0)


def glob_match(pattern: str, text: str) -> bool:
    """完整相对路径匹配，显式处理作为完整分量的 **。"""
    psegs = pattern.split("/")
    tsegs = text.split("/")

    def rec(pi: int, ti: int) -> bool:
        if pi == len(psegs):
            return ti == len(tsegs)
        pseg = psegs[pi]
        if pseg == "**":
            # ** 吃掉零个或多个完整目录分量
            for take in range(ti, len(tsegs) + 1):
                if rec(pi + 1, take):
                    return True
            return False
        if ti >= len(tsegs):
            return False
        if segment_match(pseg, tsegs[ti]) and rec(pi + 1, ti + 1):
            return True
        return False

    return rec(0, 0)


# ---------------------------------------------------------------- 规则解析
class NaiveRule:
    def __init__(self, scope_dir, source_file, line_no, raw, action,
                 pattern, anchored, dir_only):
        self.scope_dir = scope_dir
        self.source_file = source_file
        self.line_no = line_no
        self.raw = raw
        self.action = action
        self.pattern = pattern
        self.anchored = anchored
        self.dir_only = dir_only

    def source(self) -> str:
        return f"{self.source_file}:{self.line_no}"

    def hits(self, relpath: str, kind: str) -> bool:
        if self.dir_only and kind != DIR:
            return False
        if self.anchored:
            prefix = self.scope_dir + "/" if self.scope_dir else ""
            if not relpath.startswith(prefix):
                return False
            return glob_match(self.pattern, relpath[len(prefix):])
        if self.scope_dir and not relpath.startswith(self.scope_dir + "/"):
            return False
        return glob_match(self.pattern, relpath.rsplit("/", 1)[-1])


def parse_rules(rule_texts: dict[str, str]) -> list[NaiveRule]:
    rules: list[NaiveRule] = []
    for source_file in sorted(rule_texts, key=lambda p: (p.count("/"), p)):
        if source_file == ".ignore":
            scope = ""
        elif source_file.endswith("/.ignore"):
            scope = source_file[: -len("/.ignore")]
        else:
            continue
        for line_no, raw in enumerate(rule_texts[source_file].splitlines(), start=1):
            if raw.strip() == "" or raw.startswith("#"):
                continue
            body = raw
            action = "exclude"
            if body.startswith("!"):
                action = "include"
                body = body[1:]
            dir_only = body.endswith("/")
            if dir_only:
                body = body[:-1]
            anchored = body.startswith("/") or ("/" in body and not body.startswith("/"))
            if body.startswith("/"):
                body = body[1:]
            if body == "":
                continue
            rules.append(NaiveRule(scope, source_file, line_no, raw,
                                   action, body, anchored, dir_only))
    return rules


# ---------------------------------------------------------------- 解释器
class NaiveInterpreter:
    def __init__(self, rule_texts: dict[str, str]):
        self.rules = parse_rules(rule_texts)
        self._chain_cache: dict[str, list[NaiveRule]] = {}

    def _chain(self, directory: str) -> list[NaiveRule]:
        """祖先规则（浅→深）+ 本目录规则，朴素地全表扫描（带记忆化）。"""
        if directory in self._chain_cache:
            return self._chain_cache[directory]
        scopes = [""]
        parts = directory.split("/") if directory else []
        for d in range(1, len(parts) + 1):
            scopes.append("/".join(parts[:d]))
        chain = [r for r in self.rules if r.scope_dir in scopes]
        # 稳定排序：作用域深度优先，同作用域按行号（解析序即行序）
        chain.sort(key=lambda r: (r.scope_dir.count("/") if r.scope_dir else 0,
                                  r.line_no, r.source_file))
        self._chain_cache[directory] = chain
        return chain

    def evaluate(self, path: str, kind: str):
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        hit = None
        for rule in self._chain(directory):
            if rule.hits(path, kind):
                hit = rule
        if hit is None:
            return True, None, "default-include"
        return hit.action == "include", hit, "matched"

    def check(self, path: str, kind: str):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            anc = "/".join(parts[:depth])
            included, rule, _ = self.evaluate(anc, DIR)
            if not included:
                return False, rule, "ancestor-excluded"
        return self.evaluate(path, kind)
