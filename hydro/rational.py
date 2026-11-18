"""精确有理数工具。

模型规定全部体积、面积、核系数、比例均为有理数。统一使用
:class:`fractions.Fraction` 参与运算，SQLite/JSON 中以规范化文本
``"p/q"``（整数则为 ``"n"``）冻结，任何环节不使用浮点近似。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Union

# 有理数的可接受输入类型：Fraction / int / 规范化文本
RationalLike = Union[Fraction, int, str]


def frac(value: RationalLike) -> Fraction:
    """把输入转换为非退化的精确 Fraction。

    接受 :class:`Fraction`、整数以及形如 ``"3/4"``、``"2"``、``"-1/5"``
    的十进制整分数字符串；不接受小数浮点文本，避免隐式精度损失。
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("空字符串不是合法有理数")
        if "/" in s:
            num_s, den_s = s.split("/", 1)
            den = int(den_s.strip())
            if den == 0:
                raise ZeroDivisionError(f"有理数分母为零: {value!r}")
            return Fraction(int(num_s.strip()), den)
        return Fraction(int(s), 1)
    if isinstance(value, float):
        # 显式拒绝：题面要求全部精确有理，浮点不得进入事实仓
        raise TypeError(
            f"拒绝浮点输入 {value!r}：请改用整数或 'p/q' 有理文本"
        )
    raise TypeError(f"无法转换为有理数的类型: {type(value).__name__}")


def frac_text(value: RationalLike) -> str:
    """把有理数编码为规范化冻结文本（最低项，负号在分子）。"""
    f = value if isinstance(value, Fraction) else frac(value)
    return str(f)


# 便捷别名
F = frac
