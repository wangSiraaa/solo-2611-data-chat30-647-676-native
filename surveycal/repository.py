"""SQLite 本地持久化。

产品表只插入、不更新、不删除：权重产品固定后不随源更正改变；控制更正只会
产生新的产品行。所有汇总（产品/报告）都可经外键追到每名参与样本及其权重。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import (
    ControlUnit,
    ProductKind,
    ResponseBatch,
    Sample,
    WeightProduct,
)
from .statistics import Report

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_persons (
    project_id  TEXT NOT NULL,
    row_idx     INTEGER NOT NULL,
    person_id   TEXT NOT NULL,
    base_weight REAL NOT NULL,
    design_json TEXT NOT NULL,
    lo_ratio    REAL,
    hi_ratio    REAL,
    PRIMARY KEY (project_id, person_id)
);
CREATE TABLE IF NOT EXISTS design_spec (
    project_id  TEXT PRIMARY KEY,
    fields_json TEXT NOT NULL,
    global_lo   REAL NOT NULL,
    global_hi   REAL NOT NULL,
    sample_sha  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_units (
    project_id    TEXT NOT NULL,
    code          TEXT NOT NULL,
    target        REAL NOT NULL,
    selector_json TEXT,
    members_json  TEXT NOT NULL,
    resolved_json TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    revision      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, code, revision)
);
CREATE TABLE IF NOT EXISTS weight_products (
    product_id      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS weight_entries (
    product_id  TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    row_idx     INTEGER NOT NULL,
    base_weight REAL NOT NULL,
    weight      REAL NOT NULL,
    residual_json TEXT NOT NULL,
    PRIMARY KEY (product_id, person_id),
    FOREIGN KEY (product_id) REFERENCES weight_products(product_id)
);
CREATE TABLE IF NOT EXISTS response_batches (
    batch_id    TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    indicator   TEXT NOT NULL,
    source_sha  TEXT NOT NULL,
    UNIQUE (project_id, indicator)
);
CREATE TABLE IF NOT EXISTS responses (
    batch_id    TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    indicator   TEXT NOT NULL,
    value       REAL,
    is_missing  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, person_id),
    FOREIGN KEY (batch_id) REFERENCES response_batches(batch_id)
);
CREATE TABLE IF NOT EXISTS reports (
    report_id   TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    group_field TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (product_id) REFERENCES weight_products(product_id)
);
CREATE TABLE IF NOT EXISTS report_cells (
    report_id       TEXT NOT NULL,
    indicator       TEXT NOT NULL,
    group_field     TEXT NOT NULL,
    group_value     TEXT NOT NULL,
    total_weight    REAL NOT NULL,
    valid_weight    REAL NOT NULL,
    missing_weight  REAL NOT NULL,
    weighted_total  REAL,
    mean            REAL,
    valid_n         INTEGER NOT NULL,
    missing_n       INTEGER NOT NULL,
    note            TEXT NOT NULL,
    PRIMARY KEY (report_id, indicator, group_value),
    FOREIGN KEY (report_id) REFERENCES reports(report_id)
);
"""


class Repository:
    """以单文件 SQLite 保存整个加工链路。"""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO project_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    def get_meta(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM project_meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    # ---------------------------------------------------------------- sample
    def save_sample(self, project_id: str, sample: Sample) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO design_spec VALUES(?,?,?,?,?)",
            (
                project_id,
                json.dumps({k: list(v) for k, v in sample.design.fields.items()}, ensure_ascii=False),
                sample.design.global_lo,
                sample.design.global_hi,
                sample.source_sha256,
            ),
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO sample_persons VALUES(?,?,?,?,?,?,?)",
            [
                (
                    project_id,
                    i,
                    p.person_id,
                    p.base_weight,
                    json.dumps(p.design, ensure_ascii=False),
                    p.lo,
                    p.hi,
                )
                for i, p in enumerate(sample.persons)
            ],
        )
        self.conn.commit()

    def load_sample(self, project_id: str) -> Sample:
        spec_row = self.conn.execute(
            "SELECT * FROM design_spec WHERE project_id=?", (project_id,)
        ).fetchone()
        if spec_row is None:
            raise KeyError(f"项目 {project_id} 尚未导入样本")
        from .models import DesignSpec

        spec = DesignSpec(
            fields={k: tuple(v) for k, v in json.loads(spec_row["fields_json"]).items()},
            global_lo=spec_row["global_lo"],
            global_hi=spec_row["global_hi"],
        )
        rows = self.conn.execute(
            "SELECT * FROM sample_persons WHERE project_id=? ORDER BY row_idx",
            (project_id,),
        ).fetchall()
        from .models import Person

        persons = tuple(
            # Person 自身做有限性/边界校验，持久化数据损坏时会显式报错
            Person(
                person_id=r["person_id"],
                base_weight=r["base_weight"],
                design=json.loads(r["design_json"]),
                lo=r["lo_ratio"],
                hi=r["hi_ratio"],
            )
            for r in rows
        )
        return Sample(persons, spec, spec_row["sample_sha"])

    # --------------------------------------------------------------- controls
    def save_controls(
        self,
        project_id: str,
        units: tuple[ControlUnit, ...],
        membership: dict[str, frozenset[int]],
        sample: Sample,
        revision: int = 1,
    ) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO control_units VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    project_id,
                    u.code,
                    u.target,
                    json.dumps(u.selector, ensure_ascii=False),
                    json.dumps(list(u.members), ensure_ascii=False),
                    json.dumps(
                        [sample.persons[j].person_id for j in sorted(membership[u.code])],
                        ensure_ascii=False,
                    ),
                    u.label,
                    revision,
                )
                for u in units
            ],
        )
        self.set_meta(f"{project_id}:control_revision", revision)
        self.conn.commit()

    def load_controls(
        self, project_id: str, sample: Sample, revision: int | None = None
    ):
        rev = revision or self.get_meta(f"{project_id}:control_revision") or 1
        rows = self.conn.execute(
            "SELECT * FROM control_units WHERE project_id=? AND revision=? ORDER BY rowid",
            (project_id, rev),
        ).fetchall()
        if not rows:
            raise KeyError(f"项目 {project_id} 没有 revision={rev} 的控制数")
        units = tuple(
            ControlUnit(
                code=r["code"],
                target=r["target"],
                selector=json.loads(r["selector_json"]) if r["selector_json"] else None,
                members=tuple(json.loads(r["members_json"])),
                label=r["label"],
            )
            for r in rows
        )
        idx = {p.person_id: i for i, p in enumerate(sample.persons)}
        membership = {
            r["code"]: frozenset(idx[pid] for pid in json.loads(r["resolved_json"]))
            for r in rows
        }
        return units, membership, rev

    # --------------------------------------------------------------- products
    def save_product(self, project_id: str, product: WeightProduct, status: str) -> None:
        """产品只插入；相同 product_id 再写会被主键拒绝，保证固定不可变。"""
        try:
            self.conn.execute(
                "INSERT INTO weight_products(product_id, project_id, kind, snapshot_json, status) "
                "VALUES(?,?,?,?,?)",
                (
                    product.product_id,
                    project_id,
                    product.kind.value,
                    json.dumps(product.to_export_dict(), ensure_ascii=False),
                    status,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"产品 {product.product_id} 已存在，权重产品固定不可变") from exc
        self.conn.executemany(
            "INSERT INTO weight_entries VALUES(?,?,?,?,?,?)",
            [
                (
                    product.product_id,
                    pid,
                    i,
                    product.base_weights[i],
                    product.weights[i],
                    # 逐单元残差向量随产品快照保存；每人行冗余一份以便按人追溯
                    json.dumps(
                        {
                            "person_id": pid,
                            "weight_ratio": product.weights[i] / product.base_weights[i],
                            "per_unit_residuals": list(product.residuals),
                        },
                        ensure_ascii=False,
                    ),
                )
                for i, pid in enumerate(product.sample_ids)
            ],
        )
        self.conn.commit()

    def list_products(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT product_id, kind, status, created_at FROM weight_products "
            "WHERE project_id=? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def load_product(self, product_id: str) -> WeightProduct:
        row = self.conn.execute(
            "SELECT snapshot_json FROM weight_products WHERE product_id=?",
            (product_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"权重产品不存在: {product_id}")
        from .ingest import parse_external_product

        return parse_external_product(json.loads(row["snapshot_json"]))

    def trace_product(self, product_id: str) -> dict:
        """汇总 -> 每个参与样本及其权重/残差的完整追溯。"""
        product = self.load_product(product_id)
        rows = self.conn.execute(
            "SELECT person_id, row_idx, base_weight, weight FROM weight_entries "
            "WHERE product_id=? ORDER BY row_idx",
            (product_id,),
        ).fetchall()
        return {
            "product_id": product_id,
            "kind": product.kind.value,
            "objective": product.objective,
            "tol": product.tol,
            "controls": [
                {"code": c["code"], "target": c["target"], "residual": product.residuals[i]}
                for i, c in enumerate(product.controls)
            ],
            "entries": [dict(r) for r in rows],
        }

    # -------------------------------------------------------------- responses
    def save_responses(self, project_id: str, batches: dict[str, ResponseBatch]) -> None:
        for ind, batch in batches.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO response_batches VALUES(?,?,?,?)",
                (batch.batch_id, project_id, ind, batch.source_sha256),
            )
            records = [
                (batch.batch_id, pid, ind, v, 0) for pid, v in batch.values.items()
            ] + [(batch.batch_id, pid, ind, None, 1) for pid in batch.missing_ids]
            self.conn.executemany(
                "INSERT OR REPLACE INTO responses VALUES(?,?,?,?,?)", records
            )
        self.conn.commit()

    def load_responses(self, project_id: str) -> dict[str, ResponseBatch]:
        batch_rows = self.conn.execute(
            "SELECT * FROM response_batches WHERE project_id=?", (project_id,)
        ).fetchall()
        out: dict[str, ResponseBatch] = {}
        for br in batch_rows:
            rows = self.conn.execute(
                "SELECT * FROM responses WHERE batch_id=?", (br["batch_id"],)
            ).fetchall()
            values = {r["person_id"]: r["value"] for r in rows if not r["is_missing"]}
            missing = tuple(r["person_id"] for r in rows if r["is_missing"])
            out[br["indicator"]] = ResponseBatch(
                batch_id=br["batch_id"],
                indicator=br["indicator"],
                values=values,
                missing_ids=missing,
                source_sha256=br["source_sha"],
            )
        return out

    # ---------------------------------------------------------------- reports
    def save_report(self, project_id: str, report: Report) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reports VALUES(?,?,?,?,datetime('now'))",
            (report.report_id, report.product_id, project_id, report.group_field),
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO report_cells VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    report.report_id,
                    c.indicator,
                    c.group_field,
                    c.group_value,
                    c.total_weight,
                    c.valid_weight,
                    c.missing_weight,
                    c.weighted_total,
                    c.mean,
                    c.valid_n,
                    c.missing_n,
                    c.note,
                )
                for c in report.cells
            ],
        )
        self.conn.commit()

    def list_reports(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.report_id, r.product_id, r.group_field, r.created_at "
            "FROM reports r WHERE r.project_id=? ORDER BY r.rowid",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def load_report_cells(self, report_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM report_cells WHERE report_id=? ORDER BY indicator, group_value",
            (report_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def report_detail(self, report_id: str) -> dict:
        head = self.conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if head is None:
            raise KeyError(f"报告不存在: {report_id}")
        return {
            "report_id": report_id,
            "product_id": head["product_id"],
            "group_field": head["group_field"],
            "cells": self.load_report_cells(report_id),
        }
