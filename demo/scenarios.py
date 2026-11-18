"""端到端演示：组织变更后的层级预算分解与支出占额。

运行：

    python -m demo.scenarios

每次运行先清空 demo_data/，从零建立两个 SQLite 库，依次演示：

1. 三层树余分分配（下取整 + 余数降序 + 并列按部门 ID）；
2. 带未结承诺的部门迁移（整棵子树移动、换版重算、身份不改）；
3. 新预算不足被拒绝（组织新产品照发，预算仓继续绑旧版）；
4. 支出后部分取消（实际支出不撤销，只释放剩余占额）；
5. 并发承诺争额度（最多一方成功）与同请求重试不二次占额；
6. 大规模场景：20 部门 / 深度 5 / 100 笔支出，独立重算 + 事件级审计。
"""

from __future__ import annotations

import random
import shutil
import threading
from pathlib import Path

from orgbudget import BudgetService, OrgService
from orgbudget.audit import audit_scheme, independent_allocate
from orgbudget.budget import WeightedChild, allocate_children
from orgbudget.errors import CommitmentClosedError, InsufficientFunds

DATA_DIR = Path(__file__).resolve().parent.parent / "demo_data"


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def main() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    org = OrgService(DATA_DIR / "org.db")
    budget = BudgetService(DATA_DIR / "budget.db", org)

    # ------------------------------------------------------------ 1. 余分分配
    _line("案例 1：三层树余分分配（整数分，余数降序、并列按部门 ID）")
    print("  父预算 100 分，三个等权孩子 C/A/B：floor 各 33，余 1 分并列 -> 按 ID 给 A")
    r = allocate_children(100, [WeightedChild("C", 1),
                                WeightedChild("A", 1),
                                WeightedChild("B", 1)])
    print(f"  分配结果: {r}")
    _check("总和严格等于父预算", sum(r.values()) == 100)
    _check("余分按部门 ID 并列规则给 A", r == {"A": 34, "B": 33, "C": 33})

    print("  父预算 11 分，权重 6:3:1 -> floor 6/3/1，余 1 分给余数最大的 6 权重孩子")
    r2 = allocate_children(
        11, [WeightedChild("x", 6), WeightedChild("y", 3), WeightedChild("z", 1)]
    )
    print(f"  分配结果: {r2}")
    _check("余数大的优先补分", r2 == {"x": 7, "y": 3, "z": 1})
    # 与 Fraction 独立实现交叉核对。
    cross = independent_allocate(11, [("x", 6), ("y", 3), ("z", 1)])
    _check("Fraction 独立实现结果一致", cross == r2)

    # ------------------------------------------------------------ 2. 迁移
    _line("案例 2：带未结承诺的部门迁移")
    for dept, parent in [
        ("HQ", None), ("A", "HQ"), ("B", "HQ"),
        ("A1", "A"), ("A2", "A"), ("B1", "B"),
    ]:
        org.create_department(dept, parent)
    v1 = org.publish("初始三层树").version
    p1 = org.get_product(v1)
    weights = {(d, k): 1 for d in p1.department_ids for k in p1.children(d)}
    budget.register_root_budget(v1, 10_000, weights)
    print("  v1 树: HQ -> {A, B}；A -> {A1, A2}；B -> B1；根预算 10000 分，等权分解")
    print(f"  v1 叶预算: A1={budget.budget_of('A1')}, A2={budget.budget_of('A2')}, "
          f"B1={budget.budget_of('B1')}")

    budget.create_commitment("REQ-1", "A1", 2_000)
    budget.register_spend("REQ-1", 700, "SP-1")
    budget.create_commitment("REQ-2", "A2", 500)
    s = budget.summary("A1")
    print(f"  A1 承诺 REQ-1=2000，已支出 700；汇总: 预算 {s.budget} / 支出 {s.spent} / "
          f"未结 {s.open_committed} / 可用 {s.available}")
    _check("叶可用额 = 预算 - 支出 - 未结承诺", s.available == 2500 - 700 - 1300)

    org.move_subtree("A", "B")
    v2 = org.publish("A 整棵子树移到 B 下").version
    p2 = org.get_product(v2)
    print(f"  迁移后 A1 祖先链: {' -> '.join(p2.ancestors('A1'))}")
    new_weights = {(d, k): 1 for d in p2.department_ids for k in p2.children(d)}
    new_weights[("B", "A")] = 3
    new_weights[("B", "B1")] = 1
    scheme2 = budget.revise_budget(v2, 10_000, new_weights)
    c1 = budget.get_commitment("REQ-1")
    print(f"  换版后 REQ-1 身份: 部门={c1.dept_id} 金额={c1.amount} 已支={c1.spent} 剩余={c1.remaining}")
    _check("承诺部门身份未被改挂", c1.dept_id == "A1" and c1.spent == 700)
    b_summary = budget.summary("B")
    print(f"  新树 B 汇总: 预算 {b_summary.budget} / 支出 {b_summary.spent} / 未结 {b_summary.open_committed}")
    _check("迁移后祖先汇总沿新树", b_summary.spent == 700 and b_summary.open_committed == 1800)
    old_a1 = budget.summary("A1", scheme_id=1)
    old_product = org.get_product(v1)
    print(f"  旧方案 v1 冻结: A1 旧预算={old_a1.budget}，旧产品里 A 的父={old_product.parent_of['A']}")
    _check("旧版查询沿旧树、不随新移动改变",
           old_product.parent_of["A"] == "HQ" and old_a1.budget == 2500)
    audit = audit_scheme(budget.db_path, org.db_path, scheme2.scheme_id)
    _check("独立审计通过（新方案）", audit.spend_events == 1 and audit.total_spent == 700,
           f"{len(audit.checks)} 项不变量")

    # ------------------------------------------------------------ 3. 拒绝
    _line("案例 3：新预算不足，整次换版被拒绝")
    org.create_department("C", "HQ")
    org.create_department("C1", "C")
    v3 = org.publish("新增 C/C1").version
    p3 = org.get_product(v3)
    w3 = {(d, k): 1 for d in p3.department_ids for k in p3.children(d)}
    try:
        budget.revise_budget(v3, 100, w3)
        raise AssertionError("应当拒绝")
    except InsufficientFunds as exc:
        print(f"  按预期拒绝: {exc}")
    rejected = budget.rejected_revisions()
    active = budget.active_scheme()
    print(f"  拒绝留痕 {len(rejected)} 条；当前生效方案仍绑定组织 v{active.org_version}")
    _check("拒绝后预算仓继续明确绑定旧版", active.org_version == v2)
    _check("组织新产品不受预算拒绝影响", org.get_product(v3).version == v3)
    _check("迁移部门没有两份额度", budget.summary("A1").budget == budget.budget_of("A1"))

    # ------------------------------------------------------------ 4. 部分取消
    _line("案例 4：支出后部分取消承诺")
    budget.create_commitment("REQ-3", "A2", 400)
    budget.register_spend("REQ-3", 150, "SP-2")
    cancelled = budget.cancel_commitment("REQ-3")
    print(f"  REQ-3 金额 400，支出 150 后取消: 状态={cancelled.status}，"
          f"已支保留={cancelled.spent}，剩余释放={cancelled.remaining}")
    _check("取消不能撤销已实际支出", cancelled.spent == 150 and cancelled.remaining == 250)
    try:
        budget.register_spend("REQ-3", 1, "SP-3")
        raise AssertionError("应当拒绝")
    except CommitmentClosedError:
        print("  取消后再支出被拒绝（CommitmentClosedError）")
    s = budget.summary("A2")
    print(f"  A2 汇总: 预算 {s.budget} / 支出 {s.spent} / 未结 {s.open_committed} / 可用 {s.available}")
    _check("未结占额已释放、支出仍计入", s.spent == 150 and s.open_committed == 500)

    # ------------------------------------------------------------ 5. 并发
    _line("案例 5：并发承诺争剩余额度与同请求重试")
    org.create_department("D", "HQ")
    org.create_department("D1", "D")
    v4 = org.publish("新增并发叶 D1").version
    p4 = org.get_product(v4)
    # 新树 HQ -> {B, C, D}，预算人员显式补齐每条直接父子边的权重：
    # 根层 6:1:3；B 下 5:1 偏向 A 子树，A 下等权，确保 A1 仍拿到 2500。
    w4 = {
        ("HQ", "B"): 6, ("HQ", "C"): 1, ("HQ", "D"): 3,
        ("B", "A"): 5, ("B", "B1"): 1,
        ("A", "A1"): 1, ("A", "A2"): 1,
    }
    for dept in p4.department_ids:
        for kid in p4.children(dept):
            w4.setdefault((dept, kid), 1)
    budget.revise_budget(v4, 10_000, w4)
    d1_budget = budget.budget_of("D1")
    # 单笔超过半数预算：两笔并发不可能同时成立，最多一方成功。
    race_amount = d1_budget * 3 // 5
    print(f"  叶 D1 预算 {d1_budget}，两个连接同时各申请 {race_amount} 承诺")
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def race(request_no: str) -> None:
        svc = BudgetService(DATA_DIR / "budget.db", org)
        barrier.wait()
        try:
            svc.create_commitment(request_no, "D1", race_amount)
            results.append(("ok", request_no))
        except InsufficientFunds:
            results.append(("reject", request_no))

    threads = [threading.Thread(target=race, args=(n,)) for n in ("RA", "RB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"  并发结果: {sorted(results)}")
    _check("最多一方成功", sorted(x[0] for x in results) == ["ok", "reject"])
    _check("只占一份额度", budget.summary("D1").open_committed == race_amount)

    barrier2 = threading.Barrier(2)
    dup: list[object] = []

    def retry_same() -> None:
        svc = BudgetService(DATA_DIR / "budget.db", org)
        barrier2.wait()
        dup.append(svc.create_commitment("DUP-1", "D1", 100).request_no)

    threads = [threading.Thread(target=retry_same) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"  同请求号 DUP-1 并发重试结果: {dup}；承诺条数={len(budget.open_commitments('D1'))}")
    _check("同请求重试不二次占额", dup == ["DUP-1", "DUP-1"]
           and budget.summary("D1").open_committed == race_amount + 100)

    # ------------------------------------------------------------ 6. 大规模
    _line("案例 6：大规模场景 —— 20 部门 / 深度 5 / 100 笔支出")
    big_org = OrgService(DATA_DIR / "big_org.db")
    big_budget = BudgetService(DATA_DIR / "big_budget.db", big_org)
    tree_20 = [
        ("HQ", None),
        ("D1", "HQ"), ("D2", "HQ"), ("D3", "HQ"), ("D4", "HQ"),
        ("D11", "D1"), ("D12", "D1"),
        ("D21", "D2"), ("D22", "D2"),
        ("D31", "D3"), ("D32", "D3"),
        ("D41", "D4"), ("D42", "D4"),
        ("D111", "D11"), ("D211", "D21"), ("D311", "D31"), ("D411", "D41"),
        ("D1111", "D111"), ("D2111", "D211"), ("D11111", "D1111"),
    ]
    for dept, parent in tree_20:
        big_org.create_department(dept, parent)
    bv1 = big_org.publish("20 部门初始树").version
    bp1 = big_org.get_product(bv1)
    rng = random.Random(20261118)
    bw1 = {(d, k): rng.randint(1, 9) for d in bp1.department_ids
           for k in bp1.children(d)}
    root_budget = 100_000_000
    big_budget.register_root_budget(bv1, root_budget, bw1)
    leaves = [d for d in bp1.department_ids if bp1.is_leaf(d)]
    print(f"  部门数={len(bp1.department_ids)} 叶数={len(leaves)} 最大深度={bp1.max_depth()}")

    open_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    seq = 0
    spends = 0
    while spends < 100:
        leaf = rng.choice(leaves)
        if not open_by_leaf[leaf] or rng.random() < 0.7:
            summary = big_budget.summary(leaf)
            if summary.available <= 0:
                continue
            seq += 1
            req = f"BIG-REQ-{seq:04d}"
            big_budget.create_commitment(req, leaf, min(rng.randint(100, 50_000),
                                                        summary.available))
            open_by_leaf[leaf].append(req)
        req = rng.choice(open_by_leaf[leaf])
        c = big_budget.get_commitment(req)
        if c.remaining <= 0:
            continue
        amount = min(rng.randint(50, 5_000), c.remaining)
        spends += 1
        big_budget.register_spend(req, amount, f"BIG-SP-{spends:04d}")
        if rng.random() < 0.125 and c.remaining - amount > 0:
            big_budget.cancel_commitment(req)
            open_by_leaf[leaf].remove(req)
    print(f"  生成承诺 {seq} 笔、实际支出事件 {spends} 笔")

    audit1 = audit_scheme(big_budget.db_path, big_org.db_path)
    print("  独立审计（v1）:")
    for item in audit1.checks:
        print(f"    - {item}")
    _check("100 笔支出全部落账", audit1.spend_events == 100)
    _check("20 部门 / 8 叶 / 深度 5",
           audit1.departments == 20 and audit1.leaves == 8 and bp1.max_depth() == 5)

    big_org.move_subtree("D3", "D4")
    bv2 = big_org.publish("D3 子树迁移到 D4 下").version
    bp2 = big_org.get_product(bv2)
    bw2 = {(d, k): rng.randint(1, 9) for d in bp2.department_ids
           for k in bp2.children(d)}
    scheme2 = big_budget.revise_budget(bv2, root_budget, bw2)
    audit2 = audit_scheme(big_budget.db_path, big_org.db_path, scheme2.scheme_id)
    audit_old = audit_scheme(big_budget.db_path, big_org.db_path, 1)
    print(f"  迁移后 D311 祖先链: {' -> '.join(bp2.ancestors('D311'))}")
    print(f"  v2 审计 {len(audit2.checks)} 项不变量全部通过；"
          f"支出总额 {audit2.total_spent} 分，未结剩余 {audit2.total_open_remaining} 分")
    _check("换版后事件级审计通过", audit2.spend_events == 100
           and audit2.total_spent == audit1.total_spent)
    _check("旧方案仍按旧树审计通过", audit_old.org_version == bv1)

    _line("全部演示完成")
    print(f"  数据库目录: {DATA_DIR}")
    print("  - org.db / budget.db      小案例组织库与预算库")
    print("  - big_org.db / big_budget.db  20 部门大规模场景")


if __name__ == "__main__":
    main()
