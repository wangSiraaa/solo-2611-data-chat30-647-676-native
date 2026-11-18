"""pytest 固定装置：每个测试用独立临时目录与 SQLite 库，数据全部为合成虚构数据。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surveycal import generate, ingest  # noqa: E402
from surveycal.repository import Repository  # noqa: E402


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("data"))
    return generate.generate_all(d)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "work.db")


def load_case(case_dir):
    """从案例目录直接（不经 SQLite）登记样本、控制与成员。"""
    sample = ingest.load_sample_csv(
        os.path.join(case_dir, "sample.csv"),
        os.path.join(case_dir, "controls.json"),
    )
    _, units = ingest.load_controls_json(os.path.join(case_dir, "controls.json"))
    membership = ingest.resolve_membership(sample, units)
    return sample, units, membership
