"""记录解析：校验规则、行号/原字节范围、扩展地址语义、EOF 规则。"""

import unittest

from heximg.errors import (
    AddressOutOfRange,
    HexParseError,
    UnsupportedRecordType,
)
from heximg.records import (
    REC_EXT_LINEAR,
    parse_hex,
)
from heximg.synth import (
    corrupt_byte_count,
    corrupt_checksum,
    data_record,
    eof_record,
    extended_linear_record,
    extended_segment_record,
    start_linear_unsupported,
    start_segment_unsupported,
)


class ParseValidationTests(unittest.TestCase):
    def test_minimal_file(self):
        parsed = parse_hex(eof_record() + "\n", source="x")
        self.assertEqual(parsed.eof_line, 1)
        self.assertEqual(parsed.writes, [])

    def test_missing_colon(self):
        with self.assertRaises(HexParseError) as cm:
            parse_hex("00000001FF\n", source="x")
        self.assertEqual(cm.exception.line_no, 1)

    def test_non_hex_chars_report_line_and_range(self):
        line = data_record(0, b"\x01")
        bad = line.replace("01", "zz")
        with self.assertRaises(HexParseError) as cm:
            parse_hex(bad + "\n", source="x")
        self.assertEqual(cm.exception.line_no, 1)
        self.assertEqual(cm.exception.byte_range, (0, len(bad)))

    def test_odd_hex_length(self):
        with self.assertRaises(HexParseError) as cm:
            parse_hex(":100\n", source="x")
        self.assertEqual(cm.exception.line_no, 1)

    def test_bad_checksum(self):
        line = corrupt_checksum(data_record(0, b"\x01\x02"))
        with self.assertRaises(HexParseError) as cm:
            parse_hex(line + "\n", source="x")
        self.assertIn("校验和", str(cm.exception))
        self.assertEqual(cm.exception.line_no, 1)

    def test_byte_count_mismatch(self):
        line = corrupt_byte_count(data_record(0, b"\x01\x02"))
        with self.assertRaises(HexParseError) as cm:
            parse_hex(line + "\n", source="x")
        self.assertIn("字节数", str(cm.exception))

    def test_unsupported_type_05_and_03(self):
        for bad in (start_linear_unsupported(0x12345678),
                    start_segment_unsupported(0x1000, 0x2000)):
            with self.assertRaises(UnsupportedRecordType) as cm:
                parse_hex(bad + "\n", source="x")
            self.assertEqual(cm.exception.line_no, 1)

    def test_record_too_short(self):
        with self.assertRaises(HexParseError):
            parse_hex(":1234\n", source="x")

    def test_blank_lines_allowed_including_after_eof(self):
        text = "\n".join([
            "",
            data_record(0, b"\xaa"),
            "",
            eof_record(),
            "",
            "   ",
        ]) + "\n"
        parsed = parse_hex(text, source="x")
        self.assertEqual(len(parsed.writes), 1)
        self.assertEqual(parsed.eof_line, 4)

    def test_record_after_eof_rejected_with_line_no(self):
        text = "\n".join([
            data_record(0, b"\xaa"),
            eof_record(),
            data_record(0, b"\xbb"),
        ]) + "\n"
        with self.assertRaises(HexParseError) as cm:
            parse_hex(text, source="x")
        self.assertEqual(cm.exception.line_no, 3)
        self.assertIn("EOF", str(cm.exception))

    def test_only_one_eof(self):
        text = eof_record() + "\n" + eof_record() + "\n"
        with self.assertRaises(HexParseError) as cm:
            parse_hex(text, source="x")
        self.assertEqual(cm.exception.line_no, 2)


class ByteRangeTests(unittest.TestCase):
    def test_ranges_match_physical_offsets_lf(self):
        text = "\n".join([
            data_record(0, b"\xaa"),
            data_record(1, b"\xbb"),
            eof_record(),
        ]) + "\n"
        blob = text.encode()
        parsed = parse_hex(blob, source="x")
        # 手工核算偏移
        line0 = data_record(0, b"\xaa")
        line1 = data_record(1, b"\xbb")
        expected = [
            (0, len(line0)),
            (len(line0) + 1, len(line0) + 1 + len(line1)),
        ]
        for w, exp in zip(parsed.writes, expected):
            self.assertEqual(w.byte_range, exp)
        self.assertEqual(blob[parsed.writes[0].byte_range[0]:
                             parsed.writes[0].byte_range[1]],
                         line0.encode())
        self.assertEqual(blob[parsed.writes[1].byte_range[0]:
                             parsed.writes[1].byte_range[1]],
                         line1.encode())

    def test_ranges_match_physical_offsets_crlf(self):
        lines = [data_record(0, b"\xaa"), data_record(1, b"\xbb"),
                 eof_record()]
        blob = ("\r\n".join(lines) + "\r\n").encode()
        parsed = parse_hex(blob, source="x")
        l0, l1 = lines[0], lines[1]
        self.assertEqual(parsed.writes[0].byte_range, (0, len(l0)))
        self.assertEqual(parsed.writes[1].byte_range,
                         (len(l0) + 2, len(l0) + 2 + len(l1)))
        self.assertEqual(
            blob[parsed.writes[1].byte_range[0]:
                 parsed.writes[1].byte_range[1]],
            l1.encode(),
        )

    def test_leading_whitespace_does_not_shift_range(self):
        line = data_record(0, b"\xaa")
        text = "   " + line + "\n"
        parsed = parse_hex(text, source="x")
        # 范围覆盖物理行（含前导空白），而不是 strip 后的记录
        self.assertEqual(parsed.writes[0].byte_range,
                         (0, 3 + len(line)))


class AddressSemanticsTests(unittest.TestCase):
    def test_linear_base_replaces_not_accumulates(self):
        text = "\n".join([
            extended_linear_record(0x0001),
            data_record(0x0010, b"\x01"),
            extended_linear_record(0x0002),  # 替换，不是 0x0003
            data_record(0x0010, b"\x02"),
            eof_record(),
        ]) + "\n"
        parsed = parse_hex(text, source="x")
        self.assertEqual([w.address for w in parsed.writes],
                         [0x10010, 0x20010])

    def test_segment_base_is_shift4(self):
        text = "\n".join([
            extended_segment_record(0xFFFF),
            data_record(0x0010, b"\x01"),
            eof_record(),
        ]) + "\n"
        parsed = parse_hex(text, source="x")
        self.assertEqual(parsed.writes[0].address, 0xFFFF0 + 0x10)

    def test_cross_64kib_inside_one_record(self):
        # segment 0xFFFF + offset 0x000C，数据跨 0x100000
        text = "\n".join([
            extended_segment_record(0xFFFF),
            data_record(0x000C, bytes(8)),
            eof_record(),
        ]) + "\n"
        parsed = parse_hex(text, source="x")
        self.assertEqual(parsed.writes[0].address, 0xFFFFC)
        self.assertEqual(len(parsed.writes[0].data), 8)

    def test_address_out_of_range_rejected(self):
        # 线性基址 0xFFFF + 偏移 0xFFFE + 4 字节 → 越过 0xFFFFFFFF
        text = "\n".join([
            extended_linear_record(0xFFFF),
            data_record(0xFFFE, bytes(4)),
            eof_record(),
        ]) + "\n"
        with self.assertRaises(AddressOutOfRange) as cm:
            parse_hex(text, source="x")
        self.assertIn("超出", str(cm.exception))

    def test_boundary_address_0xffffffff_accepted(self):
        text = "\n".join([
            extended_linear_record(0xFFFF),
            data_record(0xFFFF, b"\x7E"),
            eof_record(),
        ]) + "\n"
        parsed = parse_hex(text, source="x")
        self.assertEqual(parsed.writes[0].address, 0xFFFFFFFF)

    def test_ext_address_scope_does_not_leak_between_parses(self):
        text_a = "\n".join([
            extended_linear_record(0x000A),
            data_record(0, b"\x01"),
            eof_record(),
        ]) + "\n"
        text_b = "\n".join([
            # 无任何扩展地址记录：基址必须为 0，而非泄漏的 0xA0000
            data_record(0, b"\x02"),
            eof_record(),
        ]) + "\n"
        a = parse_hex(text_a, source="a")
        b = parse_hex(text_b, source="b")
        self.assertEqual(a.writes[0].address, 0xA0000)
        self.assertEqual(b.writes[0].address, 0x0)

    def test_ext_record_data_length_validated(self):
        # 手工构造一条长度错误的 04 记录
        payload = bytes([0x01, 0x00, 0x00, REC_EXT_LINEAR, 0x00])
        ck = (-sum(payload)) & 0xFF
        line = ":" + (payload + bytes([ck])).hex().upper()
        with self.assertRaises(HexParseError):
            parse_hex(line + "\n", source="x")

    def test_eof_with_data_rejected(self):
        payload = bytes([0x01, 0, 0, 0x01, 0x00])
        ck = (-sum(payload)) & 0xFF
        line = ":" + (payload + bytes([ck])).hex().upper()
        with self.assertRaises(HexParseError):
            parse_hex(line + "\n", source="x")


if __name__ == "__main__":
    unittest.main()
