"""交付验证入口与自建小例。

覆盖题面要求的全部情形：
1. 独立逐水团传播小例（手算核对每一步体积）；
2. 零延迟汇合（同一时间步内按 DAG 真实依赖求值）；
3. 分流后重汇（多路径份额相加，不按降雨 id 去重）；
4. 核尾（峰值过后的延迟尾部不被 R 截断）；
5. 出口损失比例（离开本模型体积）；
6. 降雨格修订（显式版本，峰值时刻随真实调用改变，旧分析绑定旧产品）；
7. INCOMPLETE（缺格不等于零）；
8. 发布失败不改变旧产品；
9. 外部同格式网络核产品交换，身份一致且脉冲查询不依赖仓库。

运行：``python -m hydro.validate``
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Tuple

from .catalog import Catalog, CatalogDraft
from .engine import AnalysisRequest, Engine
from .explain import SourceExplainer
from .rainfall import RainRepo
from .rational import F
from .storage import Store

ZERO = Fraction(0)


# ----------------------------------------------------------------------
# 案例 1：独立逐水团传播小例
# a --d1,p1--> b --d0,p1--> c；b 有 1/2 出口损失；核均为 [1]（瞬时）
# 面积 A_a=2, A_b=3, A_c=5；仅 a 在 t=0 有 r-l=1
# 手算：
#   t0: a 本地体积 2*1=2，出流 2；边->b 延迟1 当天不到
#   t1: b 收到 2；b 出流 2；离开 1（1/2），边->c(零延迟) 传 1
#       同一步 c 即收到 1，c 出流 1，c 全离开 1
# 守恒：输入 2 = b 离开 1 + c 离开 1
# ----------------------------------------------------------------------
def build_parcel_catalog() -> Catalog:
    d = CatalogDraft()
    d.add_node("a", 2, [1])
    d.add_node("b", 3, [1])
    d.add_node("c", 5, [1])
    d.add_edge("a", "b", 1, 1)
    d.add_edge("b", "c", 0, F("1/2"))  # b 另 1/2 离开模型
    return d.publish()


def case_parcel_propagation(store: Store) -> List[str]:
    log: List[str] = []
    cat = build_parcel_catalog()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    cells = {
        ("a", 0): (F(1), 0), ("a", 1): (0, 0),
        ("b", 0): (0, 0), ("b", 1): (0, 0),
        ("c", 0): (0, 0), ("c", 1): (0, 0),
    }
    rain = repo.import_batch(0, 2, ["a", "b", "c"], cells)
    eng = Engine(cat)
    rep = eng.analyze(AnalysisRequest.make(("a", "b", "c"), 0, 2), rain, store)

    assert rep.status == "OK" and rep.conserved, "案例1 应守恒"
    ra, rb, rc = (rep.node_reports[n] for n in ("a", "b", "c"))
    # 绝对时间下标：start=0
    assert ra.at("outflow", 0) == 2, ra.at("outflow", 0)
    assert rb.at("upstream", 1) == 2 and rb.at("outflow", 1) == 2
    assert rb.at("leave", 1) == 1
    assert rc.at("upstream", 1) == 1 and rc.at("outflow", 1) == 1
    assert rc.at("leave", 1) == 1
    assert rep.input_volume_total == 2 and rep.leave_volume_total == 2
    assert rep.residual == 0
    # t0 的 b/c 必须为零（延迟未到，不能提前出现）
    assert rb.at("outflow", 0) == 0 and rc.at("outflow", 0) == 0
    log.append("案例1 逐水团传播：手算 2 ->(d1) 2 ->(d0,p1/2) 1，守恒 2=1+1 ✓")

    # 来源分解：c@t=1 的水来自 a@t=0，路径 a->b->c
    ex = SourceExplainer(cat, rain).explain("c", 1)
    assert len(ex.paths) == 1
    pc = ex.paths[0]
    assert pc.origin == "a" and pc.input_step == 0
    assert pc.path == ("a", "b", "c") and pc.path_delay == 1
    assert pc.path_proportion == F("1/2") and pc.volume == 1
    assert ex.by_source == {("a", 0): 1} and ex.verify()
    log.append("案例1 来源分解：c@t=1 全部来自 a@t=0，路径 a-b-c，份额 1/2 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 2：零延迟汇合
# x,y 同一步零延迟汇入 z；x@0、y@0 同时给脉冲，z@0 当步即见两者之和
# ----------------------------------------------------------------------
def case_zero_delay_confluence(store: Store) -> List[str]:
    log: List[str] = []
    d = CatalogDraft()
    d.add_node("x", 1, [1])
    d.add_node("y", 1, [1])
    d.add_node("z", 1, [1])
    d.add_edge("x", "z", 0, 1)
    d.add_edge("y", "z", 0, 1)
    cat = d.publish()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    cells = {}
    for n in ("x", "y", "z"):
        cells[(n, 0)] = (F(1) if n in ("x", "y") else 0, 0)
    rain = repo.import_batch(0, 1, ["x", "y", "z"], cells)
    rep = Engine(cat).analyze(AnalysisRequest.make(("z",), 0, 1), rain)
    rz = rep.node_reports["z"]
    assert rz.at("upstream", 0) == 2 and rz.at("outflow", 0) == 2
    assert rz.at("local", 0) == 0  # 上游来水不乘 z 的面积/核
    assert rep.conserved and rep.input_volume_total == 2
    ex = SourceExplainer(cat, rain).explain("z", 0)
    assert set(ex.by_source) == {("x", 0), ("y", 0)}
    assert ex.outflow == 2 and ex.verify()
    log.append("案例2 零延迟汇合：z@0 同一步收到 x+y=2，上游不乘 z 面积/核 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 3：分流后重汇
# s 分两路 s->m1->g 与 s->m2->g（延迟不同），g 收到两个真实路径份额，
# 即使来源是同一个降雨格也不去重。
# ----------------------------------------------------------------------
def case_split_remerge(store: Store) -> List[str]:
    log: List[str] = []
    d = CatalogDraft()
    d.add_node("s", 1, [1])
    d.add_node("m1", 1, [1])
    d.add_node("m2", 1, [1])
    d.add_node("g", 1, [1])
    d.add_edge("s", "m1", 0, F("1/3"))
    d.add_edge("s", "m2", 0, F("2/3"))
    d.add_edge("m1", "g", 1, 1)
    d.add_edge("m2", "g", 2, 1)
    cat = d.publish()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    cells = {(n, 0): (0, 0) for n in ("s", "m1", "m2", "g")}
    cells[("s", 0)] = (F(1), 0)
    rain = repo.import_batch(0, 1, ["s", "m1", "m2", "g"], cells)
    rep = Engine(cat).analyze(AnalysisRequest.make(("g",), 0, 1), rain)
    rg = rep.node_reports["g"]
    assert rg.at("outflow", 1) == F("1/3")   # 路径1 延迟1
    assert rg.at("outflow", 2) == F("2/3")   # 路径2 延迟2
    assert rep.horizon_end >= 2              # 尾期延伸到最长延迟
    ex = SourceExplainer(cat, rain)
    e1 = ex.explain("g", 1)
    e2 = ex.explain("g", 2)
    assert len(e1.paths) == 1 and e1.paths[0].path == ("s", "m1", "g")
    assert len(e2.paths) == 1 and e2.paths[0].path == ("s", "m2", "g")
    # 同一降雨格 (s,0)，两条路径各自列示、不去重
    assert e1.by_source == {("s", 0): F("1/3")}
    assert e2.by_source == {("s", 0): F("2/3")}
    assert rep.conserved and rep.leave_volume_total == 1
    log.append("案例3 分流重汇：同一雨格 s@0 经两条路径分别于 t1(1/3)、t2(2/3) 到达，不去重 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 4 + 5：核尾与出口损失
# q 核 [1/4, 1/2, 1/4]（长 3），面积 4；q->o 延迟 0、比例 3/4，
# q 自身 1/4 离开；o 全离开。窗口只有 1 步，输出必须延伸过核尾。
# 输入 r-l=1 @t0 => 本地体积 4；q 出流 [1,2,1]（t0,t1,t2）
# ----------------------------------------------------------------------
def case_kernel_tail_and_loss(store: Store) -> List[str]:
    log: List[str] = []
    d = CatalogDraft()
    d.add_node("q", 4, [F("1/4"), F("1/2"), F("1/4")])
    d.add_node("o", 1, [1])
    d.add_edge("q", "o", 0, F("3/4"))
    cat = d.publish()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    rain = repo.import_batch(
        0, 1, ["q", "o"], {("q", 0): (1, 0), ("o", 0): (0, 0)}
    )
    rep = Engine(cat).analyze(AnalysisRequest.make(("q", "o"), 0, 1), rain)
    rq, ro = rep.node_reports["q"], rep.node_reports["o"]
    assert [rq.at("outflow", t) for t in range(3)] == [1, 2, 1]
    assert rq.peak_steps == [1] and rq.peak_outflow == 2
    # R=1 但时域必须延伸到 t2（核尾），不能在 R 截断
    assert rep.horizon_end == 2 and len(rq.outflow) == 3
    assert [rq.at("leave", t) for t in range(3)] == [F("1/4"), F("1/2"), F("1/4")]
    assert [ro.at("outflow", t) for t in range(3)] == [F("3/4"), F("3/2"), F("3/4")]
    assert rep.input_volume_total == 4
    assert rep.leave_volume_total == 4  # q 离开 1 + o 离开 3
    assert rep.conserved
    log.append("案例4/5 核尾+出口损失：窗口1步但曲线延伸至t2；输入4 = q离开1 + o离开3 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 6：降雨修订改变峰值时刻；旧分析绑定旧产品
# u 核 [1/2,1/2] 面积2；把雨格 t0->0、t1->1，峰值时刻从 0/1 平移到 1/2
# ----------------------------------------------------------------------
def case_revision(store: Store) -> List[str]:
    log: List[str] = []
    d = CatalogDraft()
    d.add_node("u", 2, [F("1/2"), F("1/2")])
    cat = d.publish()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    rain_v1 = repo.import_batch(
        0, 2, ["u"], {("u", 0): (1, 0), ("u", 1): (0, 0)}
    )
    eng = Engine(cat)
    rep_v1 = eng.analyze(AnalysisRequest.make(("u",), 0, 2), rain_v1, store)
    assert rep_v1.node_reports["u"].peak_steps == [0, 1]  # 并列峰值全部返回

    rain_v2 = repo.revise_cell(rain_v1, "u", 0, rain=0)
    rain_v2 = repo.revise_cell(rain_v2, "u", 1, rain=1)
    assert rain_v2.rain_hash != rain_v1.rain_hash
    # 链式修订：v2 的直接父版本是上一步的中间版本，最终溯源到 v1
    chain = [rain_v2.rain_hash]
    parent = rain_v2.parent_hash
    while parent is not None:
        chain.append(parent)
        parent = store.load_rain_meta(parent)["parent_hash"]
    assert chain[-1] == rain_v1.rain_hash
    rep_v2 = eng.analyze(AnalysisRequest.make(("u",), 0, 2), rain_v2, store)
    assert rep_v2.node_reports["u"].peak_steps == [1, 2]  # 峰值时刻真实移动
    assert rep_v1.rain_hash != rep_v2.rain_hash and rep_v1.analysis_id != rep_v2.analysis_id

    # 旧档案仍在仓中且绑定旧降雨哈希
    old_row = store.load_analysis(rep_v1.analysis_id)
    assert old_row["rain_hash"] == rain_v1.rain_hash
    assert old_row["status"] == "OK"
    log.append("案例6 降雨修订：峰值时刻 [0,1] -> [1,2]；新版本新曲线，旧分析仍绑定旧雨档 ✓")

    # 核修订同样改变曲线：换成瞬时核后峰值收束到单点
    d2 = CatalogDraft.from_catalog(cat).replace_kernel("u", [1])
    cat2 = d2.publish()
    assert cat2.content_hash != cat.content_hash
    repo2 = RainRepo(store, cat2)
    rain2 = repo2.import_batch(
        0, 2, ["u"], {("u", 0): (1, 0), ("u", 1): (0, 0)}
    )
    rep2 = Engine(cat2).analyze(AnalysisRequest.make(("u",), 0, 2), rain2)
    assert rep2.node_reports["u"].peak_steps == [0]
    log.append("案例6b 核修订：新核 [1] 使峰值时刻变为 [0]，目录身份改变、旧目录不变 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 7：INCOMPLETE —— 缺格不等于零
# ----------------------------------------------------------------------
def case_incomplete(store: Store) -> List[str]:
    log: List[str] = []
    cat = build_parcel_catalog()
    store.save_catalog(cat)
    repo = RainRepo(store, cat)
    # 只导入 a 的数据，请求 b（闭包需要 a+b），b 缺格
    rain = repo.import_batch(0, 2, ["a"], {("a", 0): (1, 0), ("a", 1): (0, 0)})
    rep = Engine(cat).analyze(AnalysisRequest.make(("b",), 0, 2), rain, store)
    assert rep.status == "INCOMPLETE" and rep.incomplete
    assert ("b", 0) in rep.missing_cells and ("b", 1) in rep.missing_cells
    assert rep.node_reports == {}  # 不产生任何水文曲线
    row = store.load_analysis(rep.analysis_id)
    assert row["status"] == "INCOMPLETE" and row["report_json"] is None
    log.append("案例7 INCOMPLETE：缺 b 格即拒绝补零，不产出曲线，档案状态 INCOMPLETE ✓")
    return log


# ----------------------------------------------------------------------
# 案例 8：发布失败不改变旧产品（环、核和≠1、比例和>1）
# ----------------------------------------------------------------------
def case_publish_failure() -> List[str]:
    log: List[str] = []
    good = build_parcel_catalog()
    h0 = good.content_hash

    bad_ring = CatalogDraft.from_catalog(good).add_edge("c", "a", 0, F("1/4"))
    try:
        bad_ring.publish()
        raise AssertionError("环目录必须发布失败")
    except Exception as ex:  # noqa: BLE001
        assert "环" in str(ex)

    bad_kernel = CatalogDraft.from_catalog(good).replace_kernel("a", [F("1/2"), F("1/3")])
    try:
        bad_kernel.publish()
        raise AssertionError("核和不为1必须发布失败")
    except Exception as ex:  # noqa: BLE001
        assert "核之和" in str(ex)

    bad_prop = CatalogDraft.from_catalog(good)
    bad_prop.add_edge("a", "c", 0, 1)  # a 原出边比例已是1，再加即超
    try:
        bad_prop.publish()
        raise AssertionError("比例和>1必须发布失败")
    except Exception as ex:  # noqa: BLE001
        assert "比例之和" in str(ex)

    assert good.content_hash == h0 and len(good.edges) == 2
    log.append("案例8 发布失败：环/核和≠1/比例和>1 全部拒绝，旧冻结产品哈希不变 ✓")
    return log


# ----------------------------------------------------------------------
# 案例 9：外部同格式产品交换 + 目录脉冲查询独立于仓库
# ----------------------------------------------------------------------
def case_external_product() -> List[str]:
    log: List[str] = []
    cat = build_parcel_catalog()
    payload = cat.to_payload()
    import json as _json

    blob = _json.dumps(payload, ensure_ascii=False)
    restored = Catalog.from_payload(_json.loads(blob), verify_hash=cat.content_hash)
    assert restored.content_hash == cat.content_hash
    # 不经过任何仓库/降雨，直接做单位脉冲查询
    pulse = restored.unit_pulse("a")
    assert pulse["a"] == {0: 1}
    assert pulse["b"] == {1: 1}
    assert pulse["c"] == {1: F("1/2")}  # b 的 1/2 出口损失
    # 自动时域覆盖核尾+最长延迟；全网守恒：脉冲总量1 = 离开总量
    total_leaving = sum(
        sum(seq.values(), ZERO) * restored.leave_fraction(nid)
        for nid, seq in pulse.items()
    )
    assert total_leaving == 1
    log.append("案例9 外部产品：JSON 往返身份一致；脉冲查询 a@0 -> b@1 -> c@1(1/2)，不依赖仓库 ✓")
    return log


def run_demo(store_path: str = ":memory:") -> Tuple[Store, List[str]]:
    """运行全部自建案例，返回事实仓与日志。"""
    store = Store(store_path)
    logs: List[str] = []
    logs += case_parcel_propagation(store)
    logs += case_zero_delay_confluence(store)
    logs += case_split_remerge(store)
    logs += case_kernel_tail_and_loss(store)
    logs += case_revision(store)
    logs += case_incomplete(store)
    logs += case_publish_failure()
    logs += case_external_product()
    return store, logs


def main() -> int:
    store, logs = run_demo()
    print("=" * 72)
    print("降雨响应核与河网汇流事实仓 —— 交付验证")
    print("=" * 72)
    for line in logs:
        print(" ", line)
    print("-" * 72)
    print(f"全部 {len(logs)} 个案例断言通过；SQLite 档案：")
    for r in store.list_analyses():
        print(f"  analysis {r['analysis_id'][:12]}… status={r['status']}")
    print(f"目录版本 {len(store.list_catalogs())} 个，降雨版本 {len(store.list_rain())} 个")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
