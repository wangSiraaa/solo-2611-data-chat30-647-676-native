"""十进制数值内核：全程 ``decimal.Decimal`` / 整数，禁止 binary float。

外部输入只接受十进制文本，``nan`` / ``inf`` / ``sNaN`` 一律拒绝；
中间计算把上下文精度放到 200（38 位类型 + 除法保护位），舍入只在
每个实际求值节点按节点自身 scale 用 ROUND_HALF_EVEN 显式发生，
随后做 precision 溢出检查。绝不依赖上下文的隐式舍入。
"""

from decimal import Decimal, getcontext, ROUND_HALF_EVEN

from .errors import CompileError, DataError, DIV_BY_ZERO, OVERFLOW, INPUT_ERROR
from .types import DecimalType, MAX_PRECISION

# 38 位合法值 + 除法未舍入商的保护位，远大于任何节点需求。
getcontext().prec = 200

_NULL = Decimal(0)  # 仅用于哨兵初始化，不参与输出


def parse_decimal_text(text, node_id=None):
    """把十进制文本严格解析为 Decimal，拒绝 NaN/Inf 与非数值文本。

    Decimal 直接从字符串构造，不经过 float。
    """
    if not isinstance(text, str):
        raise CompileError(f"十进制值必须是文本，得到 {type(text).__name__}",
                           node_id=node_id)
    try:
        value = Decimal(text.strip())
    except Exception as exc:  # InvalidOperation 等
        raise CompileError(f"无法解析十进制文本: {text!r}: {exc}",
                           node_id=node_id)
    if not value.is_finite():
        raise CompileError(f"NaN/Infinity 不是合法十进制输入: {text!r}",
                           node_id=node_id)
    return value


def fits(value: Decimal, t: DecimalType) -> bool:
    """检查已按 t.scale 舍入后的值能否被 decimal(p,s) 表示（不隐式舍入）。"""
    # 规范化到声明 scale 后必须与原值相等，否则输入本身 scale 过大。
    scaled = value.quantize(Decimal(1).scaleb(-t.scale),
                            rounding=ROUND_HALF_EVEN)
    if scaled != value:
        return False
    # Decimal.adjusted() 返回最高有效位的 10 进制指数；+1 即整数位数。
    if value == 0:
        int_digits = 0
    else:
        int_digits = value.adjusted() + 1
    return int_digits <= t.int_digits


def check_input(value: Decimal, t: DecimalType, node_id=None):
    """对输入列值做严格可表示性检查，不合格按输入数据错误拒绝该行。"""
    if not fits(value, t):
        raise DataError(
            f"输入值 {value} 不能由 {t} 精确表示（不做隐式舍入）",
            code=INPUT_ERROR, node_id=node_id,
        )


def round_to_node(value: Decimal, t: DecimalType, node_id) -> Decimal:
    """节点级 half-even 舍入 + precision 溢出检查。

    每个实际求值的节点都必须经过本函数，不允许只在最终输出舍入。
    """
    quantum = Decimal(1).scaleb(-t.scale)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        int_digits = 0
    else:
        int_digits = rounded.adjusted() + 1
    if int_digits > t.int_digits:
        raise DataError(
            f"节点 {node_id} 舍入后整数位 {int_digits} 超过 {t} 的容量 "
            f"{t.int_digits}（precision 溢出），值={rounded}",
            code=OVERFLOW, node_id=node_id,
        )
    return rounded


def add(a: Decimal, b: Decimal) -> Decimal:
    return a + b


def sub(a: Decimal, b: Decimal) -> Decimal:
    return a - b


def mul(a: Decimal, b: Decimal) -> Decimal:
    return a * b


def div(a: Decimal, b: Decimal, t: DecimalType, node_id) -> Decimal:
    """精确除法（高保护精度）后交给节点级 half-even 舍入。

    除数为 0 在此处显式抛出带节点来源的 DataError。上下文精度 200 相对
    最多 38 位的最终 scale 留有 160+ 保护位，未舍入商在被截断方向上
    不可能出现影响 half-even 结果的残余误差。
    """
    if b == 0:
        raise DataError(
            f"节点 {node_id} 发生除零（{a} / 0）",
            code=DIV_BY_ZERO, node_id=node_id,
        )
    raw = a / b
    return round_to_node(raw, t, node_id)


def round_arith_result(value: Decimal, t: DecimalType, node_id) -> Decimal:
    """加减乘的节点级舍入入口（除法在 div 内联以保证除零先于舍入）。"""
    return round_to_node(value, t, node_id)


def decimal_to_text(value: Decimal) -> str:
    """规范十进制文本（科学计数法展开为普通定点文本），全程不涉及 float。"""
    sign, digits, exponent = value.as_tuple()
    digit_str = "".join(str(d) for d in digits)
    if exponent >= 0:
        body = digit_str + "0" * exponent
        int_part, frac_part = body, ""
    else:
        frac_len = -exponent
        if frac_len >= len(digit_str):
            int_part = "0"
            frac_part = "0" * (frac_len - len(digit_str)) + digit_str
        else:
            int_part = digit_str[:-frac_len]
            frac_part = digit_str[-frac_len:]
    text = int_part + (("." + frac_part) if frac_part else "")
    return ("-" + text) if sign else text


# 重新导出，执行器/输出层只依赖本模块的数值抽象。
__all__ = [
    "Decimal", "MAX_PRECISION", "ROUND_HALF_EVEN",
    "parse_decimal_text", "fits", "check_input", "round_to_node",
    "add", "sub", "mul", "div", "round_arith_result", "decimal_to_text",
]
