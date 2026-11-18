"""路径校验与基础类型。

对外路径一律使用 POSIX 风格 ``/`` 分隔的相对路径，按 Unicode 码点精确比较：
不做大小写折叠、不做 Unicode 规范化。禁止绝对路径、NUL 字符以及 ``..`` /
空分量（包括尾斜杠）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 三种路径类型。符号链接是独立类型：即便是“仅目录”规则也不匹配链接，
# 因为我们从不跟随链接，无法也不应假定其目标类型。
FILE = "file"
DIR = "dir"
LINK = "link"
VALID_KINDS = frozenset({FILE, DIR, LINK})


class InvalidPath(ValueError):
    """路径不满足安全约束（绝对路径、NUL、空分量或 ``..``）。"""


def validate_relpath(path: str) -> str:
    """校验并返回规范化显示用的相对路径。

    只做拒绝，不做静默改写：``.``、``..``、空分量、绝对路径、NUL 一律拒绝。
    反斜杠是普通文件名字符，不是分隔符。
    """
    if not isinstance(path, str):
        raise InvalidPath(f"path must be str, got {type(path).__name__}")
    if "\x00" in path:
        raise InvalidPath("NUL character is forbidden in paths")
    if path.startswith("/"):
        raise InvalidPath(f"absolute paths are forbidden: {path!r}")
    if path == "":
        raise InvalidPath("empty path")
    parts = path.split("/")
    for part in parts:
        if part == "":
            raise InvalidPath(f"empty path component in {path!r} (trailing slash?)")
        if part == "..":
            raise InvalidPath(f"'..' components are forbidden: {path!r}")
        if part == ".":
            raise InvalidPath(f"'.' components are forbidden: {path!r}")
    return path


def split(path: str) -> list[str]:
    """拆分为路径分量（调用前应已通过 :func:`validate_relpath`）。"""
    return path.split("/")


def parent_of(path: str) -> str | None:
    """返回父目录相对路径；根级路径返回 None。"""
    idx = path.rfind("/")
    if idx < 0:
        return None
    return path[:idx]


def ancestors(path: str) -> list[str]:
    """由浅到深返回全部祖先目录的相对路径（不含自身）。"""
    parts = split(path)
    out: list[str] = []
    for depth in range(1, len(parts)):
        out.append("/".join(parts[:depth]))
    return out


@dataclass(frozen=True, slots=True)
class CheckInput:
    """一次 check 调用的结构化输入。"""

    path: str
    kind: str
