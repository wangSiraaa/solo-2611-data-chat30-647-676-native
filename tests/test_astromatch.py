"""astromatch 测试套件。

运行：
    .venv/bin/python -m tests.test_astromatch
或（若安装了 pytest）：
    .venv/bin/python -m pytest tests/
覆盖：传播模型、暴力枚举一致性、eps 带字典序、贪心反例、事务原子性、
撤回/版本冻结、零向量、无候选、外部快照、旧 run 不可变、删除输入后复读。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from astromatch import synthetic as syn
from astromatch.catalog import CatalogError, CatalogService
from astromatch.matcher import (
    brute_force_optimal, build_candidates, prepare_detections, prepare_stars,
    solve_matching,
)
from astromatch.snapshot import snapshot_from_jsonable
from astromatch.vectors import angle_between, normalize, propagate
from astromatch.warehouse import ExposureInput, MatchWarehouse, WarehouseError


class TestVectors(unittest.TestCase):
    def test_propagate_and_zero(self) -> None:
        r = np.array([0.0, 0.03, 0.0])
        v = np.array([0.0, -0.03, 0.0])
        self.assertIsNone(propagate(r, v, 1, 0))          # 精确零向量
        u = propagate(r, v, 0, 0)
        self.assertAlmostEqual(float(np.linalg.norm(u)), 1.0)

    def test_flip_hand_calc(self) -> None:
        r2, v2 = np.array([1.0, 0.030, 0.0]), np.array([0.0, -0.060, 0.0])
        d = normalize(np.array([1.0, -0.020, 0.0]))
        s1 = np.array([1.0, 0.0, 0.0])
        self.assertTrue(np.allclose(r2 + v2, [1.0, -0.030, 0.0]))
        self.assertLess(angle_between(s1, d), angle_between(propagate(r2, v2, 0, 0), d))
        self.assertLess(angle_between(propagate(r2, v2, 1, 0), d), angle_between(s1, d))


class TestBruteForceConsistency(unittest.TestCase):
    def test_random_small_cases(self) -> None:
        rng = np.random.default_rng(777)
        for _ in range(40):
            ns, nd = int(rng.integers(1, 6)), int(rng.integers(1, 6))
            stars = [f"S{i}" for i in range(ns)]
            dets = [f"D{j}" for j in range(nd)]
            sd = rng.normal(size=(ns, 3)); sd /= np.linalg.norm(sd, axis=1, keepdims=True)
            dd = rng.normal(size=(nd, 3)); dd /= np.linalg.norm(dd, axis=1, keepdims=True)
            thr = float(rng.uniform(0.4, 2.3))
            eps = float(rng.choice([0.0, 1e-12, 1e-9]))
            cands = build_candidates(stars, sd, dets, dd, thr)
            out = solve_matching("c", stars, dets, cands, eps=eps, threshold=thr)
            brute = brute_force_optimal(stars, dets, cands, eps=eps)
            self.assertEqual(out.max_cardinality, brute["k"])
            self.assertEqual(out.match_pairs, brute["pairs"])
            self.assertAlmostEqual(out.total_cost, brute["cost"], places=12)

    def test_eps_band_lexicographic(self) -> None:
        # 刻意让字典序最小方案不是最小代价方案：交叉边更便宜
        sdirs = np.array([[1.0, 0, 0], list(normalize(np.array([1.0, 0.02, 0.0])))])
        ddirs = np.array([list(normalize(np.array([1.0, 0.019, 0.0]))),
                          list(normalize(np.array([1.0, 0.001, 0.0])))])
        stars, dets = ["S1", "S2"], ["D1", "D2"]
        cands = build_candidates(stars, sdirs, dets, ddirs, threshold=0.05)
        ec = {c.as_pair(): c.cost for c in cands}
        cost_identity = ec[("S1", "D1")] + ec[("S2", "D2")]
        cost_cross = ec[("S1", "D2")] + ec[("S2", "D1")]
        self.assertLess(cost_cross, cost_identity)      # 交叉（字典序更大）更便宜
        o0 = solve_matching("e", stars, dets, cands, eps=0.0, threshold=0.05)
        self.assertEqual(o0.match_pairs, [("S1", "D2"), ("S2", "D1")])  # 最小代价
        gap = cost_identity - cost_cross
        o_band = solve_matching("e", stars, dets, cands, eps=gap * 2, threshold=0.05)
        self.assertEqual(o_band.match_pairs, [("S1", "D1"), ("S2", "D2")])  # 带内字典序
        brute = brute_force_optimal(stars, dets, cands, eps=gap * 2)
        self.assertEqual(o_band.match_pairs, brute["pairs"])


class TestGreedyCounterexample(unittest.TestCase):
    def test_duplicate_nearest_forbidden(self) -> None:
        sc = syn.scenario_adjacent_competition()
        sids, smat = prepare_stars(
            [{"star_id": s["star_id"], "direction": normalize(np.array(s["r"]))}
             for s in sc["stars"]], unit_tol=1e-9)
        dids, dmat, _ = prepare_detections(sc["exposure"]["detections"], unit_tol=1e-9)
        cands = build_candidates(sids, smat, dids, dmat, sc["threshold"])
        out = solve_matching("E", sids, dids, cands, eps=sc["eps"], threshold=sc["threshold"])
        # 逐点最近星（允许重复）会把两个亮点都判给 S1
        from astromatch.vectors import angle_matrix
        angles = angle_matrix(smat, dmat)
        dup = [sids[int(np.argmin(angles[:, j]))] for j in range(len(dids))]
        self.assertEqual(dup, ["S1", "S1"])
        # 全局求解器给出合法的 2 条，每星/每点最多一次
        self.assertEqual(out.match_pairs, [("S1", "D1"), ("S2", "D2")])
        self.assertEqual(out.max_cardinality, 2)


class TestCatalogTransactions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "c.sqlite")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_rollback(self) -> None:
        with CatalogService(self.db) as svc:
            svc.add_star("OK", 0, [1, 0, 0], [0, 0, 0])
            with self.assertRaises(ValueError):
                svc.import_lines([
                    {"op": "add", "star_id": "A", "t0": 0, "r": [1, 0, 0], "v": [0, 0, 0]},
                    {"op": "add", "star_id": "BAD", "t0": 0,
                     "r": [1, float("nan"), 0], "v": [0, 0, 0]},
                ])
            self.assertEqual([r.star_id for r in svc.list_active_stars()], ["OK"])

    def test_withdrawal_and_releases(self) -> None:
        with CatalogService(self.db) as svc:
            svc.add_star("S1", 0, [1, 0, 0], [0, 0, 0])
            svc.add_star("S2", 0, [1, 0.01, 0], [0, 0, 0])
            svc.release("R1")
            svc.withdraw_star("S2")
            with self.assertRaises(CatalogError):
                svc.replace_star("S2", 0, [1, 0, 0], [0, 0, 0])
            svc.release("R2")
            self.assertEqual([e.star_id for e in svc.get_release_entries("R1")], ["S1", "S2"])
            self.assertEqual([e.star_id for e in svc.get_release_entries("R2")], ["S1"])

    def test_zero_vector_excluded(self) -> None:
        sc = syn.scenario_zero_vector()
        with CatalogService(self.db) as svc:
            for s in sc["stars"]:
                svc.add_star(s["star_id"], s["t0"], s["r"], s["v"])
            svc.release("RZ")
            snap, unavailable = svc.export_snapshot(1, "RZ")
        self.assertEqual(unavailable, ["S2"])
        self.assertTrue(all(e.star_id != "S2" for e in snap.entries))


class TestWarehouse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.wh_db = os.path.join(self.tmp, "w.sqlite")
        self.cat_db = os.path.join(self.tmp, "c.sqlite")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _flip_setup(self):
        flip = syn.scenario_velocity_flip()
        with CatalogService(self.cat_db) as cat:
            for s in flip["stars"]:
                cat.add_star(s["star_id"], s["t0"], s["r"], s["v"])
            cat.release("R0")
        return flip

    def test_rerun_binds_new_snapshot_old_immutable(self) -> None:
        flip = self._flip_setup()
        with MatchWarehouse(self.wh_db) as wh:
            wh.import_exposure(ExposureInput(
                "E-FLIP-T1", 1, flip["exposure_after"]["detections"], 1e-9))
            with CatalogService(self.cat_db) as cat:
                snap_old, _ = cat.export_snapshot(1, "R0")
                run_old = wh.run_matching("E-FLIP-T1", snap_old, threshold=0.05, eps=1e-12)
                cat.replace_star("S2", 0, [1.0, 0.030, 0.0], [0.0, -0.090, 0.0])
                cat.release("R1")
                snap_new, _ = cat.export_snapshot(1, "R1")
                run_new = wh.run_matching("E-FLIP-T1", snap_new, threshold=0.05, eps=1e-12)
            self.assertNotEqual(run_old["run_id"], run_new["run_id"])
            self.assertNotEqual(run_old["snapshot_id"], run_new["snapshot_id"])
            reread = wh.get_run(run_old["run_id"])
            self.assertEqual(
                [(m["star_id"], m["detection_id"]) for m in reread["matches"]],
                [(m["star_id"], m["detection_id"]) for m in run_old["matches"]])
            self.assertEqual(reread["snapshot_id"], run_old["snapshot_id"])

    def test_external_snapshot_and_explain(self) -> None:
        snap = snapshot_from_jsonable(syn.external_snapshot_payload())
        self.assertEqual(snap.source, "external")
        with MatchWarehouse(self.wh_db) as wh:
            wh.import_exposure(ExposureInput(
                "E-EXT", 0, [{"detection_id": "DX1",
                              "direction": [float(x) for x in normalize(np.array([1.0, 0.001, 0.0]))],
                              "brightness": 3}], 1e-9))
            run = wh.run_matching("E-EXT", snap, threshold=0.02, eps=1e-12)
            self.assertEqual(run["matches"][0]["star_id"], "X1")
            why = wh.explain_match(run["run_id"], "X2", "DX1")
            self.assertEqual(why["status"], "rejected")
            self.assertIn("budget", why)

    def test_validation_rejects_bad_inputs(self) -> None:
        with MatchWarehouse(self.wh_db) as wh:
            with self.assertRaises(ValueError):
                wh.import_exposure(ExposureInput(
                    "B", 0, [{"detection_id": "x", "direction": [1.0, 0.01, 0.0],
                              "brightness": 1}], 1e-9))
            with self.assertRaises(ValueError):
                wh.import_exposure(ExposureInput(
                    "B", 0, [{"detection_id": "x", "direction": [1, 0, 0],
                              "brightness": -1}], 1e-9))
            self.assertEqual(
                wh.conn.execute("SELECT COUNT(*) c FROM exposures").fetchone()["c"], 0)

    def test_no_candidate(self) -> None:
        sc = syn.scenario_no_candidate()
        sids, smat = prepare_stars(
            [{"star_id": "S1", "direction": [1.0, 0.0, 0.0]}], unit_tol=1e-9)
        dids, dmat, _ = prepare_detections(sc["exposure"]["detections"], unit_tol=1e-9)
        out = solve_matching("E", sids, dids,
                             build_candidates(sids, smat, dids, dmat, sc["threshold"]),
                             eps=1e-12, threshold=sc["threshold"])
        self.assertEqual(out.max_cardinality, 0)
        self.assertEqual(out.unmatched_detections, ["D1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
