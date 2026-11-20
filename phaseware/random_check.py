"""随机化性质测试：主求解器（AC-3+回溯）与独立全枚举在大量随机小问题上逐量对齐。

运行：python -m phaseware.random_check
不属于交付的验证命令（validate 已内置），但可重复执行做更强的正确性保证。
"""

from __future__ import annotations

import random

from .exact import (
    BinaryConstraint,
    UnaryConstraint,
    Solver,
    brute_force_enumerate,
)


def _random_problem(rng: random.Random) -> tuple[
    list[str],
    list[BinaryConstraint],
    dict[str, list[UnaryConstraint]],
    dict[str, tuple[int, int]],
]:
    n = rng.randint(1, 5)
    nodes = [f"n{i}" for i in range(n)]
    m = rng.randint(2, 8)
    bound = rng.randint(1, 4)
    domains = {x: (-bound, bound) for x in nodes}

    unary: dict[str, list[UnaryConstraint]] = {
        x: [UnaryConstraint(f"bound[{x}]", x, frozenset(range(-bound, bound + 1)))]
        for x in nodes
    }
    # 随机锚点（保证落在界内）。
    for x in nodes:
        if rng.random() < 0.35:
            val = rng.randint(-bound, bound)
            unary[x].append(
                UnaryConstraint(f"anchor[{x}]={val}", x, frozenset({val}))
            )

    # 随机边：可能含平行边、正反向并存，候选集合大小 1..3。
    binary: list[BinaryConstraint] = []
    edge_no = 0
    for _ in range(rng.randint(0, 2 * n)):
        u, v = rng.sample(nodes, 2) if n >= 2 else (nodes[0], nodes[0])
        r = rng.randrange(m)
        k_lo = rng.randint(-1, 0)
        k_hi = rng.randint(0, 2)
        allowed = frozenset(r + m * k for k in range(k_lo, k_hi + 1))
        # 裁剪到全局界可达范围，避免无意义大集合。
        allowed = frozenset(d for d in allowed if -2 * bound <= d <= 2 * bound)
        if not allowed:
            continue
        edge_no += 1
        binary.append(BinaryConstraint(f"e{edge_no}", u, v, allowed))

    return nodes, binary, unary, domains


def run(iterations: int = 200, seed: int = 20261120) -> dict[str, int]:
    rng = random.Random(seed)
    stats = {"iterations": 0, "sat": 0, "unsat": 0, "mismatches": 0}
    for it in range(iterations):
        nodes, binary, unary, domains = _random_problem(rng)
        solver = Solver(nodes, binary, unary, domains)
        res = solver.solve(cap=2)
        enum = brute_force_enumerate(sorted(nodes), binary, unary, domains, cap=500_000)
        assert enum is not None, "小问题必须能全枚举"
        stats["iterations"] += 1

        if not enum:
            assert not res.feasible, f"iter {it}: 主求解器误判 SAT"
            stats["unsat"] += 1
            continue
        assert res.feasible, f"iter {it}: 主求解器误判 UNSAT"
        stats["sat"] += 1

        ordered = sorted(enum, key=lambda a: tuple(a[x] for x in sorted(nodes)))
        assert res.solutions[0] == ordered[0], f"iter {it}: 字典序最小解不一致"
        assert (len(enum) == 1) == (
            not res.truncated and len(res.solutions) == 1
        ), f"iter {it}: 唯一性判断不一致"

        for x in nodes:
            ext = solver.extrema(x)
            assert ext is not None
            lo_v, hi_v, lo_w, hi_w = ext
            vals = [a[x] for a in enum]
            assert lo_v == min(vals), f"iter {it}: 节点 {x} 最小值不一致"
            assert hi_v == max(vals), f"iter {it}: 节点 {x} 最大值不一致"
            assert lo_w[x] == lo_v and hi_w[x] == hi_v, "见证未取到极值"
            # 见证必须满足全部约束。
            assert not solver.check_assignment(lo_w), "最小见证违反约束"
            assert not solver.check_assignment(hi_w), "最大见证违反约束"
    return stats


if __name__ == "__main__":
    out = run()
    print(out)
    assert out["mismatches"] == 0
    print("随机性质测试通过：AC-3+回溯与独立全枚举在可行性/唯一性/逐节点极值上完全一致")
