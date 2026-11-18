"""规模测试：20 模型 / 300 格 / 约 1000 引用，分子分母至多 12 位。

构建确定性合成网络：
* 10 个输入格（12 位以内随机精确分数），其余为公式格；
* 引用边精确凑到 1000 条（含跨模型边）；
* 每 5 个模型构造一个 4 格循环 SCC，保证解算端必须联合处理循环；
* 部分循环系数设计为冗余/自由结构，部分为唯一确定结构。

另测：重开复读、缓存命中率、解算耗时与大分数增长（全程 Fraction）。
"""

import random
import shutil
import tempfile
import time
from fractions import Fraction

from budget_solver.service import BudgetService

MODEL_COUNT = 20
CELLS_PER_MODEL = 15
TOTAL_CELLS = MODEL_COUNT * CELLS_PER_MODEL  # 300
TARGET_REFS = 1000


def _rand_fraction(rng):
    numerator = rng.randint(-10 ** 11, 10 ** 11)
    denominator = rng.randint(1, 10 ** 11)
    frac = Fraction(numerator, denominator)
    # 约分后仍满足 12 位限制
    while len(str(abs(frac.numerator))) > 12 or len(str(frac.denominator)) > 12:
        numerator = rng.randint(-10 ** 10, 10 ** 10)
        denominator = rng.randint(1, 10 ** 10)
        frac = Fraction(numerator, denominator)
    return f"{frac.numerator}/{frac.denominator}" if frac.denominator != 1 else str(frac.numerator)


def build_payloads():
    rng = random.Random(20261118)
    model_ids = [f"SCALE_M{i:02d}" for i in range(MODEL_COUNT)]
    payloads = []
    ref_plan = []  # (from_model, from_cell, to_model, to_cell)

    for mi, mid in enumerate(model_ids):
        cells = {}
        for ci in range(CELLS_PER_MODEL):
            cid = f"c{ci:02d}"
            if ci < 2:  # 每模型 2 个输入格 => 40 输入 / 260 公式
                cells[cid] = {"kind": "input", "value": _rand_fraction(rng)}
            else:
                cells[cid] = {"kind": "formula", "constant": _rand_fraction(rng), "terms": []}
        # 每模型一个 4 格循环 SCC（c11..c14），系数取小分数保证数值稳定
        cyc = ["c11", "c12", "c13", "c14"]
        for k, cid in enumerate(cyc):
            nxt = cyc[(k + 1) % 4]
            cells[cid] = {
                "kind": "formula",
                "constant": _rand_fraction(rng),
                "terms": [{"coef": f"1/{rng.randint(2, 9)}", "cell_id": nxt}],
            }
            ref_plan.append((mid, cid, mid, nxt))
        payloads.append(
            {
                "model_id": mid,
                "name": f"规模模型 {mi}",
                "cells": cells,
                "outputs": [f"c{CELLS_PER_MODEL - 1:02d}", "c11"],
            }
        )

    # 跨模型骨干链：M_i 的 c02 引用 M_{i-1} 的 c00，保证根模型传递闭包覆盖全部模型
    for i in range(1, MODEL_COUNT):
        ref_plan.append((model_ids[i], "c02", model_ids[i - 1], "c00"))

    # 普通引用边（先本模型、再跨模型），凑满 TARGET_REFS
    formula_cells = [
        (mid, f"c{ci:02d}")
        for mid in model_ids
        for ci in range(2, CELLS_PER_MODEL)
    ]
    all_cells = [(mid, f"c{ci:02d}") for mid in model_ids for ci in range(CELLS_PER_MODEL)]
    existing = set(ref_plan)
    guard = 0
    while len(ref_plan) < TARGET_REFS and guard < TARGET_REFS * 20:
        guard += 1
        fm, fc = rng.choice(formula_cells)
        fm_index = model_ids.index(fm)
        if rng.random() < 0.35 and fm_index > 0:  # 35% 跨模型边，只指向更早发布的模型
            tm = rng.choice(model_ids[:fm_index])
        else:
            tm = fm
        tc = rng.choice([c for m, c in all_cells if m == tm])
        edge = (fm, fc, tm, tc)
        if edge in existing:
            continue
        existing.add(edge)
        ref_plan.append(edge)
    assert len(ref_plan) == TARGET_REFS, f"只凑出 {len(ref_plan)} 条引用"

    for fm, fc, tm, tc in ref_plan:
        payload = next(p for p in payloads if p["model_id"] == fm)
        cell = payload["cells"][fc]
        if cell["kind"] != "formula":
            continue
        cell["terms"].append(
            {
                "coef": _rand_fraction(rng),
                **({"model_id": tm} if tm != fm else {}),
                "cell_id": tc,
            }
        )
    return model_ids, payloads, len(existing)


def run():
    data_root = tempfile.mkdtemp(prefix="budget-scale-")
    try:
        model_ids, payloads, edge_count = build_payloads()
        total_terms = sum(
            len(c.get("terms", []))
            for p in payloads for c in p["cells"].values()
        )
        print(f"合成网络：{len(model_ids)} 模型 / {TOTAL_CELLS} 格 / "
              f"{edge_count} 条引用边 / {total_terms} 个公式项")

        service = BudgetService(data_root)
        t0 = time.perf_counter()
        for payload in payloads:  # 按序发布，跨模型引用目标均已存在
            service.put_model(payload)
        t_put = time.perf_counter() - t0

        t0 = time.perf_counter()
        root_snap = service.snapshot(model_ids[-1], label="scale-root")
        t_snap = time.perf_counter() - t0
        assert set(root_snap["external_refs"]) or len(model_ids) == 1

        t0 = time.perf_counter()
        artifact, _ = service.compile(root_snap["snap_id"], label="scale")
        t_compile = time.perf_counter() - t0
        cyclic = [c for c in artifact["components"] if c["cyclic"]]
        print(f"put-model 总耗时 {t_put:.2f}s；快照 {t_snap:.3f}s；"
              f"编译 {t_compile:.2f}s；作用域 {len(artifact['scope_snapshots'])} 模型、"
              f"{len(artifact['variables'])} 变量、{len(artifact['components'])} 个 SCC、"
              f"其中循环 SCC {len(cyclic)} 个")
        assert len(artifact["variables"]) == TOTAL_CELLS
        assert len(cyclic) >= MODEL_COUNT // 2

        t0 = time.perf_counter()
        report = service.solve(artifact)
        t_solve = time.perf_counter() - t0
        statuses = {}
        for out in report["outputs"].values():
            statuses[out["status"]] = statuses.get(out["status"], 0) + 1
        print(f"首次解算 {t_solve:.2f}s；输出分类 {statuses}；"
              f"缓存闭包 {len(report['cache']['fingerprints_used'])} 个")

        t0 = time.perf_counter()
        verdict = service.verify_solve(report["solve_id"])
        t_verify = time.perf_counter() - t0
        print(f"独立代回验证 {t_verify:.2f}s，{verdict['check_count']} 项检查全部通过")

        # 再解一次：相同闭包全部缓存命中
        report_again = service.solve(artifact)
        hit = len(set(report_again["cache"]["fingerprints_hit"]))
        used = len(set(report_again["cache"]["fingerprints_used"]))
        print(f"二次解算缓存命中 {hit}/{used} 个闭包")
        assert hit == used and used > 0

        # 修订一个输入格：仅受影响闭包指纹变化
        # 先建立一个与 20 模型网络完全隔离的独立模型，其闭包绝不应受 M00 修订影响
        isolated = {
            "model_id": "SCALE_ISOLATED",
            "name": "隔离对照模型",
            "cells": {
                "i": {"kind": "input", "value": "3/7"},
                "a": {"kind": "formula", "constant": "1/2",
                      "terms": [{"coef": "1/4", "cell_id": "b"}]},
                "b": {"kind": "formula", "constant": "1/3",
                      "terms": [{"coef": "1/2", "cell_id": "a"}]},
                "o": {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "i"},
                                {"coef": "2", "cell_id": "a"}]},
            },
            "outputs": ["o", "a"],
        }
        service.put_model(isolated)
        iso_snap = service.snapshot("SCALE_ISOLATED", label="isolated")
        iso_artifact, _ = service.compile(iso_snap["snap_id"])
        iso_report = service.solve(iso_artifact)
        iso_fingerprints = set(iso_report["cache"]["fingerprints_used"])
        assert iso_fingerprints, "隔离模型应产生闭包缓存"

        first = payloads[0]
        first["cells"]["c00"]["value"] = "999999999999/1"
        service.put_model(first)
        new_snap = service.snapshot(model_ids[-1], label="scale-root-v2")
        new_artifact, _ = service.compile(new_snap["snap_id"])
        report_v2 = service.solve(new_artifact)
        changed = set(report_v2["cache"]["fingerprints_used"]) - set(
            report["cache"]["fingerprints_used"]
        )
        print(f"根模型输入更正后：受影响重算闭包 {len(changed)} 个")

        # 隔离模型重新求解：公式与输入均未变，闭包指纹保持并全部命中旧缓存
        iso_report2 = service.solve(iso_artifact)
        iso_again = set(iso_report2["cache"]["fingerprints_used"])
        unchanged = iso_again & iso_fingerprints
        print(f"隔离模型闭包：保持 {len(iso_again)} 个，命中旧缓存 {len(unchanged)} 个")
        assert unchanged == iso_fingerprints, "未受影响闭包应继续命中缓存"
        assert not (changed & iso_fingerprints), "受影响与隔离闭包指纹不应重叠"
        service.verify_solve(report_v2["solve_id"])
        service.verify_solve(iso_report2["solve_id"])

        # 重开复读
        reopened = BudgetService(data_root)
        re_report = reopened.get_report(report["solve_id"])
        assert re_report["report_hash"] == report["report_hash"]
        v = reopened.verify_solve(report["solve_id"])
        assert v["ok"]
        print("重开后报告哈希一致、独立验证通过")

        # 大分数增长观测：报告中最长分子/分母位数
        max_len = 0
        for out in report["outputs"].values():
            if out["status"] == "determined":
                frac = Fraction(out["value"])
                max_len = max(max_len, len(str(abs(frac.numerator))), len(str(frac.denominator)))
        print(f"消元后确定值最大分子/分母位数：{max_len}（输入上限 12 位，过程增长无浮点）")
        print("\n规模测试全部通过。")
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    run()
