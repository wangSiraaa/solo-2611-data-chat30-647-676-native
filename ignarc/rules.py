"""``.ignore`` 规则解析。

规则文件按行处理，规则按文件中的行序生效：

* 空行（只含空白也视为空行）与 ``#`` 开头的行是注释/空白，不产生规则；
* 行首 ``!`` 表示重新包含，其余为排除；
* 行尾 ``/`` 表示该规则仅匹配目录；
* 规则体含 ``/`` 时锚定到规则所在目录的根，前导 ``/`` 仅用于显式表达锚定；
* 不含 ``/`` 的规则按单个路径分量匹配，作用于所在目录及其全部后代。

不做任何反斜杠转义扩展，``#`` 只在行首有注释含义。
"""

from __future__ import annotations

from dataclasses import dataclass

from .patterns import Matcher, compile_pattern

INCLUDE = "include"
EXCLUDE = "exclude"
ACTIONS = frozenset({INCLUDE, EXCLUDE})

IGNORE_FILENAME = ".ignore"


@dataclass(frozen=True, slots=True)
class Rule:
    """一条编译后的规则。"""

    scope_dir: str          # 规则所在目录的相对路径，根目录为 ""
    source_file: str        # 规则文件相对采集根的路径
    line_no: int            # 1 起始的物理行号
    raw: str                # 去掉行终止符后的原始行
    action: str             # INCLUDE / EXCLUDE
    pattern: str            # 去掉 !、前导/、尾 / 后的规则体
    anchored: bool          # 是否锚定到 scope_dir 的完整相对路径
    dir_only: bool          # 尾 /：仅匹配目录
    matcher: Matcher

    @property
    def source(self) -> str:
        """可追溯的来源坐标，例如 ``src/.ignore:7``。"""
        return f"{self.source_file}:{self.line_no}"

    def is_effective_for(self, kind: str) -> bool:
        """目录专属规则不命中文件与符号链接。"""
        if self.dir_only and kind != "dir":
            return False
        return True

    def matches(self, relpath: str, kind: str) -> bool:
        """判断规则是否命中给定路径。

        ``relpath`` 是相对采集根的完整路径；规则自身的作用域裁剪在引擎层完成，
        这里只负责模式与 kind 的匹配。
        """
        if not self.is_effective_for(kind):
            return False
        if self.anchored:
            # 锚定模式只在自身作用域的严格子树内生效（规则不约束所在目录本身），
            # 匹配相对 scope 的完整路径
            prefix = self.scope_dir + "/" if self.scope_dir else ""
            if not relpath.startswith(prefix):
                return False
            text = relpath[len(prefix):]
            return self.matcher.matches(text)
        # 无斜杠规则：作用域是严格子树（含后代目录中的任意路径分量）
        if self.scope_dir and not relpath.startswith(self.scope_dir + "/"):
            return False
        # 匹配该路径的任意一个分量
        return self.matcher.matches(relpath.rsplit("/", 1)[-1])


def parse_ignore_text(
    text: str,
    *,
    scope_dir: str,
    source_file: str,
) -> list[Rule]:
    """解析一份 .ignore 文本，返回按行序排列的 Rule 列表。"""
    rules: list[Rule] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        # 只去行终止符；splitlines 已处理 \n 与 \r\n。空白行直接跳过。
        if raw.strip() == "" or raw.startswith("#"):
            continue

        body = raw
        action = EXCLUDE
        if body.startswith("!"):
            action = INCLUDE
            body = body[1:]

        dir_only = body.endswith("/")
        if dir_only:
            body = body[:-1]

        if body.startswith("/"):
            body = body[1:]
            anchored = True
        else:
            anchored = "/" in body

        if body == "":
            # 形如 "!/"、"/" 的行没有可匹配的规则体，跳过
            continue

        matcher = compile_pattern(body, dir_only)
        rules.append(
            Rule(
                scope_dir=scope_dir,
                source_file=source_file,
                line_no=line_no,
                raw=raw,
                action=action,
                pattern=body,
                anchored=anchored,
                dir_only=dir_only,
                matcher=matcher,
            )
        )
    return rules
