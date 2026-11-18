"""编码配置、矩阵可逆性校验、条带编码与恢复。

矩阵布局: 前 k 行为单位阵 (数据片 D0..D{k-1}), 其后为登记配置给定的
长度 k 字节向量 (编码片)。配置登记时枚举全部 C(n, k) 个行组合,
每个 k*k 矩阵都必须在 GF(2^8) 上可逆, 否则拒绝该配置。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import gf


class ConfigError(ValueError):
    """配置非法 (参数范围、行格式或不满足 MDS 性质)。"""


@dataclass(frozen=True)
class CodeConfig:
    k: int
    n: int
    L: int
    coding_rows: tuple[tuple[int, ...], ...]

    def to_spec(self) -> dict:
        return {
            "field": "GF(2^8)",
            "poly": "0x11d",
            "k": self.k,
            "n": self.n,
            "L": self.L,
            "coding_rows": [list(row) for row in self.coding_rows],
        }

    @property
    def digest(self) -> str:
        return config_digest(self)

    @property
    def matrix(self) -> tuple[tuple[int, ...], ...]:
        """完整 n*k 编码矩阵 (前 k 行为单位阵)。"""
        rows = [tuple(1 if i == j else 0 for j in range(self.k)) for i in range(self.k)]
        rows.extend(tuple(r) for r in self.coding_rows)
        return tuple(rows)


def config_digest(config: CodeConfig) -> str:
    """配置摘要: 对规范化 JSON 取 SHA-256, 作为条带身份绑定的一部分。"""
    canonical = json.dumps(config.to_spec(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def make_config(k: int, n: int, L: int, coding_rows: list[list[int]] | None = None) -> CodeConfig:
    """构造并校验配置; coding_rows 为 None 时生成 Cauchy 编码行 (MDS)。"""
    if not isinstance(k, int) or not (2 <= k <= 6):
        raise ConfigError("k 必须是 2..6 的整数")
    if not isinstance(n, int) or not (k <= n <= 10):
        raise ConfigError("n 必须是 k..10 的整数")
    if not isinstance(L, int) or L <= 0:
        raise ConfigError("L 必须是正整数")

    if coding_rows is None:
        coding_rows = cauchy_coding_rows(k, n - k)

    rows = tuple(tuple(int(x) for x in row) for row in coding_rows)
    if len(rows) != n - k:
        raise ConfigError(f"编码行数量应为 n-k={n - k}, 实际 {len(rows)}")
    for r in rows:
        if len(r) != k or any(not (0 <= x <= 255) for x in r):
            raise ConfigError("每个编码行必须是长度 k 的字节向量 (元素 0..255)")
        if all(x == 0 for x in r):
            raise ConfigError("编码行不能为全零向量")
    if len({r for r in rows}) != len(rows):
        raise ConfigError("编码行不得重复 (重复行必然导致某些 k 行组合不可逆)")

    config = CodeConfig(k=k, n=n, L=L, coding_rows=rows)
    validate_mds(config)
    return config


def cauchy_coding_rows(k: int, m: int) -> list[list[int]]:
    """生成 m 个 Cauchy 编码行, 与单位阵拼接后任意 k 行可逆。

    C[i][j] = 1 / (x_i XOR y_j), x 取自 {k..k+m-1}, y 取自 {0..k-1},
    两组下标互不相交。GF(2^8) 上 Cauchy 矩阵的任意子方阵可逆。
    """
    rows: list[list[int]] = []
    for i in range(m):
        x = k + i
        rows.append([gf.inv(x ^ y) for y in range(k)])
    return rows


def validate_mds(config: CodeConfig) -> None:
    """枚举全部 C(n, k) 行组合做高斯消元, 任一不可逆即拒绝。"""
    n, k = config.n, config.k
    matrix = config.matrix
    for combo in _combinations(range(n), k):
        sub = [matrix[i] for i in combo]
        if invert_matrix(sub) is None:
            raise ConfigError(
                f"配置不满足 MDS: 行组合 {list(combo)} 在 GF(2^8) 上不可逆"
            )


def _combinations(items, r: int):
    """小范围组合枚举 (n<=10, 避免引入 itertools 依赖习惯之外的东西)。"""
    items = list(items)
    if r == 0:
        yield ()
        return
    for i in range(len(items) - r + 1):
        for tail in _combinations(items[i + 1 :], r - 1):
            yield (items[i], *tail)


def invert_matrix(rows: list[tuple[int, ...]] | tuple[tuple[int, ...], ...]):
    """GF(2^8) 高斯-约当消元求逆; 奇异矩阵返回 None。"""
    m = len(rows)
    aug = [list(rows[i]) + [1 if i == j else 0 for j in range(m)] for i in range(m)]
    for col in range(m):
        pivot = next((r for r in range(col, m) if aug[r][col] != 0), None)
        if pivot is None:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        p = gf.inv(aug[col][col])
        aug[col] = [gf.mul(x, p) for x in aug[col]]
        for r in range(m):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [x ^ gf.mul(factor, y) for x, y in zip(aug[r], aug[col])]
    return tuple(tuple(row[m:]) for row in aug)


def encode_stripe(config: CodeConfig, data: bytes) -> list[bytes]:
    """把恰好 k*L 字节编码为 n 个长度 L 的片 (域内线性组合)。"""
    k, L = config.k, config.L
    if len(data) != k * L:
        raise ValueError(f"条带数据必须恰为 k*L={k * L} 字节, 实际 {len(data)}")
    pieces = [data[j * L : (j + 1) * L] for j in range(k)]
    matrix = config.matrix
    return [
        gf.linear_combine([(matrix[i][j], pieces[j]) for j in range(k) if matrix[i][j] != 0])
        for i in range(config.n)
    ]


def recover_stripe(config: CodeConfig, indexed_pieces: dict[int, bytes]) -> list[bytes]:
    """从任意 >=k 个不同序号的合法片恢复 k 个数据片。

    取其中序号最小的 k 个 (任意 k 个都合法, 因为配置已通过 MDS 校验),
    解 A * D = P 得 D = A^{-1} * P。调用方负责片身份与摘要过滤。
    """
    if len(indexed_pieces) < config.k:
        raise ValueError("有效片不足 k 个, 拒绝靠补零恢复")
    bad_len = [idx for idx, p in indexed_pieces.items() if len(p) != config.L]
    if bad_len:
        raise ValueError(f"片长度与配置 L={config.L} 不符: 序号 {sorted(bad_len)}")

    chosen = sorted(indexed_pieces)[: config.k]
    matrix = config.matrix
    sub = [tuple(matrix[i]) for i in chosen]
    inv_sub = invert_matrix(sub)
    if inv_sub is None:  # 理论上不会发生: 登记时已枚举验证
        raise ValueError("所选 k 行不可逆 (配置未通过 MDS 校验?)")

    recovered: list[bytes] = []
    for j in range(config.k):
        pairs = [(inv_sub[j][t], indexed_pieces[chosen[t]]) for t in range(config.k)]
        recovered.append(gf.linear_combine(pairs))
    return recovered


def reencode_match(config: CodeConfig, data_pieces: list[bytes],
                   indexed_pieces: dict[int, bytes]) -> list[int]:
    """用恢复出的数据片重新编码, 找出与提供片不一致的序号。"""
    data = b"".join(data_pieces)
    full = encode_stripe(config, data)
    return [idx for idx, piece in indexed_pieces.items() if full[idx] != piece]
