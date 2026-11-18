"""pcapng_kit 的 unittest 测试套件（仅标准库）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from pcapng_kit import (
    BodyBufferLimitError,
    Catalog,
    FactStatus,
    FlowKey,
    PcapngContractError,
    PcapngCorruptionError,
    TrafficStore,
)
from pcapng_kit.blocks import (
    BT_EPB,
    BYTE_ORDER_MAGIC,
    LINKTYPE_ETHERNET,
    LINKTYPE_LINUX_SLL,
    OPT_IF_TSOFFSET,
    OPT_IF_TSRESOL,
)
from pcapng_kit.generator import (
    SECTION_PLAN,
    build_arp_frame,
    build_sll_udp_frame,
    build_tcp_frame,
    build_udp_frame,
    expected_timestamp,
    generate_sample,
    generate_scale_sample,
)
from pcapng_kit.writer import InterfaceSpec, PcapNGWriter

ROOT = Path(__file__).resolve().parent.parent


class _TempFileMixin:
    def setUp(self) -> None:  # noqa: D401
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> Path:
        return self.tmp / name


class StructureTests(_TempFileMixin, unittest.TestCase):
    def test_section_endianness_switches_within_one_file(self) -> None:
        target = self.path("endian.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 100, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"a"))
            w.start_section("big")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 200, build_udp_frame("3.3.3.3", "4.4.4.4", 3, 4, b"bb"))

        with Catalog.open(target) as cat:
            self.assertEqual([s.endian for s in cat.sections], ["little", "big"])
            self.assertEqual(cat.packets[0].endian, "little")
            self.assertEqual(cat.packets[1].endian, "big")
            # 第二节能正确按大端解释时间戳整数。
            self.assertEqual(cat.packets[1].timestamp, Fraction(200, 1_000_000))

    def test_decimal_and_binary_resolutions_and_negative_offsets(self) -> None:
        target = self.path("res.pcapng")
        cases = [
            ("little", 10, 6, 0, 1_000_000),
            ("big", 10, 9, -37, 2_000_000_000),
            ("little", 2, 24, 5, 16_777_216),
            ("big", 2, 32, -123456, 4_294_967_296),
        ]
        with PcapNGWriter(target) as w:
            for endian, base, exponent, offset, raw in cases:
                w.start_section(endian)
                w.write_interface(
                    InterfaceSpec(
                        ts_base=base, ts_exponent=exponent, ts_offset=offset
                    )
                )
                w.write_packet(0, raw, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))

        with Catalog.open(target) as cat:
            self.assertEqual(len(cat.sections), len(cases))
            for section, (endian, base, exponent, offset, raw) in zip(
                cat.sections, cases
            ):
                iface = cat.interfaces[section.section_id]
                self.assertEqual(iface.ts_base, base)
                self.assertEqual(iface.ts_exponent, exponent)
                self.assertEqual(iface.ts_offset, Fraction(offset))
                pkt = cat.packets[section.section_id]
                self.assertEqual(
                    pkt.timestamp,
                    expected_timestamp(raw, base, exponent, offset),
                )
                # 二进制分辨率必须保持精确有理数，不得出现二进制浮点尾巴。
                self.assertIsInstance(pkt.timestamp, Fraction)

    def test_64bit_timestamp_uses_high_and_low_words(self) -> None:
        target = self.path("ts64.pcapng")
        raw = (0x11223344 << 32) | 0x55667788
        with PcapNGWriter(target) as w:
            w.start_section("big")
            w.write_interface(InterfaceSpec(ts_base=10, ts_exponent=9))
            w.write_packet(0, raw, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))
        with Catalog.open(target) as cat:
            pkt = cat.packets[0]
            self.assertEqual(pkt.ts_high, 0x11223344)
            self.assertEqual(pkt.ts_low, 0x55667788)
            self.assertEqual(pkt.timestamp, Fraction(raw, 1_000_000_000))

    def test_same_timestamp_multiple_packets_kept_distinct(self) -> None:
        target = self.path("same_ts.pcapng")
        frame = build_udp_frame("1.1.1.1", "2.2.2.2", 9, 9, b"x")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            for _ in range(3):
                w.write_packet(0, 777, frame)
        with Catalog.open(target) as cat:
            self.assertEqual(len(cat.packets), 3)
            self.assertTrue(all(p.timestamp == Fraction(777, 1_000_000) for p in cat.packets))
            # 三个包的字节范围互不重叠。
            ranges = [(p.block_offset, p.block_end) for p in cat.packets]
            self.assertEqual(len(set(ranges)), 3)

    def test_snaplen_truncation_is_flagged_and_body_bounded(self) -> None:
        target = self.path("trunc.pcapng")
        frame = build_udp_frame("10.0.0.1", "10.0.0.2", 4000, 53, b"A" * 80)
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(snaplen=40))
            info = w.write_packet(0, 1, frame, snaplen=40)
        self.assertTrue(info["truncated"])
        with Catalog.open(target) as cat:
            pkt = cat.packets[0]
            self.assertTrue(pkt.truncated)
            self.assertEqual(pkt.captured_len, 40)
            self.assertEqual(pkt.orig_len, len(frame))
            body = cat.read_body(pkt)
            self.assertEqual(len(body), 40)
            # 正文范围严格落在块范围内，且不含 4 字节对齐填充。
            self.assertEqual(pkt.body_end - pkt.body_offset, 40)
            self.assertGreaterEqual(pkt.body_offset, pkt.block_offset)
            self.assertLessEqual(pkt.body_end, pkt.block_end)

            store = TrafficStore.build(cat)
            fact = store.facts[0]
            self.assertEqual(fact.status, FactStatus.INCOMPLETE)
            # 仍可确定 IP/UDP 字段；完整性显式为 False。
            self.assertEqual(fact.src_ip, "10.0.0.1")
            self.assertEqual(fact.dst_ip, "10.0.0.2")
            self.assertFalse(fact.udp_payload_complete)

    def test_duplicate_single_valued_options_are_contract_violations(self) -> None:
        target = self.path("dup_opt.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())  # 接口 0：合同内
            # 手工构造带两个 if_tsresol 的 IDB。
            def opt(code: int, value: bytes) -> bytes:
                prefix = w._pack("HH", code, len(value))
                pad = b"\x00" * (-len(value) % 4)
                return prefix + value + pad
            body = w._pack("HHI", LINKTYPE_ETHERNET, 0, 0)
            body += opt(OPT_IF_TSRESOL, b"\x06")
            body += opt(OPT_IF_TSRESOL, b"\x09")
            body += w._pack("HH", 0, 0)
            w._write_block(0x00000001, body)
            w.write_packet(0, 1, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))

        with Catalog.open(target) as cat:
            # 违规 IDB 被隔离；接口 0 仍来自合法 IDB，EPB 正常入库。
            self.assertEqual(len(cat.interfaces), 1)
            self.assertEqual(len(cat.packets), 1)
            self.assertTrue(
                any("duplicate if_tsresol" in c.reason for c in cat.corruptions)
            )

    def test_repeated_if_name_is_allowed_and_preserved(self) -> None:
        target = self.path("dup_name.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(name="eth0", duplicate_name=True))
        with Catalog.open(target) as cat:
            iface = cat.interfaces[0]
            self.assertEqual(iface.names, ["eth0", "eth0-alias"])

    def test_trailer_length_corruption_is_isolated_and_section_recovers(self) -> None:
        target = self.path("trailer.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))
            bad_body = w._pack("IIIII", 0, 0, 0, 1, 1) + b"\x00" * 4
            # 24 字节正文 -> 总长度 36（4 字节对齐），仅尾长度被篡改。
            w.write_corrupt_trailer(BT_EPB, bad_body, bad_trailer=0xDEADBEEF)
            w.start_section("big")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 2, build_udp_frame("3.3.3.3", "4.4.4.4", 3, 4, b"y"))

        with Catalog.open(target) as cat:
            fatal = [c for c in cat.corruptions if c.fatal]
            self.assertEqual(len(fatal), 1)
            self.assertIn("trailing length", fatal[0].reason)
            self.assertTrue(fatal[0].recovered)
            self.assertEqual(len(cat.sections), 2)
            # 损坏块前后的包都保留，扫描没有中断。
            self.assertEqual(len(cat.packets), 2)
            self.assertEqual(cat.packets[1].section_id, 1)

    def test_corrupt_first_shb_recovers_at_next_shb(self) -> None:
        target = self.path("bad_first_shb.pcapng")
        with PcapNGWriter(target) as w:
            # 先写一个合法 SHB 取得正确字节，再把它的尾长度改坏。
            start, _end = w.start_section("little")
            w.write_interface(InterfaceSpec())
        data = bytearray(target.read_bytes())
        total = int.from_bytes(data[4:8], "little")
        data[total - 4: total] = (0xDEADBEEF).to_bytes(4, "little")
        with PcapNGWriter(target) as w2:
            w2.write_raw(bytes(data))
            w2.start_section("big")
            w2.write_interface(InterfaceSpec())
            w2.write_packet(0, 9, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"z"))
        with Catalog.open(target) as cat:
            fatal = [c for c in cat.corruptions if c.fatal]
            self.assertTrue(fatal)
            self.assertTrue(fatal[0].recovered)
            # 坏 SHB 被跳过，恢复节正常入库。
            self.assertEqual(len(cat.sections), 1)
            self.assertEqual(cat.sections[0].endian, "big")
            self.assertEqual(len(cat.packets), 1)

    def test_unrecoverable_trailing_corruption_keeps_prefix(self) -> None:
        target = self.path("tail_bad.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))
            # 结尾追加无法再同步的碎片（不含任何 SHB magic）。
            w.write_raw(b"\x00" * 16 + b"junk-without-magic")
        with Catalog.open(target) as cat:
            self.assertEqual(len(cat.packets), 1)
            self.assertTrue(any(not c.recovered for c in cat.corruptions))

    def test_unaligned_block_length_is_corruption(self) -> None:
        target = self.path("align.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            # 手工写一个总长度 13（未 4 字节对齐）的未知块。
            w.write_raw(w._pack("II", 0x00000099, 13) + b"\x00" * 5 + w._pack("I", 13))
            w.start_section("big")
            w.write_interface(InterfaceSpec())
        with Catalog.open(target) as cat:
            self.assertTrue(any("4-byte aligned" in c.reason for c in cat.corruptions))
            self.assertEqual(len(cat.sections), 2)

    def test_unknown_blocks_are_skipped_never_parsed_as_packets(self) -> None:
        target = self.path("unknown.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_unknown_block(0x00000005, b"interface statistics-ish")
            w.write_custom_block(b"private payload", private=True)
            w.write_packet(0, 1, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"x"))
        with Catalog.open(target) as cat:
            self.assertEqual(len(cat.unknown_blocks), 1)
            self.assertEqual(len(cat.custom_blocks), 1)
            self.assertEqual(len(cat.packets), 1)
            # 自定义块正文可按登记范围原样回读。
            cb = cat.custom_blocks[0]
            raw = cat.read_range(cb.body_offset, cb.body_end - cb.body_offset)
            # 自定义块正文 = 4 字节私有字段 + 4 字节对齐后的载荷。
            self.assertTrue(raw.startswith(b"\x00\x00\x00\x00"))
            self.assertIn(b"private payload", raw)

    def test_epb_referencing_missing_interface_is_contract_violation(self) -> None:
        target = self.path("noif.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"ok"))
            w.write_packet(5, 2, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"bad"))
        with Catalog.open(target) as cat:
            self.assertEqual(len(cat.packets), 1)
            self.assertTrue(any("interface 5" in c.reason for c in cat.corruptions))

    def test_interface_resolution_does_not_leak_across_sections(self) -> None:
        target = self.path("noleak.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(ts_base=10, ts_exponent=9, ts_offset=-9))
            w.write_packet(0, 1_000_000_000, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"a"))
            w.start_section("big")
            # 同号接口 0，但故意用缺省分辨率（10^-6）且无偏移。
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1_000_000, build_udp_frame("1.1.1.1", "2.2.2.2", 1, 2, b"b"))
        with Catalog.open(target) as cat:
            self.assertEqual(cat.interfaces[0].resolution, Fraction(1, 1_000_000_000))
            self.assertEqual(cat.interfaces[1].resolution, Fraction(1, 1_000_000))
            self.assertEqual(cat.packets[0].timestamp, Fraction(1) - 9)
            self.assertEqual(cat.packets[1].timestamp, Fraction(1))


class TrafficTests(_TempFileMixin, unittest.TestCase):
    def _build_store(self, target: Path) -> tuple[Catalog, TrafficStore]:
        cat = Catalog.open(target)
        store = TrafficStore.build(cat)
        return cat, store

    def test_supported_and_unsupported_classification(self) -> None:
        target = self.path("traffic.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(link_type=LINKTYPE_ETHERNET))
            w.write_interface(InterfaceSpec(link_type=LINKTYPE_LINUX_SLL, name="sll"))
            w.write_packet(0, 1, build_udp_frame("10.0.0.1", "10.0.0.2", 1000, 53, b"q"))
            w.write_packet(0, 2, build_arp_frame())
            w.write_packet(0, 3, build_tcp_frame("10.0.0.1", "10.0.0.2", 1000, 80))
            w.write_packet(1, 4, build_sll_udp_frame(b"sll"))
        with Catalog.open(target) as cat:
            store = TrafficStore.build(cat)
            statuses = [f.status for f in store]
            self.assertEqual(statuses[0], FactStatus.OK)
            self.assertTrue(all(s is FactStatus.UNSUPPORTED for s in statuses[1:]))
            # unsupported 不是坏文件：没有框架/合同损坏。
            self.assertEqual(cat.corruptions, [])

    def test_reverse_direction_and_cross_section_never_merged(self) -> None:
        target = self.path("flows.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, build_udp_frame("10.0.0.1", "10.0.0.2", 1111, 2222, b"fwd"))
            w.write_packet(0, 2, build_udp_frame("10.0.0.2", "10.0.0.1", 2222, 1111, b"rev"))
            w.start_section("big")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 3, build_udp_frame("10.0.0.1", "10.0.0.2", 1111, 2222, b"other-section"))
        with Catalog.open(target) as cat:
            store = TrafficStore.build(cat)
            self.assertEqual(len(store.flow_keys()), 3)
            fwd = FlowKey(0, 0, "10.0.0.1", "10.0.0.2", 1111, 2222)
            rev = FlowKey(0, 0, "10.0.0.2", "10.0.0.1", 2222, 1111)
            cross = FlowKey(1, 0, "10.0.0.1", "10.0.0.2", 1111, 2222)
            self.assertIn(fwd, store.flow_keys())
            self.assertIn(rev, store.flow_keys())
            self.assertIn(cross, store.flow_keys())
            stats = store.window(Fraction(0), Fraction(100), flow=fwd)
            self.assertEqual(stats.packet_count, 1)
            self.assertEqual(stats.source_packet_indices, [0])

    def test_half_open_window_counts_and_payload_length(self) -> None:
        target = self.path("window.pcapng")
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            payload = b"hello"  # 5 字节 UDP 正文
            for raw in (100, 200, 300):
                w.write_packet(
                    0, raw,
                    build_udp_frame("10.0.0.1", "10.0.0.2", 1, 2, payload),
                )
        with Catalog.open(target) as cat:
            store = TrafficStore.build(cat)
            t = lambda us: Fraction(us, 1_000_000)
            # 半开：下界包计入，上界包排除。
            stats = store.window(t(100), t(300))
            self.assertEqual(stats.packet_count, 2)
            self.assertEqual(stats.source_packet_indices, [0, 1])
            self.assertEqual(stats.complete_udp_packet_count, 2)
            self.assertEqual(stats.complete_udp_payload_bytes, 10)
            self.assertEqual(stats.wire_bytes, sum(f.wire_len for f in store.facts[:2]))
            self.assertEqual(
                stats.captured_bytes, sum(f.captured_len for f in store.facts[:2])
            )
            # 空窗与零宽窗。
            self.assertEqual(store.window(t(999), t(1000)).packet_count, 0)
            self.assertEqual(store.window(t(200), t(200)).packet_count, 0)

    def test_ipv4_options_header_length_is_honored(self) -> None:
        target = self.path("ihl.pcapng")
        frame = build_udp_frame("10.0.0.1", "10.0.0.2", 1, 2, b"opts", ihl_words=7)
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, frame)
        with Catalog.open(target) as cat:
            store = TrafficStore.build(cat)
            fact = store.facts[0]
            self.assertEqual(fact.status, FactStatus.OK)
            self.assertEqual(fact.ip_header_len, 28)
            self.assertEqual(fact.udp_payload_len, 4)

    def test_traffic_module_never_opens_files_directly(self) -> None:
        # 结构保证：traffic 模块源码不导入 open/pathlib/os 等文件入口，
        # 所有字节都经 Catalog 的公开 API 获取。
        import inspect

        import pcapng_kit.traffic as traffic_mod

        source = inspect.getsource(traffic_mod)
        for forbidden in ("open(", "pathlib", "import os", "from os"):
            self.assertNotIn(forbidden, source)

    def test_body_buffer_limit_is_enforced(self) -> None:
        target = self.path("big.pcapng")
        big = build_udp_frame("10.0.0.1", "10.0.0.2", 1, 2, b"Z" * 60000)
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, big)
        with Catalog.open(target, body_buffer_limit=1024) as cat:
            self.assertEqual(cat.packets[0].captured_len, len(big))
            with self.assertRaises(BodyBufferLimitError):
                cat.read_body(cat.packets[0])
            store = TrafficStore.build(cat)
            self.assertEqual(store.facts[0].status, FactStatus.READ_ERROR)


class SampleTruthTests(_TempFileMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        cls.sample = generate_sample(cls.out)
        cls.truth = json.loads(Path(cls.sample["truth"]).read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_packet_matches_independent_truth(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            self.assertEqual(len(cat.packets), len(self.truth["packets"]))
            for pkt, expected in zip(cat.packets, self.truth["packets"]):
                self.assertEqual(
                    pkt.timestamp, Fraction(expected["expected_timestamp"])
                )
                self.assertEqual(
                    pkt.interface_uid,
                    (expected["section_id"], expected["interface_id"]),
                )
                self.assertEqual(
                    (pkt.body_offset, pkt.body_end), tuple(expected["body_range"])
                )
                self.assertEqual(
                    (pkt.block_offset, pkt.block_end), tuple(expected["block_range"])
                )
                self.assertEqual(pkt.captured_len, expected["captured_len"])
                self.assertEqual(pkt.orig_len, expected["orig_len"])
                self.assertEqual(pkt.truncated, expected["truncated"])
                body = cat.read_body(pkt)
                self.assertEqual(
                    hashlib.sha256(body).hexdigest(),
                    expected["captured_sha256"],
                )

    def test_section_plans_reflected_in_catalog(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            for section, plan in zip(cat.sections[:5], SECTION_PLAN):
                endian, base, exponent, offset = plan
                self.assertEqual(section.endian, endian)
                ifaces = [
                    i for i in cat.interfaces if i.section_id == section.section_id
                ]
                self.assertTrue(all(i.ts_base == base for i in ifaces))
                self.assertTrue(all(i.ts_exponent == exponent for i in ifaces))
                self.assertTrue(all(i.ts_offset == Fraction(offset) for i in ifaces))
            # 恢复节存在且为大端。
            self.assertEqual(cat.sections[-1].endian, "big")

    def test_stats_recomputed_from_source_truth(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            store = TrafficStore.build(cat)
            t0 = min(p.timestamp for p in cat.packets)
            start, end = t0, t0 + Fraction(1, 100)
            stats = store.window(start, end)

            count = wire = captured = payload = 0
            for expected, fact in zip(self.truth["packets"], store.facts):
                ts = Fraction(expected["expected_timestamp"])
                if start <= ts < end:
                    count += 1
                    wire += expected["orig_len"]
                    captured += expected["captured_len"]
                    if fact.status is FactStatus.OK and fact.udp_payload_complete:
                        payload += fact.udp_payload_len
            self.assertEqual(stats.packet_count, count)
            self.assertEqual(stats.wire_bytes, wire)
            self.assertEqual(stats.captured_bytes, captured)
            self.assertEqual(stats.complete_udp_payload_bytes, payload)

    def test_reopen_range_readback_matches_digest(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            digest1 = hashlib.sha256(
                b"".join(cat.read_body(p) for p in cat.packets)
            ).hexdigest()
        with Catalog.open(self.sample["pcap"]) as reopened:
            digest2 = hashlib.sha256(
                b"".join(reopened.read_body(p) for p in reopened.packets)
            ).hexdigest()
            # 重开后逐范围回读与首遍完全一致。
            self.assertEqual(digest1, digest2)

    def test_planned_corruption_recorded_and_recovered(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            planned = self.truth["corruptions_planned"][0]
            hit = next(
                c for c in cat.corruptions if c.offset == planned["offset"]
            )
            self.assertTrue(hit.fatal)
            self.assertTrue(hit.recovered)
            self.assertIn(planned["reason_contains"], hit.reason)

    def test_truncated_packets_never_counted_as_complete_payload(self) -> None:
        with Catalog.open(self.sample["pcap"]) as cat:
            store = TrafficStore.build(cat)
            for fact in store.facts:
                if fact.record.truncated:
                    self.assertNotEqual(fact.status, FactStatus.OK)
                    self.assertFalse(fact.udp_payload_complete)


class ScaleTest(_TempFileMixin, unittest.TestCase):
    def test_100k_packets_five_sections_twenty_interfaces(self) -> None:
        result = generate_scale_sample(
            self.tmp, sections=5, interfaces=20, packets=100_000
        )
        with Catalog.open(result["pcap"]) as cat:
            self.assertEqual(len(cat.sections), 5)
            self.assertEqual(
                sum(
                    1
                    for s in cat.sections
                    for i in cat.interfaces
                    if i.section_id == s.section_id
                ),
                100,
            )
            self.assertEqual(len(cat.packets), 100_000)

            # 按 JSONL 真值逐包核对时间与来源哈希。
            with open(result["truth"], encoding="utf-8") as handle:
                for pkt, line in zip(cat.packets, handle):
                    expected = json.loads(line)
                    self.assertEqual(
                        pkt.timestamp, Fraction(expected["expected_timestamp"])
                    )
                    body = cat.read_body(pkt)
                    self.assertEqual(
                        hashlib.sha256(body).hexdigest(),
                        expected["captured_sha256"],
                    )
            store = TrafficStore.build(cat)
            self.assertEqual(len(store), 100_000)
            # 全量窗统计可运行且包数守恒。
            full = store.window(Fraction(-10**12), Fraction(10**18))
            self.assertEqual(full.packet_count, 100_000)
            self.assertEqual(
                full.captured_bytes, sum(p.captured_len for p in cat.packets)
            )


if __name__ == "__main__":
    unittest.main()
