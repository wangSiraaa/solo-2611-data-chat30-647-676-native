"""执行事件事实仓（SQLite）：事件日志 + 派生投影。

设计
----
* **事实**：``exec_journal`` 按 op_seq 顺序保存所有已受理操作
  （物料创建 UNIT_CREATE、显式迁移 MIGRATE、工步事件 EVENT，其中的事件
  字段包含 acceptance_seq）。重开数据库时只重放日志即可重建全部投影。
* **投影**：物料、绑定、尝试三张表由日志派生，查询全部基于内存投影，
  每次提交在同一事务中整体重写，因此不会出现半更新。
* **事件顺序**：一批事件必须严格按给定的严格递增 acceptance_seq 排列导入，
  且所有新 acceptance_seq 必须大于历史最大值；业务时刻 time 可以乱序。
  任何一条非法 -> 整批不改动业务投影，并记录拒绝原因。
* **设备占用**：[START, END)；在途尝试以 [START, START+标准耗时) 预留，
  完工时用实际 [START, END) 复核。完工与下一开始同秒允许。
* **路线绑定**：物料创建/迁移时绑定产品快照；尝试记录自己开始时的产品，
  FINISH 按该尝试自己的产品解释，旧工步不会因后续编辑/迁移而改写。
* **计数**：合格完工按“物料件数”计，accepted_counted 保证不重复计数。
* **终态**：当前处于合格/报废终态时拒绝一切新工步；断开在途尝试后，
  显式迁移是唯一能改变绑定/状态的操作。
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .route import (
    RouteDefinition,
    RouteError,
    RouteState,
    route_from_dict,
)

DEFAULT_DEVICE_TYPES: Tuple[str, ...] = ("CNC", "ASSEMBLY", "INSPECTION", "REWORK")
# 规范设备目录：设备 ID -> 种类。演示、测试、CLI 初始化共用，避免命名漂移。
DEFAULT_DEVICES: Dict[str, str] = {
    "dev_cnc": "CNC",
    "dev_asm": "ASSEMBLY",
    "dev_insp": "INSPECTION",
    "dev_rw": "REWORK",
}

MAX_UNITS = 100
MAX_EVENTS = 1000

_START = "START"
_FINISH = "FINISH"
_CANCEL = "CANCEL"

_IN_FLIGHT = "IN_FLIGHT"
_DONE = "DONE"
_CANCELLED = "CANCELLED"

_ACTIVE = "ACTIVE"
_ACCEPTED = "ACCEPTED"
_SCRAP = "SCRAP"


class WarehouseError(ValueError):
    """执行仓的对外错误（创建、迁移、查询等）。"""


class EventError(WarehouseError):
    """单条事件非法。携带定位信息，供整批拒绝时解释。"""

    def __init__(
        self,
        code: str,
        message: str,
        index: Optional[int] = None,
        acceptance_seq: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.index = index
        self.acceptance_seq = acceptance_seq
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "index": self.index,
            "acceptance_seq": self.acceptance_seq,
            "details": self.details,
        }


@dataclass(frozen=True)
class EventInput:
    acceptance_seq: int
    event_type: str
    unit_id: str
    attempt_id: str
    time: int
    step_id: Optional[str] = None
    device_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EventInput":
        if not isinstance(raw, Mapping):
            raise EventError("BAD_EVENT", f"事件必须是字典，得到 {type(raw).__name__}")
        seq = raw.get("acceptance_seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise EventError("BAD_ACCEPTANCE_SEQ", f"acceptance_seq 必须是整数，实际 {seq!r}")
        etype = str(raw.get("event_type", "")).upper()
        if etype not in (_START, _FINISH, _CANCEL):
            raise EventError(
                "BAD_EVENT_TYPE",
                f"event_type 必须是 START/FINISH/CANCEL，实际 {raw.get('event_type')!r}",
                acceptance_seq=seq,
            )
        for field_name in ("unit_id", "attempt_id"):
            value = raw.get(field_name)
            if not isinstance(value, str) or not value:
                raise EventError(
                    "BAD_EVENT",
                    f"{field_name} 必须是非空字符串，实际 {value!r}",
                    acceptance_seq=seq,
                )
        time = raw.get("time")
        if isinstance(time, bool) or not isinstance(time, int):
            raise EventError("BAD_TIME", f"time 必须是整数，实际 {time!r}", acceptance_seq=seq)
        step_id = raw.get("step_id")
        device_id = raw.get("device_id")
        if etype == _START:
            if not isinstance(step_id, str) or not step_id:
                raise EventError("BAD_EVENT", "START 必须提供 step_id", acceptance_seq=seq)
            if not isinstance(device_id, str) or not device_id:
                raise EventError("BAD_EVENT", "START 必须提供 device_id", acceptance_seq=seq)
        return cls(
            acceptance_seq=seq,
            event_type=etype,
            unit_id=str(raw["unit_id"]),
            attempt_id=str(raw["attempt_id"]),
            time=time,
            step_id=step_id if isinstance(step_id, str) else None,
            device_id=device_id if isinstance(device_id, str) else None,
        )

    def to_dict(self) -> dict:
        return {
            "acceptance_seq": self.acceptance_seq,
            "event_type": self.event_type,
            "unit_id": self.unit_id,
            "attempt_id": self.attempt_id,
            "time": self.time,
            "step_id": self.step_id,
            "device_id": self.device_id,
        }


# ============================================================ 持久化结构

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_products (
    route_id TEXT NOT NULL,
    version  TEXT NOT NULL,
    payload  TEXT NOT NULL,
    PRIMARY KEY (route_id, version)
);
CREATE TABLE IF NOT EXISTS exec_devices (
    device_id   TEXT PRIMARY KEY,
    device_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_journal (
    op_seq  INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_batches (
    batch_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    event_count   INTEGER NOT NULL,
    applied_count INTEGER NOT NULL,
    message       TEXT,
    payload       TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 以下为派生投影表
CREATE TABLE IF NOT EXISTS exec_units (
    unit_id          TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    current_state    TEXT NOT NULL,
    route_id         TEXT NOT NULL,
    version          TEXT NOT NULL,
    binding_seq      INTEGER NOT NULL,
    accepted_counted INTEGER NOT NULL,
    created_op_seq   INTEGER NOT NULL,
    first_event_time INTEGER,
    last_event_time  INTEGER,
    terminal_time    INTEGER
);
CREATE TABLE IF NOT EXISTS exec_bindings (
    binding_seq   INTEGER PRIMARY KEY,
    op_seq        INTEGER NOT NULL,
    unit_id       TEXT NOT NULL,
    route_id      TEXT NOT NULL,
    version       TEXT NOT NULL,
    state_at_bind TEXT NOT NULL,
    reason        TEXT NOT NULL,
    old_route_id  TEXT,
    old_version   TEXT,
    old_state     TEXT
);
CREATE TABLE IF NOT EXISTS exec_attempts (
    attempt_id        TEXT PRIMARY KEY,
    unit_id           TEXT NOT NULL,
    step_id           TEXT NOT NULL,
    device_id         TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    version           TEXT NOT NULL,
    from_state        TEXT NOT NULL,
    to_state          TEXT NOT NULL,
    status            TEXT NOT NULL,
    start_time        INTEGER NOT NULL,
    end_time          INTEGER,
    planned_end       INTEGER NOT NULL,
    standard_duration INTEGER NOT NULL,
    start_op_seq      INTEGER NOT NULL,
    end_op_seq        INTEGER,
    binding_seq       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_events (
    acceptance_seq INTEGER PRIMARY KEY,
    op_seq         INTEGER NOT NULL,
    event_type     TEXT NOT NULL,
    unit_id        TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    time           INTEGER NOT NULL,
    step_id        TEXT,
    device_id      TEXT,
    batch_id       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_unit ON exec_events(unit_id, acceptance_seq);
CREATE INDEX IF NOT EXISTS idx_attempts_unit ON exec_attempts(unit_id, start_op_seq);
CREATE INDEX IF NOT EXISTS idx_attempts_device ON exec_attempts(device_id, start_time);
CREATE INDEX IF NOT EXISTS idx_bindings_unit ON exec_bindings(unit_id, binding_seq);
""".strip()


class _Transaction:
    """可重入事务：外层 BEGIN IMMEDIATE，内层 SAVEPOINT。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.depth = 0

    def __enter__(self) -> sqlite3.Connection:
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


# ============================================================ 投影状态

@dataclass
class Binding:
    binding_seq: int
    op_seq: int
    unit_id: str
    route_id: str
    version: str
    state_at_bind: str
    reason: str
    old_route_id: Optional[str] = None
    old_version: Optional[str] = None
    old_state: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "binding_seq": self.binding_seq,
            "op_seq": self.op_seq,
            "unit_id": self.unit_id,
            "route_id": self.route_id,
            "version": self.version,
            "state_at_bind": self.state_at_bind,
            "reason": self.reason,
            "old_route_id": self.old_route_id,
            "old_version": self.old_version,
            "old_state": self.old_state,
        }


@dataclass
class UnitState:
    unit_id: str
    status: str
    current_state: str
    route_id: str
    version: str
    binding_seq: int
    accepted_counted: int
    created_op_seq: int
    first_event_time: Optional[int] = None
    last_event_time: Optional[int] = None
    terminal_time: Optional[int] = None


@dataclass
class Attempt:
    attempt_id: str
    unit_id: str
    step_id: str
    device_id: str
    route_id: str
    version: str
    from_state: str
    to_state: str
    status: str
    start_time: int
    planned_end: int
    standard_duration: int
    start_op_seq: int
    binding_seq: int
    end_time: Optional[int] = None
    end_op_seq: Optional[int] = None

    def occupancy(self) -> Optional[Tuple[int, int]]:
        if self.status == _DONE:
            return (self.start_time, int(self.end_time))
        if self.status == _IN_FLIGHT:
            # 在途按标准工时预留，避免同设备并发预订。
            return (self.start_time, self.planned_end)
        if self.status == _CANCELLED and self.end_time is not None:
            # 取消释放设备，但取消前实际占用区间仍作为历史事实参与重叠检查。
            return (self.start_time, int(self.end_time))
        return None

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "unit_id": self.unit_id,
            "step_id": self.step_id,
            "device_id": self.device_id,
            "route_id": self.route_id,
            "version": self.version,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "planned_end": self.planned_end,
            "standard_duration": self.standard_duration,
            "start_op_seq": self.start_op_seq,
            "end_op_seq": self.end_op_seq,
            "binding_seq": self.binding_seq,
        }


@dataclass
class Projection:
    units: Dict[str, UnitState] = field(default_factory=dict)
    attempts: Dict[str, Attempt] = field(default_factory=dict)
    bindings: Dict[str, List[Binding]] = field(default_factory=dict)
    events: Dict[int, dict] = field(default_factory=dict)
    next_binding_seq: int = 1
    next_op_seq: int = 1
    max_acceptance_seq: int = 0

    def clone(self) -> "Projection":
        return copy.deepcopy(self)

    def in_flight(self, unit_id: str) -> Optional[Attempt]:
        for a in self.attempts.values():
            if a.unit_id == unit_id and a.status == _IN_FLIGHT:
                return a
        return None

    def unit_attempts(self, unit_id: str) -> List[Attempt]:
        return sorted(
            (a for a in self.attempts.values() if a.unit_id == unit_id),
            key=lambda a: a.start_op_seq,
        )

    def signature(self) -> dict:
        return {
            "units": {k: vars(v) for k, v in sorted(self.units.items())},
            "attempts": {k: a.to_dict() for k, a in sorted(self.attempts.items())},
            "bindings": {
                k: [b.to_dict() for b in sorted(v, key=lambda x: x.binding_seq)]
                for k, v in sorted(self.bindings.items())
            },
            "events": {k: dict(v) for k, v in sorted(self.events.items())},
            "next_binding_seq": self.next_binding_seq,
            "next_op_seq": self.next_op_seq,
            "max_acceptance_seq": self.max_acceptance_seq,
        }


@dataclass
class BatchResult:
    batch_id: str
    status: str  # ACCEPTED / REJECTED
    event_count: int
    applied_count: int
    error: Optional[dict] = None
    op_seq_range: Optional[Tuple[int, int]] = None

    @property
    def ok(self) -> bool:
        return self.status == "ACCEPTED"

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "event_count": self.event_count,
            "applied_count": self.applied_count,
            "error": self.error,
            "op_seq_range": list(self.op_seq_range) if self.op_seq_range else None,
        }


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """半开区间 [start, end) 相交判定。空区间不相交。"""
    return a_start < b_end and b_start < a_end


def _status_from_kind(kind: RouteState) -> str:
    if kind is RouteState.ACCEPTED:
        return _ACCEPTED
    if kind is RouteState.SCRAP:
        return _SCRAP
    return _ACTIVE


# ============================== PART_ENGINE ==============================


def _coerce_product(product: Any) -> RouteDefinition:
    if isinstance(product, RouteDefinition):
        return product
    if isinstance(product, Mapping):
        try:
            return route_from_dict(product)
        except RouteError as exc:
            raise WarehouseError(f"外部产品定义非法: {exc}") from exc
    raise WarehouseError(f"产品必须是 RouteDefinition 或同格式字典，得到 {type(product).__name__}")


class Warehouse:
    """执行事件事实仓。线程内单连接使用，不做并发写入承诺。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=OFF")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._products: Dict[Tuple[str, str], dict] = {}
        self._devices: Dict[str, str] = {}
        self._load_config()
        self.proj = Projection()
        self._replay_journal()
        # 保证投影表与内存一致（重开时可能上次中断）。
        with _Transaction(self.conn):
            self._persist_projection(self.proj, self.conn)

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 配置加载
    def _load_config(self) -> None:
        self._products.clear()
        for r in self.conn.execute("SELECT route_id, version, payload FROM exec_products"):
            self._products[(r["route_id"], r["version"])] = json.loads(r["payload"])
        self._devices.clear()
        for r in self.conn.execute("SELECT device_id, device_type FROM exec_devices"):
            self._devices[r["device_id"]] = r["device_type"]

    def register_device(self, device_id: str, device_type: str) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise WarehouseError("device_id 必须是非空字符串")
        if not isinstance(device_type, str) or not device_type:
            raise WarehouseError("device_type 必须是非空字符串")
        with _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO exec_devices(device_id, device_type) VALUES(?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET device_type=excluded.device_type",
                (device_id, device_type),
            )
        self._devices[device_id] = device_type

    def register_products(self, products: Iterable[Any]) -> None:
        for p in products:
            self.register_product(p)

    def register_product(self, product: Any) -> Tuple[str, str]:
        """接受外部同格式产品。相同版本重复注册必须完全一致。"""
        route = _coerce_product(product)
        payload = route.to_dict()
        key = (route.route_id, route.version)
        existing = self._products.get(key)
        if existing is not None:
            if existing != payload:
                raise WarehouseError(
                    f"产品 {key[0]}@{key[1]} 已注册且内容不同；历史产品不可改写，请使用新版本"
                )
            return key
        with _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO exec_products(route_id, version, payload) VALUES(?, ?, ?)",
                (route.route_id, route.version, json.dumps(payload, ensure_ascii=False)),
            )
        self._products[key] = payload
        return key

    def get_product(self, route_id: str, version: str) -> RouteDefinition:
        try:
            payload = self._products[(route_id, version)]
        except KeyError as exc:
            raise WarehouseError(f"执行仓未注册产品 {route_id}@{version}") from exc
        return route_from_dict(payload)

    def list_products(self) -> List[dict]:
        return [
            {"route_id": rid, "version": ver, "payload": payload}
            for (rid, ver), payload in sorted(self._products.items())
        ]

    def list_devices(self) -> List[dict]:
        return [
            {"device_id": d, "device_type": t} for d, t in sorted(self._devices.items())
        ]

    def device_type(self, device_id: str) -> Optional[str]:
        return self._devices.get(device_id)

    # ------------------------------------------------------------ 创建物料
    def create_unit(
        self,
        unit_id: str,
        product: Any = None,
        *,
        route_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> dict:
        if not isinstance(unit_id, str) or not unit_id:
            raise WarehouseError("unit_id 必须是非空字符串")
        if unit_id in self.proj.units:
            raise WarehouseError(f"物料 {unit_id} 已存在")
        if len(self.proj.units) >= MAX_UNITS:
            raise WarehouseError(f"物料数超过上限 {MAX_UNITS}")
        if product is not None:
            rid, ver = self.register_product(product)
        else:
            if not route_id or not version:
                raise WarehouseError("必须提供 product 或 route_id+version")
            rid, ver = route_id, version
            if (rid, ver) not in self._products:
                raise WarehouseError(f"执行仓未注册产品 {rid}@{ver}")
        route = self.get_product(rid, ver)
        op = {
            "kind": "UNIT_CREATE",
            "payload": {
                "unit_id": unit_id,
                "route_id": rid,
                "version": ver,
                "reason": "create",
            },
        }
        return self._commit_ops([op], actor="create_unit")

    # ------------------------------------------------------------ 显式迁移
    def migrate_unit(
        self,
        unit_id: str,
        mapping: Mapping[str, str],
        *,
        product: Any = None,
        route_id: Optional[str] = None,
        version: Optional[str] = None,
        reason: str = "explicit migration",
    ) -> dict:
        unit = self.proj.units.get(unit_id)
        if unit is None:
            raise WarehouseError(f"未知物料 {unit_id}")
        if not isinstance(mapping, Mapping) or not mapping:
            raise WarehouseError("mapping 必须是非空映射 old_state -> new_state")
        if product is not None:
            rid, ver = self.register_product(product)
        else:
            if not route_id or not version:
                raise WarehouseError("必须提供 product 或 route_id+version")
            rid, ver = route_id, version
        if (rid, ver) not in self._products:
            raise WarehouseError(f"执行仓未注册产品 {rid}@{ver}")
        op = {
            "kind": "MIGRATE",
            "payload": {
                "unit_id": unit_id,
                "new_route_id": rid,
                "new_version": ver,
                "mapping": {str(k): str(v) for k, v in mapping.items()},
                "reason": reason,
            },
        }
        return self._commit_ops([op], actor="migrate_unit")

    # ------------------------------------------------------------ 事件导入
    def import_events(self, events: Sequence[Mapping[str, Any]], batch_id: Optional[str] = None) -> BatchResult:
        """原子导入一批事件，按 acceptance_seq 严格递增顺序执行。

        输入列表本身可以乱序（例如按业务时刻排过序）；仓库只会按每条事件
        给定的 acceptance_seq 排序执行，绝不按业务时刻 time 重排，因此迟到
        矛盾不会被掩盖。acceptance_seq 全批唯一且都必须大于历史最大值。
        非法整批不改动业务投影，返回 REJECTED 结果并附原因/定位。
        """
        batch = batch_id or f"batch-{uuid.uuid4().hex[:12]}"
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
            raise WarehouseError("events 必须是事件字典序列")
        tagged: List[Tuple[int, EventInput]] = []
        try:
            for i, raw in enumerate(events):
                try:
                    tagged.append((i, EventInput.from_mapping(raw)))
                except EventError as exc:
                    exc.index = i
                    raise
            if not tagged:
                raise EventError("EMPTY_BATCH", "事件批不能为空", index=0)
            seen: Dict[int, int] = {}
            for i, ev in tagged:
                if ev.acceptance_seq in seen:
                    raise EventError(
                        "DUPLICATE_SEQ",
                        f"受理序号 {ev.acceptance_seq} 在本批第 {seen[ev.acceptance_seq]} 与第 {i} 条重复",
                        index=i,
                        acceptance_seq=ev.acceptance_seq,
                    )
                seen[ev.acceptance_seq] = i
            tagged.sort(key=lambda pair: pair[1].acceptance_seq)
            parsed = [ev for _, ev in tagged]
            first_seq = parsed[0].acceptance_seq
            if first_seq <= self.proj.max_acceptance_seq:
                raise EventError(
                    "ACCEPTANCE_ORDER",
                    f"受理序号 {first_seq} 未大于已受理最大值 {self.proj.max_acceptance_seq}",
                    index=tagged[0][0],
                    acceptance_seq=first_seq,
                )
            if len(self.proj.events) + len(parsed) > MAX_EVENTS:
                raise EventError(
                    "EVENT_LIMIT",
                    f"事件总数将超过上限 {MAX_EVENTS}",
                    index=0,
                    acceptance_seq=first_seq,
                )
        except EventError as exc:
            return self._record_rejection(batch, events, exc)

        # 克隆后逐条应用，任一条失败则整批丢弃。
        trial = self.proj.clone()
        try:
            for orig_index, ev in tagged:
                try:
                    self._apply_event(trial, ev, batch_id=batch)
                except EventError as exc:
                    exc.index = orig_index
                    raise
        except EventError as exc:
            return self._record_rejection(batch, events, exc)

        ops = [
            {
                "kind": "EVENT",
                "payload": {"event": ev.to_dict(), "batch_id": batch},
            }
            for ev in parsed
        ]
        # trial 仅用于预验证；提交时基于当前真实投影重新应用，保证事件只生效一次。
        result = self._commit_ops(ops, actor="import_events", batch_id=batch)
        seqs = result["op_seqs"]
        return BatchResult(
            batch_id=batch,
            status="ACCEPTED",
            event_count=len(parsed),
            applied_count=len(parsed),
            op_seq_range=(seqs[0], seqs[-1]),
        )

    def _record_rejection(
        self, batch: str, events: Sequence[Mapping[str, Any]], exc: EventError
    ) -> BatchResult:
        error = exc.to_dict()
        with _Transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO exec_batches"
                "(batch_id, status, event_count, applied_count, message, payload) "
                "VALUES(?, 'REJECTED', ?, 0, ?, ?)",
                (
                    batch,
                    len(events),
                    exc.message,
                    json.dumps({"error": error, "events": list(events)}, ensure_ascii=False, default=str),
                ),
            )
        return BatchResult(
            batch_id=batch,
            status="REJECTED",
            event_count=len(events),
            applied_count=0,
            error=error,
        )

    def get_batch(self, batch_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM exec_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not r:
            return None
        out = dict(r)
        if out.get("payload"):
            out["payload"] = json.loads(out["payload"])
        return out

    def list_batches(self) -> List[dict]:
        rows = self.conn.execute("SELECT batch_id, status, event_count, applied_count, message FROM exec_batches ORDER BY rowid")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 应用逻辑
    def _apply_event(self, proj: Projection, ev: EventInput, batch_id: str) -> None:
        if ev.acceptance_seq in proj.events:
            raise EventError("DUPLICATE_SEQ", f"受理序号 {ev.acceptance_seq} 已存在", acceptance_seq=ev.acceptance_seq)
        if ev.acceptance_seq <= proj.max_acceptance_seq:
            raise EventError(
                "ACCEPTANCE_ORDER",
                f"受理序号 {ev.acceptance_seq} 未大于当前最大值 {proj.max_acceptance_seq}",
                acceptance_seq=ev.acceptance_seq,
            )
        if ev.event_type == _START:
            self._apply_start(proj, ev)
        elif ev.event_type == _FINISH:
            self._apply_finish(proj, ev)
        else:
            self._apply_cancel(proj, ev)
        proj.max_acceptance_seq = ev.acceptance_seq
        proj.events[ev.acceptance_seq] = {
            "acceptance_seq": ev.acceptance_seq,
            "op_seq": proj.next_op_seq,
            "event_type": ev.event_type,
            "unit_id": ev.unit_id,
            "attempt_id": ev.attempt_id,
            "time": ev.time,
            "step_id": ev.step_id,
            "device_id": ev.device_id,
            "batch_id": batch_id,
        }

    def _unit_or_error(self, proj: Projection, unit_id: str, ev: EventInput) -> UnitState:
        unit = proj.units.get(unit_id)
        if unit is None:
            raise EventError(
                "UNKNOWN_UNIT",
                f"未知物料 {unit_id}；请先创建物料并绑定产品",
                acceptance_seq=ev.acceptance_seq,
            )
        return unit

    def _device_occupancies(
        self, proj: Projection, device_id: str, *, exclude_attempt: Optional[str] = None
    ) -> List[Tuple[Attempt, int, int]]:
        out = []
        for a in proj.attempts.values():
            if a.device_id != device_id or a.attempt_id == exclude_attempt:
                continue
            occ = a.occupancy()
            if occ is None:
                continue
            out.append((a, occ[0], occ[1]))
        out.sort(key=lambda x: (x[1], x[2], x[0].attempt_id))
        return out

    def _apply_start(self, proj: Projection, ev: EventInput) -> None:
        assert ev.step_id and ev.device_id
        unit = self._unit_or_error(proj, ev.unit_id, ev)
        if ev.attempt_id in proj.attempts:
            raise EventError(
                "DUPLICATE_ATTEMPT",
                f"attempt_id {ev.attempt_id} 已存在",
                acceptance_seq=ev.acceptance_seq,
            )
        if unit.status != _ACTIVE:
            raise EventError(
                "UNIT_TERMINAL",
                f"物料 {unit.unit_id} 已处于 {unit.current_state}（{unit.status}），"
                f"终态不允许新工步",
                acceptance_seq=ev.acceptance_seq,
            )
        busy = proj.in_flight(unit.unit_id)
        if busy is not None:
            raise EventError(
                "UNIT_IN_FLIGHT",
                f"物料 {unit.unit_id} 已有在途尝试 {busy.attempt_id}"
                f"（step {busy.step_id}，开始于 {busy.start_time}）",
                acceptance_seq=ev.acceptance_seq,
                details={"attempt_id": busy.attempt_id, "step_id": busy.step_id},
            )
        route = self.get_product(unit.route_id, unit.version)
        try:
            step = route.get_step(ev.step_id)
        except RouteError as exc:
            raise EventError(
                "STEP_NOT_IN_BOUND_PRODUCT",
                f"物料绑定产品 {unit.route_id}@{unit.version} 中没有 step {ev.step_id}"
                f"（迁移后旧工步按旧产品解释，但新绑定产品必须包含该 step）: {exc}",
                acceptance_seq=ev.acceptance_seq,
                details={"route_id": unit.route_id, "version": unit.version, "step_id": ev.step_id},
            )
        if step.from_state != unit.current_state:
            raise EventError(
                "STATE_MISMATCH",
                f"物料当前状态 {unit.current_state}，但 step {ev.step_id} 要求起点 {step.from_state}",
                acceptance_seq=ev.acceptance_seq,
                details={"current_state": unit.current_state, "required_from": step.from_state},
            )
        dev_type = self._devices.get(ev.device_id)
        if dev_type is None:
            raise EventError(
                "UNKNOWN_DEVICE",
                f"设备 {ev.device_id} 未登记",
                acceptance_seq=ev.acceptance_seq,
            )
        if dev_type != step.device_type:
            raise EventError(
                "DEVICE_TYPE_MISMATCH",
                f"设备 {ev.device_id} 是 {dev_type}，step {ev.step_id} 需要 {step.device_type}",
                acceptance_seq=ev.acceptance_seq,
                details={"device_type": dev_type, "required_type": step.device_type},
            )
        new_start = ev.time
        new_end = ev.time + step.duration
        if step.duration > 0:
            for other, o_start, o_end in self._device_occupancies(proj, ev.device_id):
                if _overlaps(new_start, new_end, o_start, o_end):
                    raise EventError(
                        "DEVICE_BUSY",
                        f"设备 {ev.device_id} 在 [{new_start}, {new_end}) 被尝试 {other.attempt_id} "
                        f"[{o_start}, {o_end}) 占用（{other.status}）",
                        acceptance_seq=ev.acceptance_seq,
                        details={
                            "device_id": ev.device_id,
                            "occupying_attempt": other.attempt_id,
                            "occupying_status": other.status,
                            "occupying_interval": [o_start, o_end],
                            "requested_interval": [new_start, new_end],
                        },
                    )
        attempt = Attempt(
            attempt_id=ev.attempt_id,
            unit_id=unit.unit_id,
            step_id=ev.step_id,
            device_id=ev.device_id,
            route_id=unit.route_id,
            version=unit.version,
            from_state=step.from_state,
            to_state=step.to_state,
            status=_IN_FLIGHT,
            start_time=ev.time,
            planned_end=new_end,
            standard_duration=step.duration,
            start_op_seq=proj.next_op_seq,
            binding_seq=unit.binding_seq,
        )
        proj.attempts[attempt.attempt_id] = attempt
        self._touch_event_time(unit, ev.time)

    def _apply_finish(self, proj: Projection, ev: EventInput) -> None:
        attempt = proj.attempts.get(ev.attempt_id)
        if attempt is None:
            raise EventError(
                "UNKNOWN_ATTEMPT",
                f"未知尝试 {ev.attempt_id}（FINISH 无法提交）",
                acceptance_seq=ev.acceptance_seq,
            )
        if attempt.unit_id != ev.unit_id:
            raise EventError(
                "ATTEMPT_UNIT_MISMATCH",
                f"尝试 {ev.attempt_id} 属于物料 {attempt.unit_id}，不是 {ev.unit_id}",
                acceptance_seq=ev.acceptance_seq,
            )
        if attempt.status != _IN_FLIGHT:
            raise EventError(
                "ATTEMPT_NOT_IN_FLIGHT",
                f"尝试 {ev.attempt_id} 状态为 {attempt.status}，无法 FINISH",
                acceptance_seq=ev.acceptance_seq,
                details={"status": attempt.status},
            )
        if ev.time < attempt.start_time:
            raise EventError(
                "FINISH_BEFORE_START",
                f"尝试 {ev.attempt_id} 的 FINISH 时刻 {ev.time} 早于 START {attempt.start_time}",
                acceptance_seq=ev.acceptance_seq,
                details={"start_time": attempt.start_time, "finish_time": ev.time},
            )
        unit = self._unit_or_error(proj, attempt.unit_id, ev)
        if unit.status != _ACTIVE:
            raise EventError(
                "UNIT_TERMINAL",
                f"物料 {unit.unit_id} 已处于终态 {unit.current_state}，但仍有尝试需要 FINISH",
                acceptance_seq=ev.acceptance_seq,
            )
        if unit.binding_seq != attempt.binding_seq:
            raise EventError(
                "BINDING_CHANGED",
                f"尝试 {attempt.attempt_id} 属于绑定 #{attempt.binding_seq}，"
                f"物料当前绑定 #{unit.binding_seq}；迁移前不得改变在途尝试",
                acceptance_seq=ev.acceptance_seq,
            )
        # 用尝试自己的产品解释该步（旧产品），与迁移后的新绑定无关。
        route = self.get_product(attempt.route_id, attempt.version)
        try:
            step = route.get_step(attempt.step_id)
        except RouteError as exc:
            raise EventError(
                "STEP_NOT_IN_BOUND_PRODUCT",
                f"尝试 {attempt.attempt_id} 的产品 {attempt.route_id}@{attempt.version} "
                f"缺少 step {attempt.step_id}: {exc}",
                acceptance_seq=ev.acceptance_seq,
            )
        if step.from_state != attempt.from_state or step.to_state != attempt.to_state:
            raise EventError(
                "PRODUCT_SNAPSHOT_INCONSISTENT",
                f"尝试 {attempt.attempt_id} 的产品快照与记录的状态不一致",
                acceptance_seq=ev.acceptance_seq,
            )
        if unit.current_state != attempt.from_state:
            raise EventError(
                "STATE_MISMATCH",
                f"物料当前状态 {unit.current_state} 与尝试起点 {attempt.from_state} 不一致",
                acceptance_seq=ev.acceptance_seq,
            )
        # 复核实际区间 [start, end) 不与同设备其它占用重叠。
        if ev.time > attempt.start_time:
            for other, o_start, o_end in self._device_occupancies(
                proj, attempt.device_id, exclude_attempt=attempt.attempt_id
            ):
                if _overlaps(attempt.start_time, ev.time, o_start, o_end):
                    raise EventError(
                        "DEVICE_OVERLAP_ON_FINISH",
                        f"尝试 {attempt.attempt_id} 实际区间 [{attempt.start_time}, {ev.time}) "
                        f"与设备 {attempt.device_id} 上尝试 {other.attempt_id} [{o_start}, {o_end}) 重叠",
                        acceptance_seq=ev.acceptance_seq,
                        details={
                            "device_id": attempt.device_id,
                            "occupying_attempt": other.attempt_id,
                            "occupying_interval": [o_start, o_end],
                            "actual_interval": [attempt.start_time, ev.time],
                        },
                    )
        attempt.status = _DONE
        attempt.end_time = ev.time
        attempt.end_op_seq = proj.next_op_seq
        unit.current_state = attempt.to_state
        new_status = _status_from_kind(route.states[attempt.to_state])
        unit.status = new_status
        if new_status == _ACCEPTED:
            # 按“件”计：首次进入合格终态才 +1，返工回流不重复。
            if not unit.accepted_counted:
                unit.accepted_counted = 1
            unit.terminal_time = ev.time
        elif new_status == _SCRAP:
            unit.terminal_time = ev.time
        self._touch_event_time(unit, ev.time)

    def _apply_cancel(self, proj: Projection, ev: EventInput) -> None:
        attempt = proj.attempts.get(ev.attempt_id)
        if attempt is None:
            raise EventError(
                "UNKNOWN_ATTEMPT",
                f"未知尝试 {ev.attempt_id}（CANCEL 无法释放）",
                acceptance_seq=ev.acceptance_seq,
            )
        if attempt.unit_id != ev.unit_id:
            raise EventError(
                "ATTEMPT_UNIT_MISMATCH",
                f"尝试 {ev.attempt_id} 属于物料 {attempt.unit_id}，不是 {ev.unit_id}",
                acceptance_seq=ev.acceptance_seq,
            )
        if attempt.status != _IN_FLIGHT:
            raise EventError(
                "ATTEMPT_NOT_IN_FLIGHT",
                f"尝试 {ev.attempt_id} 状态为 {attempt.status}，无法 CANCEL",
                acceptance_seq=ev.acceptance_seq,
            )
        attempt.status = _CANCELLED
        attempt.end_time = ev.time
        attempt.end_op_seq = proj.next_op_seq
        # 不移动物料状态，仅释放设备与在途占位。
        unit = proj.units[attempt.unit_id]
        self._touch_event_time(unit, ev.time)

    @staticmethod
    def _touch_event_time(unit: UnitState, t: int) -> None:
        if unit.first_event_time is None or t < unit.first_event_time:
            unit.first_event_time = t
        if unit.last_event_time is None or t > unit.last_event_time:
            unit.last_event_time = t

    # ------------------------------------------------------------ 提交/持久化
    def _commit_ops(
        self,
        ops: Sequence[dict],
        *,
        actor: str,
        proj: Optional[Projection] = None,
        batch_id: Optional[str] = None,
    ) -> dict:
        working = (proj or self.proj).clone()
        assigned = []
        for op in ops:
            payload = dict(op.get("payload", {}))
            new_op = {"kind": op["kind"], "payload": payload}
            new_op["op_seq"] = working.next_op_seq
            assigned.append(new_op)
            self._apply_op(working, new_op)
        with _Transaction(self.conn):
            for new_op in assigned:
                self.conn.execute(
                    "INSERT INTO exec_journal(op_seq, kind, payload) VALUES(?, ?, ?)",
                    (new_op["op_seq"], new_op["kind"], json.dumps(new_op, ensure_ascii=False)),
                )
            self._persist_projection(working, self.conn)
            if batch_id is not None:
                self.conn.execute(
                    "INSERT OR REPLACE INTO exec_batches"
                    "(batch_id, status, event_count, applied_count, message) "
                    "VALUES(?, 'ACCEPTED', ?, ?, ?)",
                    (batch_id, len(ops), len(ops), None),
                )
        self.proj = working
        return {
            "actor": actor,
            "op_seqs": [op["op_seq"] for op in assigned],
            "units": len(working.units),
            "attempts": len(working.attempts),
            "events": len(working.events),
        }

    def _apply_op(self, proj: Projection, op: dict) -> None:
        kind = op["kind"]
        payload = op["payload"]
        if kind == "UNIT_CREATE":
            self._apply_create(proj, payload, op["op_seq"])
        elif kind == "MIGRATE":
            self._apply_migrate(proj, payload, op["op_seq"])
        elif kind == "EVENT":
            ev = EventInput.from_mapping(payload["event"])
            self._apply_event(proj, ev, payload.get("batch_id", "replay"))
            proj.next_op_seq = op["op_seq"] + 1
            return
        else:
            raise WarehouseError(f"未知日志操作 {kind}")
        proj.next_op_seq = op["op_seq"] + 1

    def _apply_create(self, proj: Projection, payload: dict, op_seq: int) -> None:
        unit_id = payload["unit_id"]
        rid, ver = payload["route_id"], payload["version"]
        if unit_id in proj.units:
            raise EventError("DUPLICATE_UNIT", f"物料 {unit_id} 已存在")
        route = self.get_product(rid, ver)
        binding = Binding(
            binding_seq=proj.next_binding_seq,
            op_seq=op_seq,
            unit_id=unit_id,
            route_id=rid,
            version=ver,
            state_at_bind=route.initial_state,
            reason=payload.get("reason", "create"),
        )
        proj.next_binding_seq += 1
        proj.units[unit_id] = UnitState(
            unit_id=unit_id,
            status=_ACTIVE,
            current_state=route.initial_state,
            route_id=rid,
            version=ver,
            binding_seq=binding.binding_seq,
            accepted_counted=0,
            created_op_seq=op_seq,
        )
        proj.bindings.setdefault(unit_id, []).append(binding)

    def _apply_migrate(self, proj: Projection, payload: dict, op_seq: int) -> None:
        unit_id = payload["unit_id"]
        unit = proj.units.get(unit_id)
        if unit is None:
            raise EventError("UNKNOWN_UNIT", f"未知物料 {unit_id}")
        busy = proj.in_flight(unit_id)
        if busy is not None:
            raise EventError(
                "UNIT_IN_FLIGHT",
                f"物料 {unit_id} 有在途尝试 {busy.attempt_id}，不允许迁移",
            )
        mapping = payload["mapping"]
        if unit.current_state not in mapping:
            raise EventError(
                "MIGRATION_NOT_MAPPED",
                f"物料当前状态 {unit.current_state} 不在迁移映射 {sorted(mapping)} 中",
                details={"current_state": unit.current_state, "mapping": mapping},
            )
        rid, ver = payload["new_route_id"], payload["new_version"]
        route = self.get_product(rid, ver)
        new_state = mapping[unit.current_state]
        if new_state not in route.states:
            raise EventError(
                "MIGRATION_TARGET_MISSING",
                f"迁移目标状态 {new_state} 不存在于产品 {rid}@{ver}",
                details={"new_state": new_state, "states": sorted(route.states)},
            )
        old = Binding(
            binding_seq=proj.next_binding_seq,
            op_seq=op_seq,
            unit_id=unit_id,
            route_id=rid,
            version=ver,
            state_at_bind=new_state,
            reason=payload.get("reason", "explicit migration"),
            old_route_id=unit.route_id,
            old_version=unit.version,
            old_state=unit.current_state,
        )
        proj.next_binding_seq += 1
        unit.route_id = rid
        unit.version = ver
        unit.current_state = new_state
        unit.binding_seq = old.binding_seq
        unit.status = _status_from_kind(route.states[new_state])
        if unit.status == _ACCEPTED and not unit.accepted_counted:
            unit.accepted_counted = 1
        proj.bindings.setdefault(unit_id, []).append(old)

    # ------------------------------------------------------------ 投影持久化
    def _persist_projection(self, proj: Projection, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM exec_units")
        conn.execute("DELETE FROM exec_bindings")
        conn.execute("DELETE FROM exec_attempts")
        conn.execute("DELETE FROM exec_events")
        conn.executemany(
            "INSERT INTO exec_units(unit_id, status, current_state, route_id, version, "
            "binding_seq, accepted_counted, created_op_seq, first_event_time, last_event_time, terminal_time) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (u.unit_id, u.status, u.current_state, u.route_id, u.version, u.binding_seq,
                 u.accepted_counted, u.created_op_seq, u.first_event_time, u.last_event_time,
                 u.terminal_time)
                for u in proj.units.values()
            ],
        )
        conn.executemany(
            "INSERT INTO exec_bindings(binding_seq, op_seq, unit_id, route_id, version, "
            "state_at_bind, reason, old_route_id, old_version, old_state) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (b.binding_seq, b.op_seq, b.unit_id, b.route_id, b.version, b.state_at_bind,
                 b.reason, b.old_route_id, b.old_version, b.old_state)
                for lst in proj.bindings.values() for b in lst
            ],
        )
        conn.executemany(
            "INSERT INTO exec_attempts(attempt_id, unit_id, step_id, device_id, route_id, version, "
            "from_state, to_state, status, start_time, end_time, planned_end, standard_duration, "
            "start_op_seq, end_op_seq, binding_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (a.attempt_id, a.unit_id, a.step_id, a.device_id, a.route_id, a.version,
                 a.from_state, a.to_state, a.status, a.start_time, a.end_time, a.planned_end,
                 a.standard_duration, a.start_op_seq, a.end_op_seq, a.binding_seq)
                for a in proj.attempts.values()
            ],
        )
        conn.executemany(
            "INSERT INTO exec_events(acceptance_seq, op_seq, event_type, unit_id, attempt_id, "
            "time, step_id, device_id, batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (e["acceptance_seq"], e["op_seq"], e["event_type"], e["unit_id"], e["attempt_id"],
                 e["time"], e["step_id"], e["device_id"], e["batch_id"])
                for e in proj.events.values()
            ],
        )

    def _replay_journal(self) -> None:
        proj = Projection()
        rows = list(self.conn.execute("SELECT op_seq, kind, payload FROM exec_journal ORDER BY op_seq"))
        for r in rows:
            op = json.loads(r["payload"])
            op["op_seq"] = r["op_seq"]
            self._apply_op(proj, op)
            proj.next_op_seq = max(proj.next_op_seq, r["op_seq"] + 1)
        self.proj = proj

    # ============================================================ 查询
    def get_unit(self, unit_id: str) -> dict:
        unit = self.proj.units.get(unit_id)
        if unit is None:
            raise WarehouseError(f"未知物料 {unit_id}")
        route = self.get_product(unit.route_id, unit.version)
        in_flight = self.proj.in_flight(unit_id)
        remaining = None
        if unit.status == _ACTIVE:
            remaining = route.shortest_remaining(unit.current_state)
        attempts = self.proj.unit_attempts(unit_id)
        return {
            "unit_id": unit.unit_id,
            "status": unit.status,
            "current_state": unit.current_state,
            "state_kind": route.states[unit.current_state].value,
            "route_id": unit.route_id,
            "version": unit.version,
            "binding_seq": unit.binding_seq,
            "accepted_counted": bool(unit.accepted_counted),
            "in_flight_attempt": in_flight.attempt_id if in_flight else None,
            "expected_remaining_duration": remaining,
            "completable": remaining is not None if unit.status == _ACTIVE else unit.status == _ACCEPTED,
            "first_event_time": unit.first_event_time,
            "last_event_time": unit.last_event_time,
            "terminal_time": unit.terminal_time,
            "attempt_count": len(attempts),
        }

    def list_units(self) -> List[dict]:
        return [self.get_unit(uid) for uid in sorted(self.proj.units)]

    def unit_position(self, unit_id: str) -> dict:
        """物料当前位置：状态、绑定产品、设备上是否在途、预计剩余标准工时。

        ``expected_remaining_duration`` 是从“已提交状态”出发的最短剩余工时，
        与路线目录的语义一致。若存在在途尝试，额外给出 ``remaining_after_in_flight``
        （从该尝试完成后状态到合格终态的最短工时），便于预计含当前工序的完工。
        """
        unit = self.proj.units.get(unit_id)
        if unit is None:
            raise WarehouseError(f"未知物料 {unit_id}")
        info = self.get_unit(unit_id)
        in_flight = self.proj.in_flight(unit_id)
        after = None
        if in_flight is not None:
            route = self.get_product(in_flight.route_id, in_flight.version)
            after = route.shortest_remaining(in_flight.to_state)
        return {
            "unit_id": unit_id,
            "status": info["status"],
            "current_state": info["current_state"],
            "route_id": info["route_id"],
            "version": info["version"],
            "on_device": in_flight.device_id if in_flight else None,
            "in_flight_attempt": in_flight.attempt_id if in_flight else None,
            "in_flight_step": in_flight.step_id if in_flight else None,
            "in_flight_target_state": in_flight.to_state if in_flight else None,
            "in_flight_planned_end": in_flight.planned_end if in_flight else None,
            "expected_remaining_duration": info["expected_remaining_duration"],
            "remaining_after_in_flight": after,
        }

    def unit_attempts(self, unit_id: str) -> List[dict]:
        if unit_id not in self.proj.units:
            raise WarehouseError(f"未知物料 {unit_id}")
        return [a.to_dict() for a in self.proj.unit_attempts(unit_id)]

    def unit_bindings(self, unit_id: str) -> List[dict]:
        if unit_id not in self.proj.units:
            raise WarehouseError(f"未知物料 {unit_id}")
        return [b.to_dict() for b in self.proj.bindings.get(unit_id, [])]

    # ------------------------------------------------------------ 设备事实
    def device_busy(self, device_id: str, at_time: Optional[int] = None) -> Optional[dict]:
        """返回设备在 at_time 时正在加工的尝试；空闲返回 None。

        at_time 为 None 时取“当前时刻” = 已受理事件的最大业务时刻。
        """
        if device_id not in self._devices:
            raise WarehouseError(f"未知设备 {device_id}")
        if at_time is None:
            at_time = self._logical_now()
        for attempt, start, end in self._device_occupancies(self.proj, device_id):
            if start <= at_time < end:
                return {
                    "device_id": device_id,
                    "device_type": self._devices[device_id],
                    "at_time": at_time,
                    "attempt_id": attempt.attempt_id,
                    "unit_id": attempt.unit_id,
                    "step_id": attempt.step_id,
                    "status": attempt.status,
                    "interval": [start, end],
                }
        return None

    def device_timeline(self, device_id: str) -> List[dict]:
        """设备忙闲事实：所有已开始尝试的占用区间（含在途预留）。"""
        if device_id not in self._devices:
            raise WarehouseError(f"未知设备 {device_id}")
        out = []
        for attempt, start, end in self._device_occupancies(self.proj, device_id):
            out.append(
                {
                    "device_id": device_id,
                    "attempt_id": attempt.attempt_id,
                    "unit_id": attempt.unit_id,
                    "step_id": attempt.step_id,
                    "status": attempt.status,
                    "interval": [start, end],
                    "actual": [attempt.start_time, attempt.end_time],
                }
            )
        return out

    def device_fact(self, device_id: str) -> dict:
        return {
            "device_id": device_id,
            "device_type": self._devices.get(device_id),
            "busy_now": self.device_busy(device_id),
            "timeline": self.device_timeline(device_id),
        }

    def _logical_now(self) -> int:
        times = [e["time"] for e in self.proj.events.values()]
        return max(times) if times else 0

    # ------------------------------------------------------------ 工时与产出
    def duration_facts(self, unit_id: Optional[str] = None) -> dict:
        """实际加工时长与实际等待时长。

        加工时长 = 已完成尝试的 (end-start) 之和。
        等待时长 = 相邻两次尝试之间，上一尝试结束到下一尝试开始的正间隔之和；
                   只统计已完工尝试。对在途尝试，从上一结束到其在途开始计等待。
        """
        def wait_and_work(attempts: List[Attempt]) -> Tuple[int, int, int]:
            work = 0
            wait = 0
            prev_end: Optional[int] = None
            # 业务时长按业务时刻（start_time）排序，而非受理序号。
            for a in sorted(attempts, key=lambda x: (x.start_time, x.start_op_seq)):
                if prev_end is not None and a.start_time > prev_end:
                    wait += a.start_time - prev_end
                if a.status == _DONE:
                    work += int(a.end_time) - a.start_time
                    prev_end = int(a.end_time)
                elif a.status == _IN_FLIGHT:
                    prev_end = a.start_time
            return work, wait, len(attempts)

        if unit_id is not None:
            if unit_id not in self.proj.units:
                raise WarehouseError(f"未知物料 {unit_id}")
            work, wait, n = wait_and_work(self.proj.unit_attempts(unit_id))
            return {
                "unit_id": unit_id,
                "attempt_count": n,
                "actual_work_duration": work,
                "actual_wait_duration": wait,
            }
        total_work = total_wait = total_attempts = 0
        per_unit = {}
        for uid in sorted(self.proj.units):
            work, wait, n = wait_and_work(self.proj.unit_attempts(uid))
            per_unit[uid] = {
                "attempt_count": n,
                "actual_work_duration": work,
                "actual_wait_duration": wait,
            }
            total_work += work
            total_wait += wait
            total_attempts += n
        return {
            "attempt_count": total_attempts,
            "actual_work_duration": total_work,
            "actual_wait_duration": total_wait,
            "units": per_unit,
        }

    def accepted_units(self) -> List[str]:
        return sorted(uid for uid, u in self.proj.units.items() if u.status == _ACCEPTED)

    def accepted_count(self) -> int:
        """合格完工件数：每件物料最多计一次，返工回流不重复。"""
        return sum(1 for u in self.proj.units.values() if u.accepted_counted)

    def scrap_count(self) -> int:
        return sum(1 for u in self.proj.units.values() if u.status == _SCRAP)

    def summary(self) -> dict:
        return {
            "units": len(self.proj.units),
            "active": sum(1 for u in self.proj.units.values() if u.status == _ACTIVE),
            "accepted": self.accepted_count(),
            "scrap": self.scrap_count(),
            "attempts": len(self.proj.attempts),
            "events": len(self.proj.events),
            "max_acceptance_seq": self.proj.max_acceptance_seq,
            "work_and_wait": self.duration_facts(),
        }

    # ------------------------------------------------------------ 解释入口
    def explain_start(
        self,
        unit_id: str,
        step_id: str,
        device_id: str,
        time: int,
    ) -> dict:
        """解释某次 START 是否允许，以及不允许的具体原因。

        不改变任何状态；返回 allowed/reasons/occupying_attempt 等诊断。
        """
        reasons: List[dict] = []
        blocking: Optional[dict] = None
        unit = self.proj.units.get(unit_id)
        if unit is None:
            return {
                "allowed": False,
                "unit_id": unit_id,
                "step_id": step_id,
                "device_id": device_id,
                "time": time,
                "reasons": [{"code": "UNKNOWN_UNIT", "message": f"未知物料 {unit_id}"}],
                "occupying_attempt": None,
                "current": None,
            }
        current = self.get_unit(unit_id)
        if unit.status != _ACTIVE:
            reasons.append(
                {
                    "code": "UNIT_TERMINAL",
                    "message": f"物料已处于 {unit.current_state}（{unit.status}），终态不再允许新工步",
                }
            )
        busy = self.proj.in_flight(unit_id)
        if busy is not None:
            reasons.append(
                {
                    "code": "UNIT_IN_FLIGHT",
                    "message": f"物料已有在途尝试 {busy.attempt_id} 占用设备 {busy.device_id}",
                    "attempt_id": busy.attempt_id,
                }
            )
        route = self.get_product(unit.route_id, unit.version)
        step = None
        try:
            step = route.get_step(step_id)
        except RouteError as exc:
            reasons.append(
                {
                    "code": "STEP_NOT_IN_BOUND_PRODUCT",
                    "message": str(exc),
                }
            )
        if step is not None and step.from_state != unit.current_state:
            reasons.append(
                {
                    "code": "STATE_MISMATCH",
                    "message": f"step {step_id} 起点 {step.from_state}，物料当前 {unit.current_state}；"
                    f"迁移后旧工步需按旧产品解释",
                }
            )
        dev_type = self._devices.get(device_id)
        if dev_type is None:
            reasons.append({"code": "UNKNOWN_DEVICE", "message": f"设备 {device_id} 未登记"})
        elif step is not None and dev_type != step.device_type:
            reasons.append(
                {
                    "code": "DEVICE_TYPE_MISMATCH",
                    "message": f"设备 {device_id} 是 {dev_type}，需要 {step.device_type}",
                }
            )
        if step is not None and step.duration > 0 and device_id in self._devices:
            req = (time, time + step.duration)
            for other, o_start, o_end in self._device_occupancies(self.proj, device_id):
                if _overlaps(req[0], req[1], o_start, o_end):
                    blocking = {
                        "attempt_id": other.attempt_id,
                        "unit_id": other.unit_id,
                        "step_id": other.step_id,
                        "status": other.status,
                        "interval": [o_start, o_end],
                    }
                    reasons.append(
                        {
                            "code": "DEVICE_BUSY",
                            "message": f"设备 {device_id} 在 {list(req)} 被尝试 {other.attempt_id} "
                            f"[{o_start}, {o_end}) 占用",
                            "attempt_id": other.attempt_id,
                        }
                    )
                    break
        return {
            "allowed": not reasons,
            "unit_id": unit_id,
            "step_id": step_id,
            "device_id": device_id,
            "time": time,
            "reasons": reasons,
            "occupying_attempt": blocking,
            "current": current,
        }

    def explain_unit(self, unit_id: str) -> dict:
        """跨版本到达当前状态的完整解释：绑定历史 + 尝试历史 + 当前投影。"""
        if unit_id not in self.proj.units:
            raise WarehouseError(f"未知物料 {unit_id}")
        unit = self.proj.units[unit_id]
        bindings = self.proj.bindings.get(unit_id, [])
        journey = []
        for b in bindings:
            journey.append(
                {
                    "binding_seq": b.binding_seq,
                    "route_id": b.route_id,
                    "version": b.version,
                    "state_at_bind": b.state_at_bind,
                    "reason": b.reason,
                    "from": (
                        {"route_id": b.old_route_id, "version": b.old_version, "state": b.old_state}
                        if b.old_route_id
                        else None
                    ),
                }
            )
        attempts = []
        for a in self.proj.unit_attempts(unit_id):
            attempts.append(
                {
                    "attempt_id": a.attempt_id,
                    "step_id": a.step_id,
                    "device_id": a.device_id,
                    "route_id": a.route_id,
                    "version": a.version,
                    "binding_seq": a.binding_seq,
                    "from_state": a.from_state,
                    "to_state": a.to_state,
                    "status": a.status,
                    "start_time": a.start_time,
                    "end_time": a.end_time,
                }
            )
        return {
            "unit_id": unit_id,
            "current": self.get_unit(unit_id),
            "position": self.unit_position(unit_id),
            "journey": journey,
            "attempts": attempts,
            "accepted_counted": bool(unit.accepted_counted),
        }
