#!/usr/bin/env python3
"""一键复跑: 本地生成对象, 归档, 制造缺片/坏片/跨条带/截断清单等场景,
从独立恢复目录恢复并与生成器真值逐字节比对。

用法:
  PYTHONPATH=. python3 scripts/demo.py [工作目录]
不传工作目录时使用系统临时目录。退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecstore import shard as shard_mod  # noqa: E402
from ecstore.manifest import parse_manifest  # noqa: E402
from ecstore.repository import ObjectRepository  # noqa: E402


def gen(seed: int, size: int) -> bytes:
    out = bytearray(size)
    state = seed or 1
    for i in range(size):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (state >> 16) & 0xFF
    return bytes(out)


def main() -> int:
    work = Path(sys.argv[1] if len(sys.argv) > 1
                else tempfile.mkdtemp(prefix="ecstore_demo_"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"工作目录: {work}")

    repo = ObjectRepository(work / "repo")
    repo.service.register(3, 5, 64)  # k=3 n=5 L=64, 条带 192 字节

    truth = gen(4242, 500)  # 3 个条带, 末条带补零
    truth_sha = hashlib.sha256(truth).hexdigest()
    repo.import_object(truth, "demo-object")
    src = repo.root / "objects" / truth_sha[:16]

    def stage(name: str) -> Path:
        d = work / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        shutil.copy(src / "manifest.json", d / "manifest.json")
        shutil.copytree(src / "shards", d / "shards")
        return d

    # 1. 完整恢复
    d = stage("rec_full")
    r = repo.recover_object(d, work / "full.bin")
    assert r.complete and (work / "full.bin").read_bytes() == truth
    print(f"[1] 完整恢复: complete={r.complete}, {r.exported_length} 字节, 真值一致")

    # 2. 每条带删 2 片 (剩 k=3 片) 仍恢复
    d = stage("rec_minimal")
    for f in d.joinpath("shards").iterdir():
        if any(f"piece_{i:02d}" in f.name for i in (3, 4)):
            f.unlink()
    r = repo.recover_object(d, work / "minimal.bin")
    assert r.complete and (work / "minimal.bin").read_bytes() == truth
    print(f"[2] 每带仅 k 片: complete={r.complete}, 有效序号 {r.stripe_status[0].valid_indices}")

    # 3. 坏片: 翻转正文 -> 隔离, 其余片仍够恢复
    d = stage("rec_corrupt")
    victim = sorted(d.joinpath("shards").iterdir())[0]
    raw = bytearray(victim.read_bytes())
    raw[-1] ^= 0xFF
    victim.write_bytes(raw)
    r = repo.recover_object(d, work / "corrupt.bin")
    assert r.complete and (work / "corrupt.bin").read_bytes() == truth
    print(f"[3] 坏片隔离: quarantined={len(r.stripe_status[0].quarantined)}, 仍完整恢复")

    # 4. 缺到不足 k: 部分恢复, 只给已验证前缀, 不标 complete
    d = stage("rec_partial")
    for f in d.joinpath("shards").iterdir():
        if f.name.startswith("stripe_0002"):
            f.unlink()  # 最后一条带片全删
    r = repo.recover_object(d, work / "partial.bin")
    assert not r.complete and r.exported_length == 192 * 2
    assert Path(r.output_path).read_bytes() == truth[:384]
    print(f"[4] 部分恢复: complete=False, 已验证前缀 {r.exported_length} 字节, "
          f"verified_ranges={r.verified_ranges}")

    # 5. 跨条带混入: 另一个对象的片 -> foreign, 不参与解码
    other = gen(99, 500)
    repo.import_object(other, "intruder-source")
    other_sha = hashlib.sha256(other).hexdigest()
    d = stage("rec_foreign")
    intruder = sorted((repo.root / "objects" / other_sha[:16] / "shards").iterdir())[0]
    shutil.copy(intruder, d / "shards" / intruder.name)
    info = repo.inspect_dir(d)
    foreign = [i["file"] for st in info["stripes"] for i in st["foreign"]]
    r = repo.recover_object(d, work / "foreign.bin")
    assert r.complete and intruder.name in foreign
    print(f"[5] 跨条带片: foreign={foreign}, 原对象仍完整恢复")

    # 6. 截断清单: 拒绝发布
    d = stage("rec_truncated")
    mp = d / "manifest.json"
    raw = mp.read_bytes()
    mp.write_bytes(raw[: len(raw) // 2])
    r = repo.recover_object(d, work / "truncated.bin")
    assert not r.complete and r.manifest_error and not (work / "truncated.bin").exists()
    print(f"[6] 截断清单: 拒绝恢复 ({r.manifest_error[:40]}...)")

    # 7. 半开范围读 (跨条带)
    d = stage("rec_range")
    seg = repo.read_range(d, 150, 260)
    assert seg == truth[150:260]
    print(f"[7] 范围读 [150,260): {len(seg)} 字节与真值一致")

    # 8. 关闭重开: 新仓实例只凭目录恢复
    repo2 = ObjectRepository(work / "repo")
    d = stage("rec_reopen")
    r = repo2.recover_object(d, work / "reopen.bin")
    assert r.complete and (work / "reopen.bin").read_bytes() == truth
    print("[8] 关闭重开: 新实例恢复成功, 无内存缓存依赖")

    print("\n全部场景断言通过。")
    print(f"清单样例: {src / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
