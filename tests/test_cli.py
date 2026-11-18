"""命令行调用链测试：导入、校准、发布、报告、查询、验证与外部产品。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ENV = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}


def run(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "surveycal", *map(str, args)],
        capture_output=True, text=True, env=ENV,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, payload


def test_cli_full_chain(data_dir, tmp_path):
    db = tmp_path / "c.db"
    case = data_dir["joint_marginal"]
    rc, out = run("import", "--db", db, "--project", "p",
                  "--sample", f"{case}/sample.csv", "--controls", f"{case}/controls.json")
    assert rc == 0 and out["n_persons"] == 150

    rc, out = run("calibrate", "--db", db, "--project", "p")
    assert rc == 0 and out["status"] == "publishable"
    assert out["worst_residual_abs"] < 1e-6

    export = tmp_path / "wp.json"
    rc, out = run("publish", "--db", db, "--project", "p", "--export", export)
    assert rc == 0 and out["kind"] == "official" and out["verify"]["passed"]
    pid = out["product_id"]

    rc, out = run("verify", "--db", db, "--project", "p", "--product-id", pid)
    assert rc == 0 and out["passed"]

    rc, out = run("query", "--db", db, "--project", "p", "products")
    assert rc == 0 and any(x["product_id"] == pid for x in out)

    rc, out = run("query", "--db", db, "--project", "p", "trace", pid)
    assert rc == 0 and len(out["entries"]) == 150


def test_cli_infeasible_refuses_publish(data_dir, tmp_path):
    db = tmp_path / "e.db"
    case = data_dir["empty_unit"]
    run("import", "--db", db, "--project", "p",
        "--sample", f"{case}/sample.csv", "--controls", f"{case}/controls.json")
    rc, out = run("calibrate", "--db", db, "--project", "p")
    assert rc == 0 and out["status"] == "infeasible"
    rc, out = run("publish", "--db", db, "--project", "p")
    assert rc == 3 and out["error_type"] == "InfeasibleError"


def test_cli_report_with_missing_responses(data_dir, tmp_path):
    db = tmp_path / "m.db"
    case = data_dir["missing_responses"]
    run("import", "--db", db, "--project", "p",
        "--sample", f"{case}/sample.csv", "--controls", f"{case}/controls.json")
    run("import-resp", "--db", db, "--project", "p",
        "--responses", f"{case}/responses.csv")
    _, pub = run("publish", "--db", db, "--project", "p")
    pid = pub["product_id"]
    rc, out = run("report", "--db", db, "--project", "p", "--product-id", pid,
                  "--group", "region", "--indicators", "income,score")
    assert rc == 0
    income_r1 = next(c for c in out["cells"] if c["indicator"] == "income" and c["group_value"] == "R1")
    # P2 缺失：其权重 2.0 计入缺失权重；P1 的 100 是唯一有效回答
    assert income_r1["missing_weight"] == 2.0
    assert income_r1["valid_weight"] == 1.0
    assert income_r1["mean"] == 100.0


def test_cli_external_product_import_and_reject(data_dir, tmp_path):
    db1, db2 = tmp_path / "a.db", tmp_path / "b.db"
    case = data_dir["joint_marginal"]
    run("import", "--db", db1, "--project", "p",
        "--sample", f"{case}/sample.csv", "--controls", f"{case}/controls.json")
    export = tmp_path / "wp.json"
    run("publish", "--db", db1, "--project", "p", "--export", export)
    rc, out = run("import-product", "--db", db2, "--project", "ext", "--product", export)
    assert rc == 0 and out["imported"]

    # 篡改后外部产品应被拒绝（退出码 2）
    doc = json.loads(export.read_text(encoding="utf-8"))
    doc["weights"][0] += 1.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    rc, out = run("import-product", "--db", db2, "--project", "ext2", "--product", bad)
    assert rc == 2 and out["rejected"] is True


def test_cli_revision_workflow(data_dir, tmp_path):
    db = tmp_path / "r.db"
    c1, c2 = data_dir["joint_marginal"], data_dir["control_revision"]
    run("import", "--db", db, "--project", "p",
        "--sample", f"{c1}/sample.csv", "--controls", f"{c1}/controls.json")
    _, p1 = run("publish", "--db", db, "--project", "p")
    run("import", "--db", db, "--project", "p",
        "--sample", f"{c2}/sample.csv", "--controls", f"{c2}/controls.json")
    _, p2 = run("publish", "--db", db, "--project", "p")
    assert p1["product_id"] != p2["product_id"]
    _, products = run("query", "--db", db, "--project", "p", "products")
    assert len(products) == 2  # 旧产品保留，新产品追加
