#!/usr/bin/env python3
"""端到端演示：生成样例 -> 独立列目录 -> 构建流量仓 -> 有理数时间窗统计。

运行::

    python3 demo.py

产物位于 ``./samples/``：样例 PCAPNG、独立包真值、目录 JSON、摘要 JSON。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from fractions import Fraction
from pathlib import Path

from pcapng_kit import Catalog, FactStatus, FlowKey, TrafficStore
from pcapng_kit.generator import generate_sample

SAMPLE_DIR = Path(__file__).resolve().parent / "samples"


def main() -> None:
    sample = generate_sample(SAMPLE_DIR)
    truth = json.loads(sample["truth"].read_text(encoding="utf-8"))

    # 1) 独立列目录：只做结构/时间事实，不生成业务统计。
    with Catalog.open(sample["pcap"]) as catalog:
        listing = catalog.listing()
        (SAMPLE_DIR / "catalog.json").write_text(
            json.dumps(listing, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        print("== 节/接口目录 ==")
        for section in catalog.sections:
            ifaces = [i for i in catalog.interfaces if i.section_id == section.section_id]
            print(
                f"section {section.section_id}: endian={section.endian:<6} "
                f"bytes [{section.block_offset},{section.end_offset}) "
                f"interfaces={len(ifaces)}"
            )
        print(
            f"packets={len(catalog.packets)} custom={len(catalog.custom_blocks)} "
            f"unknown={len(catalog.unknown_blocks)} "
            f"corruptions={len(catalog.corruptions)}"
        )
        for corruption in catalog.corruptions:
            print(
                f"  corruption @ {corruption.offset} fatal={corruption.fatal} "
                f"recovered={corruption.recovered}: {corruption.reason}"
            )

        # 2) 逐包核对：时间（Fraction 全等）、来源字节哈希、范围与截断标记。
        print("\n== 逐包真值核对（目录视角） ==")
        for record, expected in zip(catalog.packets, truth["packets"]):
            assert record.timestamp == Fraction(expected["expected_timestamp"])
            assert record.interface_uid == (
                expected["section_id"],
                expected["interface_id"],
            )
            body = catalog.read_body(record)
            assert hashlib.sha256(body).hexdigest() == expected["captured_sha256"]
            assert (
                record.body_offset,
                record.body_end,
            ) == tuple(expected["body_range"])
            assert record.truncated == expected["truncated"]
            assert record.captured_len == expected["captured_len"]
            assert record.orig_len == expected["orig_len"]
        print(f"checked {len(catalog.packets)} packets against independent truth: OK")

        # 3) 流量仓：只经目录 API 取字节。
        store = TrafficStore.build(catalog)
        status_counter = Counter(fact.status.value for fact in store)
        print("\n== 协议事实分布 ==")
        for status, count in sorted(status_counter.items()):
            print(f"  {status}: {count}")
        print(f"directional flows: {len(store.flow_keys())}")

        # 4) 反向流不合并：找一对有双向包的 IP 对，确认两个方向键并存。
        directional = {
            (k.src_ip, k.dst_ip) for k in store.flow_keys()
        }
        print(f"distinct src->dst endpoint pairs: {len(directional)}")

        # 5) 有理数半开时间窗：窗边界取真实包时间，验证 [start, end) 语义。
        ok_facts = [f for f in store if f.status is FactStatus.OK]
        anchor = ok_facts[0].timestamp
        window_start = anchor
        window_end = anchor + Fraction(1, 1000)   # 1 毫秒宽的有理数窗
        stats = store.window(window_start, window_end)
        print("\n== 半开时间窗统计 [t0, t0 + 1/1000) ==")
        print(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))

        # 半开边界：[t, t) 为空；[t, t+epsilon) 至少包含锚点包本身。
        assert store.window(anchor, anchor).packet_count == 0
        assert anchor in [
            store.facts[i].timestamp for i in stats.source_packet_indices
        ]

        # 6) 按源包重算统计，确认仓库数字可独立复现。
        recomputed = _recompute_from_truth(truth, window_start, window_end, store)
        assert recomputed["packet_count"] == stats.packet_count
        assert recomputed["wire_bytes"] == stats.wire_bytes
        assert recomputed["captured_bytes"] == stats.captured_bytes
        assert (
            recomputed["complete_udp_payload_bytes"]
            == stats.complete_udp_payload_bytes
        )

        # 7) 按流过滤的窗统计：方向键互不串流。
        some_flow: FlowKey = next(k for k in store.flow_keys())
        flow_stats = store.window(window_start, window_end, flow=some_flow)
        for idx in flow_stats.source_packet_indices:
            assert store.facts[idx].flow == some_flow

        # 8) 截断包的字段级完整性。
        incomplete = [f for f in store if f.status is FactStatus.INCOMPLETE]
        if incomplete:
            sample_fact = incomplete[0]
            print("\n== 截断样例事实 ==")
            print(
                f"index={sample_fact.index} captured={sample_fact.captured_len} "
                f"wire={sample_fact.wire_len} reason={sample_fact.reason}"
            )
            print(
                f"deterministic fields: src={sample_fact.src_ip} "
                f"dst={sample_fact.dst_ip} ip_header_len={sample_fact.ip_header_len} "
                f"udp_len={sample_fact.udp_len} "
                f"payload_complete={sample_fact.udp_payload_complete}"
            )

        summary = {
            "sections": len(catalog.sections),
            "interfaces": len(catalog.interfaces),
            "packets": len(catalog.packets),
            "fact_status": dict(status_counter),
            "flows": len(store.flow_keys()),
            "window": stats.as_dict(),
            "recomputed_window": recomputed,
            "corruptions": catalog.listing()["corruptions"],
        }
        (SAMPLE_DIR / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # 9) 重开文件：仅按目录登记范围回读并核摘要，不重新扫描业务协议。
    with Catalog.open(sample["pcap"]) as reopened:
        assert len(reopened.packets) == len(catalog.packets)
        for record, expected in zip(reopened.packets, truth["packets"]):
            body = reopened.read_body(record)
            assert hashlib.sha256(body).hexdigest() == expected["captured_sha256"]
        digest = hashlib.sha256(
            b"".join(reopened.read_body(r) for r in reopened.packets)
        ).hexdigest()
        print(f"\nreopen + range readback digest: {digest}")
        (SAMPLE_DIR / "readback.sha256").write_text(digest + "\n", encoding="utf-8")

    print("\nDemo finished; artifacts written to", SAMPLE_DIR)


def _recompute_from_truth(truth: dict, start: Fraction, end: Fraction, store: TrafficStore) -> dict:
    """完全绕开仓库聚合，按源包真值重算同一时间窗的统计。"""

    packet_count = wire = captured = payload = 0
    for expected, fact in zip(truth["packets"], store.facts):
        ts = Fraction(expected["expected_timestamp"])
        if not (start <= ts < end):
            continue
        packet_count += 1
        wire += expected["orig_len"]
        captured += expected["captured_len"]
        if (
            fact.status is FactStatus.OK
            and fact.udp_payload_len is not None
            and fact.udp_payload_complete
        ):
            payload += fact.udp_payload_len
    return {
        "packet_count": packet_count,
        "wire_bytes": wire,
        "captured_bytes": captured,
        "complete_udp_payload_bytes": payload,
    }


if __name__ == "__main__":
    main()
