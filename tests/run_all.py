"""端到端复跑测试: 域运算手算、全 k 片组合枚举、故障场景与独立真值比对。

运行: PYTHONPATH=/workspace python3 tests/run_all.py
所有产物落在系统临时目录, 与源码仓完全隔离; 真值 (生成器原始字节) 单独
存放, 恢复进程只接触恢复目录, 比较在测试层完成。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecstore import gf, codec, shard as shard_mod  # noqa: E402
from ecstore.cli import main as cli_main  # noqa: E402
from ecstore.manifest import parse_manifest  # noqa: E402
from ecstore.repository import ObjectRepository  # noqa: E402
from ecstore.service import StripeService, stripe_identity  # noqa: E402

PASS = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# 测试层直接用 CLI 主入口, 模拟独立进程调用 (无共享内存)
def cli(*argv: str) -> tuple[int, dict | str]:
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    out = buf.getvalue().strip()
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, out


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ------------------------------------------------------------ 1. 域运算
def test_field() -> None:
    section("1. GF(2^8, 0x11d) 域运算: 手算与独立参考实现")
    check("加法即异或", gf.add(0x57, 0x13) == (0x57 ^ 0x13))
    check("零元吸收", gf.mul(0, 123) == 0 and gf.mul(123, 0) == 0)
    check("单位元", gf.mul(1, 0xA5) == 0xA5)

    def xtime(x: int) -> int:
        x <<= 1
        return (x ^ 0x11D) & 0xFF if x & 0x100 else x & 0xFF

    # 手算: 0x02*0x57 = xtime; 0x03*0x57 = xtime(0x57) XOR 0x57
    check("手算 02*57=xtime(57)", gf.mul(0x02, 0x57) == xtime(0x57) == 0xAE)
    check("手算 03*57=ae^57=f9", gf.mul(0x03, 0x57) == (xtime(0x57) ^ 0x57) == 0xF9)
    check("手算 80*02=1d (归约)", gf.mul(0x80, 0x02) == 0x1D)

    # 全量 256x256: 表乘法 vs 独立慢速俄罗斯农夫乘法
    bad = [(a, b) for a in range(256) for b in range(256)
           if gf.mul(a, b) != gf.mul_slow(a, b)]
    check("65536 对乘法与慢速实现完全一致", not bad, str(bad[:3]))
    # 逆元全量
    check("255 个非零元素逆元正确",
          all(gf.mul(a, gf.inv(a)) == 1 for a in range(1, 256)))
    # 生成元阶
    x = 1
    for _ in range(255):
        x = gf.mul(x, gf.GEN)
    check("生成元 0x02 的阶为 255", x == 1 and gf.GEN == 0x02)
    # 除法
    check("域除法", gf.div(0x57, 0x13) == gf.mul(0x57, gf.inv(0x13)))


# ------------------------------------------------------------ 2. 手算编码
def test_hand_encoding() -> None:
    section("2. 很短向量手算编码 (不调用 encode_stripe)")
    cfg = codec.make_config(2, 4, 4)
    D0 = bytes([0x57, 0x13, 0x00, 0xFF])
    D1 = bytes([0x01, 0x02, 0x80, 0x7F])
    r2, r3 = cfg.coding_rows

    def hand_lc(c0: int, c1: int, a: bytes, b: bytes) -> bytes:
        return bytes(gf.mul(c0, x) ^ gf.mul(c1, y) for x, y in zip(a, b))

    E2 = hand_lc(r2[0], r2[1], D0, D1)
    E3 = hand_lc(r3[0], r3[1], D0, D1)
    got = codec.encode_stripe(cfg, D0 + D1)
    check("数据片 D0/D1 直通 (单位阵)", got[0] == D0 and got[1] == D1)
    check("编码片 D2 与手算一致", got[2] == E2)
    check("编码片 D3 与手算一致", got[3] == E3)
    # 逐字节展示一个手算样例
    byte0 = gf.mul(r2[0], 0x57) ^ gf.mul(r2[1], 0x01)
    check(f"逐字节样例 {hex(r2[0])}*57 ^ {hex(r2[1])}*01 = {hex(byte0)}",
          byte0 == E2[0])
    check("线性组合是域运算非整数加乘", E2 != bytes(
        ((r2[0] * x + r2[1] * y) & 0xFF) for x, y in zip(D0, D1)))


# ------------------------------------------------------------ 3. 全组合枚举
def test_all_combos() -> None:
    section("3. 枚举小配置全部 k 片组合核对恢复")
    for k, n, L in [(2, 4, 4), (3, 4, 5), (2, 3, 7)]:
        cfg = codec.make_config(k, n, L)
        data = bytes((i * 37 + 11) % 256 for i in range(k * L))
        pieces = codec.encode_stripe(cfg, data)
        combos = list(itertools.combinations(range(n), k))
        check(f"k={k},n={n}: 共 C({n},{k})={len(combos)} 个组合全部精确恢复",
              all(b"".join(codec.recover_stripe(
                  cfg, {i: pieces[i] for i in combo})) == data
                  for combo in combos))
    # 非 MDS 配置登记拒绝
    rejected = True
    try:
        codec.make_config(2, 3, 4, coding_rows=[[1, 0]])  # 与单位阵行重复
        rejected = False
    except codec.ConfigError:
        pass
    check("非 MDS 配置 (重复单位行) 被拒绝", rejected)
    try:
        codec.make_config(2, 3, 4, coding_rows=[[0, 0]])
        rejected = False
    except codec.ConfigError:
        pass
    check("全零编码行被拒绝", rejected)
    # 参数范围
    for bad in ((1, 3, 4), (7, 7, 4), (3, 2, 4), (2, 4, 0)):
        try:
            codec.make_config(*bad)
            check(f"非法参数 {bad} 被拒绝", False)
        except codec.ConfigError:
            check(f"非法参数 {bad} 被拒绝", True)


# ------------------------------------------------------------ 4. 独立服务
def test_service(tmp: Path) -> None:
    section("4. 独立条带服务: 登记/编码/核验/恢复与故障分类")
    svc_root = tmp / "svc"
    code, out = cli("svc-register", str(svc_root), "--k", "2", "--n", "4",
                    "--L", "4", "--json")
    digest = out["config_digest"]
    check("服务登记成功", code == 0 and len(digest) == 64)
    code, out = cli("svc-register", str(svc_root), "--k", "2", "--n", "4",
                    "--L", "4", "--json")
    check("重复登记幂等", code == 0 and out["config_digest"] == digest)

    data = bytes([0x57, 0x13, 0x00, 0xFF, 0x01, 0x02, 0x80, 0x7F])
    inp = tmp / "stripe.bin"
    inp.write_bytes(data)
    enc = tmp / "enc"
    code, meta = cli("svc-encode", str(svc_root), "--config-digest", digest,
                     "--input", str(inp), "--out-dir", str(enc), "--json")
    sid = meta["stripe_id"]
    check("编码输出含配置摘要/stripe_id/各片摘要",
          meta["config_digest"] == digest and len(meta["piece_sha256"]) == 4)
    check("stripe_id 绑定配置+原始摘要",
          sid == stripe_identity(digest, sha(data)))

    # 全片核验
    code, rep = cli("svc-verify", str(svc_root), "--dir", str(enc),
                    "--stripe-id", sid, "--json")
    check("全片 verify 成功", code == 0 and rep["status"] == "recovered")

    # 任意 2 片恢复 (服务层, 遍历全部组合)
    ok = True
    for combo in itertools.combinations(range(4), 2):
        d = tmp / f"svc_rec_{'_'.join(map(str, combo))}"
        d.mkdir()
        for i in combo:
            shutil.copy(enc / f"shard_{i:02d}.bin", d)
        outp = d / "out.bin"
        code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                        "--stripe-id", sid, "--output", str(outp), "--json")
        ok = ok and code == 0 and outp.read_bytes() == data
    check("服务层全部 6 个 2 片组合恢复且字节相等", ok)

    # 缺片: 仅 1 片 -> insufficient, 退出码 2, 不产出
    d = tmp / "svc_insufficient"
    d.mkdir()
    shutil.copy(enc / "shard_00.bin", d)
    code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                    "--stripe-id", sid, "--output", str(d / "x.bin"), "--json")
    check("少于 k 片返回不足且不写正文",
          code == 2 and rep["status"] == "insufficient"
          and not (d / "x.bin").exists())

    # 坏片 (正文翻转, 头部摘要仍旧) -> 隔离 + 不足
    d = tmp / "svc_corrupt"
    d.mkdir()
    shutil.copy(enc / "shard_00.bin", d)
    raw = bytearray((enc / "shard_01.bin").read_bytes())
    raw[-1] ^= 0xFF
    (d / "shard_01.bin").write_bytes(raw)
    code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                    "--stripe-id", sid, "--output", str(d / "x.bin"),
                    "--json")
    check("摘要不符的片被隔离并导致不足",
          code == 2 and rep["status"] == "insufficient"
          and len(rep["quarantined"]) == 1)

    # 同序号冲突: 两个不同正文的 0 号片
    d = tmp / "svc_conflict"
    d.mkdir()
    shutil.copy(enc / "shard_00.bin", d / "a.bin")
    parsed = shard_mod.parse_shard((enc / "shard_00.bin").read_bytes())
    evil_body = bytes(b ^ 0x01 for b in parsed.body)
    # 结构合法 (头部摘要与 evil_body 自洽), 但同序号正文不同 -> 冲突
    (d / "b.bin").write_bytes(shard_mod.build_shard(
        parsed.config_digest, parsed.stripe_id, parsed.index, evil_body))
    shutil.copy(enc / "shard_01.bin", d)
    code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                    "--stripe-id", sid, "--output", str(d / "x.bin"),
                    "--json")
    check("同序号不同正文判冲突", code == 2 and rep["status"] == "conflict"
          and len(rep["conflicts"]) == 1)

    # 重复片: 同片复制两份 -> 去重后仍可恢复
    d = tmp / "svc_dup"
    d.mkdir()
    shutil.copy(enc / "shard_00.bin", d / "p0.bin")
    shutil.copy(enc / "shard_00.bin", d / "p0_copy.bin")
    shutil.copy(enc / "shard_02.bin", d)
    code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                    "--stripe-id", sid, "--output", str(d / "out.bin"),
                    "--json")
    check("重复片去重后恢复成功",
          code == 0 and (d / "out.bin").read_bytes() == data
          and len(rep["duplicates"]) == 1)

    # 跨条带混入: 用另一份数据编码, 其 0 号片带不同 stripe_id
    other_data = bytes([9, 8, 7, 6, 5, 4, 3, 2])
    enc2 = tmp / "enc_other"
    cli("svc-encode", str(svc_root), "--config-digest", digest,
        "--input", _write(tmp / "other.bin", other_data),
        "--out-dir", str(enc2), "--json")
    d = tmp / "svc_foreign"
    d.mkdir()
    shutil.copy(enc / "shard_00.bin", d)
    shutil.copy(enc2 / "shard_00.bin", d / "foreign0.bin")
    shutil.copy(enc / "shard_01.bin", d)
    code, rep = cli("svc-recover", str(svc_root), "--dir", str(d),
                    "--stripe-id", sid, "--output", str(d / "out.bin"),
                    "--json")
    check("跨条带片被标记 foreign 且不参与恢复",
          code == 0 and (d / "out.bin").read_bytes() == data
          and len(rep["foreign"]) == 1)


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


# ------------------------------------------------------------ 5. 对象仓
def gen_object(seed: int, size: int) -> bytes:
    out = bytearray(size)
    state = seed or 1
    for i in range(size):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (state >> 16) & 0xFF
    return bytes(out)


def make_recovery_dir(repo: ObjectRepository, object_sha: str,
                      dest: Path) -> Path:
    """把清单和编码片单独复制到恢复目录 (仓外, 模拟可独立搬运)。"""
    src = repo.root / "objects" / object_sha[:16]
    dest.mkdir(parents=True)
    shutil.copy(src / "manifest.json", dest / "manifest.json")
    shutil.copytree(src / "shards", dest / "shards")
    return dest


def test_repository(tmp: Path) -> None:
    section("5. 对象仓: 归档/恢复/范围读/故障场景/独立真值")
    repo_root = tmp / "repo"
    repo = ObjectRepository(repo_root)
    # 主配置 k=3 n=5 L=64 (条带 192 字节)
    cfg = repo.service.register(3, 5, 64)
    digest = cfg.digest

    sizes = [0, 1, 191, 192, 193, 400, 1024, 4097, 65537, 1024 * 1024]
    truths: dict[str, bytes] = {}
    for idx, size in enumerate(sizes):
        data = gen_object(seed=100 + idx, size=size)
        truths[sha(data)] = data
        res = repo.import_object(data, name=f"obj{idx}")
        check(f"对象 {idx} (size={size}) 归档, 摘要寻址",
              res["object_sha256"] == sha(data))

    check("共 10 个对象", len(repo.list_objects()) == 10)

    # 零字节对象: 合法空清单, 无假条带
    zero_sha = sha(b"")
    mani0 = parse_manifest((repo.root / "objects" / zero_sha[:16]
                            / "manifest.json").read_bytes())
    check("零字节对象清单合法且无条带",
          mani0.object_length == 0 and len(mani0.stripes) == 0)
    shard_dir0 = repo.root / "objects" / zero_sha[:16] / "shards"
    check("零字节对象不生成任何片", not shard_dir0.exists()
          or not any(shard_dir0.iterdir()))

    # 全部对象: 独立恢复目录 + 恢复 + 与真值逐字节比较
    all_ok = True
    for object_sha, truth in truths.items():
        rec_dir = make_recovery_dir(repo, object_sha,
                                    tmp / "recovery_full" / object_sha[:12])
        out = tmp / "restored" / f"{object_sha[:12]}.bin"
        result = repo.recover_object(rec_dir, out)
        got = out.read_bytes() if out.exists() else None
        all_ok = all_ok and result.complete and got == truth
    check("10 个对象全部完整恢复且与生成器真值字节相等", all_ok)

    # 末条带补零: size=193 (第二条带仅 1 个真实字节, 其余补零)
    sha193 = sha(truths_for(truths, 193))
    mani = parse_manifest((repo.root / "objects" / sha193[:16]
                           / "manifest.json").read_bytes())
    check("size=193 清单保留真实长度", mani.object_length == 193
          and len(mani.stripes) == 2)
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_193")
    out = tmp / "out_193.bin"
    result = repo.recover_object(rec_dir, out)
    check("尾部补零对象恢复后按真实长度截断",
          result.complete and len(out.read_bytes()) == 193
          and out.read_bytes() == truths[sha193])

    # 缺片场景: 每条带删掉 2 个片 -> 每带剩 3 片 (恰 k), 仍完整
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_missing2")
    for f in sorted((rec_dir / "shards").iterdir()):
        # 每带删除序号 3,4
        if any(f"piece_{i:02d}" in f.name for i in (3, 4)):
            f.unlink()
    result = repo.recover_object(rec_dir, tmp / "out_missing2.bin")
    check("每条带仅 k 个片仍完整恢复",
          result.complete and (tmp / "out_missing2.bin").read_bytes()
          == truths[sha193])

    # 不足场景: 每条带删到只剩 2 片 (<k=3) -> 不 complete, 不发布
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_insufficient")
    kept = 0
    for f in sorted((rec_dir / "shards").iterdir()):
        if f"piece_00" in f.name or f"piece_01" in f.name:
            continue
        f.unlink()
    result = repo.recover_object(rec_dir, tmp / "out_insuf.bin")
    check("有效片不足时不标 complete 且无完整输出",
          not result.complete and not (tmp / "out_insuf.bin").exists())
    info = repo.inspect_dir(rec_dir)
    check("inspect 逐条带报告 insufficient",
          all(s["status"] == "insufficient" for s in info["stripes"]))

    # 坏片: 翻转一个片正文 (头部摘要不更新) -> 隔离; 剩 4 片仍可恢复
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_corrupt")
    target = sorted((rec_dir / "shards").iterdir())[0]
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0xAA
    target.write_bytes(raw)
    result = repo.recover_object(rec_dir, tmp / "out_corrupt.bin")
    st0 = result.stripe_status[0]
    check("坏片被隔离且其余片支撑完整恢复",
          result.complete and len(st0.quarantined) == 1
          and (tmp / "out_corrupt.bin").read_bytes() == truths[sha193])

    # 同序号冲突: 在合法片之外, 再放一个结构合法但同序号不同正文的片
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_conflict")
    target = sorted((rec_dir / "shards").iterdir())[0]
    parsed = shard_mod.parse_shard(target.read_bytes())
    evil_body = bytes(b ^ 0x01 for b in parsed.body)
    (rec_dir / "shards" / "conflict_copy.bin").write_bytes(
        shard_mod.build_shard(parsed.config_digest, parsed.stripe_id,
                              parsed.index, evil_body))
    info = repo.inspect_dir(rec_dir)
    check("仓层识别同序号冲突",
          any(len(s["conflicts"]) == 1 for s in info["stripes"]))

    # 跨条带混入: 把对象 B 的片放进 A 的恢复目录 -> foreign, A 仍可恢复
    sha400 = sha(truths_for(truths, 400))
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_foreign")
    b_shards = sorted((repo.root / "objects" / sha400[:16] / "shards").iterdir())
    # 保留归档命名 stripe_XXXX_piece_YY.bin, 仓可按文件名归因到同位置条带
    shutil.copy(b_shards[0], rec_dir / "shards" / b_shards[0].name)
    info = repo.inspect_dir(rec_dir)
    result = repo.recover_object(rec_dir, tmp / "out_foreign.bin")
    foreign_files = [i["file"] for st in info["stripes"] for i in st["foreign"]]
    check("跨 stripe 片判 foreign 且不拼入",
          result.complete and b_shards[0].name in foreign_files
          and (tmp / "out_foreign.bin").read_bytes() == truths[sha193])

    # 无法按文件名归因的外来片 -> 未归属隔离列表, 同样不参与解码
    rec_dir2 = make_recovery_dir(repo, sha193, tmp / "rec_foreign2")
    shutil.copy(b_shards[0], rec_dir2 / "shards" / "intruder.bin")
    info2 = repo.inspect_dir(rec_dir2)
    check("无法归因的外来片进入未归属隔离列表",
          any(i["file"] == "intruder.bin"
              for i in info2["unattributed_quarantined"]))

    # 清单与片摘要不符 (清单被改): 仓层 manifest_mismatch + 服务层隔离
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_manifest_mm")
    mani_path = rec_dir / "manifest.json"
    doc = json.loads(mani_path.read_text())
    first_key = sorted(doc["stripes"][0]["piece_sha256"])[0]
    doc["stripes"][0]["piece_sha256"][first_key] = "00" * 32
    mani_path.write_text(json.dumps(doc))
    info = repo.inspect_dir(rec_dir)
    check("片摘要与清单不符被单独标注",
          len(info["stripes"][0]["manifest_mismatch"]) == 1)

    # 截断清单: 不可恢复
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_truncated")
    raw = (rec_dir / "manifest.json").read_bytes()
    (rec_dir / "manifest.json").write_bytes(raw[: len(raw) // 2])
    result = repo.recover_object(rec_dir, tmp / "out_trunc.bin")
    check("截断清单拒绝恢复且不产出",
          not result.complete and result.manifest_error
          and not (tmp / "out_trunc.bin").exists())

    # 部分恢复: 第一条带完好, 第二条带不足 -> 导出已验证前缀, 不 complete
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_partial")
    for f in (rec_dir / "shards").iterdir():
        if f.name.startswith("stripe_0001"):
            f.unlink()  # 第二条带片全删
    result = repo.recover_object(rec_dir, tmp / "partial_out.bin")
    check("部分恢复仅导出连续已验证前缀 (192 字节) 且不标 complete",
          not result.complete and result.exported_length == 192
          and result.output_path is not None
          and Path(result.output_path).read_bytes() == truths[sha193][:192])

    # 范围读: 半开区间, 跨条带
    rec_dir = make_recovery_dir(repo, sha193, tmp / "rec_range")
    data = repo.read_range(rec_dir, 100, 193)
    check("范围读 [100,193) 与真值一致 (跨条带)", data == truths[sha193][100:193])
    data = repo.read_range(rec_dir, 0, 1)
    check("范围读单字节", data == truths[sha193][0:1])
    data = repo.read_range(rec_dir, 50, 50)
    check("空区间返回空", data == b"")
    try:
        repo.read_range(tmp / "rec_partial", 0, 193)
        check("跨越未恢复条带的范围读被拒绝", False)
    except Exception:
        check("跨越未恢复条带的范围读被拒绝", True)
    try:
        repo.read_range(rec_dir, 0, 194)
        check("越界范围读被拒绝", False)
    except Exception:
        check("越界范围读被拒绝", True)

    # 零字节对象范围读与恢复
    rec_zero = make_recovery_dir(repo, zero_sha, tmp / "rec_zero")
    result = repo.recover_object(rec_zero, tmp / "zero_out.bin")
    check("零字节对象恢复 complete 且输出空文件",
          result.complete and (tmp / "zero_out.bin").read_bytes() == b"")
    check("零字节对象范围读", repo.read_range(rec_zero, 0, 0) == b"")

    # 整对象摘要不符不得发布: 篡改恢复出的预期 (用错误 expected 走服务层)
    svc = repo.service
    bad = svc.recover(rec_dir / "shards",
                      expected_stripe_id=parse_manifest(
                          (rec_dir / "manifest.json").read_bytes()
                      ).stripes[0].stripe_id,
                      expected_original_digest="00" * 32)
    check("原始条带摘要核对失败时不发布成功", bad.status == "failed")


def truths_for(truths: dict[str, bytes], size: int) -> bytes:
    for data in truths.values():
        if len(data) == size:
            return data
    raise KeyError(size)


# ------------------------------------------------------------ 6. 外部清单
def test_external(tmp: Path) -> None:
    section("6. 消费外部同格式清单与片 (不同仓实例, 外部配置)")
    producer = ObjectRepository(tmp / "producer")
    producer.service.register(2, 4, 16)
    data = gen_object(777, 100)
    res = producer.import_object(data, "external_obj")
    object_sha = res["object_sha256"]
    rec_dir = tmp / "external_inbox"
    make_recovery_dir(producer, object_sha, rec_dir)

    # 消费方仓: 全新空仓, 没有任何配置; 靠清单内嵌 config_spec 登记
    consumer = ObjectRepository(tmp / "consumer")
    check("消费方仓初始无配置", consumer.service.list_configs() == [])
    result = consumer.recover_object(rec_dir, tmp / "external_out.bin")
    check("外部清单+片可被新仓消费并字节相等",
          result.complete and (tmp / "external_out.bin").read_bytes() == data)
    check("消费后配置按清单规格自动登记",
          len(consumer.service.list_configs()) == 1)

    # 编码参数相同但不同次编码: 换一个同规格对象的片, stripe_id 不同 -> foreign
    other = gen_object(888, 100)
    producer.import_object(other, "external_obj2")
    other_sha = sha(other)
    other_dir = make_recovery_dir(producer, other_sha, tmp / "external_other")
    intruder = sorted((other_dir / "shards").iterdir())[0]
    shutil.copy(intruder, rec_dir / "shards" / "mixed.bin")
    info = consumer.inspect_dir(rec_dir)
    foreign_files = [i["file"] for st in info["stripes"] for i in st["foreign"]]
    unattributed = [i["file"] for i in info["unattributed_quarantined"]]
    check("同参数不同次编码的片按 stripe_id 判 foreign",
          "mixed.bin" in foreign_files or "mixed.bin" in unattributed)


# ------------------------------------------------------------ 7. 持久化与不可变
def test_persistence(tmp: Path) -> None:
    section("7. 关闭重开持久化 / 旧清单不可变 / 摘要寻址")
    repo = ObjectRepository(tmp / "repo")  # 重开已有仓
    zero_sha = sha(b"")
    rec_dir = make_recovery_dir(repo, zero_sha, tmp / "reopen_zero")
    result = repo.recover_object(rec_dir, tmp / "reopen_zero.bin")
    check("重开后零字节对象仍可恢复", result.complete)

    # 同名对象再次导入 (不同正文): 旧清单不变, 别名保留历史
    old_mani = (repo.root / "objects"
                / sha(gen_object(100, 0))[:16] / "manifest.json")
    old_text = old_mani.read_bytes()
    new_data = gen_object(200, 50)
    repo.import_object(new_data, name="obj0")
    check("旧清单字节未改变", old_mani.read_bytes() == old_text)
    alias = json.loads((repo.root / "aliases" / "obj0.json").read_text())
    check("别名保留新旧两个对象摘要",
          len(alias["history"]) == 2 and alias["object_sha256"] != sha(new_data)
          and sha(new_data) in alias["history"])
    check("对象总数增加到 11", len(repo.list_objects()) == 11)

    # 重开后再做一次完整 inspect (走 CLI, 全新进程视角)
    data400 = gen_object(105, 400)  # 100 + obj5 的下标 5
    sha400 = sha(data400)
    rec_dir = tmp / "reopen_full"
    make_recovery_dir(repo, sha400, rec_dir)
    code, info = cli("inspect", str(repo.root), "--recovery-dir", str(rec_dir),
                     "--json")
    check("CLI 重开 inspect 完整", code == 0 and info["complete"])


# ------------------------------------------------------------ 8. 配置验证缓存
def test_config_cache(tmp: Path) -> None:
    section("8. 配置验证缓存与 MDS 枚举覆盖")
    svc = StripeService(tmp / "cache_svc")
    import time
    t0 = time.perf_counter()
    cfg = svc.register(6, 10, 4096)   # C(10,6)=210 个 6 阶矩阵求逆
    t1 = time.perf_counter()
    again = svc.register(6, 10, 4096)
    t2 = time.perf_counter()
    check("最大规模 k=6,n=10 配置登记通过 MDS 校验", cfg.digest == again.digest)
    check("重复登记命中缓存 (明显更快)", (t2 - t1) < (t1 - t0))
    check("配置持久化到磁盘, 重开可见",
          StripeService(tmp / "cache_svc").get_config(cfg.digest).n == 10)
    # 规模条带真实编码一轮 L=4096
    data = gen_object(5, 6 * 4096)
    meta = svc.encode(cfg.digest, data, tmp / "big_enc")
    pieces = [shard_mod.parse_shard(
        (tmp / "big_enc" / f"shard_{i:02d}.bin").read_bytes()) for i in range(10)]
    chosen = {0: pieces[0].body, 3: pieces[3].body, 5: pieces[5].body,
              7: pieces[7].body, 8: pieces[8].body, 9: pieces[9].body}
    from ecstore import codec as c
    rec = b"".join(c.recover_stripe(cfg, chosen))
    check("L=4096 条带任意 6 片恢复字节相等", rec == data
          and meta["original_sha256"] == sha(data))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ecstore_test_"))
    print(f"临时工作目录: {tmp}")
    try:
        test_field()
        test_hand_encoding()
        test_all_combos()
        test_service(tmp)
        test_repository(tmp)
        test_external(tmp)
        test_persistence(tmp)
        test_config_cache(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项, 失败 {len(FAILURES)} 项")
    if FAILURES:
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
