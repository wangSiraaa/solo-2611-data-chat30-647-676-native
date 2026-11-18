"""独立 oracle：用整数与 Fraction 逐节点模拟表达式，不使用 decimal.Decimal。

与生产执行器是两套独立实现：

* 数值用 ``fractions.Fraction``（整数比），half-even 舍入手写为纯整数
  四舍五入（银行家舍入），类型位宽公式与编译器共用同一套推导规则；
* 递归求值并记录每个实际求值节点的舍入后结果（trace），供测试逐节点比对；
* 同样实现 CASE 短路、NULL 传播、除零与溢出判定，未选分支不被访问。

测试以本 oracle 的逐节点结果为基准，验证行式/列式两个执行器。
"""

from fractions import Fraction

from ..planner import Plan
from ..types import DecimalType


def _quantize_half_even(value: Fraction, scale: int) -> int:
    """把 Fraction 按 10^-scale 做 half-even 量化，返回整数系数。"""
    factor = 10 ** scale
    num = value.numerator * factor
    den = value.denominator
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den:
        q += 1
    elif twice == den and (q & 1):
        q += 1
    return q


def _int_digits(coef: int) -> int:
    if coef == 0:
        return 0
    return len(str(abs(coef)))


class OracleError(Exception):
    def __init__(self, code, node_id, message):
        super().__init__(message)
        self.code = code
        self.node_id = node_id


class Oracle:
    def __init__(self, plan: Plan):
        self.plan = plan
        self.nodes = plan.nodes
        self.trace = {}          # nid -> (coef, scale) 每个实际求值节点

    def run_row(self, row_text):
        """单行：row_text 为 {列名: 文本|None}。返回 (Decimal-like, error)。

        成功时返回 (coef, scale) 整数表示；失败返回 (None, OracleError)。
        """
        self.trace = {}
        try:
            return self._eval(self.plan.root_id, row_text), None
        except OracleError as exc:
            return None, exc

    def _eval(self, nid, row):
        node = self.nodes[nid]
        op = node.op

        if op == "column":
            raw = row[node.name]
            if raw is None:
                return None
            return self._text_to_coef(raw, node.type, nid)

        if op == "const":
            if node.value is None:
                return None
            return self._text_to_coef(node.value, node.type, nid)

        if op in ("add", "sub", "mul", "div"):
            return self._eval_arith(nid, row)

        if op == "cast":
            v = self._eval(node.children[0], row)
            if v is None:
                return None
            return self._round_check(v, node.to, nid)

        if op == "cmp":
            return self._eval_cmp(nid, row)

        if op == "case":
            for cond_id, then_id in node.when_children:
                cond = self._eval(cond_id, row)
                if cond is True:
                    return self._eval(then_id, row)
            if node.else_child is not None:
                return self._eval(node.else_child, row)
            return None

        raise OracleError("compile_error", nid, f"未知算子 {op}")  # pragma: no cover

    def _eval_arith(self, nid, row):
        node = self.nodes[nid]
        left = self._eval(node.children[0], row)
        right = self._eval(node.children[1], row)
        if left is None or right is None:
            return None
        lc, ls = left
        rc, rs = right
        a = Fraction(lc, 10 ** ls)
        b = Fraction(rc, 10 ** rs)

        if node.op == "div":
            if b == 0:
                raise OracleError("div_by_zero", nid,
                                  f"节点 {nid} 除零")
            raw = a / b
        else:
            raw = {"add": a + b, "sub": a - b, "mul": a * b}[node.op]

        return self._round_check_fraction(raw, node.type, nid)

    def _eval_cmp(self, nid, row):
        node = self.nodes[nid]
        left = self._eval(node.children[0], row)
        right = self._eval(node.children[1], row)
        if left is None or right is None:
            return None
        a = Fraction(left[0], 10 ** left[1])
        b = Fraction(right[0], 10 ** right[1])
        ops = {"lt": a < b, "le": a <= b, "eq": a == b,
               "ne": a != b, "ge": a >= b, "gt": a > b}
        return ops[node.cmp]

    def _round_check(self, value, t: DecimalType, nid):
        coef, scale = value
        return self._round_check_fraction(Fraction(coef, 10 ** scale), t, nid)

    def _round_check_fraction(self, raw: Fraction, t: DecimalType, nid):
        coef = _quantize_half_even(raw, t.scale)
        # 溢出检查只看整数位数（系数总位数减去 scale），与生产引擎一致。
        int_digits = max(0, _int_digits(coef) - t.scale)
        if int_digits > t.int_digits:
            raise OracleError("overflow", nid,
                              f"节点 {nid} precision 溢出，coef={coef}")
        result = (coef, t.scale)
        # 只记录实际到达的节点（未选 CASE 分支根本不会进入本函数）。
        self.trace[nid] = result
        return result

    def _text_to_coef(self, text, t: DecimalType, nid):
        """文本 -> (coef, scale)，并严格按声明类型检查可表示性。"""
        s = text.strip()
        neg = s.startswith("-")
        if neg or s.startswith("+"):
            s = s[1:]
        if "." in s:
            int_part, frac_part = s.split(".", 1)
        else:
            int_part, frac_part = s, ""
        if not int_part:
            int_part = "0"
        digits = int_part + frac_part
        if not digits.isdigit():
            raise OracleError("input_error", nid,
                              f"非法十进制文本 {text!r}")
        scale = len(frac_part)
        coef = int(digits or "0")
        if neg:
            coef = -coef
        # 可表示性按输入自身 scale 判断：小数位不超声明 scale，
        # 整数位（输入系数的位数 - 输入 scale）不超声明整数位。
        if scale > t.scale or (_int_digits(coef) - scale) > t.int_digits:
            raise OracleError("input_error", nid,
                              f"输入 {text} 不可由 {t} 表示")
        # 统一到声明 scale（输入已证明可精确表示，只是补零）。
        coef *= 10 ** (t.scale - scale)
        result = (coef, t.scale)
        self.trace[nid] = result
        return result
