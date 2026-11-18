"""SQLite 持久化与重开测试：产品不可变、汇总可追到每人、外部产品可消费。"""

from __future__ import annotations

import json
import os

import pytest

from surveycal import calibration as C, ingest, statistics
from surveycal.models import CalibStatus, ProductKind
from surveycal.repository import Repository

from .conftest import load_case


def _publish(case_dir, project="p"):
    sample, units, mem = load_case(case_dir)
    r = C.calibrate(sample, units, mem, cross_check=False)
    product = C.build_product(sample, units, mem, r, created_from={"project": project})
    return sample, units, mem, r, product


def test_full_persistence_and_reopen(data_dir, db_path):
    sample, units, mem, r, product = _publish(data_dir["joint_marginal"])
    with Repository(db_path) as repo:
        repo.save_sample("p", sample)
        repo.save_controls("p", units, mem, sample)
        repo.save_product("p", product, CalibStatus.PUBLISHABLE.value)

    # 重开：新连接、新 Repository 对象
    with Repository(db_path) as repo:
        sample2 = repo.load_sample("p")
        units2, mem2, rev = repo.load_controls("p", sample2)
        product2 = repo.load_product(product.product_id)
        assert product2.weights == product.weights
        assert product2.product_id == product.product_id
        # 汇总 -> 每个参与样本及权重的追溯
        trace = repo.trace_product(product.product_id)
        assert len(trace["entries"]) == len(sample.persons)
        assert {e["person_id"] for e in trace["entries"]} == set(sample.ids)
        assert all(e["weight"] > 0 for e in trace["entries"])


def test_product_is_immutable_after_fixed(data_dir, db_path):
    sample, units, mem, r, product = _publish(data_dir["joint_marginal"])
    with Repository(db_path) as repo:
        repo.save_sample("p", sample)
        repo.save_controls("p", units, mem, sample)
        repo.save_product("p", product, CalibStatus.PUBLISHABLE.value)
        # 同一 product_id 再写必须被主键拒绝
        with pytest.raises(ValueError):
            repo.save_product("p", product, CalibStatus.PUBLISHABLE.value)


def test_control_revision_creates_new_product_keeps_old(data_dir, db_path):
    s1, u1, m1, r1, p1 = _publish(data_dir["joint_marginal"])
    s2, u2, m2 = load_case(data_dir["control_revision"])
    r2 = C.calibrate(s2, u2, m2, cross_check=False)
    p2 = C.build_product(s2, u2, m2, r2, created_from={"project": "p", "rev": 2})
    with Repository(db_path) as repo:
        repo.save_sample("p", s1)
        repo.save_controls("p", u1, m1, s1, revision=1)
        repo.save_product("p", p1, CalibStatus.PUBLISHABLE.value)
        repo.save_controls("p", u2, m2, s2, revision=2)
        repo.save_product("p", p2, CalibStatus.PUBLISHABLE.value)
        products = repo.list_products("p")
        assert len(products) == 2
        assert {x["product_id"] for x in products} == {p1.product_id, p2.product_id}
        # 旧产品仍可按原 ID 取到，且权重不被源更正改变
        assert repo.load_product(p1.product_id).weights == p1.weights


def test_report_persistence_and_cells(data_dir, db_path):
    sample, units, mem, r, product = _publish(data_dir["missing_responses"])
    batches = ingest.load_responses_csv(
        os.path.join(data_dir["missing_responses"], "responses.csv"), sample
    )
    with Repository(db_path) as repo:
        repo.save_sample("p", sample)
        repo.save_controls("p", units, mem, sample)
        repo.save_product("p", product, CalibStatus.PUBLISHABLE.value)
        repo.save_responses("p", batches)
        loaded_batches = repo.load_responses("p")
        report = statistics.publish_report(product, loaded_batches, "region")
        repo.save_report("p", report)
        detail = repo.report_detail(report.report_id)
        assert detail["product_id"] == product.product_id
        cells = repo.load_report_cells(report.report_id)
        assert len(cells) == len(report.cells)
        # 缺失权重被持久化且非负
        assert all(c["missing_weight"] >= 0 for c in cells)


def test_external_product_roundtrip(data_dir, tmp_path, db_path):
    sample, units, mem, r, product = _publish(data_dir["joint_marginal"])
    ext = tmp_path / "external.json"
    ext.write_text(json.dumps(product.to_export_dict(), ensure_ascii=False), encoding="utf-8")
    loaded = ingest.load_external_product(str(ext))
    verify = C.verify_product(loaded)
    assert verify["passed"]
    with Repository(db_path) as repo:
        repo.save_product("ext", loaded, loaded.kind.value)
        again = repo.load_product(loaded.product_id)
        assert again.weights == product.weights


def test_external_tampered_product_rejected_on_import(data_dir, tmp_path):
    sample, units, mem, r, product = _publish(data_dir["joint_marginal"])
    doc = product.to_export_dict()
    doc["weights"][0] += 1.0  # 篡改
    ext = tmp_path / "bad.json"
    ext.write_text(json.dumps(doc), encoding="utf-8")
    loaded = ingest.load_external_product(str(ext))
    verify = C.verify_product(loaded)
    assert not verify["passed"]
