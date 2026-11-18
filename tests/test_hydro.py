"""unittest 全量测试（标准库，无需 pytest）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import unittest
from fractions import Fraction
from unittest import mock

from hydro import graph
from hydro.catalog import Catalog, CatalogDraft, MAX_KERNEL, MAX_NODES, MAX_OUT_EDGES
from hydro.cli import main as cli_main
from hydro.engine import AnalysisRequest, Engine
from hydro.errors import (
    AnalysisError,
    CatalogError,
    ExplainError,
    RainError,
    StoreError,
)
from hydro.explain import SourceExplainer
from hydro.rainfall import RainRepo, MAX_INPUT_STEPS
from hydro.rational import F, frac
from hydro.storage import Store
from hydro.validate import run_demo

F0 = Fraction(0)


def three_node_catalog():
    d = CatalogDraft()
    d.add_node("a", 2, [1])
    d.add_node("b", 3, [1])
    d.add_node("c", 5, [1])
    d.add_edge("a", "b", 1, 1)
    d.add_edge("b", "c", 0, F("1/2"))
    return d.publish()


class RationalTests(unittest.TestCase):
    def test_roundtrip_and_negative(self):
        self.assertEqual(frac("3/4"), Fraction(3, 4))
        self.assertEqual(frac(5), Fraction(5))
        self.assertEqual(frac(Fraction(1, 7)), Fraction(1, 7))
        self.assertEqual(str(frac("-2/9")), "-2/9")

    def test_float_rejected(self):
        with self.assertRaises(TypeError):
            frac(0.25)

    def test_bad_text(self):
        with self.assertRaises((ValueError, ZeroDivisionError)):
            frac("1/0")


class CatalogTests(unittest.TestCase):
    def test_kernel_must_sum_to_one(self):
        d = CatalogDraft()
        d.add_node("a", 1, [F("1/2"), F("1/3")])
        with self.assertRaises(CatalogError):
            d.publish()

    def test_negative_kernel_rejected_at_publish(self):
        d = CatalogDraft()
        d.add_node("a", 1, [F("2"), F("-1")])
        with self.assertRaises(CatalogError):
            d.publish()

    def test_nonpositive_area_rejected(self):
        d = CatalogDraft()
        d.add_node("a", 0, [1])
        with self.assertRaises(CatalogError):
            d.publish()

    def test_proportion_sum_cap(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 0, 1)
        d.add_node("c", 1, [1])
        d.add_edge("a", "c", 0, F("1/2"))  # 和 = 1.5
        with self.assertRaises(CatalogError):
            d.publish()

    def test_cycle_rejected(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 0, 1)
        d.add_edge("b", "a", 0, 1)
        with self.assertRaises(ValueError):
            d.publish()

    def test_self_loop_rejected(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_edge("a", "a", 0, 1)
        with self.assertRaises(CatalogError):
            d.publish()

    def test_negative_delay_rejected(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", -1, 1)
        with self.assertRaises(CatalogError):
            d.publish()

    def test_limits(self):
        d = CatalogDraft()
        for i in range(MAX_NODES):
            d.add_node(f"n{i}", 1, [1])
        with self.assertRaises(CatalogError):
            d.add_node("extra", 1, [1])
        d2 = CatalogDraft()
        d2.add_node("hub", 1, [1])
        for i in range(MAX_OUT_EDGES):
            d2.add_node(f"m{i}", 1, [1])
            d2.add_edge("hub", f"m{i}", 0, F("1/3"))
        d2.add_node("m3", 1, [1])
        with self.assertRaises(CatalogError):
            d2.add_edge("hub", "m3", 0, F("1/9"))
        d3 = CatalogDraft()
        d3.add_node("x", 1, [F(1)] + [0] * MAX_KERNEL)  # 21 项
        with self.assertRaises(CatalogError):
            d3.publish()

    def test_publish_failure_keeps_old_product(self):
        good = three_node_catalog()
        h = good.content_hash
        draft = CatalogDraft.from_catalog(good).add_edge("c", "a", 0, 1)
        with self.assertRaises(ValueError):
            draft.publish()
        self.assertEqual(good.content_hash, h)
        self.assertEqual(len(good.edges), 2)

    def test_identity_deterministic_and_distinct(self):
        c1 = three_node_catalog()
        c2 = three_node_catalog()
        self.assertEqual(c1.content_hash, c2.content_hash)
        c3 = CatalogDraft.from_catalog(c1).replace_kernel("a", [F("1/2"), F("1/2")]).publish()
        self.assertNotEqual(c1.content_hash, c3.content_hash)

    def test_leave_fraction(self):
        cat = three_node_catalog()
        self.assertEqual(cat.leave_fraction("a"), 0)
        self.assertEqual(cat.leave_fraction("b"), F("1/2"))
        self.assertEqual(cat.leave_fraction("c"), 1)

    def test_unit_pulse_independent_of_repo(self):
        cat = three_node_catalog()
        pulse = cat.unit_pulse("a")
        self.assertEqual(pulse["a"], {0: 1})
        self.assertEqual(pulse["b"], {1: 1})
        self.assertEqual(pulse["c"], {1: F("1/2")})
        # 守恒：脉冲 1 个体积最终全部「离开」
        total = F0
        for nid, seq in pulse.items():
            total += sum(seq.values(), F0) * cat.leave_fraction(nid)
        self.assertEqual(total, 1)

    def test_unit_pulse_with_kernel_tail(self):
        d = CatalogDraft()
        d.add_node("s", 1, [F("1/4"), F("3/4")])
        cat = d.publish()
        pulse = cat.unit_pulse("s")
        self.assertEqual(pulse["s"], {0: F("1/4"), 1: F("3/4")})

    def test_longest_delay_matrix_numpy_vs_python(self):
        cat = three_node_catalog()
        with mock.patch.object(graph, "_HAVE_NUMPY", False):
            mat_py, idx = graph.longest_delay_matrix(cat.node_ids, cat._out, cat.order)
        mat_np, _ = graph.longest_delay_matrix(cat.node_ids, cat._out, cat.order)
        for i, ni in enumerate(cat.node_ids):
            for j, nj in enumerate(cat.node_ids):
                self.assertEqual(int(mat_py[i][j]), int(mat_np[idx[ni], idx[nj]]))
        self.assertEqual(cat.longest_delay("a", "c"), 1)
        self.assertIsNone(cat.longest_delay("c", "a"))
        self.assertEqual(cat.longest_delay("a", "a"), 0)


class RainfallTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cat = three_node_catalog()
        self.repo = RainRepo(self.store, self.cat)

    def tearDown(self):
        self.store.close()

    def _rect(self, values=None):
        values = values or {}
        cells = {}
        for n in ("a", "b", "c"):
            for t in range(2):
                cells[(n, t)] = values.get((n, t), (0, 0))
        return cells

    def test_missing_cell_rejected(self):
        cells = self._rect()
        del cells[("b", 1)]
        with self.assertRaises(RainError):
            self.repo.import_batch(0, 2, ["a", "b", "c"], cells)

    def test_negative_cell_rejected(self):
        cells = self._rect({("a", 0): (-1, 0)})
        with self.assertRaises(RainError):
            self.repo.import_batch(0, 2, ["a", "b", "c"], cells)

    def test_step_limit(self):
        with self.assertRaises(RainError):
            self.repo.import_batch(0, MAX_INPUT_STEPS + 1, ["a"],
                                  {("a", t): (0, 0) for t in range(MAX_INPUT_STEPS + 1)})

    def test_revision_creates_version_and_keeps_old(self):
        v1 = self.repo.import_batch(0, 2, ["a", "b", "c"], self._rect({("a", 0): (1, 0)}))
        v2 = self.repo.revise_cell(v1, "a", 0, rain=0)
        self.assertNotEqual(v1.rain_hash, v2.rain_hash)
        self.assertEqual(v2.parent_hash, v1.rain_hash)
        # 旧版本仍可加载且内容不变
        loaded = self.repo.load(v1.rain_hash)
        self.assertEqual(loaded.cell("a", 0), (1, 0))
        self.assertEqual(self.repo.load(v2.rain_hash).cell("a", 0), (0, 0))

    def test_noop_revision_rejected(self):
        v1 = self.repo.import_batch(0, 1, ["a"], {("a", 0): (1, 0)})
        with self.assertRaises(RainError):
            self.repo.revise_cell(v1, "a", 0, rain=1)

    def test_content_addressing_idempotent(self):
        cells = self._rect()
        b1 = self.repo.import_batch(0, 2, ["a", "b", "c"], cells)
        b2 = self.repo.import_batch(0, 2, ["a", "b", "c"], cells)
        self.assertEqual(b1.rain_hash, b2.rain_hash)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cat = three_node_catalog()
        self.repo = RainRepo(self.store, self.cat)

    def _rain(self, values):
        cells = {}
        for n in ("a", "b", "c"):
            for t in range(2):
                cells[(n, t)] = values.get((n, t), (0, 0))
        return self.repo.import_batch(0, 2, ["a", "b", "c"], cells)

    def test_hand_propagation_and_conservation(self):
        rain = self._rain({("a", 0): (1, 0)})
        rep = Engine(self.cat).analyze(
            AnalysisRequest.make(("a", "b", "c"), 0, 2), rain, self.store
        )
        self.assertTrue(rep.conserved)
        self.assertEqual(rep.input_volume_total, 2)
        self.assertEqual(rep.leave_volume_total, 2)
        self.assertEqual(rep.node_reports["a"].at("outflow", 0), 2)
        self.assertEqual(rep.node_reports["b"].at("outflow", 1), 2)
        self.assertEqual(rep.node_reports["c"].at("outflow", 1), 1)

    def test_effective_is_max_r_minus_l(self):
        # r < l 时有效量为 0，本地体积必须为 0
        rain = self._rain({("a", 0): (F("1/3"), F("2/3"))})
        rep = Engine(self.cat).analyze(AnalysisRequest.make(("a",), 0, 2), rain)
        self.assertEqual(rep.node_reports["a"].local_volume_total, 0)
        # r-l = 1/6，面积 2 => 体积 1/3
        rain2 = self._rain({("a", 0): (F("2/3"), F("1/2"))})
        rep2 = Engine(self.cat).analyze(AnalysisRequest.make(("a",), 0, 2), rain2)
        self.assertEqual(rep2.node_reports["a"].local_volume_total, F("1/3"))

    def test_tail_extends_beyond_R(self):
        d = CatalogDraft()
        d.add_node("q", 4, [F("1/4"), F("1/2"), F("1/4")])
        cat = d.publish()
        self.store.save_catalog(cat)
        repo = RainRepo(self.store, cat)
        rain = repo.import_batch(0, 1, ["q"], {("q", 0): (1, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("q",), 0, 1), rain)
        self.assertEqual(rep.horizon_end, 2)
        self.assertEqual(
            [rep.node_reports["q"].at("outflow", t) for t in range(3)], [1, 2, 1]
        )

    def test_tied_peaks_all_steps(self):
        d = CatalogDraft()
        d.add_node("u", 2, [F("1/2"), F("1/2")])
        cat = d.publish()
        self.store.save_catalog(cat)
        repo = RainRepo(self.store, cat)
        rain = repo.import_batch(0, 2, ["u"], {("u", 0): (1, 0), ("u", 1): (0, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("u",), 0, 2), rain)
        self.assertEqual(rep.node_reports["u"].peak_steps, [0, 1])

    def test_incomplete_no_curves(self):
        rain = self.repo.import_batch(0, 2, ["a"], {("a", 0): (1, 0), ("a", 1): (0, 0)})
        rep = Engine(self.cat).analyze(AnalysisRequest.make(("b",), 0, 2), rain, self.store)
        self.assertEqual(rep.status, "INCOMPLETE")
        self.assertTrue(rep.incomplete)
        self.assertIn(("b", 0), rep.missing_cells)
        self.assertEqual(rep.node_reports, {})

    def test_identity_mismatch(self):
        other = CatalogDraft().add_node("z", 1, [1]).publish()
        repo2 = RainRepo(Store(":memory:"), other)
        rain = repo2.import_batch(0, 1, ["z"], {("z", 0): (1, 0)})
        with self.assertRaises(AnalysisError):
            Engine(self.cat).analyze(AnalysisRequest.make(("a",), 0, 1), rain)

    def test_zero_delay_same_step_dependency(self):
        d = CatalogDraft()
        d.add_node("x", 1, [1])
        d.add_node("y", 1, [1])
        d.add_node("z", 1, [1])
        d.add_edge("x", "z", 0, 1)
        d.add_edge("y", "z", 0, 1)
        cat = d.publish()
        self.store.save_catalog(cat)
        repo = RainRepo(self.store, cat)
        cells = {("x", 0): (1, 0), ("y", 0): (1, 0), ("z", 0): (0, 0)}
        rain = repo.import_batch(0, 1, ["x", "y", "z"], cells)
        rep = Engine(cat).analyze(AnalysisRequest.make(("z",), 0, 1), rain)
        self.assertEqual(rep.node_reports["z"].at("outflow", 0), 2)

    def test_split_remerge_no_dedup(self):
        d = CatalogDraft()
        for n in ("s", "m1", "m2", "g"):
            d.add_node(n, 1, [1])
        d.add_edge("s", "m1", 0, F("1/3"))
        d.add_edge("s", "m2", 0, F("2/3"))
        d.add_edge("m1", "g", 1, 1)
        d.add_edge("m2", "g", 2, 1)
        cat = d.publish()
        self.store.save_catalog(cat)
        repo = RainRepo(self.store, cat)
        cells = {(n, 0): (0, 0) for n in ("s", "m1", "m2", "g")}
        cells[("s", 0)] = (1, 0)
        rain = repo.import_batch(0, 1, ["s", "m1", "m2", "g"], cells)
        rep = Engine(cat).analyze(AnalysisRequest.make(("g",), 0, 1), rain)
        g = rep.node_reports["g"]
        self.assertEqual(g.at("outflow", 1), F("1/3"))
        self.assertEqual(g.at("outflow", 2), F("2/3"))
        self.assertTrue(rep.conserved)

    def test_analysis_immutability_binding(self):
        rain = self._rain({("a", 0): (1, 0)})
        eng = Engine(self.cat)
        rep1 = eng.analyze(AnalysisRequest.make(("c",), 0, 2), rain, self.store)
        rain2 = self.repo.revise_cell(rain, "a", 0, rain=0)
        rep2 = eng.analyze(AnalysisRequest.make(("c",), 0, 2), rain2, self.store)
        self.assertNotEqual(rep1.analysis_id, rep2.analysis_id)
        row = self.store.load_analysis(rep1.analysis_id)
        self.assertEqual(row["rain_hash"], rain.rain_hash)
        archived = json.loads(row["report_json"])
        self.assertEqual(archived["rain_hash"], rain.rain_hash)

    def test_edge_delay_revision_moves_peak_time(self):
        # 题面：边延迟修订必须通过真实调用改变曲线和峰值时刻
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 1, 1)
        cat_v1 = d.publish()
        store = Store(":memory:")
        repo = RainRepo(store, cat_v1)
        rain = repo.import_batch(0, 1, ["a", "b"],
                                 {("a", 0): (1, 0), ("b", 0): (0, 0)})
        rep_v1 = Engine(cat_v1).analyze(
            AnalysisRequest.make(("b",), 0, 1), rain
        )
        self.assertEqual(rep_v1.node_reports["b"].peak_steps, [1])

        # 修订边延迟 1 -> 3，发布为新目录产品
        cat_v2 = (
            CatalogDraft.from_catalog(cat_v1)
            .remove_edge("a", "b", 1)
            .add_edge("a", "b", 3, 1)
            .publish()
        )
        self.assertNotEqual(cat_v1.content_hash, cat_v2.content_hash)
        repo2 = RainRepo(store, cat_v2)
        rain2 = repo2.import_batch(0, 1, ["a", "b"],
                                   {("a", 0): (1, 0), ("b", 0): (0, 0)})
        rep_v2 = Engine(cat_v2).analyze(
            AnalysisRequest.make(("b",), 0, 1), rain2
        )
        self.assertEqual(rep_v2.node_reports["b"].peak_steps, [3])
        # 时域尾部随延迟拉长
        self.assertGreater(rep_v2.horizon_end, rep_v1.horizon_end)
        self.assertTrue(rep_v2.conserved)
        # 旧目录产品仍在仓中且身份不变
        self.assertEqual(store.load_catalog(cat_v1.content_hash).content_hash,
                         cat_v1.content_hash)
        store.close()


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        d = CatalogDraft()
        for n in ("s", "m1", "m2", "g"):
            d.add_node(n, 1, [1])
        d.add_edge("s", "m1", 0, F("1/3"))
        d.add_edge("s", "m2", 0, F("2/3"))
        d.add_edge("m1", "g", 1, 1)
        d.add_edge("m2", "g", 2, 1)
        self.cat = d.publish()
        self.repo = RainRepo(self.store, self.cat)
        cells = {(n, 0): (0, 0) for n in ("s", "m1", "m2", "g")}
        cells[("s", 0)] = (1, 0)
        self.rain = self.repo.import_batch(0, 1, ["s", "m1", "m2", "g"], cells)
        self.engine = Engine(self.cat)

    def test_path_level_breakdown(self):
        ex = SourceExplainer(self.cat, self.rain)
        e1 = ex.explain("g", 1)
        self.assertEqual(len(e1.paths), 1)
        self.assertEqual(e1.paths[0].path, ("s", "m1", "g"))
        self.assertEqual(e1.outflow, F("1/3"))
        e2 = ex.explain("g", 2)
        self.assertEqual(e2.paths[0].path, ("s", "m2", "g"))
        self.assertEqual(e2.outflow, F("2/3"))

    def test_cross_check_matches_report_every_step(self):
        rep = self.engine.analyze(
            AnalysisRequest.make(("g", "m1", "m2"), 0, 1), self.rain
        )
        diffs = SourceExplainer(self.cat, self.rain).cross_check(rep)
        for _, _, explained, reported in diffs:
            self.assertEqual(explained, reported)

    def test_kernel_source_attribution(self):
        d = CatalogDraft()
        d.add_node("s", 2, [F("1/2"), F("1/2")])
        d.add_node("g", 1, [1])
        d.add_edge("s", "g", 1, 1)
        cat = d.publish()
        store = Store(":memory:")
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 1, ["s", "g"], {("s", 0): (1, 0), ("g", 0): (0, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("g",), 0, 1), rain)
        # 本地体积 2，核各半 => 1@t0,1@t1，边延迟1 => g 在 t1、t2 各收到 1
        self.assertEqual(rep.node_reports["g"].at("outflow", 1), 1)
        self.assertEqual(rep.node_reports["g"].at("outflow", 2), 1)
        ex = SourceExplainer(cat, rain).explain("g", 2)
        self.assertEqual(ex.by_source, {("s", 0): 1})
        self.assertEqual(ex.paths[0].kernel_index, 1)
        store.close()

    def test_explain_incomplete_report(self):
        store = Store(":memory:")
        cat = three_node_catalog()
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 1, ["a"], {("a", 0): (1, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("b",), 0, 1), rain)
        with self.assertRaises(ExplainError):
            SourceExplainer(cat, rain).cross_check(rep)
        store.close()


class BoundaryTests(unittest.TestCase):
    def _store_cat(self, draft):
        store = Store(":memory:")
        cat = draft.publish()
        store.save_catalog(cat)
        return store, cat

    def test_all_zero_rain_conserved_and_no_peak(self):
        d = CatalogDraft()
        d.add_node("a", 2, [F("1/2"), F("1/2")])
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 3, ["a"], {("a", t): (0, 0) for t in range(3)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("a",), 0, 3), rain)
        self.assertTrue(rep.conserved)
        self.assertEqual(rep.input_volume_total, 0)
        self.assertEqual(rep.leave_volume_total, 0)
        self.assertEqual(rep.node_reports["a"].peak_steps, [])
        self.assertEqual(rep.node_reports["a"].peak_outflow, 0)
        store.close()

    def test_zero_proportion_edge_carries_nothing(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 0, 0)  # 零比例边：a 的水全部从 a 离开
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 1, ["a", "b"],
                                  {("a", 0): (1, 0), ("b", 0): (0, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("a", "b"), 0, 1), rain)
        self.assertEqual(rep.node_reports["b"].at("outflow", 0), 0)
        self.assertEqual(rep.node_reports["a"].at("leave", 0), 1)
        self.assertTrue(rep.conserved)
        store.close()

    def test_window_not_starting_at_zero(self):
        d = CatalogDraft()
        d.add_node("a", 2, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 1, 1)
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        L, R = 5, 7
        rain = repo.import_batch(
            L, R, ["a", "b"],
            {(n, t): (0, 0) for n in ("a", "b") for t in range(L, R)}
            | {("a", 5): (1, 0)},
        )
        rep = Engine(cat).analyze(AnalysisRequest.make(("b",), L, R), rain)
        # 体积 2 在 t5 出流，延迟 1 => b 在 t6 收到 2
        self.assertEqual(rep.node_reports["a"].at("outflow", 5), 2)
        self.assertEqual(rep.node_reports["b"].at("outflow", 6), 2)
        self.assertEqual(rep.node_reports["b"].at("outflow", 5), 0)
        self.assertTrue(rep.conserved)
        store.close()

    def test_horizon_exact_tail_and_zeros_after(self):
        # 单节点核长 3，窗口 [0,2)，无延迟 => T = (2-1)+(3-1)+0 = 3
        d = CatalogDraft()
        d.add_node("a", 1, [F("1/4"), F("1/2"), F("1/4")])
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 2, ["a"],
                                 {("a", 0): (1, 0), ("a", 1): (0, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("a",), 0, 2), rain)
        self.assertEqual(rep.horizon_end, 3)
        seq = rep.node_reports["a"].outflow
        self.assertEqual(len(seq), 4)  # t0..t3
        # 最后一个尾部时间仍可能非零（t2 核尾 1/4），其后不存在更晚的步
        self.assertEqual(seq[2], F("1/4"))
        store.close()

    def test_side_branch_rain_excluded_from_leaf_closure(self):
        # a -> c；旁支 b 与 c 无关。请求 c 时 b 的降雨不得进入守恒账
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_node("c", 1, [1])
        d.add_edge("a", "c", 0, 1)
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        cells = {
            ("a", 0): (1, 0), ("b", 0): (100, 0), ("c", 0): (0, 0),
        }
        rain = repo.import_batch(0, 1, ["a", "b", "c"], cells)
        rep = Engine(cat).analyze(AnalysisRequest.make(("c",), 0, 1), rain)
        self.assertEqual(rep.closure_nodes, ("a", "c"))
        self.assertEqual(rep.input_volume_total, 1)       # 不含 b 的 100
        self.assertEqual(rep.leave_volume_total, 1)
        self.assertTrue(rep.conserved)
        store.close()

    def test_explain_outside_window_is_empty(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 1, ["a"], {("a", 0): (1, 0)})
        ex = SourceExplainer(cat, rain).explain("a", 5)
        self.assertEqual(ex.outflow, 0)
        self.assertEqual(ex.paths, ())
        self.assertEqual(ex.by_source, {})
        self.assertTrue(ex.verify())
        store.close()

    def test_parallel_edges_different_delays_both_propagate(self):
        d = CatalogDraft()
        d.add_node("a", 1, [1])
        d.add_node("b", 1, [1])
        d.add_edge("a", "b", 0, F("1/3"))
        d.add_edge("a", "b", 2, F("2/3"))
        store, cat = self._store_cat(d)
        repo = RainRepo(store, cat)
        rain = repo.import_batch(0, 1, ["a", "b"],
                                 {("a", 0): (1, 0), ("b", 0): (0, 0)})
        rep = Engine(cat).analyze(AnalysisRequest.make(("b",), 0, 1), rain)
        self.assertEqual(rep.node_reports["b"].at("outflow", 0), F("1/3"))
        self.assertEqual(rep.node_reports["b"].at("outflow", 2), F("2/3"))
        # a 出边比例和 = 1，无离开；b 全离开 => 守恒
        self.assertTrue(rep.conserved)
        self.assertEqual(rep.leave_volume_total, 1)
        store.close()


class StorageTests(unittest.TestCase):
    def test_unknown_keys(self):
        store = Store(":memory:")
        with self.assertRaises(StoreError):
            store.load_catalog("nope")
        with self.assertRaises(StoreError):
            store.load_analysis("nope")
        store.close()

    def test_external_catalog_roundtrip_hash_guard(self):
        cat = three_node_catalog()
        payload = cat.to_payload()
        good = Catalog.from_payload(payload, verify_hash=cat.content_hash)
        self.assertEqual(good.content_hash, cat.content_hash)
        with self.assertRaises(CatalogError):
            Catalog.from_payload(payload, verify_hash="0" * 64)


class CliTests(unittest.TestCase):
    def test_end_to_end_cli(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli_main(["validate"])
        self.assertEqual(rc, 0)
        self.assertIn("全部通过", buf.getvalue())

    def test_cli_catalog_rain_analyze_explain(self):
        import tempfile, os

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["catalog-node", "--db", path, "--node", "a",
                               "--area", "2", "--kernel", "1", "--publish"])
                self.assertEqual(rc, 0)
                h = json.loads(out.getvalue())["catalog_hash"]
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["catalog-node", "--db", path, "--catalog", h,
                               "--node", "b", "--area", "3", "--kernel", "1",
                               "--publish"])
                self.assertEqual(rc, 0)
                h = json.loads(out.getvalue())["catalog_hash"]
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["catalog-edge", "--db", path, "--catalog", h,
                               "--from", "a", "--to", "b", "--delay", "1",
                               "--proportion", "1"])
                self.assertEqual(rc, 0)
                h = json.loads(out.getvalue())["catalog_hash"]
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["pulse", "--db", path, "--catalog", h, "--node", "a"])
                self.assertEqual(rc, 0)
                self.assertIn('"b"', out.getvalue())
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["rain-import", "--db", path, "--catalog", h,
                               "--lo", "0", "--hi", "1", "--nodes", "a,b",
                               "--cell", "a:0:1:0", "--cell", "b:0:0:0"])
                self.assertEqual(rc, 0)
                rh = json.loads(out.getvalue())["rain_hash"]
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["analyze", "--db", path, "--catalog", h,
                               "--rain", rh, "--nodes", "b", "--lo", "0", "--hi", "1"])
                self.assertEqual(rc, 0)
                report = json.loads(out.getvalue())
                self.assertEqual(report["status"], "OK")
                self.assertTrue(report["conservation"]["conserved"])
            with mock.patch("sys.stdout", io.StringIO()) as out:
                rc = cli_main(["explain", "--db", path, "--catalog", h,
                               "--rain", rh, "--node", "b", "--time", "1"])
                self.assertEqual(rc, 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["outflow"], "2")
                self.assertEqual(payload["by_source"], {"a@t0": "2"})
        finally:
            os.remove(path)


class DemoSuiteTests(unittest.TestCase):
    def test_run_demo_all_pass(self):
        store, logs = run_demo(":memory:")
        self.assertEqual(len(logs), 10)
        store.close()


if __name__ == "__main__":
    unittest.main()
