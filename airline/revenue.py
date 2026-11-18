"""运输收入事实仓：不可变票联切面 + 机场实际使用证据 + 可重读冻结批次。

与票联服务物理隔离（独立 SQLite 文件/库），只接收发布出来的完整切面与使用事件：
- 收入端不能自行改变票联资格，只做解释；
- 只有切面内确实 USED、且存在航段一致的唯一实际使用证据的联才确认运输收入；
- 缺证据 / 航段不符 / 重复冲突分别保留诊断；“出票成功”永远不等于已运输；
- 重签原联（EXCHANGED）不确认收入；新联继承的票值沿真实 exchange_parents 祖先链
  递归重算分摊基数，补收不计入运输收入；
- 批次按明确双侧切面（某一切面版本 + 截止某一证据序号）冻结生成，晚到证据只让
  新批次中的先前待核联转为收入，旧批次永不改写；
- 退款与手续费分别统计。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .allocation import allocate_values

# --------------------------------------------------------------------------- 异常


class RevenueError(Exception):
    """收入仓异常基类。"""


class InvalidSnapshot(RevenueError):
    """切面不完整或引用不一致。"""


class EventConflict(RevenueError):
    """同一事件 ID 对应不同内容。"""


# --------------------------------------------------------------------------- 决策与诊断枚举

DECISION_RECOGNIZED = "RECOGNIZED"                       # 确认运输收入
DECISION_PENDING_OPEN = "PENDING_OPEN"                   # 仍 OPEN，等待终结与证据
DECISION_TERMINAL_NOT_USED = "TERMINAL_NOT_USED"         # REFUNDED/EXCHANGED，非运输
DIAG_MISSING_EVIDENCE = "MISSING_EVIDENCE"               # 切面 USED 但无使用证据
DIAG_SEGMENT_MISMATCH = "SEGMENT_MISMATCH"               # 证据航段与切面航段不符
DIAG_DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"           # 同一联多条一致证据
DIAG_UNMATCHED_EVIDENCE = "UNMATCHED_EVIDENCE"           # 证据指向切面外的票联
DIAG_EVIDENCE_ON_INACTIVE = "EVIDENCE_ON_INACTIVE"       # 证据挂在非 USED 联（资格不符）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    taken_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events(
    event_id TEXT PRIMARY KEY,
    coupon_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches(
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    event_cutoff_seq INTEGER NOT NULL,
    built_at TEXT NOT NULL,
    money_json TEXT NOT NULL,
    recognized_total INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items(
    batch_id INTEGER NOT NULL,
    coupon_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    status TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    original_value INTEGER NOT NULL,
    transport_basis INTEGER NOT NULL,
    decision TEXT NOT NULL,
    recognized_amount INTEGER NOT NULL,
    event_ids_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY(batch_id, coupon_id)
);
CREATE TABLE IF NOT EXISTS batch_diagnostics(
    batch_id INTEGER NOT NULL,
    diag_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(snapshot: dict[str, Any]) -> str:
    body = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 纯函数：切面评估（无 I/O，批次与当前解释共用）


def compute_transport_bases(snapshot: dict[str, Any]) -> dict[str, int]:
    """沿真实重签关系递归计算每联的运输票值基数（整数分）。

    - 出生联（无 parent_exchange_id）：基数即其原票值，全部源自首次付款；
    - 重签新联：基数 = 按本次新联权重，对“父联基数之和”重新做整数分摊；
    - 补收/退还只影响新联原票值（现金口径），不进入运输基数；
    - 因而一条根票链上各存活/终结分支的基数守恒，等于首次出票票价。
    """
    coupons = {c["coupon_id"]: c for c in snapshot["coupons"]}
    parents: dict[str, list[str]] = {}
    for link in snapshot["exchange_parents"]:
        parents.setdefault(link["new_coupon_id"], []).append(link["parent_coupon_id"])

    memo: dict[str, int] = {}

    def basis(coupon_id: str) -> int:
        if coupon_id in memo:
            return memo[coupon_id]
        coupon = coupons[coupon_id]
        exchange_id = coupon["parent_exchange_id"]
        if exchange_id is None:
            memo[coupon_id] = coupon["original_value"]
            return memo[coupon_id]
        parent_ids = parents.get(coupon_id, [])
        parent_basis_sum = sum(basis(pid) for pid in parent_ids)
        siblings = [
            c for c in snapshot["coupons"] if c["parent_exchange_id"] == exchange_id
        ]
        alloc = allocate_values(
            parent_basis_sum, [(c["coupon_id"], c["weight"]) for c in siblings]
        )
        for cid, value in alloc.items():
            memo[cid] = value
        return memo[coupon_id]

    for cid in coupons:
        basis(cid)
    return memo


def money_summary(snapshot: dict[str, Any], bases: dict[str, int]) -> dict[str, int]:
    """现金口径汇总（整数分）。补收独立列示，绝不混入运输收入。"""
    coupons = {c["coupon_id"]: c for c in snapshot["coupons"]}
    issuance_fare = sum(
        t["fare"] for t in snapshot["tickets"] if t["birth_exchange_id"] is None
    )
    surcharges = sum(e["surcharge"] for e in snapshot["exchanges"])
    exchange_refunds = sum(e["refund_amount"] for e in snapshot["exchanges"])
    refund_fees = 0
    refund_amounts = 0
    for term in snapshot["terminations"]:
        if term["status"] == "REFUNDED":
            detail = term["detail"]
            refund_fees += detail["fee"]
            refund_amounts += detail["refund_amount"]
    return {
        "issuance_fare": issuance_fare,            # 首次出票付款
        "surcharges": surcharges,                  # 历次重签补收（非运输收入）
        "exchange_refunds": exchange_refunds,      # 历次重签退还
        "refund_fees": refund_fees,                # 退票手续费（分别统计）
        "refund_amounts": refund_amounts,          # 退票退款（分别统计）
        # 只数仍承载价值的 OPEN/USED 联：EXCHANGED 的基数已转移给后代，
        # REFUNDED 的基数已变成退款+手续费离开体系，计入都会重复或虚增。
        "transport_basis_total": sum(
            base for cid, base in bases.items()
            if coupons[cid]["status"] in ("OPEN", "USED")
        ),
        "transport_basis_gross": sum(bases.values()),  # 含已转移/已退联的毛额，仅解释用
    }


def evaluate_facet(
    snapshot: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """对一个明确双侧切面做完整解释，返回逐联决策与诊断。纯函数。"""
    coupons = {c["coupon_id"]: c for c in snapshot["coupons"]}
    bases = compute_transport_bases(snapshot)

    # 证据按票联归组，保持导入顺序。
    events_by_coupon: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        events_by_coupon.setdefault(ev["coupon_id"], []).append(ev)

    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    recognized_total = 0

    for coupon in sorted(coupons.values(), key=lambda c: c["coupon_id"]):
        cid = coupon["coupon_id"]
        base = bases[cid]
        evs = events_by_coupon.get(cid, [])
        event_ids = [e["event_id"] for e in evs]
        item: dict[str, Any] = {
            "coupon_id": cid,
            "ticket_id": coupon["ticket_id"],
            "status": coupon["status"],
            "segment_id": coupon["segment_id"],
            "original_value": coupon["original_value"],
            "transport_basis": base,
            "event_ids": event_ids,
            "recognized_amount": 0,
        }

        if coupon["status"] != "USED":
            # OPEN 待核；REFUNDED/EXCHANGED 终态非运输（重签原联不在此确认收入）。
            item["decision"] = (
                DECISION_PENDING_OPEN if coupon["status"] == "OPEN"
                else DECISION_TERMINAL_NOT_USED
            )
            if evs:
                # 证据不能赋予票联资格：非 USED 联上的使用事件单独留痕。
                diagnostics.append({
                    "diag_type": DIAG_EVIDENCE_ON_INACTIVE, "ref_id": cid,
                    "detail": {"facet_status": coupon["status"], "event_ids": event_ids},
                })
        elif not evs:
            item["decision"] = DIAG_MISSING_EVIDENCE
            diagnostics.append({"diag_type": DIAG_MISSING_EVIDENCE, "ref_id": cid,
                                "detail": {"reason": "切面为 USED 但无实际使用证据"}})
        else:
            mismatched = [e for e in evs if e["segment_id"] != coupon["segment_id"]]
            if mismatched:
                item["decision"] = DIAG_SEGMENT_MISMATCH
                diagnostics.append({
                    "diag_type": DIAG_SEGMENT_MISMATCH, "ref_id": cid,
                    "detail": {
                        "facet_segment": coupon["segment_id"],
                        "event_segments": [e["segment_id"] for e in evs],
                        "event_ids": event_ids,
                    },
                })
            elif len(evs) > 1:
                item["decision"] = DIAG_DUPLICATE_CONFLICT
                diagnostics.append({
                    "diag_type": DIAG_DUPLICATE_CONFLICT, "ref_id": cid,
                    "detail": {"event_ids": event_ids,
                               "reason": "同一票联存在多条一致使用证据，冲突待人工处置"},
                })
            else:
                item["decision"] = DECISION_RECOGNIZED
                item["recognized_amount"] = base
                recognized_total += base

        items.append(item)

    # 证据指向切面内不存在的票联：保留诊断，不静默丢弃。
    for ev in events:
        if ev["coupon_id"] not in coupons:
            diagnostics.append({
                "diag_type": DIAG_UNMATCHED_EVIDENCE,
                "ref_id": ev["event_id"],
                "detail": {"coupon_id": ev["coupon_id"], "segment_id": ev["segment_id"]},
            })

    return {
        "items": items,
        "diagnostics": diagnostics,
        "bases": bases,
        "recognized_total": recognized_total,
        "money": money_summary(snapshot, bases),
    }


# --------------------------------------------------------------------------- 收入仓


class RevenueWarehouse:
    """独立收入事实仓。与票联服务无共享连接、无回调，仅通过切面/事件对接。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        # :memory: 下每次操作开新连接会丢库，改用共享缓存 URI 并保留活连接。
        self._memory = path == ":memory:"
        if self._memory:
            self._dsn = f"file:revenue_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self._dsn, uri=True)
        else:
            self._dsn = path
            self._keeper = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._dsn, uri=self._memory, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        # executescript 会隐式提交，不能放在已开启的事务里。
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- 切面导入

    def import_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """导入完整票联切面；内容相同则幂等返回既有版本，否则生成新版本。"""
        self._validate_snapshot(snapshot)
        h = _content_hash(snapshot)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT snapshot_id FROM snapshots WHERE content_hash=?", (h,)
            ).fetchone()
            if row is not None:
                return {"snapshot_id": row["snapshot_id"], "deduplicated": True}
            cur = conn.execute(
                "INSERT INTO snapshots(content_hash,taken_at,imported_at,raw_json)"
                " VALUES(?,?,?,?)",
                (h, snapshot.get("taken_at", ""), _utcnow(), json.dumps(snapshot)),
            )
            return {"snapshot_id": cur.lastrowid, "deduplicated": False}

    @staticmethod
    def _validate_snapshot(snapshot: dict[str, Any]) -> None:
        required = {"tickets", "coupons", "exchanges", "exchange_parents", "terminations"}
        missing = required - set(snapshot)
        if missing:
            raise InvalidSnapshot(f"切面缺少必需部分: {sorted(missing)}")
        ticket_ids = {t["ticket_id"] for t in snapshot["tickets"]}
        coupon_ids = {c["coupon_id"] for c in snapshot["coupons"]}
        for c in snapshot["coupons"]:
            if c["ticket_id"] not in ticket_ids:
                raise InvalidSnapshot(
                    f"票联 {c['coupon_id']} 引用了不存在的票 {c['ticket_id']}"
                )
            if c["status"] not in {"OPEN", "USED", "REFUNDED", "EXCHANGED"}:
                raise InvalidSnapshot(f"票联 {c['coupon_id']} 状态非法: {c['status']}")
        exchange_ids = {e["exchange_id"] for e in snapshot["exchanges"]}
        for link in snapshot["exchange_parents"]:
            if link["new_coupon_id"] not in coupon_ids:
                raise InvalidSnapshot(f"重签链新联 {link['new_coupon_id']} 不在切面内")
            if link["parent_coupon_id"] not in coupon_ids:
                raise InvalidSnapshot(f"重签链父联 {link['parent_coupon_id']} 不在切面内")
            if link["exchange_id"] not in exchange_ids:
                raise InvalidSnapshot(
                    f"重签链引用了不存在的重签 {link['exchange_id']}"
                )
        for c in snapshot["coupons"]:
            if c["parent_exchange_id"] is not None \
                    and c["parent_exchange_id"] not in exchange_ids:
                raise InvalidSnapshot(
                    f"票联 {c['coupon_id']} 的出生重签 {c['parent_exchange_id']} 不存在"
                )

    # ------------------------------------------------------------- 使用证据导入

    def import_usage_event(self, event_id: str, coupon_id: str, segment_id: str) -> dict[str, Any]:
        """导入一条机场实际使用事件。

        - 事件 ID 全局唯一：同号同文幂等，同号异文拒绝；
        - 同一票联/航段的不同事件 ID 不在这里删除，全部进入证据流，
          由批次评估按 DUPLICATE_CONFLICT 诊断裁决（保留冲突现场而非静默丢弃）。
        """
        if not event_id or not coupon_id or not segment_id:
            raise RevenueError("event_id / coupon_id / segment_id 均不能为空")
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM usage_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["coupon_id"] != coupon_id or existing["segment_id"] != segment_id:
                    raise EventConflict(f"事件 {event_id} 已用于不同内容（同号异文）")
                return {"event_id": event_id, "status": "IDEMPOTENT", "accepted": False}

            conn.execute(
                "INSERT INTO usage_events(event_id,coupon_id,segment_id,imported_at)"
                " VALUES(?,?,?,?)",
                (event_id, coupon_id, segment_id, _utcnow()),
            )
            dup = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events "
                "WHERE coupon_id=? AND segment_id=?", (coupon_id, segment_id)
            ).fetchone()["c"]
            if dup > 1:
                return {"event_id": event_id, "status": "ACCEPTED_DUPLICATE",
                        "accepted": True, "duplicate_count": dup}
            return {"event_id": event_id, "status": "ACCEPTED", "accepted": True}

    # ------------------------------------------------------------- 内部读取

    def _load_snapshot(self, conn: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT raw_json FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise RevenueError(f"切面版本 {snapshot_id} 不存在")
        return json.loads(row["raw_json"])

    def _latest_snapshot_id(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(snapshot_id) AS m FROM snapshots").fetchone()
        if row["m"] is None:
            raise RevenueError("收入仓尚无任何票联切面，无法生成批次")
        return row["m"]

    def _events_up_to(self, conn: sqlite3.Connection, cutoff: int) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT event_id,coupon_id,segment_id FROM usage_events "
            "WHERE rowid<=? ORDER BY rowid", (cutoff,)).fetchall()]

    # ------------------------------------------------------------- 批次生成（冻结、可重读）

    def build_batch(self, snapshot_id: int | None = None) -> dict[str, Any]:
        """按明确双侧切面生成不可变批次：指定（或最新）切面 + 当前全部证据截止点。"""
        with self._tx() as conn:
            sid = snapshot_id if snapshot_id is not None else self._latest_snapshot_id(conn)
            snapshot = self._load_snapshot(conn, sid)
            cutoff = conn.execute(
                "SELECT COALESCE(MAX(rowid),0) AS m FROM usage_events"
            ).fetchone()["m"]
            events = self._events_up_to(conn, cutoff)
            result = evaluate_facet(snapshot, events)

            cur = conn.execute(
                "INSERT INTO batches(snapshot_id,event_cutoff_seq,built_at,money_json,"
                "recognized_total) VALUES(?,?,?,?,?)",
                (sid, cutoff, _utcnow(),
                 json.dumps(result["money"], sort_keys=True), result["recognized_total"]),
            )
            batch_id = cur.lastrowid
            for item in result["items"]:
                conn.execute(
                    "INSERT INTO batch_items(batch_id,coupon_id,ticket_id,status,segment_id,"
                    "original_value,transport_basis,decision,recognized_amount,event_ids_json,"
                    "detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, item["coupon_id"], item["ticket_id"], item["status"],
                     item["segment_id"], item["original_value"], item["transport_basis"],
                     item["decision"], item["recognized_amount"],
                     json.dumps(item["event_ids"]), json.dumps(item, sort_keys=True)),
                )
            for diag in result["diagnostics"]:
                conn.execute(
                    "INSERT INTO batch_diagnostics(batch_id,diag_type,ref_id,detail_json)"
                    " VALUES(?,?,?,?)",
                    (batch_id, diag["diag_type"], diag["ref_id"],
                     json.dumps(diag["detail"], sort_keys=True)),
                )
            return self._explain(conn, batch_id)

    def explain_batch(self, batch_id: int) -> dict[str, Any]:
        """重读一个历史批次；旧批次不随后续证据/切面变化。"""
        with self._tx() as conn:
            return self._explain(conn, batch_id)

    def _explain(self, conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
        header = conn.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if header is None:
            raise RevenueError(f"批次 {batch_id} 不存在")
        items = [json.loads(r["detail_json"]) for r in conn.execute(
            "SELECT detail_json FROM batch_items WHERE batch_id=? ORDER BY coupon_id",
            (batch_id,)).fetchall()]
        diagnostics = [
            {"diag_type": r["diag_type"], "ref_id": r["ref_id"],
             "detail": json.loads(r["detail_json"])}
            for r in conn.execute(
                "SELECT diag_type,ref_id,detail_json FROM batch_diagnostics "
                "WHERE batch_id=? ORDER BY diag_type,ref_id", (batch_id,)).fetchall()
        ]
        return {
            "batch_id": batch_id,
            "snapshot_id": header["snapshot_id"],
            "event_cutoff_seq": header["event_cutoff_seq"],
            "built_at": header["built_at"],
            "recognized_total": header["recognized_total"],
            "money": json.loads(header["money_json"]),
            "items": items,
            "diagnostics": diagnostics,
            "decision_counts": self._count_decisions(items),
        }

    def explain_current(self) -> dict[str, Any]:
        """不生成批次的实时解释：最新切面 + 截至当前的全部证据。"""
        with self._tx() as conn:
            sid = self._latest_snapshot_id(conn)
            snapshot = self._load_snapshot(conn, sid)
            cutoff = conn.execute(
                "SELECT COALESCE(MAX(rowid),0) AS m FROM usage_events"
            ).fetchone()["m"]
            events = self._events_up_to(conn, cutoff)
            result = evaluate_facet(snapshot, events)
            result["snapshot_id"] = sid
            result["event_cutoff_seq"] = cutoff
            result["decision_counts"] = self._count_decisions(result["items"])
            return result

    @staticmethod
    def _count_decisions(items: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item["decision"]] = counts.get(item["decision"], 0) + 1
        return counts

    def list_batches(self) -> list[dict[str, Any]]:
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT batch_id,snapshot_id,event_cutoff_seq,built_at,recognized_total "
                "FROM batches ORDER BY batch_id").fetchall()]
