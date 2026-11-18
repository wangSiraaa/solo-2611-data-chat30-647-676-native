"""测试辅助：AST 构造器与执行器/oracle 结果换算。"""

from fractions import Fraction

from ..decimals import Decimal


def col(name):
    return {"op": "column", "name": name}


def const(value, p, s):
    return {"op": "const", "value": None if value is None else str(value),
            "type": [p, s]}


def arith(op, left, right):
    return {"op": op, "left": left, "right": right}


def cmp(c, left, right):
    return {"op": "cmp", "cmp": c, "left": left, "right": right}


def cast(arg, p, s):
    return {"op": "cast", "arg": arg, "to": [p, s]}


def case(whens, else_ast=None):
    return {"op": "case", "when": whens, "else": else_ast}


def when(cond, then):
    return {"cond": cond, "then": then}


def engine_value_to_fraction(value):
    """执行器 Decimal 结果 -> Fraction（经文本，绝不经 float）。"""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    sign, digits, exp = d.as_tuple()
    coef = int("".join(str(x) for x in digits))
    if sign:
        coef = -coef
    if exp >= 0:
        return Fraction(coef * (10 ** exp), 1)
    return Fraction(coef, 10 ** (-exp))


def oracle_value_to_fraction(pair):
    if pair is None:
        return None
    coef, scale = pair
    return Fraction(coef, 10 ** scale)
