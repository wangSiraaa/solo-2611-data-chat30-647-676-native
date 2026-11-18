"""确定性合成采集树生成器。

只在调用方指定的输出目录内创建文件，绝不触碰其他路径。覆盖题面点名的
全部案例：``**`` 零层与多层匹配、子规则覆盖、未重新包含的父目录、目录与
文件同名模式、符号链接（根外/成环）、中文文件名；再用随机数据补足
5000 项 / 深度 12 / 100 份规则 / 约 20MiB 正文的规模。
"""

from __future__ import annotations

import os
import random
from pathlib import Path

SEED = 20261118
TARGET_ITEMS = 5000
TARGET_DEPTH = 12
TARGET_IGNORE_FILES = 100
TARGET_BYTES = 20 * 1024 * 1024


class SyntheticTree:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.rng = random.Random(SEED)
        self.item_count = 0
        self.byte_count = 0
        self.ignore_count = 0
        self.created_links: list[str] = []

    # ------------------------------------------------------------- 基础原语
    def mkdir(self, rel: str) -> Path:
        p = self.root / rel
        p.mkdir(parents=True, exist_ok=True)
        self.item_count += 1
        return p

    def file(self, rel: str, content: bytes | str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        p.write_bytes(content)
        self.item_count += 1
        self.byte_count += len(content)
        return p

    def ignore(self, rel_dir: str, lines: list[str]) -> None:
        target_dir = self.root if rel_dir == "" else self.root / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / ".ignore"
        if path.exists():
            raise AssertionError(
                f"refusing to overwrite existing rule file at {rel_dir}"
            )
        text = "\n".join(lines) + "\n"
        path.write_text(text, encoding="utf-8")
        self.item_count += 1
        self.byte_count += len(text.encode("utf-8"))
        self.ignore_count += 1

    def symlink(self, rel: str, target: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() or p.is_symlink():
            p.unlink()
        os.symlink(target, p)
        self.item_count += 1
        self.created_links.append(rel)

    # ------------------------------------------------------- 定向语义案例
    def _case_starstar(self) -> None:
        """** 零层与多层匹配。"""
        self.mkdir("case_starstar")
        self.ignore("case_starstar", [
            "# ** 零层：直接子文件也命中",
            "**/secret.log",
            "# a/**/b 零层与多层",
            "logs/**/debug.txt",
            "# 裸 ** 全树排除特定后缀",
            "**/*.tmp",
            "# 再用 include 救回一个深层文件",
            "!logs/deep/keep/debug.txt",
        ])
        # secret.log：零层命中
        self.file("case_starstar/secret.log", b"zero-level secret\n")
        self.file("case_starstar/sub/secret.log", b"nested secret\n")
        # logs/**/debug.txt：零层（logs/debug.txt）与多层
        self.file("case_starstar/logs/debug.txt", b"d0\n")
        self.file("case_starstar/logs/a/debug.txt", b"d1\n")
        self.file("case_starstar/logs/a/b/debug.txt", b"d2\n")
        # 被重新包含的深层文件
        self.mkdir("case_starstar/logs/deep/keep")
        self.file("case_starstar/logs/deep/keep/debug.txt", b"kept\n")
        # *.tmp 全树
        self.file("case_starstar/x.tmp", b"t\n")
        self.file("case_starstar/sub/y.tmp", b"t\n")
        self.file("case_starstar/sub/z/zz.tmp", b"t\n")
        # 不该误伤的普通文件
        self.file("case_starstar/notes.txt", b"ok\n")

    def _case_override(self) -> None:
        """子目录规则覆盖祖先 + 父目录未重新包含。"""
        self.mkdir("case_override")
        self.ignore("case_override", [
            "*.log",                 # 全子树排除日志
            "locked/",               # 仅目录规则：排除 locked 目录
        ])
        self.ignore("case_override/sub", [
            "!*.log",                # 子目录重新包含日志
            "*.log",                 # 同文件内后者胜：再次排除
            "!special.log",          # 最后命中：救回 special
        ])
        self.file("case_override/a.log", b"excluded by root rule\n")
        self.file("case_override/sub/b.log", b"re-excluded locally\n")
        self.file("case_override/sub/special.log", b"re-included\n")
        self.file("case_override/sub/plain.txt", b"plain\n")

        # 父目录 locked 被排除：内部 ! 规则不得隔空复活
        self.ignore("case_override/locked", [
            "!inside.log",
            "!deep/inside.log",
        ])
        self.file("case_override/locked/inside.log", b"must stay excluded\n")
        self.file("case_override/locked/deep/inside.log", b"also excluded\n")
        # 只重新包含文件却不包含其父目录的情形（父仍被 locked/ 剪枝）
        self.mkdir("case_override/locked/deep")

    def _case_same_name(self) -> None:
        """目录与文件同名模式 + 尾斜杠仅目录。

        同一物理路径不能既是文件又是目录，因此把同名实体放在不同位置，
        用同一组规则分别检验文件与目录的命中差异。
        """
        self.mkdir("case_samename")
        self.ignore("case_samename", [
            "data/",        # 仅排除名为 data 的目录，不碰同名文件
            "report",       # 无斜杠：同名的文件和目录都排除
        ])
        # 名为 data 的目录：被 data/ 排除
        self.mkdir("case_samename/dirholder/data")
        self.file("case_samename/dirholder/data/inner.bin",
                  b"inside excluded dir\n")
        # 名为 data 的普通文件（另一位置）：data/ 不命中文件，保留
        self.file("case_samename/fileholder/data",
                  b"a FILE literally named data, kept\n")
        # 名为 report 的文件与目录：无斜杠规则两者皆排除
        self.file("case_samename/fileholder/report", b"file report excluded\n")
        self.mkdir("case_samename/dirholder/report")
        self.file("case_samename/dirholder/report/child",
                  b"dir report excluded too\n")
        # 对照组：data.txt 不应被 data/ 命中
        self.file("case_samename/data.txt", b"similarly named file, kept\n")

    def _case_symlinks(self) -> None:
        """符号链接：保存文本不跟随，根外/成环均不得读到外部正文。"""
        self.mkdir("case_links/inside")
        self.file("case_links/inside/target.txt", b"link target body\n")
        self.file("case_links/inside/中文目标.txt", "中文正文\n".encode())
        # 正常内部链接
        self.symlink("case_links/ok_link.txt", "inside/target.txt")
        self.symlink("case_links/中文链接.txt", "inside/中文目标.txt")
        # 指向根外（词法逃逸）
        self.symlink("case_links/escape_abs", "/etc/hostname")
        self.symlink("case_links/escape_rel", "../../outside_secret.txt")
        # 成环
        self.symlink("case_links/loop_a", "loop_b")
        self.symlink("case_links/loop_b", "loop_a")
        # 链接到被排除目录里的文件（链接本身由规则决定，目标不跟随）
        self.ignore("case_links", ["inside/"])
        self.symlink("case_links/dangling", "inside/missing.txt")

    def _case_unicode(self) -> None:
        """中文文件名与中文规则，按 Unicode 码点精确比较。"""
        self.mkdir("case_unicode/目录甲")
        self.ignore("case_unicode", [
            "*.临时",
            "目录甲/秘密*.txt",
            "!目录甲/秘密公开.txt",
            # 正则元字符必须按普通字符处理
            r"a+b(c) [d].txt",
        ])
        self.file("case_unicode/目录甲/秘密计划.txt", "排除\n".encode())
        self.file("case_unicode/目录甲/秘密公开.txt", "重新包含\n".encode())
        self.file("case_unicode/目录甲/普通.txt", "保留\n".encode())
        self.file("case_unicode/草稿.临时", "临时文件\n".encode())
        self.file("case_unicode/目录甲/草稿.临时", "深层临时\n".encode())
        # 正则元字符必须按普通字符处理
        self.file("case_unicode/a+b(c) [d].txt", b"literal regex chars\n")

    def _case_metachar(self) -> None:
        """? 与 * 的边界：? 恰好一个非斜杠字符。"""
        self.mkdir("case_meta")
        self.ignore("case_meta", [
            "a?c",
            "*.bak",
        ])
        self.file("case_meta/abc", b"q matched\n")
        self.file("case_meta/ac", b"q must not match zero chars\n")
        self.file("case_meta/axxc", b"q must not match two chars\n")
        self.file("case_meta/x.bak", b"star matched\n")
        self.file("case_meta/.bak", b"star zero chars matched\n")
        self.file("case_meta/bak", b"no dot, kept\n")

    def _case_anchored(self) -> None:
        """含斜杠模式锚定到所在目录；前导 / 仅显式锚定。"""
        self.mkdir("case_anchor/sub/deep")
        self.ignore("case_anchor", [
            "/top_only.txt",
            "sub/deep/x.dat",
        ])
        self.file("case_anchor/top_only.txt", b"anchored exclude\n")
        self.file("case_anchor/sub/top_only.txt", b"not at anchor root, kept\n")
        self.file("case_anchor/sub/deep/x.dat", b"anchored relative exclude\n")
        self.file("case_anchor/sub/deep/y.dat", b"kept\n")

    def _case_deep_chain(self) -> None:
        """深度 12 的目录链，验证祖先剪枝与逐层继承。"""
        parts = [f"d{i:02d}" for i in range(TARGET_DEPTH)]
        chain = "case_deep/" + "/".join(parts)
        self.mkdir(chain)
        # 浅层排除一个中间层，深层 .ignore 存在但不应被用于复活
        mid = "case_deep/" + "/".join(parts[:6])
        self.ignore("case_deep", [f"{parts[0]}/{parts[1]}/{parts[2]}/{parts[3]}/{parts[4]}/{parts[5]}/"])
        self.ignore(mid, ["!revive.txt"])
        self.file(chain + "/revive.txt", b"cannot revive under excluded ancestor\n")
        self.file(chain + "/normal.bin", b"also unreachable\n")
        # 链外的可控深度文件应正常入选
        self.mkdir("case_deep/alive_branch")
        self.file("case_deep/alive_branch/ok.txt", b"alive\n")

    # ------------------------------------------------------- 随机规模填充
    def _random_fill(self) -> None:
        rng = self.rng
        pool = "abcdefghijklmnopqrstuvwxyz0123456789_中文目录文件"

        def name(n: int = 8) -> str:
            return "".join(rng.choice(pool) for _ in range(n))

        # 先建 100 个左右分散的 .ignore（含定向案例已建的数量）
        random_dirs: list[str] = []
        d = "bulk"
        used_dirs: set[str] = set()
        i = 0
        while self.ignore_count < TARGET_IGNORE_FILES and i < TARGET_IGNORE_FILES * 4:
            i += 1
            depth = rng.randint(0, 5)
            rel = f"{d}/ruleset_{i:03d}_{name(8)}"
            for _ in range(depth):
                rel = f"{rel}/{name(6)}"
            if rel in used_dirs:
                continue
            used_dirs.add(rel)
            random_dirs.append(rel)
            patterns = [
                f"*.{rng.choice(['tmp', 'log', 'cache', 'bak', '临时'])}",
                f"{name(5)}/",
                f"**/{name(4)}.skip",
                f"!keep_{i}.dat",
                "# generated rule",
                f"/{name(5)}.local",
            ]
            chosen = [rng.choice(patterns) for _ in range(rng.randint(20, 100))]
            self.ignore(rel, chosen)
        if self.ignore_count < TARGET_IGNORE_FILES:
            raise RuntimeError("failed to reach target ignore file count")

        # 用随机文件把条目数与字节数推到目标规模
        avg_body = max(1, TARGET_BYTES // TARGET_ITEMS)
        guard = 0
        while (self.item_count < TARGET_ITEMS
               or self.byte_count < TARGET_BYTES) and guard < 200000:
            guard += 1
            base = rng.choice(random_dirs) if random_dirs and rng.random() < 0.7 else "bulk/misc"
            depth = rng.randint(0, 6)
            rel = base
            for _ in range(depth):
                rel = f"{rel}/{name(5)}"
            # 绝不落到规则目录本身，避免覆盖其中的 .ignore
            if rel in used_dirs:
                rel = f"{rel}/payload"
            if rng.random() < 0.25:
                self.mkdir(rel)
            else:
                size = rng.randint(avg_body // 2, avg_body * 2)
                # 控制总量：接近上限时生成小文件
                if self.byte_count > TARGET_BYTES * 0.98:
                    size = min(size, 64)
                body = bytes(rng.randrange(256) for _ in range(min(size, 4096)))
                if size > 4096:
                    body = (body * ((size // 4096) + 1))[:size]
                self.file(f"{rel}/{name(10)}.dat", body)

        # 保证字节总量达标但不过量太多
        while self.byte_count < TARGET_BYTES:
            missing = TARGET_BYTES - self.byte_count
            chunk = min(missing, 8192)
            self.file(f"bulk/padding/{name(12)}.pad",
                      bytes(rng.randrange(256) for _ in range(chunk)))

    def build(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        self._case_starstar()
        self._case_override()
        self._case_same_name()
        self._case_symlinks()
        self._case_unicode()
        self._case_metachar()
        self._case_anchored()
        self._case_deep_chain()
        self._random_fill()
        return {
            "root": str(self.root),
            "items": self.item_count,
            "bytes": self.byte_count,
            "ignore_files": self.ignore_count,
            "symlinks": self.created_links,
            "seed": SEED,
        }


def generate_synthetic_tree(root: str | os.PathLike[str]) -> dict:
    return SyntheticTree(root).build()
