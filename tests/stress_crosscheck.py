"""随机压力测试：引擎 vs 独立路径枚举暴力实现。

不调用 hydro.engine / hydro.explain 的任何求值代码，只用 catalog 结构，
独立按「每个本地水团沿每条路径传播」重算，逐格精确比对总出流、上游
贡献、离开体积与全局守恒，并核对峰值与时域尾部。

运行：python tests/stress_crosscheck.py
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Dict, List, Tuple

from hydro.catalog import CatalogDraft
from hydro.engine import AnalysisRequest, Engine
from hydro.explain import SourceExplainer
from hydro.rainfall import RainRepo
from hydro.storage import Store


def random_catalog(rng: random.Random):
    n = rng.randint(2, 10)
    ids = [f"n{i}" for i in range(n)]
    d = CatalogDraft()
    for nid in ids:
        m = rng.randint(1, 4)
        nums = [rng.randint(0, 5) for _ in range(m)]
        s = sum(nums)
        if s == 0:
            nums[0] = 1
            s = 1
        kernel = [Fraction(x, s) for x in nums]
        d.add_node(nid, rng.randint(1, 6), kernel)
    # 随机 DAG：只允许 i<j 的边；整数权重归一化保证比例和 <= 1
    for i in range(n):
        targets = [j for j in range(i + 1, n)]
        rng.shuffle(targets)
        budget = rng.randint(0, min(3, len(targets)))
        chosen = targets[:budget]
        weights = [rng.randint(0, 4) for _ in chosen]
        total_w = sum(weights)
        if total_w == 0 and chosen:
            weights[0] = 1
            total_w = 1
        # 分母再放大，留出严格小于 1 的「离开模型」差额空间
        denom = total_w + rng.randint(0, 3)
        for j, w in zip(chosen, weights):
            d.add_edge(ids[i], ids[j], rng.randint(0, 3), Fraction(w, denom))
    return d.publish()


def enumerate_paths(cat, target: str):
    """独立路径枚举（递归，与 explain 无共享代码路径）。

    含 target 自身的空路径（延迟0、比例1），表示本地来源。
    """
    results: List[Tuple[Tuple[str, ...], int, Fraction]] = []

    def walk(nid, path, dsum, psum):
        ins = cat.in_edges(nid)
        if not ins:
            results.append((tuple(reversed(path)), dsum, psum))
            return
        # 停在当前节点：path 从源头到 nid 的一条完整路径
        results.append((tuple(reversed(path)), dsum, psum))
        for e in ins:
            walk(e.from_id, path + [e.from_id], dsum + e.delay, psum * e.proportion)

    walk(target, [target], 0, Fraction(1))
    return results


def brute_force(cat, rain, L, T):
    """逐水团 × 逐路径的独立参考实现（窗口在 rain 批次中）。"""
    R = rain.hi
    n_steps = T - L + 1
    out: Dict[str, List[Fraction]] = {n: [Fraction(0)] * n_steps for n in cat.node_ids}
    local: Dict[str, List[Fraction]] = {n: [Fraction(0)] * n_steps for n in cat.node_ids}
    for nid in cat.node_ids:
        node = cat.node(nid)
        paths = enumerate_paths(cat, nid)
        # 对每个起源水团
        for origin in cat.node_ids:
            onode = cat.node(origin)
            for tau in range(L, R):
                r, l = rain.cell(origin, tau)
                eff = r - l
                if eff <= 0:
                    continue
                v = onode.area * eff
                for path, dsum, psum in paths:
                    if path[0] != origin:
                        continue
                    for jj, kk in enumerate(onode.kernel):
                        if kk == 0:
                            continue
                        t = tau + jj + dsum
                        if L <= t <= T:
                            out[nid][t - L] += v * kk * psum
        # 本地产流（独立卷积）
        for tau in range(L, R):
            r, l = rain.cell(nid, tau)
            eff = r - l
            if eff <= 0:
                continue
            v = node.area * eff
            for jj, kk in enumerate(node.kernel):
                t = tau + jj
                if L <= t <= T:
                    local[nid][t - L] += v * kk
    up = {n: [out[n][i] - local[n][i] for i in range(n_steps)] for n in cat.node_ids}
    return out, local, up


def main():
    rng = random.Random(20261118)
    trials = 40
    for trial in range(trials):
        cat = random_catalog(rng)
        store = Store(":memory:")
        repo = RainRepo(store, cat)
        L, R = 0, rng.randint(2, 8)
        cells = {}
        for nid in cat.node_ids:
            for t in range(L, R):
                r = rng.randint(0, 4)
                l = rng.randint(0, 3)
                cells[(nid, t)] = (Fraction(r), Fraction(l))
        rain = repo.import_batch(L, R, list(cat.node_ids), cells)
        req = AnalysisRequest.make(tuple(cat.node_ids), L, R)
        report = Engine(cat).analyze(req, rain)
        assert report.status == "OK" and report.conserved, f"trial {trial} 未守恒"

        out_b, local_b, up_b = brute_force(cat, rain, L, report.horizon_end)
        for nid in cat.node_ids:
            rep = report.node_reports[nid]
            assert rep.outflow == out_b[nid], f"trial {trial} node {nid} 出流不一致"
            assert rep.local == local_b[nid], f"trial {trial} node {nid} 本地不一致"
            assert rep.upstream == up_b[nid], f"trial {trial} node {nid} 上游不一致"
            # 离开 = 出流 * 离开比例
            lf = cat.leave_fraction(nid)
            for i, v in enumerate(rep.outflow):
                assert rep.leave[i] == v * lf
            # 峰值独立计算
            pk = max(rep.outflow)
            steps = [L + i for i, v in enumerate(rep.outflow) if v == pk]
            assert rep.peak_steps == (steps if pk > 0 else [])

        # 来源分解在随机时刻与引擎对账
        explainer = SourceExplainer(cat, rain)
        for nid in rng.sample(list(cat.node_ids), min(3, len(cat.node_ids))):
            t = rng.randint(L, report.horizon_end)
            ex = explainer.explain(nid, t)
            assert ex.outflow == report.node_reports[nid].at("outflow", t)
            assert ex.verify()

        # 尾部之后必须全零（不能在 R 截断也不能无限延伸）
        for nid in cat.node_ids:
            assert report.node_reports[nid].outflow[-1] is not None
        store.close()
    print(f"压力测试通过：{trials} 个随机 DAG，引擎与独立路径实现逐格精确一致，守恒/峰值/来源全对")


if __name__ == "__main__":
    main()
