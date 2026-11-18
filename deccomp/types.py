"""decimal(p,s) 类型与二进制运算的类型推导。

推导规则严格按题面（与 SQL Server 风格的结果位宽公式一致，但此处是题面给定版本）：

* 加减：result_scale = max(s1, s2)，所需整数位 = max(p1-s1, p2-s2) + 1，
  原始 precision = p1 + p2。
* 乘法：result_scale = s1 + s2，所需整数位 = (p1-s1) + (p2-s2) + 1，
  原始 precision = p1 + p2。
* 除法：result_scale = max(6, s1 + p2 + 1)，
  所需整数位 = (p1-s1) + s2，原始 precision = p1 + p2。

结果 precision 至多 38。原始 precision 超过 38 时优先把 scale 向 0 收缩，
绝不削减上述所需整数位；若所需整数位本身超过 38，编译期拒绝。
"""

from dataclasses import dataclass

from .errors import CompileError

MAX_PRECISION = 38


@dataclass(frozen=True)
class DecimalType:
    precision: int
    scale: int

    def __post_init__(self):
        if not (1 <= self.precision <= MAX_PRECISION):
            raise CompileError(
                f"decimal precision 必须在 1..{MAX_PRECISION}，得到 {self.precision}"
            )
        if not (0 <= self.scale <= self.precision):
            raise CompileError(
                f"decimal scale 必须在 0..precision，得到 scale={self.scale} "
                f"precision={self.precision}"
            )

    @property
    def int_digits(self):
        """声明类型容纳的整数位数。"""
        return self.precision - self.scale

    def __str__(self):
        return f"decimal({self.precision},{self.scale})"


def derive_binary(op, t1: DecimalType, t2: DecimalType,
                  node_id=None) -> DecimalType:
    """按题面公式推导二进制算术节点的结果类型。"""
    p1, s1 = t1.precision, t1.scale
    p2, s2 = t2.precision, t2.scale
    raw_precision = p1 + p2

    if op in ("add", "sub"):
        raw_scale = max(s1, s2)
        need_int = max(p1 - s1, p2 - s2) + 1
    elif op == "mul":
        raw_scale = s1 + s2
        need_int = (p1 - s1) + (p2 - s2) + 1
    elif op == "div":
        raw_scale = max(6, s1 + p2 + 1)
        need_int = (p1 - s1) + s2
    else:  # pragma: no cover - AST 层已拦截
        raise CompileError(f"未知算术算子: {op}", node_id=node_id)

    if need_int > MAX_PRECISION:
        raise CompileError(
            f"节点 {node_id} 的 {op} 运算所需整数位 {need_int} 超过 "
            f"{MAX_PRECISION}，计划被拒绝（不得削减整数位）",
            node_id=node_id,
        )

    if raw_precision <= MAX_PRECISION:
        # 原始 precision 未超限：节点类型按公式原样声明。
        return DecimalType(raw_precision, raw_scale)

    # 原始 precision 超限：precision 截到 38，scale 优先向 0 收缩，
    # 但保留全部所需整数位。
    new_scale = min(raw_scale, MAX_PRECISION - need_int)
    return DecimalType(MAX_PRECISION, new_scale)
