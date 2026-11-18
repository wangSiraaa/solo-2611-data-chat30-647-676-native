"""组织编制服务。

维护一棵唯一根、无环、每部门至多一个父项的部门树，工作副本上的
建部门 / 移动完整子树 / 停用叶部门在 ``publish`` 时整体校验并发布为
不可变的组织产品（:class:`OrgProduct`），版本号严格递增。

持久化采用独立的 SQLite 文件，与预算执行仓物理分离：组织维护不依赖
预算，行政客户可直接使用本服务。已发布产品以快照行保存，后续移动
不会改变任何旧版本的内容。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .errors import (
    CycleError,
    DepartmentNotFound,
    DuplicateDepartment,
    InactiveDepartment,
    InvalidTree,
    NotALeaf,
    OrganizationVersionNotFound,
    RootError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 工作副本：每个部门一行，parent_id 为 NULL 即唯一根。
CREATE TABLE IF NOT EXISTS dept_current (
    dept_id         TEXT PRIMARY KEY,
    parent_id       TEXT REFERENCES dept_current(dept_id),
    active          INTEGER NOT NULL CHECK (active IN (0, 1)),
    created_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dept_parent ON dept_current(parent_id);

-- 已发布的不可变组织产品：版本头 + 全量快照。
CREATE TABLE IF NOT EXISTS org_version (
    version      INTEGER PRIMARY KEY,
    published_at TEXT NOT NULL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS org_dept (
    version   INTEGER NOT NULL REFERENCES org_version(version),
    dept_id   TEXT NOT NULL,
    parent_id TEXT,
    active    INTEGER NOT NULL,
    depth     INTEGER NOT NULL,
    PRIMARY KEY (version, dept_id)
);
CREATE INDEX IF NOT EXISTS idx_orgdept_version_parent ON org_dept(version, parent_id);
"""


@dataclass(frozen=True)
class OrgProduct:
    """某个已发布版本的不可变组织产品。

    节点集合、父子关系、停用状态全部冻结在发布时刻；祖先 / 后代查询
    只沿本产品自身的树边计算，永不引用当前工作副本。
    """

    version: Optional[int]
    published_at: Optional[str]
    parent_of: dict[str, Optional[str]]
    active: dict[str, bool]
    note: Optional[str] = None

    def __post_init__(self) -> None:
        roots = [d for d, p in self.parent_of.items() if p is None]
        if len(roots) != 1:
            raise InvalidTree(f"组织产品必须恰有一个根，实际: {roots}")
        for dept, parent in self.parent_of.items():
            if parent is not None and parent not in self.parent_of:
                raise InvalidTree(f"部门 {dept} 的父部门 {parent} 不存在")
        if set(self.active) != set(self.parent_of):
            raise InvalidTree("active 标记与部门集合不一致")
        # 无环校验：每个节点沿父链向上必须在有限步到达根。
        for dept in self.parent_of:
            seen: set[str] = set()
            cur: Optional[str] = dept
            while cur is not None:
                if cur in seen:
                    raise InvalidTree(f"检测到环，涉及部门 {cur}")
                seen.add(cur)
                cur = self.parent_of[cur]

    @property
    def root(self) -> str:
        return next(d for d, p in self.parent_of.items() if p is None)

    @property
    def department_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.parent_of))

    def contains(self, dept_id: str) -> bool:
        return dept_id in self.parent_of

    def is_active(self, dept_id: str) -> bool:
        self._require(dept_id)
        return self.active[dept_id]

    def is_leaf(self, dept_id: str) -> bool:
        self._require(dept_id)
        return not any(p == dept_id for p in self.parent_of.values())

    def children(self, parent_id: str) -> tuple[str, ...]:
        self._require(parent_id)
        return tuple(sorted(d for d, p in self.parent_of.items() if p == parent_id))

    def ancestors(self, dept_id: str) -> tuple[str, ...]:
        """根 → 直接父级 的祖先链（不含自身）。"""
        self._require(dept_id)
        chain: list[str] = []
        cur = self.parent_of[dept_id]
        while cur is not None:
            chain.append(cur)
            cur = self.parent_of[cur]
        chain.reverse()
        return tuple(chain)

    def descendants(self, dept_id: str) -> tuple[str, ...]:
        """全部严格后代，按部门 ID 升序。"""
        self._require(dept_id)
        result: list[str] = []
        stack = list(self.children(dept_id))
        while stack:
            cur = stack.pop()
            result.append(cur)
            stack.extend(self.children(cur))
        return tuple(sorted(result))

    def subtree(self, dept_id: str) -> tuple[str, ...]:
        """自身 + 全部后代。"""
        return (dept_id,) + self.descendants(dept_id)

    def depth(self, dept_id: str) -> int:
        return len(self.ancestors(dept_id))

    def max_depth(self) -> int:
        return max(self.depth(d) for d in self.parent_of)

    def _require(self, dept_id: str) -> None:
        if dept_id not in self.parent_of:
            raise DepartmentNotFound(f"组织产品中不存在部门 {dept_id}")


class OrgService:
    """组织编制服务，数据落在独立 SQLite 文件。"""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.db_path = str(db_path)
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 工作副本

    def create_department(self, dept_id: str, parent_id: Optional[str]) -> str:
        """建立部门。``parent_id`` 为 None 时建立（且仅允许）唯一根。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM dept_current WHERE dept_id = ?", (dept_id,)
            ).fetchone():
                raise DuplicateDepartment(f"部门 {dept_id} 已存在")
            if parent_id is None:
                if con.execute("SELECT 1 FROM dept_current LIMIT 1").fetchone():
                    raise RootError("根已存在，一棵组织树只能有一个根")
                created_version = 0
            else:
                row = con.execute(
                    "SELECT active FROM dept_current WHERE dept_id = ?", (parent_id,)
                ).fetchone()
                if row is None:
                    raise DepartmentNotFound(f"父部门 {parent_id} 不存在")
                if not row[0]:
                    raise InactiveDepartment(f"不能在已停用部门 {parent_id} 下建孩子")
                created_version = 0
            con.execute(
                "INSERT INTO dept_current(dept_id, parent_id, active, created_version) "
                "VALUES (?, ?, 1, ?)",
                (dept_id, parent_id, created_version),
            )
            con.commit()
        return dept_id

    def move_subtree(self, dept_id: str, new_parent_id: str) -> None:
        """把以 ``dept_id`` 为根的完整子树整体挂到新父部门下。

        根不可移动；新父不能是被移动节点自身或其后代（否则成环）；
        只改 ``dept_id`` 一条父边即天然保证“完整子树、不留部分移动”。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT parent_id, active FROM dept_current WHERE dept_id = ?",
                (dept_id,),
            ).fetchone()
            if row is None:
                raise DepartmentNotFound(f"部门 {dept_id} 不存在")
            old_parent, active = row
            if old_parent is None:
                raise RootError("根部门不能被移动")
            if not active:
                raise InactiveDepartment(f"部门 {dept_id} 已停用，不能移动")
            if new_parent_id == dept_id:
                raise CycleError("不能把部门挂到自身之下")
            target = con.execute(
                "SELECT active FROM dept_current WHERE dept_id = ?", (new_parent_id,)
            ).fetchone()
            if target is None:
                raise DepartmentNotFound(f"新父部门 {new_parent_id} 不存在")
            if not target[0]:
                raise InactiveDepartment(f"新父部门 {new_parent_id} 已停用")
            if old_parent == new_parent_id:
                con.commit()
                return
            # 新父若是被移动子树的成员，必然成环。
            subtree = self._descendants_locked(con, dept_id) | {dept_id}
            if new_parent_id in subtree:
                raise CycleError(
                    f"不能把 {dept_id} 移到其后代 {new_parent_id} 之下，会形成环"
                )
            con.execute(
                "UPDATE dept_current SET parent_id = ? WHERE dept_id = ?",
                (new_parent_id, dept_id),
            )
            con.commit()

    def deactivate_leaf(self, dept_id: str) -> None:
        """停用叶部门。根不能停用，有孩子（含历史停用孩子）就不是叶。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT parent_id, active FROM dept_current WHERE dept_id = ?",
                (dept_id,),
            ).fetchone()
            if row is None:
                raise DepartmentNotFound(f"部门 {dept_id} 不存在")
            parent_id, active = row
            if parent_id is None:
                raise RootError("根部门不能停用，组织必须始终保留唯一根")
            if not active:
                con.commit()
                return
            child = con.execute(
                "SELECT dept_id FROM dept_current WHERE parent_id = ? LIMIT 1",
                (dept_id,),
            ).fetchone()
            if child is not None:
                raise NotALeaf(f"部门 {dept_id} 仍有孩子 {child[0]}，不是叶部门")
            con.execute(
                "UPDATE dept_current SET active = 0 WHERE dept_id = ?", (dept_id,)
            )
            con.commit()

    # ---------------------------------------------------------------- 发布

    def publish(self, note: Optional[str] = None) -> OrgProduct:
        """校验当前工作副本并发布不可变组织产品，版本号递增。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            product = self._load_current_locked(con)
            current = con.execute("SELECT COALESCE(MAX(version), 0) FROM org_version").fetchone()[0]
            next_version = current + 1
            published_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO org_version(version, published_at, note) VALUES (?, ?, ?)",
                (next_version, published_at, note),
            )
            con.executemany(
                "INSERT INTO org_dept(version, dept_id, parent_id, active, depth) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        next_version,
                        dept,
                        product.parent_of[dept],
                        1 if product.active[dept] else 0,
                        product.depth(dept),
                    )
                    for dept in product.department_ids
                ],
            )
            con.commit()
        return self.get_product(next_version)

    def get_product(self, version: int) -> OrgProduct:
        """读取指定版本的不可变组织产品。"""
        with self._connect() as con:
            head = con.execute(
                "SELECT published_at, note FROM org_version WHERE version = ?",
                (version,),
            ).fetchone()
            if head is None:
                raise OrganizationVersionNotFound(f"组织产品版本 {version} 不存在")
            rows = con.execute(
                "SELECT dept_id, parent_id, active FROM org_dept WHERE version = ?",
                (version,),
            ).fetchall()
        if not rows:
            raise OrganizationVersionNotFound(f"组织产品版本 {version} 快照为空")
        return OrgProduct(
            version=version,
            published_at=head[0],
            parent_of={dept: parent for dept, parent, _ in rows},
            active={dept: bool(flag) for dept, _, flag in rows},
            note=head[1],
        )

    @property
    def current_version(self) -> Optional[int]:
        with self._connect() as con:
            return con.execute("SELECT COALESCE(MAX(version), 0) FROM org_version").fetchone()[0] or None

    def versions(self) -> tuple[tuple[int, str, Optional[str]], ...]:
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT version, published_at, note FROM org_version ORDER BY version"
                ).fetchall()
            )

    def working_product(self) -> OrgProduct:
        """当前工作副本快照（草稿，version 为 None），供行政客户直接查询。"""
        with self._connect() as con:
            return self._load_current_locked(con)

    # ---------------------------------------------------------------- 内部

    def _load_current_locked(self, con: sqlite3.Connection) -> OrgProduct:
        rows = con.execute(
            "SELECT dept_id, parent_id, active FROM dept_current"
        ).fetchall()
        if not rows:
            raise InvalidTree("组织为空，无法发布：至少需要一个根部门")
        return OrgProduct(
            version=None,
            published_at=None,
            parent_of={dept: parent for dept, parent, _ in rows},
            active={dept: bool(flag) for dept, _, flag in rows},
        )

    @staticmethod
    def _descendants_locked(con: sqlite3.Connection, dept_id: str) -> set[str]:
        result: set[str] = set()
        stack = [dept_id]
        while stack:
            parent = stack.pop()
            kids = [
                r[0]
                for r in con.execute(
                    "SELECT dept_id FROM dept_current WHERE parent_id = ?", (parent,)
                ).fetchall()
            ]
            for kid in kids:
                if kid not in result:
                    result.add(kid)
                    stack.append(kid)
        return result
