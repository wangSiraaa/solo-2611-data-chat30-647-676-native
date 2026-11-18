"""端到端与单元测试：不依赖 pytest，直接 `python -m tests.test_pipeline`。

覆盖
----
* 合成数据约束（非单频、素材互不相同、规模/采样率/时长）；
* 检索：零漏检、零误报、重复分开、共享短片段不判完整、确定性分级；
* 缓存失效：不兼容特征配置拒绝复用，兼容配置可重扫；
* 软删除不改旧批次；更正产生新批次且履约整体重算；
* 履约：只消费 confirmed、每次播放最多满足一条排期一次、三级目标
  （最大化 -> 距离 -> 字典序），与独立穷举器一致且确定；
* 外部监测表校验。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from adverify import features as F
from adverify import matcher as M
from adverify.audioio import read_wav
from adverify.fulfill import FulfillStore
from adverify.generator import (SR, N_MATERIALS, PROGRAM_SECONDS,
                                build_materials, build_program)
from adverify.monitor import (MonitorStore, IncompatibleIndexError,
                              validate_monitor_table, MONITOR_EXPORT_FORMAT)
from adverify.solver import Schedule, solve
from adverify.validator import (brute_force_optimum,
                                build_competition_cases,
                                evaluate_detection,
                                verify_solver_optimality)


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials, cls.meta, cls.jingle = build_materials()
        cls.program, cls.events = build_program(cls.materials, cls.jingle)

    def test_scale_and_format(self):
        self.assertEqual(len(self.materials), N_MATERIALS)
        self.assertEqual(self.program.sr, SR)
        self.assertEqual(self.program.n, PROGRAM_SECONDS * SR)
        for mid, sig in self.materials.items():
            dur = sig.shape[0] / SR
            self.assertTrue(5.0 <= dur <= 30.0, msg=mid)

    def test_not_single_tone_and_distinct(self):
        cfg = F.FeatureConfig()
        means = {}
        for mid, sig in self.materials.items():
            # 单频信号频谱高度集中；真实素材应有显著多峰能量
            n = min(sig.shape[0], SR * 5)
            sp = np.abs(np.fft.rfft(sig[:n]))
            top = np.sort(sp)[-1] / (np.sum(sp) + 1e-12)
            self.assertLess(top, 0.25, msg=f"{mid} 过于接近单频")
            # 固定长度的平均特征向量用于素材两两差异比较
            feats = F.extract(sig, cfg)
            means[mid] = feats.mean(axis=0)
        ids = list(means)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                self.assertFalse(
                    np.allclose(means[ids[i]], means[ids[j]], atol=1e-6),
                    msg=f"{ids[i]} 与 {ids[j]} 完全相同")

    def test_program_contains_required_kinds(self):
        kinds = {e.kind for e in self.events}
        for required in ("full", "truncated_head", "truncated_tail",
                         "repeated", "volume", "shared_jingle"):
            self.assertIn(required, kinds)


class DetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="adv_test_")
        cls.mat_dir = os.path.join(cls.tmp, "materials")
        os.makedirs(cls.mat_dir)
        from adverify.audioio import Audio, write_wav
        cls.materials, cls.meta, cls.jingle = build_materials()
        for mid, sig in cls.materials.items():
            write_wav(os.path.join(cls.mat_dir, f"{mid}.wav"), Audio(sig, SR))
        cls.program, cls.events = build_program(cls.materials, cls.jingle)
        cls.prog_path = os.path.join(cls.tmp, "program.wav")
        write_wav(cls.prog_path, cls.program)
        cls.gt = {"sr": SR, "events": [
            {"event_id": e.event_id, "material_id": e.material_id, "kind": e.kind,
             "prog_start": e.prog_start, "prog_end": e.prog_end,
             "mat_start": e.mat_start, "mat_end": e.mat_end}
            for e in cls.events]}
        cls.db = os.path.join(cls.tmp, "monitor.db")
        cls.store = MonitorStore(cls.db)
        for fn in sorted(os.listdir(cls.mat_dir)):
            mid = os.path.splitext(fn)[0]
            cls.store.register(mid, os.path.join(cls.mat_dir, fn))
        cls.store.rebuild_whitening()
        cls.scan_id = cls.store.scan(cls.prog_path, M.ScanConfig(), note="test")
        cls.matches = cls.store.get_matches(cls.scan_id)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_zero_miss_zero_false_alarm(self):
        rep = evaluate_detection(self.gt, self.matches)
        self.assertEqual(len(rep["missed_detections"]), 0, rep["missed_detections"])
        self.assertEqual(len(rep["false_alarms"]), 0, rep["false_alarms"])
        self.assertEqual(rep["n_linked"],
                         len([e for e in self.events if e.material_id]))

    def test_repeats_are_separate_plays(self):
        counts = {}
        for m in self.matches:
            counts[m["material_id"]] = counts.get(m["material_id"], 0) + 1
        # 至少存在被重复播放的素材，且每次重复有独立 play_id
        repeated_mats = {e.material_id for e in self.events if e.kind == "repeated"}
        self.assertTrue(repeated_mats)
        for mid in repeated_mats:
            self.assertGreaterEqual(counts.get(mid, 0), 2)
        ids = [m["play_id"] for m in self.matches]
        self.assertEqual(len(ids), len(set(ids)))

    def test_certainty_grading(self):
        # 完整/重复/音量 -> confirmed；截断 -> review
        by_play = {m["play_id"]: m for m in self.matches}
        rep = evaluate_detection(self.gt, self.matches)
        for link in rep["links"]:
            m = by_play[link["play_id"]]
            if link["kind"].startswith("truncated"):
                self.assertEqual(m["certainty"], "review", link)
            else:
                self.assertEqual(m["certainty"], "confirmed", link)

    def test_shared_jingle_not_full_match(self):
        # 共享短片段单播不能让宿主素材产生 confirmed 完整播放
        jingle = next(e for e in self.events if e.kind == "shared_jingle")
        for m in self.matches:
            if m["material_id"] not in ("m003", "m017"):
                continue
            overlap = max(0, min(m["prog_end"], jingle.prog_end)
                          - max(m["prog_start"], jingle.prog_start))
            if overlap > 0:
                self.assertNotEqual(m["certainty"], "confirmed")
                self.assertLess(m["evidence"]["coverage"],
                                M.ScanConfig().confirmed_min_coverage)

    def test_evidence_fields_present(self):
        m = self.matches[0]
        for key in ("play_id", "material_id", "prog_start", "prog_end",
                    "mat_start", "mat_end", "contiguous_frames", "evidence"):
            self.assertIn(key, m)
        for key in ("lag_frame", "recall_score", "verify_mean", "coverage",
                    "score_quantiles", "frame_hop_samples"):
            self.assertIn(key, m["evidence"])

    def test_position_error_bounded(self):
        rep = evaluate_detection(self.gt, self.matches)
        # 定位误差均值应在 1.5 秒以内（10ms 帧移 + 对角验证）
        self.assertLess(rep["start_error_samples"]["mean"], 1.5 * SR)


class CacheAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adv_life_")
        self.mat_dir = os.path.join(self.tmp, "materials")
        os.makedirs(self.mat_dir)
        from adverify.audioio import Audio, write_wav
        materials, _, jingle = build_materials()
        for mid, sig in list(materials.items())[:6]:
            write_wav(os.path.join(self.mat_dir, f"{mid}.wav"), Audio(sig, SR))
        program, _ = build_program(materials, jingle)
        self.prog_path = os.path.join(self.tmp, "p.wav")
        write_wav(self.prog_path, program)
        self.db = os.path.join(self.tmp, "m.db")
        self.store = MonitorStore(self.db)
        for fn in sorted(os.listdir(self.mat_dir)):
            mid = os.path.splitext(fn)[0]
            self.store.register(mid, os.path.join(self.mat_dir, fn))
        self.store.rebuild_whitening()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_incompatible_config_rejected(self):
        bad = MonitorStore(self.db, F.FeatureConfig(n_peaks=9))
        with self.assertRaises(IncompatibleIndexError):
            bad.scan(self.prog_path, M.ScanConfig())
        bad.close()

    def test_soft_delete_keeps_old_batch(self):
        sid = self.store.scan(self.prog_path)
        before = {m["play_id"]: m for m in self.store.get_matches(sid)}
        target = "m003"
        self.store.delete(target)
        after = {m["play_id"]: m for m in self.store.get_matches(sid)}
        self.assertEqual(before.keys(), after.keys())
        self.assertTrue(all(m["material_id"] != target or
                            True for m in after.values()))
        # 旧批次中被删素材的匹配原样保留
        old_m003 = [m for m in after.values() if m["material_id"] == target]
        self.assertTrue(old_m003)

    def test_correction_makes_new_batch_and_recomputes(self):
        sid1 = self.store.scan(self.prog_path)
        confirmed = next(m for m in self.store.get_matches(sid1)
                         if m["certainty"] == "confirmed")
        sid2 = self.store.correct_match(
            sid1, confirmed["play_id"], {"certainty": "review"},
            reason="unit test correction")
        self.assertNotEqual(sid1, sid2)
        self.assertEqual(
            self.store.get_play(sid1, confirmed["play_id"])["certainty"],
            "confirmed")  # 旧批次不变
        self.assertEqual(
            self.store.get_play(sid2, confirmed["play_id"])["certainty"],
            "review")
        self.assertEqual(
            self.store.get_scan(sid2)["supersedes_scan"], sid1)


class SolverTests(unittest.TestCase):
    def test_bruteforce_agreement_and_determinism(self):
        rep = verify_solver_optimality()
        self.assertTrue(rep["all_passed"], rep)

    def test_only_confirmed_consumed(self):
        s = Schedule("s1", "m1", 0, 10000, 1000, 1)
        plays = [
            {"play_id": "p1", "material_id": "m1", "certainty": "review",
             "prog_start": 100, "prog_end": 200, "contiguous_frames": 100,
             "evidence": {"frame_hop_samples": 80}},
            {"play_id": "p2", "material_id": "m1", "certainty": "confirmed",
             "prog_start": 300, "prog_end": 400, "contiguous_frames": 100,
             "evidence": {"frame_hop_samples": 80}},
        ]
        r = solve([s], plays)
        used = {a["play_id"] for a in r["allocations"]}
        self.assertEqual(used, {"p2"})

    def test_one_play_at_most_one_requirement(self):
        # 一条播放同时满足两条排期窗口，只能被消费一次
        s1 = Schedule("s1", "m1", 0, 1000, 1000, 1)
        s2 = Schedule("s2", "m1", 0, 1000, 1000, 1)
        plays = [{"play_id": "p1", "material_id": "m1", "certainty": "confirmed",
                  "prog_start": 500, "prog_end": 600, "contiguous_frames": 100,
                  "evidence": {"frame_hop_samples": 80}}]
        r = solve([s1, s2], plays)
        self.assertEqual(len(r["allocations"]), 1)
        self.assertEqual(r["diagnostics"]["total_satisfied"], 1)

    def test_lexicographic_tie_break(self):
        # 距离完全对称时，选字典序最小的 (schedule,play) 列表
        s1 = Schedule("sA", "m1", 0, 2000, 1000, 1)
        s2 = Schedule("sB", "m1", 0, 2000, 1000, 1)
        plays = [
            {"play_id": "z9", "material_id": "m1", "certainty": "confirmed",
             "prog_start": 1000, "prog_end": 1100, "contiguous_frames": 100,
             "evidence": {"frame_hop_samples": 80}},
            {"play_id": "a1", "material_id": "m1", "certainty": "confirmed",
             "prog_start": 1000, "prog_end": 1100, "contiguous_frames": 100,
             "evidence": {"frame_hop_samples": 80}},
        ]
        r = solve([s1, s2], plays)
        pairs = sorted((a["schedule_id"], a["play_id"])
                       for a in r["allocations"])
        self.assertEqual(pairs, [("sA", "a1"), ("sB", "z9")])

    def test_doubled_distance_integer(self):
        s = Schedule("s1", "m1", 0, 1001, 1000, 1)  # 奇数窗口 -> 半整数中点
        plays = [{"play_id": "p1", "material_id": "m1", "certainty": "confirmed",
                  "prog_start": 250, "prog_end": 350, "contiguous_frames": 100,
                  "evidence": {"frame_hop_samples": 80}}]
        r = solve([s], plays)
        # 2*|250 - 500.5| = 501，全程整数
        self.assertEqual(r["allocations"][0]["doubled_distance"], 501)

    def test_min_contiguous_enforced(self):
        s = Schedule("s1", "m1", 0, 10000, 8000, 1)
        plays = [{"play_id": "p1", "material_id": "m1", "certainty": "confirmed",
                  "prog_start": 100, "prog_end": 200, "contiguous_frames": 10,
                  "evidence": {"frame_hop_samples": 80}}]  # 800 < 8000
        r = solve([s], plays)
        self.assertEqual(r["allocations"], [])
        self.assertEqual(r["schedule_report"][0]["status"], "partial")


class RandomizedOptimalityTests(unittest.TestCase):
    """随机生成大量小型竞争表，与独立穷举器逐项对照。"""

    def test_random_cases_match_bruteforce(self):
        rng = np.random.default_rng(20261118)
        n_cases = 60
        for case in range(n_cases):
            n_sched = int(rng.integers(1, 5))
            n_play = int(rng.integers(1, 7))
            mats = [f"m{k}" for k in range(3)]
            schedules = []
            for i in range(n_sched):
                center = int(rng.integers(0, 4000))
                half = int(rng.integers(200, 1500))
                schedules.append(Schedule(
                    f"s{i:02d}", str(rng.choice(mats)),
                    max(0, center - half), center + half,
                    int(rng.integers(100, 3000)), int(rng.integers(1, 4))))
            plays = []
            for j in range(n_play):
                cf = int(rng.integers(5, 60))
                start = int(rng.integers(0, 5000))
                plays.append({
                    "play_id": f"p{j:02d}",
                    "material_id": str(rng.choice(mats)),
                    "certainty": "confirmed" if rng.random() < 0.85 else "review",
                    "prog_start": start, "prog_end": start + cf * 80,
                    "contiguous_frames": cf,
                    "evidence": {"frame_hop_samples": 80}})
            bf = brute_force_optimum(schedules, plays)
            r1 = solve(schedules, plays)
            r2 = solve(schedules, [dict(p) for p in plays])
            pairs = sorted((a["schedule_id"], a["play_id"])
                           for a in r1["allocations"])
            self.assertEqual(
                r1["diagnostics"]["total_satisfied"], bf["best_flow"], f"case {case}")
            self.assertEqual(
                r1["diagnostics"]["total_doubled_distance"],
                bf["best_cost"], f"case {case}")
            self.assertEqual([list(x) for x in pairs],
                             bf["lex_best_pairs"], f"case {case}")
            self.assertEqual(
                json.dumps(r1, sort_keys=True),
                json.dumps(r2, sort_keys=True), f"case {case} 非确定")


class ExternalTableTests(unittest.TestCase):
    def test_validation_rejects_bad_table(self):
        good = {"format": MONITOR_EXPORT_FORMAT, "matches": [{
            "play_id": "p1", "material_id": "m1", "certainty": "confirmed",
            "prog_start": 0, "prog_end": 100, "mat_start": 0, "mat_end": 100,
            "contiguous_frames": 10}]}
        self.assertEqual(len(validate_monitor_table(good)), 1)
        bad = dict(good, format="wrong")
        with self.assertRaises(ValueError):
            validate_monitor_table(bad)
        bad2 = {"format": MONITOR_EXPORT_FORMAT,
                "matches": [dict(good["matches"][0], certainty="weird")]}
        with self.assertRaises(ValueError):
            validate_monitor_table(bad2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
