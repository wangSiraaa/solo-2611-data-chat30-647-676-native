"""工艺路线目录的持久化层（SQLite）。

目录只管理产品版本、转换定义和设备种类，不保存任何执行事件。
每个 ``(route_id, version)`` 是不可变快照；编辑总是产生新版本。
发布状态仅影响 ``latest`` 查询，历史绑定不受影响。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .route import (
    RouteDefinition,
    RouteError,
    route_from_dict,
)


class CatalogError(ValueError):
    """目录操作错误。"""


@dataclass(frozen=True)
class ProductDefinition:
    route_id: str
    version: str
    status: str  # DRAFT / PUBLISHED
    payload: dict
    published_at: Optional[int] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cat_routes (
    route_id     TEXT NOT NULL,
    version      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'DRAFT',
    payload      TEXT NOT NULL,
    published_at INTEGER,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (route_id, version)
);
CREATE TABLE IF NOT EXISTS cat_devices (
    device_id   TEXT PRIMARY KEY,
    device_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat_routes_latest
    ON cat_routes(route_id, status, version);
"""


class CatalogRepository:
    """持久化路线目录。与执行仓完全解耦，可独立打开/重开。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA synchronous=OFF")
        self.conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CatalogRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _tx(self):
        return _Transaction(self.conn)

    # ------------------------------------------------------------------ 设备
    def register_device(self, device_id: str, device_type: str) -> None:
        if not device_id or not device_type:
            raise CatalogError("device_id/device_type 不能为空")
        with self._tx():
            self.conn.execute(
                "INSERT INTO cat_devices(device_id, device_type) VALUES(?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET device_type=excluded.device_type",
                (device_id, device_type),
            )

    def list_devices(self) -> List[dict]:
        return [
            {"device_id": r["device_id"], "device_type": r["device_type"]}
            for r in self.conn.execute("SELECT device_id, device_type FROM cat_devices ORDER BY device_id")
        ]

    def get_device(self, device_id: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT device_id, device_type FROM cat_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ 编辑/发布
    def save_draft(self, route: RouteDefinition) -> ProductDefinition:
        """新增一个草稿版本。已存在的版本不可覆盖。"""
        payload = route.to_dict()
        try:
            with self._tx():
                self.conn.execute(
                    "INSERT INTO cat_routes(route_id, version, status, payload, published_at) "
                    "VALUES(?, ?, 'DRAFT', ?, ?)",
                    (route.route_id, route.version, json.dumps(payload, ensure_ascii=False), route.published_at),
                )
        except sqlite3.IntegrityError as exc:
            raise CatalogError(
                f"路线版本 {route.route_id}@{route.version} 已存在；编辑请使用新版本号"
            ) from exc
        return ProductDefinition(route.route_id, route.version, "DRAFT", payload, route.published_at)

    def save_route(self, route: RouteDefinition) -> ProductDefinition:
        """保存并立即发布（常见入口）。"""
        self.save_draft(route)
        return self.publish(route.route_id, route.version, published_at=route.published_at)

    def publish(self, route_id: str, version: str, published_at: Optional[int] = None) -> ProductDefinition:
        with self._tx():
            cur = self.conn.execute(
                "UPDATE cat_routes SET status='PUBLISHED', published_at=COALESCE(?, published_at) "
                "WHERE route_id=? AND version=?",
                (published_at, route_id, version),
            )
            if cur.rowcount == 0:
                raise CatalogError(f"未知路线版本 {route_id}@{version}")
        return self.get(route_id, version)  # type: ignore[return-value]

    def unpublish(self, route_id: str, version: str) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE cat_routes SET status='DRAFT' WHERE route_id=? AND version=?",
                (route_id, version),
            )

    def edit_as_new_version(
        self,
        route_id: str,
        base_version: str,
        new_version: str,
        description: Optional[str] = None,
    ) -> ProductDefinition:
        """从既有版本复制出新的草稿版本，便于后续修改后发布。"""
        base = self.get_route(route_id, base_version)
        data = base.to_dict()
        data["version"] = new_version
        data["published_at"] = None
        if description is not None:
            data["description"] = description
        route = route_from_dict(data)
        return self.save_draft(route)

    # ------------------------------------------------------------------ 读取
    def get(self, route_id: str, version: str) -> Optional[ProductDefinition]:
        r = self.conn.execute(
            "SELECT route_id, version, status, payload, published_at FROM cat_routes "
            "WHERE route_id=? AND version=?",
            (route_id, version),
        ).fetchone()
        if not r:
            return None
        return ProductDefinition(
            r["route_id"], r["version"], r["status"], json.loads(r["payload"]), r["published_at"]
        )

    def get_route(self, route_id: str, version: str) -> RouteDefinition:
        product = self.get(route_id, version)
        if not product:
            raise RouteError(f"未知路线版本 {route_id}@{version}")
        return route_from_dict(product.payload)

    def latest(self, route_id: str) -> ProductDefinition:
        # 按插入顺序（rowid）取最近发布的版本，避免 v10 与 v2 的字典序歧义。
        r = self.conn.execute(
            "SELECT route_id, version, status, payload, published_at FROM cat_routes "
            "WHERE route_id=? AND status='PUBLISHED' ORDER BY rowid DESC LIMIT 1",
            (route_id,),
        ).fetchone()
        if not r:
            raise CatalogError(f"路线 {route_id} 没有已发布版本")
        return ProductDefinition(
            r["route_id"], r["version"], r["status"], json.loads(r["payload"]), r["published_at"]
        )

    def latest_route(self, route_id: str) -> RouteDefinition:
        return route_from_dict(self.latest(route_id).payload)

    def versions(self, route_id: str) -> List[ProductDefinition]:
        rows = self.conn.execute(
            "SELECT route_id, version, status, payload, published_at FROM cat_routes "
            "WHERE route_id=? ORDER BY version",
            (route_id,),
        )
        return [
            ProductDefinition(r["route_id"], r["version"], r["status"], json.loads(r["payload"]), r["published_at"])
            for r in rows
        ]

    def all_products(self) -> List[ProductDefinition]:
        rows = self.conn.execute(
            "SELECT route_id, version, status, payload, published_at FROM cat_routes "
            "ORDER BY route_id, version"
        )
        return [
            ProductDefinition(r["route_id"], r["version"], r["status"], json.loads(r["payload"]), r["published_at"])
            for r in rows
        ]

    # ------------------------------------------------------- 与执行仓的边界
    def export_product(self, route_id: str, version: str) -> dict:
        """导出与 RouteDefinition.to_dict 同格式的产品定义。

        执行仓只接受这种普通字典，不读取 cat_routes 内部结构。
        """
        return self.get_route(route_id, version).to_dict()

    # ------------------------------------------------------------ 独立校验
    def validate_sequence(self, route_id: str, version: str, step_ids: Iterable[str]):
        return self.get_route(route_id, version).validate_sequence(step_ids)

    def shortest_remaining(self, route_id: str, version: str, state: str) -> Optional[int]:
        return self.get_route(route_id, version).shortest_remaining(state)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        if self.depth == 1:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp_{self.depth}")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.depth == 1:
            if exc_type:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute("COMMIT")
        else:
            if exc_type:
                self.conn.execute(f"ROLLBACK TO sp_{self.depth}")
            self.conn.execute(f"RELEASE sp_{self.depth}")
        self.depth -= 1
        return False
