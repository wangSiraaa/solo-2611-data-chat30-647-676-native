"""流量事实仓测试：归因、聚合守恒、旧产品不变、变体重新分类。"""

import json
import os
import tempfile
import unittest

from netobs.generate import (
    build_ruleset,
    deleted_variant,
    generate_packets,
    reorder_lpm_variant,
    revised_variant,
)
from netobs.reference import (
    check_byte_conservation,
    enumerate_all,
    high_bit_points,
    reference_classify,
    small_domain_ruleset,
    verify_facts_against_reference,
)
from netobs.rules import load_ruleset, parse_ipv4, save_ruleset
from netobs.traffic import (
    Packet,
    TrafficValidationError,
    TrafficWarehouse,
    fact_identity_map,
    read_packets,
    save_analysis,
    write_packets,
)


def _packet(pid, ts, src, dst, proto="TCP", sport=1, dport=80, size=100):
    return Packet(
        packet_id=pid,
        ts=ts,
        src=parse_ipv4(src),
        dst=parse_ipv4(dst),
        protocol=proto,
        sport=sport,
        dport=dport,
        bytes=size,
    )


class PacketValidationTests(unittest.TestCase):
    def test_rejects_bad_values(self):
        with self.assertRaises(TrafficValidationError):
            _packet("a", -1, "1.1.1.1", "2.2.2.2")
        with self.assertRaises(TrafficValidationError):
            _packet("a", 0, "1.1.1.1", "2.2.2.2", size=-5)
        with self.assertRaises(TrafficValidationError):
            _packet("a", 0, "1.1.1.1", "2.2.2.2", sport=65536)
        with self.assertRaises(TrafficValidationError):
            _packet("a", 0, "1.1.1.1", "2.2.2.2", proto="ICMP")

    def test_duplicate_packet_id_rejected(self):
        rs = build_ruleset()
        wh = TrafficWarehouse(rs)
        packet = _packet("dup", 0, "1.1.1.1", "2.2.2.2")
        with self.assertRaises(TrafficValidationError):
            wh.load([packet, packet])


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.ruleset = build_ruleset()
        self.packets = generate_packets(self.ruleset, count=1500, seed=5)
        self.warehouse = TrafficWarehouse(self.ruleset, window_seconds=60)
        self.analysis = self.warehouse.load(self.packets)

    def test_every_packet_attributed_to_reference(self):
        self.assertEqual(verify_facts_against_reference(self.ruleset, self.analysis.facts), [])

    def test_conservation_including_deny(self):
        self.assertEqual(check_byte_conservation(self.analysis, self.packets, self.ruleset), [])
        deny = [s for s in self.analysis.summaries if s.action == "DENY"]
        self.assertGreater(sum(s.packets for s in deny), 0)
        self.assertGreater(sum(s.bytes for s in deny), 0)

    def test_source_packet_provenance(self):
        for summary in self.analysis.summaries:
            self.assertEqual(summary.packets, len(summary.source_packet_ids))
            self.assertEqual(len(set(summary.source_packet_ids)), len(summary.source_packet_ids))

    def test_window_partition(self):
        starts = {(p.ts // 60) * 60 for p in self.packets}
        self.assertEqual({s.window_start for s in self.analysis.summaries}, starts)

    def test_product_is_deterministic_and_old_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "facts.json")
            digest = save_analysis(self.analysis, path)
            with open(path, "rb") as fh:
                before = fh.read()
            # 用另一个规则产品装载，写入不同文件；旧文件必须不变
            variant = reorder_lpm_variant(self.ruleset)
            other = TrafficWarehouse(variant, window_seconds=60).load(self.packets)
            other_path = os.path.join(tmp, "facts_variant.json")
            save_analysis(other, other_path)
            with open(path, "rb") as fh:
                after = fh.read()
            self.assertEqual(before, after)
            self.assertNotEqual(digest, other.digest)
            # 重新装载同一产品得到同一摘要
            again = TrafficWarehouse(self.ruleset, window_seconds=60).load(self.packets)
            self.assertEqual(save_analysis(again, os.path.join(tmp, "facts2.json")), digest)

    def test_reorder_changes_at_least_one_hit(self):
        variant = reorder_lpm_variant(self.ruleset)
        other = TrafficWarehouse(variant, window_seconds=60).load(self.packets)
        base_map = fact_identity_map(self.analysis)
        other_map = fact_identity_map(other)
        changed = [pid for pid in base_map if base_map[pid] != other_map[pid]]
        self.assertGreater(len(changed), 0)
        # 总量仍然守恒
        self.assertEqual(other.bytes_total, self.analysis.bytes_total)
        self.assertEqual(other.packets_total, self.analysis.packets_total)

    def test_revision_and_deletion_reclassify(self):
        for variant in (revised_variant(self.ruleset), deleted_variant(self.ruleset)):
            other = TrafficWarehouse(variant, window_seconds=60).load(self.packets)
            self.assertEqual(other.ruleset_digest, variant.digest)
            self.assertNotEqual(other.digest, self.analysis.digest)
            self.assertEqual(verify_facts_against_reference(variant, other.facts), [])
            base_map = fact_identity_map(self.analysis)
            other_map = fact_identity_map(other)
            self.assertGreater(sum(1 for pid in base_map if base_map[pid] != other_map[pid]), 0)

    def test_product_binds_ruleset_digest(self):
        self.assertEqual(self.analysis.ruleset_digest, self.ruleset.digest)
        payload = json.loads(self.analysis.canonical_json())
        self.assertEqual(payload["ruleset_digest"], self.ruleset.digest)


class FileRoundtripTests(unittest.TestCase):
    def test_packets_roundtrip(self):
        ruleset = build_ruleset()
        packets = generate_packets(ruleset, count=50)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.jsonl")
            write_packets(packets, path)
            self.assertEqual(read_packets(path), packets)

    def test_rules_roundtrip(self):
        ruleset = build_ruleset()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            save_ruleset(ruleset, path)
            self.assertEqual(load_ruleset(path), ruleset)


class FullEnumerationTests(unittest.TestCase):
    def test_small_domain_exhaustive(self):
        ruleset, domain = small_domain_ruleset()
        result = enumerate_all(ruleset, **domain)
        self.assertEqual(result.points, 8 * 8 * 2 * 6 * 6)
        self.assertTrue(result.ok, result.mismatches)

    def test_high_bit_points(self):
        ruleset = build_ruleset()
        for point in high_bit_points():
            expected = reference_classify(ruleset, *point)
            self.assertEqual(ruleset.classify(*point), expected)
            self.assertEqual(TrafficWarehouse(ruleset).classify_packet(
                Packet(
                    packet_id="x",
                    ts=0,
                    src=point[0],
                    dst=point[1],
                    protocol=point[2],
                    sport=point[3],
                    dport=point[4],
                    bytes=0,
                )
            ).rule_id, expected[0])


if __name__ == "__main__":
    unittest.main()
