"""SQLite 事实仓：冻结目录版本、降雨版本与分析档案。

身份规则
--------
* 目录与降雨批次均为**内容寻址**：内容哈希即身份，同内容重复写入幂等；
* 已发布/已导入的产品不可变（表只允许 INSERT，更新通过新版本表达）；
* 分析档案记录所绑定的 ``catalog_hash`` 与 ``rain_hash``，旧分析永远
  指向旧产品；修订核/延迟/降雨后重新分析得到的是新档案。

全部有理量以规范化文本存储，SQLite 不参与任何算术。
"""

from __future__ import annotations

import json
import sqlite3
from typing import List, Mapping, Optional, Sequence, Tuple

from .catalog import Catalog
from .errors import StoreError
from .rational import frac_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_versions (
    catalog_hash TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rain_versions (
    rain_hash     TEXT PRIMARY KEY,
    catalog_hash  TEXT NOT NULL,
    parent_hash   TEXT,
    lo            INTEGER NOT NULL,
    hi            INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (catalog_hash) REFERENCES catalog_versions(catalog_hash)
);

CREATE TABLE IF NOT EXISTS rain_cells (
    rain_hash   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    t           INTEGER NOT NULL,
    rain        TEXT NOT NULL,
    loss        TEXT NOT NULL,
    PRIMARY KEY (rain_hash, node_id, t),
    FOREIGN KEY (rain_hash) REFERENCES rain_versions(rain_hash)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id  TEXT PRIMARY KEY,
    catalog_hash TEXT NOT NULL,
    rain_hash    TEXT NOT NULL,
    lo           INTEGER NOT NULL,
    hi           INTEGER NOT NULL,
    nodes_json   TEXT NOT NULL,
    status       TEXT NOT NULL,
    report_json  TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class Store:
    """SQLite 事实仓（文件或 ``:memory:``）。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 目录版本 ------------------------------------------------------
    def save_catalog(self, catalog: Catalog) -> str:
        """幂等保存冻结目录，返回内容哈希。旧版本永不被覆盖。"""
        payload = catalog.to_payload()
        text = catalog.canonical_text()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO catalog_versions(catalog_hash, payload_json) "
                "VALUES (?, ?)",
                (catalog.content_hash, text),
            )
        return catalog.content_hash

    def load_catalog(self, catalog_hash: str) -> Catalog:
        row = self.conn.execute(
            "SELECT payload_json FROM catalog_versions WHERE catalog_hash = ?",
            (catalog_hash,),
        ).fetchone()
        if row is None:
            raise StoreError(f"未知目录版本 {catalog_hash[:12]}…")
        return Catalog.from_payload(json.loads(row["payload_json"]), verify_hash=catalog_hash)

    def list_catalogs(self) -> List[Tuple[str, str]]:
        return [
            (r["catalog_hash"], r["created_at"])
            for r in self.conn.execute(
                "SELECT catalog_hash, created_at FROM catalog_versions ORDER BY created_at"
            )
        ]

    # ---- 降雨版本 ------------------------------------------------------
    def save_rain(
        self,
        rain_hash: str,
        catalog_hash: str,
        parent_hash: Optional[str],
        lo: int,
        hi: int,
        cells: Mapping[Tuple[str, int], Tuple[object, object]],
    ) -> str:
        """幂等保存完整降雨批次（含每格 rain/loss 有理文本）。"""
        if self.conn.execute(
            "SELECT 1 FROM rain_versions WHERE rain_hash = ?", (rain_hash,)
        ).fetchone():
            return rain_hash
        if not self.conn.execute(
            "SELECT 1 FROM catalog_versions WHERE catalog_hash = ?", (catalog_hash,)
        ).fetchone():
            raise StoreError(f"降雨批次引用了未知目录 {catalog_hash[:12]}…")
        if parent_hash is not None and not self.conn.execute(
            "SELECT 1 FROM rain_versions WHERE rain_hash = ?", (parent_hash,)
        ).fetchone():
            raise StoreError(f"修订父批次 {parent_hash[:12]}… 不存在")
        with self.conn:
            self.conn.execute(
                "INSERT INTO rain_versions(rain_hash, catalog_hash, parent_hash, lo, hi) "
                "VALUES (?, ?, ?, ?, ?)",
                (rain_hash, catalog_hash, parent_hash, lo, hi),
            )
            self.conn.executemany(
                "INSERT INTO rain_cells(rain_hash, node_id, t, rain, loss) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (rain_hash, nid, t, frac_text(r), frac_text(l))
                    for (nid, t), (r, l) in sorted(cells.items(), key=lambda x: (x[0][0], x[0][1]))
                ],
            )
        return rain_hash

    def load_rain_meta(self, rain_hash: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM rain_versions WHERE rain_hash = ?", (rain_hash,)
        ).fetchone()
        if row is None:
            raise StoreError(f"未知降雨版本 {rain_hash[:12]}…")
        return row

    def load_rain_cells(self, rain_hash: str) -> dict:
        self.load_rain_meta(rain_hash)  # 存在性检查
        out = {}
        for r in self.conn.execute(
            "SELECT node_id, t, rain, loss FROM rain_cells WHERE rain_hash = ? "
            "ORDER BY node_id, t",
            (rain_hash,),
        ):
            out[(r["node_id"], r["t"])] = (r["rain"], r["loss"])
        return out

    def list_rain(self) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT rain_hash, catalog_hash, parent_hash, lo, hi, created_at "
                "FROM rain_versions ORDER BY created_at"
            )
        )

    # ---- 分析档案 ------------------------------------------------------
    def save_analysis(
        self,
        analysis_id: str,
        catalog_hash: str,
        rain_hash: str,
        lo: int,
        hi: int,
        nodes: Sequence[str],
        status: str,
        report_json: Optional[str],
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO analyses"
                "(analysis_id, catalog_hash, rain_hash, lo, hi, nodes_json, status, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_id,
                    catalog_hash,
                    rain_hash,
                    lo,
                    hi,
                    json.dumps(list(nodes), ensure_ascii=False),
                    status,
                    report_json,
                ),
            )

    def load_analysis(self, analysis_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"未知分析 {analysis_id[:12]}…")
        return row

    def list_analyses(self) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT analysis_id, catalog_hash, rain_hash, status, created_at "
                "FROM analyses ORDER BY created_at"
            )
        )
