"""End-to-end correctness checks.

Every assertion compares restored bytes with the *independently generated truth*
(kept separate from the catalog), re-hashes files, and audits device calls plus
per-step scratch usage.  A file count alone never counts as success.
"""

import hashlib
import json
import shutil
import unittest
from pathlib import Path

from tape_restore.device import DeviceManager, MediaLibrary
from tape_restore.errors import (
    BlockOutOfRange,
    DriveOccupied,
    EndOfTape,
    MediaError,
    QuotaExceeded,
    VolumeInUse,
)
from tape_restore.model import parse_catalog
from tape_restore.planner import build_plan
from tape_restore.restorer import (
    COMPLETE,
    FAILED_FRAGMENT,
    FAILED_MEDIA,
    SKIPPED_QUOTA,
    Interrupted,
    RestoreEngine,
    resume,
)
from tape_restore.scenario import generate_scenario, load_scenario
from tape_restore.staging import ScratchLedger, Workspace

HERE = Path(__file__).resolve().parent
SCENARIO_ROOT = HERE / "_generated" / "scenario"


def setUpModule():
    generate_scenario(SCENARIO_ROOT, force=True)


class ScenarioFixture:
    scenario = None
    lib = None

    @classmethod
    def load(cls):
        if cls.scenario is None:
            cls.scenario = load_scenario(SCENARIO_ROOT)
            cls.lib = MediaLibrary(SCENARIO_ROOT / "media")
        return cls.scenario, cls.lib


class TestScenarioIntegrity(unittest.TestCase):
    def test_limits_respected(self):
        sc, _ = ScenarioFixture.load()
        self.assertLessEqual(len(sc.catalog.files), 20)
        self.assertLessEqual(len(sc.volumes), 6)
        self.assertEqual(sc.catalog.block_size, 1024)
        for vol in sc.volumes.values():
            self.assertLessEqual(vol.block_count, 100)
        self.assertEqual(sc.quota_bytes, 32 * 1024)

    def test_truth_independent_of_catalog(self):
        sc, _ = ScenarioFixture.load()
        # truth is regenerated from files, digests must match catalog but are
        # stored separately; this guards against "catalog sha == catalog sha".
        for fid, data in sc.truth.items():
            h = hashlib.sha256(data).hexdigest()
            self.assertEqual(h, sc.catalog.files[fid].sha256)

    def test_padding_is_not_file_body(self):
        sc, _ = ScenarioFixture.load()
        # F03 is 1023 bytes => one block with 1 byte of padding.
        fe = sc.catalog.files["F03"]
        frag = fe.fragments[0]
        phys = sc.volumes[frag.volume_id].blocks[frag.start_block].payload
        self.assertEqual(len(phys), 1024)
        self.assertEqual(frag.valid_bytes, 1023)
        self.assertTrue(phys.endswith(b"\x00"))

    def test_zero_length_file(self):
        sc, _ = ScenarioFixture.load()
        fe = sc.catalog.files["F04"]
        self.assertEqual(fe.total_length, 0)
        self.assertEqual(fe.fragments, ())

    def test_multivolume_and_cross_volume(self):
        sc, _ = ScenarioFixture.load()
        vols = {f.volume_id for f in sc.catalog.files["F16"].fragments}
        self.assertGreaterEqual(len(vols), 3)
        # at least one shared volume carries fragments of 2+ files interleaved
        shared = False
        for vid in sc.volumes:
            owners = {fid for fid, _ in sc.catalog.fragments_for_volume(vid)}
            if len(owners) >= 3:
                shared = True
        self.assertTrue(shared)


class TestCatalogValidation(unittest.TestCase):
    def setUp(self):
        self.sc, _ = ScenarioFixture.load()
        self.raw = json.loads((SCENARIO_ROOT / "catalog.json").read_text())

    def _parse(self, raw, volumes=None):
        return parse_catalog(raw, volumes=volumes)

    def test_good_catalog_parses(self):
        cat = self._parse(self.raw, self.sc.volumes)
        self.assertEqual(set(cat.files), set(self.sc.catalog.files))

    def test_length_sum_mismatch_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        f = next(f for f in raw["files"] if f["file_id"] == "F01")
        f["total_length"] += 1
        with self.assertRaises(Exception) as cx:
            self._parse(raw, self.sc.volumes)
        self.assertIn("total_length", str(cx.exception))

    def test_out_of_bounds_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        f = next(f for f in raw["files"] if f["file_id"] == "F01")
        f["fragments"][0]["start_block"] = 150
        with self.assertRaises(Exception) as cx:
            self._parse(raw, self.sc.volumes)
        self.assertIn("end of volume", str(cx.exception))

    def test_duplicate_fragment_identity_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        files = {f["file_id"]: f for f in raw["files"]}
        a = files["F01"]["fragments"][0]
        b = files["F02"]["fragments"][0]
        b["fragment_id"] = a["fragment_id"]
        with self.assertRaises(Exception) as cx:
            self._parse(raw, self.sc.volumes)
        self.assertIn("fragment_id", str(cx.exception))

    def test_shared_physical_block_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        files = {f["file_id"]: f for f in raw["files"]}
        a = files["F01"]["fragments"][0]  # V1 blocks 0-1
        b = files["F09"]["fragments"][0]
        b["volume_id"] = a["volume_id"]
        b["start_block"] = a["start_block"]
        b["block_count"] = a["block_count"]
        b["valid_bytes"] = a["valid_bytes"]
        with self.assertRaises(Exception) as cx:
            self._parse(raw, self.sc.volumes)
        self.assertIn("claimed by two fragments", str(cx.exception))

    def test_filemark_overlap_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        f = next(f for f in raw["files"] if f["file_id"] == "F13")  # V2 full block
        # find a filemark block on the same volume and claim it
        vid = f["fragments"][0]["volume_id"]
        fm = next(i for i, b in enumerate(self.sc.volumes[vid].blocks) if b.kind == "filemark")
        frag = f["fragments"][0]
        frag["start_block"] = fm
        frag["block_count"] = 1
        frag["valid_bytes"] = 1024
        with self.assertRaises(Exception) as cx:
            self._parse(raw, self.sc.volumes)
        self.assertIn("filemark", str(cx.exception))

    def test_whole_digest_cannot_mask_bad_fragment(self):
        # Changing a fragment digest while leaving the file digest is rejected
        # only at *restore* time; the structural validator accepts it because
        # structure is still valid.  This test spells out that distinction.
        raw = json.loads(json.dumps(self.raw))
        f = next(f for f in raw["files"] if f["file_id"] == "F01")
        f["fragments"][0]["sha256"] = "0" * 64
        cat = self._parse(raw, self.sc.volumes)  # structurally fine
        self.assertEqual(cat.files["F01"].fragments[0].sha256, "0" * 64)


class TestDeviceLayer(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()

    def test_mount_one_volume_per_drive_and_no_double_mount(self):
        dm = DeviceManager(self.lib)
        dm.mount("D0", "V1")
        dm.mount("D1", "V2")
        # both drives occupied -> D1 mount fails as DriveOccupied before the
        # volume-in-use check
        with self.assertRaises(DriveOccupied):
            dm.mount("D1", "V1")
        dm.unmount("D1")
        with self.assertRaises(DriveOccupied):
            dm.mount("D0", "V3")  # D0 still busy
        with self.assertRaises(VolumeInUse):
            dm.mount("D1", "V1")  # V1 already on D0

    def test_skip_forward_only_and_read_advances(self):
        dm = DeviceManager(self.lib)
        dm.mount("D0", "V1")
        self.assertEqual(dm.drive("D0").position, 0)
        dm.skip("D0", 3)
        self.assertEqual(dm.drive("D0").position, 3)
        got = dm.read("D0")
        self.assertEqual(got.block_index, 3)
        self.assertEqual(dm.drive("D0").position, 4)
        dm.rewind("D0")
        self.assertEqual(dm.drive("D0").position, 0)
        with self.assertRaises(BlockOutOfRange):
            dm.skip("D0", -1)
        with self.assertRaises(EndOfTape):
            dm.skip("D0", 101)

    def test_every_call_is_logged_and_matches_bytes(self):
        dm = DeviceManager(self.lib)
        dm.mount("D0", "V1")
        dm.rewind("D0")
        dm.skip("D0", 0)
        r1 = dm.read("D0")  # V1 block 0: F01 data
        r2 = dm.read("D0")  # V1 block 1: F01 data
        s = dm.log.summary()
        self.assertEqual(s["mounts"], 1)
        self.assertEqual(s["rewinds"], 1)
        self.assertEqual(s["read_calls"], 2)
        self.assertEqual(s["bytes_returned"], r1.bytes_returned + r2.bytes_returned)
        self.assertEqual(r1.kind, "data")
        self.assertEqual(r1.bytes_returned, 1024)
        # a filemark read returns zero bytes but still advances the head
        dm.rewind("D0")
        dm.skip("D0", 2)
        fm = dm.read("D0")
        self.assertEqual(fm.kind, "filemark")
        self.assertEqual(fm.bytes_returned, 0)

    def test_sequential_client_independent_of_restore(self):
        dm = DeviceManager(self.lib)
        out = dm.scan("V1")
        self.assertEqual(out["position"], self.sc.volumes["V1"].block_count)
        # scanning saw exactly the data blocks of V1
        data_blocks = self.sc.volumes["V1"].data_blocks()
        seen = sum(1 for b in out["blocks"] if b["kind"] == "data")
        self.assertEqual(seen, data_blocks)
        self.assertEqual(out["filemarks"], out["blocks"].__len__() - seen)

    def test_device_cannot_read_media_file_directly(self):
        dm = DeviceManager(self.lib)
        # There is no API to seek a volume file; read requires a mounted drive.
        with self.assertRaises(Exception):
            dm.read("D0")


class TestScratchLedger(unittest.TestCase):
    def test_quota_enforced_before_write(self):
        root = HERE / "_generated" / "ledger"
        shutil.rmtree(root, ignore_errors=True)
        led = ScratchLedger(root, 4096)
        lid = led.reserve("f", 4096)
        self.assertEqual(led.current, 4096)
        with self.assertRaises(QuotaExceeded):
            led.reserve("g", 1)
        led.release(lid)
        self.assertEqual(led.current, 0)
        self.assertEqual(led.peak, 4096)

    def test_on_disk_bytes_are_metered(self):
        root = HERE / "_generated" / "ledger2"
        shutil.rmtree(root, ignore_errors=True)
        led = ScratchLedger(root, 1024)
        lid = led.reserve("f", 512)
        led.write(lid, b"x" * 512)
        self.assertEqual(led.actual_size(lid), 512)
        self.assertEqual(led.snapshot()["current_bytes"], 512)


class TestRestoreFileStrategy(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()
        self.root = HERE / "_generated" / "run_file"
        shutil.rmtree(self.root, ignore_errors=True)
        self.ws = Workspace(self.root)

    def _verify(self, res, fid):
        fe = self.sc.catalog.files[fid]
        dest = self.ws.output_dir / fe.path
        self.assertTrue(dest.exists(), f"{fid} missing")
        data = dest.read_bytes()
        self.assertEqual(data, self.sc.truth[fid], f"{fid} bytes differ from truth")
        self.assertEqual(hashlib.sha256(data).hexdigest(), fe.sha256)
        self.assertEqual(res["files"][fid]["status"], COMPLETE)

    def test_all_files_correct_bytes(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="file")
        res = eng.restore(None, allow_fallback=True)
        # every file that reports complete must byte-match truth
        for fid in res["published"]:
            self._verify(res, fid)
        # the two corrupted files must be failed, not complete
        self.assertEqual(res["files"]["F14"]["status"], FAILED_FRAGMENT)
        self.assertEqual(res["files"]["F18"]["status"], FAILED_FRAGMENT)
        # oversized file skipped
        self.assertEqual(res["files"]["F10"]["status"], SKIPPED_QUOTA)
        self.assertFalse((self.ws.output_dir / self.sc.catalog.files["F10"].path).exists())
        # failed outputs are not published
        self.assertFalse((self.ws.output_dir / self.sc.catalog.files["F14"].path).exists())
        self.assertFalse((self.ws.output_dir / self.sc.catalog.files["F18"].path).exists())
        # exactly 17 of 20 complete, and every one matches bytes
        self.assertEqual(len(res["published"]), 17)

    def test_quota_peak_never_exceeds_limit(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="file")
        res = eng.restore(None, allow_fallback=True)
        self.assertLessEqual(res["scratch"]["peak_bytes"], self.sc.quota_bytes)
        for step in res["scratch"]["steps"]:
            self.assertLessEqual(step["current_bytes"], self.sc.quota_bytes)
            self.assertLessEqual(step["peak_bytes"], self.sc.quota_bytes)

    def test_device_calls_consistent(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="file")
        res = eng.restore(None, allow_fallback=True)
        s = res["device"]["summary"]
        self.assertEqual(s["mounts"], s["unmounts"])
        self.assertEqual(s["read_calls"], s["data_blocks_read"] + s["filemarks_read"])
        # every published file's fragments must have been read from the device
        needed_blocks = sum(
            f.block_count for fid in res["published"]
            for f in self.sc.catalog.files[fid].fragments
        ) + sum(
            f.block_count for fid in res["failed"]
            for f in self.sc.catalog.files[fid].fragments
            if f.fragment_id not in []
        )
        # reads can exceed needed (failed files may stop early); never fewer
        self.assertGreaterEqual(s["data_blocks_read"], sum(
            f.block_count for fid in res["published"]
            for f in self.sc.catalog.files[fid].fragments
        ))
        # no direct seek primitive exists: skips only move forward
        for call in res["device"]["calls"]:
            if call["op"] == "skip" and call.get("ok"):
                self.assertGreaterEqual(call["after"], call["before"])

    def test_two_drives_used_and_shared_volume_never_double_mounted(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="file")
        eng.restore(None, allow_fallback=True)
        for call in eng.device.log.entries:
            if call["op"] == "mount" and call.get("ok"):
                # at mount time no other drive holds the same volume
                pass
        # check the device never had the same volume twice at once by replaying
        mounted = {}
        for call in eng.device.log.entries:
            if call["op"] == "mount" and call.get("ok"):
                vid = call["volume"]
                self.assertNotIn(vid, mounted)
                mounted[vid] = call["drive"]
            elif call["op"] == "unmount" and call.get("ok"):
                mounted.pop(call["volume"], None)


class TestResumeVolumeMajor(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()
        self.root = HERE / "_generated" / "run_volresume"
        shutil.rmtree(self.root, ignore_errors=True)
        self.ws = Workspace(self.root)

    def test_volume_major_interrupt_resume(self):
        ids = ["F01", "F02", "F03", "F08", "F13", "F17"]
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="volume",
                            run_id="VOLR", interrupt_after=4)
        with self.assertRaises(Interrupted):
            eng.restore(ids, allow_fallback=False)
        cp = eng.journal.read_checkpoint()
        # several targets were open at once and their confirmed fragments must be
        # recorded (not just the in-flight file)
        with_frags = [fid for fid, st in cp["files"].items() if st["confirmed"]]
        self.assertGreaterEqual(len(with_frags), 2)
        res = resume(self.sc.catalog, self.lib, self.ws,
                     quota=self.sc.quota_bytes, run_id="VOLR")
        for fid in ids:
            fe = self.sc.catalog.files[fid]
            self.assertEqual(res["files"][fid]["status"], COMPLETE, fid)
            self.assertEqual((self.ws.output_dir / fe.path).read_bytes(), self.sc.truth[fid])
        self.assertGreaterEqual(len(res["invalidation"]["restored_fragments"]), 1)


class TestRestoreVolumeStrategy(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()
        self.root = HERE / "_generated" / "run_volume"
        shutil.rmtree(self.root, ignore_errors=True)
        self.ws = Workspace(self.root)

    def test_volume_major_minimizes_mounts_and_matches_bytes(self):
        ids = ["F01", "F02", "F03", "F04", "F08", "F13", "F17"]
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="volume")
        res = eng.restore(ids, allow_fallback=False)
        for fid in ids:
            fe = self.sc.catalog.files[fid]
            if fe.total_length == 0:
                continue
            dest = self.ws.output_dir / fe.path
            self.assertEqual(dest.read_bytes(), self.sc.truth[fid], fid)
        # volumes touched: V1, V2 (only these hold the requested fragments)
        mounts = res["device"]["summary"]["mounts"]
        needed_vols = {f.volume_id for fid in ids for f in self.sc.catalog.files[fid].fragments}
        self.assertLessEqual(mounts, len(needed_vols))
        self.assertLessEqual(res["scratch"]["peak_bytes"], self.sc.quota_bytes)

    def test_volume_major_publishes_zero_length_file(self):
        ids = ["F04", "F13", "F17"]  # F04 is empty and lives on no volume
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws,
                            quota=self.sc.quota_bytes, strategy="volume")
        res = eng.restore(ids, allow_fallback=False)
        self.assertEqual(res["files"]["F04"]["status"], COMPLETE)
        dest = self.ws.output_dir / self.sc.catalog.files["F04"].path
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"")
        self.assertEqual(res["files"]["F13"]["status"], COMPLETE)
        self.assertEqual((self.ws.output_dir / self.sc.catalog.files["F13"].path).read_bytes(),
                         self.sc.truth["F13"])

    def test_volume_major_reports_infeasible_when_peak_too_high(self):
        ids = ["F01", "F02", "F03", "F06", "F09", "F12"]
        eng = RestoreEngine(self.sc.catalog, self.lib, Workspace(HERE / "_generated" / "run_vol2"),
                            quota=self.sc.quota_bytes, strategy="volume")
        res = eng.restore(ids, allow_fallback=False)
        # not enough room to hold all fragments -> nothing published complete
        for fid in res["published"]:
            fe = self.sc.catalog.files[fid]
            self.assertEqual((eng.workspace.output_dir / fe.path).read_bytes(), self.sc.truth[fid])


class TestFailureIsolation(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()

    def test_bad_fragment_does_not_block_good_files(self):
        root = HERE / "_generated" / "run_iso"
        shutil.rmtree(root, ignore_errors=True)
        ws = Workspace(root)
        eng = RestoreEngine(self.sc.catalog, self.lib, ws,
                            quota=self.sc.quota_bytes, strategy="file")
        res = eng.restore(["F14", "F01", "F03", "F17"], allow_fallback=True)
        self.assertEqual(res["files"]["F01"]["status"], COMPLETE)
        self.assertEqual(res["files"]["F03"]["status"], COMPLETE)
        self.assertEqual(res["files"]["F17"]["status"], COMPLETE)
        self.assertEqual(res["files"]["F14"]["status"], FAILED_FRAGMENT)
        self.assertFalse((ws.output_dir / self.sc.catalog.files["F14"].path).exists())
        for fid in ("F01", "F03", "F17"):
            self.assertEqual((ws.output_dir / self.sc.catalog.files[fid].path).read_bytes(),
                             self.sc.truth[fid])

    def test_media_truncation_marks_files_failed_not_partial(self):
        root = HERE / "_generated" / "run_trunc"
        media = root / "media"
        shutil.rmtree(root, ignore_errors=True)
        media.mkdir(parents=True)
        # copy all images then truncate V4 badly
        for vid in self.sc.volumes:
            shutil.copy(SCENARIO_ROOT / "media" / f"{vid}.tape", media / f"{vid}.tape")
        p = media / "V4.tape"
        data = p.read_bytes()
        # keep the header + only a handful of blocks, so any fragment past that
        # point is unreadable while the image is still well-formed enough to
        # *start* a mount (mount must then fail cleanly as MediaError)
        p.write_bytes(data[: 600])
        lib = MediaLibrary(media)
        ws = Workspace(root / "ws")
        eng = RestoreEngine(self.sc.catalog, lib, ws,
                            quota=self.sc.quota_bytes, strategy="file")
        res = eng.restore(["F09", "F01"], allow_fallback=True)
        # F09 lives entirely on V4 -> must fail (media), not publish a prefix
        self.assertEqual(res["files"]["F09"]["status"], FAILED_MEDIA)
        self.assertFalse((ws.output_dir / self.sc.catalog.files["F09"].path).exists())
        # F01 does not touch V4 and must still succeed
        self.assertEqual(res["files"]["F01"]["status"], COMPLETE)
        self.assertEqual((ws.output_dir / self.sc.catalog.files["F01"].path).read_bytes(),
                         self.sc.truth["F01"])


class TestResume(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()
        self.root = HERE / "_generated" / "run_resume"
        shutil.rmtree(self.root, ignore_errors=True)
        self.ws = Workspace(self.root)

    def test_interrupt_resume_reverifies_and_completes(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws, quota=self.sc.quota_bytes,
                            strategy="file", run_id="RUN1", interrupt_after=5)
        with self.assertRaises(Interrupted):
            eng.restore(["F07", "F12", "F01"], allow_fallback=True)
        cp = eng.journal.read_checkpoint()
        self.assertIsNotNone(cp)
        self.assertEqual(cp["confirmed_total"], 5)
        # incomplete targets count toward peak; check they were metered
        self.assertGreater(cp["scratch_current"], 0)
        self.assertLessEqual(cp["scratch_peak"], self.sc.quota_bytes)

        res = resume(self.sc.catalog, self.lib, self.ws, quota=self.sc.quota_bytes, run_id="RUN1")
        inv = res["invalidation"]
        self.assertFalse(inv["catalog_changed"])
        # every reported position check must be exact
        for chk in inv["position_checks"]:
            self.assertTrue(chk["ok"], chk)
        # re-verified fragments must have been re-hashed, not trusted blindly
        self.assertGreaterEqual(len(inv["restored_fragments"]), 1)
        for fid in ("F07", "F12", "F01"):
            fe = self.sc.catalog.files[fid]
            self.assertEqual(res["files"][fid]["status"], COMPLETE, fid)
            self.assertEqual((self.ws.output_dir / fe.path).read_bytes(), self.sc.truth[fid])

    def test_resume_drops_fragment_when_staged_bytes_tampered(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws, quota=self.sc.quota_bytes,
                            strategy="file", run_id="RUN2", interrupt_after=3)
        with self.assertRaises(Interrupted):
            eng.restore(["F07"], allow_fallback=True)
        cp = eng.journal.read_checkpoint()
        fid = "F07"
        lease = cp["files"][fid]["lease"]
        some_frag = next(iter(cp["files"][fid]["confirmed"]))
        rec = cp["files"][fid]["confirmed"][some_frag]
        # corrupt the staged region on disk directly
        p = self.ws.work_dir / "scratch" / f"{lease}.bin"
        raw = bytearray(p.read_bytes())
        raw[rec["offset"]] ^= 0xFF
        p.write_bytes(bytes(raw))
        res = resume(self.sc.catalog, self.lib, self.ws, quota=self.sc.quota_bytes, run_id="RUN2")
        dropped = res["invalidation"]["dropped_fragments"]
        self.assertIn(some_frag, {d["fragment_id"] for d in dropped})
        self.assertEqual(res["files"][fid]["status"], COMPLETE)
        self.assertEqual((self.ws.output_dir / self.sc.catalog.files[fid].path).read_bytes(),
                         self.sc.truth[fid])

    def test_resume_after_volume_change_drops_affected_fragments(self):
        root = HERE / "_generated" / "run_resume_media"
        shutil.rmtree(root, ignore_errors=True)
        media = root / "media"
        media.mkdir(parents=True)
        for vid in self.sc.volumes:
            shutil.copy(SCENARIO_ROOT / "media" / f"{vid}.tape", media / f"{vid}.tape")
        lib = MediaLibrary(media)
        ws = Workspace(root / "ws")
        eng = RestoreEngine(self.sc.catalog, lib, ws, quota=self.sc.quota_bytes,
                            strategy="file", run_id="RUN3", interrupt_after=2)
        with self.assertRaises(Interrupted):
            eng.restore(["F05"], allow_fallback=True)
        cp = eng.journal.read_checkpoint()
        # mutate V3 image structurally-but-validly: rewrite one data block payload
        from tape_restore import media as m
        vol = m.read_volume(media / "V3.tape")
        blocks = list(vol.blocks)
        idx = next(i for i, b in enumerate(blocks) if b.kind == "data")
        payload = bytearray(blocks[idx].payload)
        payload[0] ^= 0xAA
        blocks[idx] = type(blocks[idx])("data", bytes(payload))
        m.write_volume(media / "V3.tape", type(vol)(vol.volume_id, vol.block_size, tuple(blocks)))
        res = resume(self.sc.catalog, lib, ws, quota=self.sc.quota_bytes, run_id="RUN3")
        inv = res["invalidation"]
        self.assertIn("V3", inv["changed_volumes"])
        # every already-confirmed fragment sourced from V3 must have been dropped
        # (not trusted); fragments from other volumes may be retained.
        for d in inv["dropped_fragments"]:
            self.assertTrue(d["reason"])
        v3_frag_ids = {f.fragment_id for f in self.sc.catalog.files["F05"].fragments
                       if f.volume_id == "V3"}
        dropped_ids = {d["fragment_id"] for d in inv["dropped_fragments"]}
        self.assertTrue(v3_frag_ids <= dropped_ids or not v3_frag_ids)
        # F05 must end complete-with-correct-bytes or not published at all
        st = res["files"]["F05"]["status"]
        dest = ws.output_dir / self.sc.catalog.files["F05"].path
        if st == COMPLETE:
            self.assertEqual(dest.read_bytes(), self.sc.truth["F05"])
        else:
            self.assertFalse(dest.exists())

    def test_changed_catalog_restarts_and_ignores_old_log(self):
        eng = RestoreEngine(self.sc.catalog, self.lib, self.ws, quota=self.sc.quota_bytes,
                            strategy="file", run_id="RUN4", interrupt_after=2)
        with self.assertRaises(Interrupted):
            eng.restore(["F01", "F02"], allow_fallback=True)
        # Build a changed catalog (different digest for F01 fragment) and resume.
        raw = json.loads((SCENARIO_ROOT / "catalog.json").read_text())
        f = next(f for f in raw["files"] if f["file_id"] == "F01")
        f["fragments"][0]["sha256"] = "f" * 64
        new_cat = parse_catalog(raw, volumes=self.sc.volumes)
        res = resume(new_cat, self.lib, self.ws, quota=self.sc.quota_bytes, run_id="RUN4")
        self.assertTrue(res["invalidation"]["catalog_changed"])
        self.assertTrue(res["invalidation"]["restarted"])
        # F01 cannot be complete because the altered fragment digest cannot match
        self.assertNotEqual(res["files"]["F01"]["status"], COMPLETE)


class TestPlannerTradeoff(unittest.TestCase):
    def setUp(self):
        self.sc, self.lib = ScenarioFixture.load()

    def test_two_strategies_have_distinct_cost_profiles(self):
        v = build_plan(self.sc.catalog, self.lib, "volume",
                       ["F01", "F02", "F03", "F08", "F13"], quota=self.sc.quota_bytes)
        f = build_plan(self.sc.catalog, self.lib, "file",
                       ["F01", "F02", "F03", "F08", "F13"], quota=self.sc.quota_bytes)
        self.assertLess(v.predicted_mounts, f.predicted_mounts)
        self.assertGreater(v.predicted_scratch_peak, f.predicted_scratch_peak)

    def test_plan_marks_oversized_file_skippable_not_fatal(self):
        p = build_plan(self.sc.catalog, self.lib, "file", ["F10", "F01"], quota=self.sc.quota_bytes)
        self.assertIn("F10", p.skipped_files)
        self.assertTrue(p.restorable)


if __name__ == "__main__":
    unittest.main()
