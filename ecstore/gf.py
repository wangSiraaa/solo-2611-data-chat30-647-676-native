"""GF(2^8) 有限域实现。

固定参数:
* 不可约多项式 0x11d (x^8 + x^4 + x^3 + x^2 + 1)
* 生成元 g = 0x02 (注意: 0x03 在该多项式下阶仅 51, 不是本原元;
  0x11b/AES 多项式下 0x03 才是本原元)
* 域加法 = 字节异或 (XOR)
* 域乘法用 exp/log 表实现; 另提供慢速参考实现 mul_slow 供测试比对。
"""

POLY = 0x11D
GEN = 0x02
FIELD_SIZE = 256

# log/exp 表: exp[i] = g^i, log[exp[i]] = i (log[0] 未定义, 置 0)
_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    # 每步乘以生成元 g=0x02 (即 xtime), 归约使用不可约多项式 0x11d
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= POLY
    # 扩展一倍, 避免取指数时回绕
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()
EXP = tuple(_EXP[:255])
LOG = tuple(_LOG)


def add(a: int, b: int) -> int:
    """域加法 = 异或。"""
    return a ^ b


def mul(a: int, b: int) -> int:
    """域乘法 (表驱动)。"""
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def inv(a: int) -> int:
    """域逆元; inv(0) 抛出 ZeroDivisionError。"""
    if a == 0:
        raise ZeroDivisionError("GF(2^8) 中 0 无逆元")
    return _EXP[255 - _LOG[a]]


def div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("GF(2^8) 中不可除以 0")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def mul_slow(a: int, b: int) -> int:
    """俄罗斯农夫乘法参考实现 (不依赖 exp/log 表)。"""
    result = 0
    aa, bb = a, b
    while bb:
        if bb & 1:
            result ^= aa
        bb >>= 1
        aa <<= 1
        if aa & 0x100:
            aa ^= POLY
    return result & 0xFF


# MUL_TABLE[c][x] = c * x, 用于对整字节向量做标量乘 (translate 风格)
MUL_TABLE: tuple[tuple[int, ...], ...] = tuple(
    tuple(mul(c, x) for x in range(256)) for c in range(256)
)


def scalar_mul(c: int, data: bytes) -> bytes:
    """域标量乘: 对 data 每个字节乘 c。"""
    table = MUL_TABLE[c]
    return bytes(table[b] for b in data)


def linear_combine(pairs: list[tuple[int, bytes]]) -> bytes:
    """域内线性组合: sum_i coef_i * data_i (XOR 求和)。

    所有 data_i 必须等长; coef=0 或空输入会被自然处理 (空输入返回 b"")。
    """
    if not pairs:
        return b""
    length = len(pairs[0][1])
    acc = bytearray(length)
    for coef, data in pairs:
        if coef == 0:
            continue
        if len(data) != length:
            raise ValueError("linear_combine: 各向量长度不一致")
        table = MUL_TABLE[coef]
        for i, b in enumerate(data):
            acc[i] ^= table[b]
    return bytes(acc)
