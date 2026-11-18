"""glob 模式编译与匹配。

支持的元字符只有三个，其余一切字符（包括 ``(``、``[``、``\\``、``.``、``+``
等正则元字符）都是普通字符，不存在反斜杠转义语法：

* ``?``  匹配恰好一个非斜杠字符；
* ``*``  匹配零个或多个非斜杠字符；
* ``**`` 仅当作为完整路径分量时有效，匹配零个或多个目录分量。
  出现在分量内部的 ``**`` 退化为两个普通 ``*``。

所有匹配按 Unicode 码点精确比较，不做大小写折叠或规范化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def translate_segment(segment: str) -> str:
    """把单个路径分量（不含 ``/``）翻译为正则片段，不加锚点。"""
    out: list[str] = []
    for ch in segment:
        if ch == "?":
            out.append("[^/]")
        elif ch == "*":
            out.append("[^/]*")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def translate_pattern(pattern: str) -> str:
    """把完整模式翻译为带 ``^...$`` 锚点的正则字符串。

    ``**`` 的组装约定（与 gitignore 习惯对齐并在文档中明示）：

    * 裸 ``**``        → ``.*``，匹配任意相对路径；
    * 前导 ``**/``     → 零个或多个前导目录分量；
    * 中部 ``/**/``    → 零个或多个中间目录分量（两侧斜杠只出现一次）；
    * 结尾 ``/**``     → 该目录之下任意深度的内容，不匹配目录自身。
    """
    segments = pattern.split("/")
    out = ["^"]
    skip_next_slash = False
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        is_star = seg == "**"
        if is_star:
            if i == last:
                if i == 0:
                    out.append(".*")  # 裸 **
                elif skip_next_slash:
                    out.append(".*")  # 形如 **/** 的退化情形
                else:
                    out.append("/.*")  # a/**：a 之下的内容
                skip_next_slash = False
            else:
                if i == 0:
                    out.append("(?:.*/)?")  # **/x
                else:
                    out.append("/(?:.*/)?")  # a/**/x
                skip_next_slash = True
        else:
            if i > 0 and not skip_next_slash:
                out.append("/")
            out.append(translate_segment(seg))
            skip_next_slash = False
    out.append("$")
    return "".join(out)


@dataclass(frozen=True, slots=True)
class Matcher:
    """编译后的模式。"""

    pattern: str
    regex: "re.Pattern[str]"
    dir_only: bool

    def matches(self, text: str) -> bool:
        return self.regex.match(text) is not None


def compile_pattern(pattern: str, dir_only: bool) -> Matcher:
    return Matcher(
        pattern=pattern,
        regex=re.compile(translate_pattern(pattern)),
        dir_only=dir_only,
    )


def segment_match(pattern: str, text: str) -> bool:
    """纯 Python 的单分量匹配（不依赖 re 模块），供朴素解释器交叉核对。

    分量内没有 ``**`` 概念，只需处理 ``*`` 与 ``?``，字符逐一精确比较。
    """

    def rec(pi: int, ti: int) -> bool:
        while pi < len(pattern):
            p = pattern[pi]
            if p == "*":
                # 连续 * 等价于一个 *；* 只吃非斜杠字符
                while pi < len(pattern) and pattern[pi] == "*":
                    pi += 1
                if pi == len(pattern):
                    return "/" not in text[ti:]
                for k in range(ti, len(text) + 1):
                    if "/" in text[ti:k]:
                        break  # 再往后只会包含斜杠，无需继续
                    if rec(pi, k):
                        return True
                return False
            if ti >= len(text):
                return False
            if p == "?":
                if text[ti] == "/":  # 题面：? 不匹配斜杠（分量内本不会有）
                    return False
                pi += 1
                ti += 1
            else:
                if p != text[ti]:  # 码点精确比较，其余字符均为普通字符
                    return False
                pi += 1
                ti += 1
        return ti == len(text)

    return rec(0, 0)
