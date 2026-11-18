"""精确有理数工具。

系统内任何数值都以 :class:`fractions.Fraction` 参与运算，JSON 边界上使用
规范化分数字符串（如 ``"3/7"``、``"-5"``）。显式拒绝 float，避免二进制近似
被当成精确答案。输入分子分母限制在 12 位十进制以内；消元过程中增长出来的
大分数不受此限。
"""

from fractions import Fraction

from .errors import ValidationError

_MAX_DIGITS = 12


def parse_rational(value, *, field="value", enforce_input_limit=True):
    """把 JSON 边界上的有理数转成 Fraction。

    接受：Python int（bool 显式拒绝）、形如 ``"12"`` / ``"-3/7"`` 的字符串。
    拒绝：float、含小数点数、非数字、分母为负或零、超过 12 位的输入。
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field} 不允许布尔值", [f"{field}={value!r}"])
    if isinstance(value, int):
        frac = Fraction(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(f"{field} 为空字符串", [f"{field}={value!r}"])
        sign = 1
        if text[0] in "+-":
            if text[0] == "-":
                sign = -1
            text = text[1:]
        if not text or "/" in text and text.count("/") > 1:
            raise ValidationError(f"{field} 不是合法有理数", [f"{field}={value!r}"])
        parts = text.split("/")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            raise ValidationError(
                f"{field} 含非数字字符", [f"{field}={value!r}"]
            ) from None
        if any(isinstance(n, bool) for n in nums):  # pragma: no cover - int 解析不出 bool
            raise ValidationError(f"{field} 非法", [f"{field}={value!r}"])
        if len(nums) == 1:
            frac = Fraction(sign * nums[0])
        else:
            numerator, denominator = nums
            if denominator == 0:
                raise ValidationError(f"{field} 分母为零", [f"{field}={value!r}"])
            if denominator < 0:
                raise ValidationError(
                    f"{field} 分母必须为正（符号放分子）", [f"{field}={value!r}"]
                )
            frac = Fraction(sign * numerator, denominator)
    elif isinstance(value, float):
        raise ValidationError(
            f"{field} 不接受 float，全部数值必须是精确整数或分数字符串",
            [f"{field}={value!r}"],
        )
    else:
        raise ValidationError(
            f"{field} 类型非法（仅 int 或分数字符串）", [f"{field}={value!r}"]
        )
    if enforce_input_limit:
        for part in (abs(frac.numerator), frac.denominator):
            if len(str(part)) > _MAX_DIGITS:
                raise ValidationError(
                    f"{field} 的分子/分母超过 {_MAX_DIGITS} 位十进制限制",
                    [f"{field}={value!r}"],
                )
    return frac


def qstr(frac):
    """Fraction 的规范化字符串：整数不带 ``/1``。"""
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def parse_qstr(text):
    """解析 :func:`qstr` 产出的规范字符串（验证器独立路径使用）。"""
    return Fraction(text)
