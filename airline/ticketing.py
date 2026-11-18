"""票联服务：出票 / 使用 / 退票 / 重签，SQLite 物化状态 + 仅追加请求日志。

设计约束（本题虚构规则，金额均为整数分）：
- 售票端不依赖收入仓即可独立服务柜台；
- 每张票有有序 coupon_id 列表，票联生命周期状态机：
  OPEN -> USED | REFUNDED | EXCHANGED，任一终结态不可再次终结；
- 重签只原子终结所选 OPEN 联并创建新票的新 OPEN 联，其余旧联身份/状态不变，
  不做整票作废重建；新票价 = 所选旧联原剩余票值 + 补收 - 退还，须非负，
  补收与退还不能同时为正；
- 旧联原票值只在其出生的那次出票/重签分摊一次，后续重签不重新分摊旧联；
- 所有操作带请求号：同号同文返回首次结果，同号异文拒绝；
- 两个连接同时终结同一联，最多一个成功（BEGIN IMMEDIATE + 条件 UPDATE）；
- 上限：最多 30 张票、每张 6 联、100 次操作（可注入调整，仅测试用）。
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


class TicketingError(Exception):
    """票联服务业务异常基类。"""


class RequestConflict(TicketingError):
    """同请求号但请求体不同。"""


class InvalidOperation(TicketingError):
    """违反票联状态机或虚构业务规则。"""


class LimitExceeded(TicketingError):
    """超出票/联/操作上限。"""


# --------------------------------------------------------------------------- 常量与默认上限

DEFAULT_MAX_TICKETS = 30
DEFAULT_MAX_COUPONS_PER_TICKET = 6
DEFAULT_MAX_OPERATIONS = 100

_OPEN = "OPEN"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    op_type TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets(
    ticket_id TEXT PRIMARY KEY,
    root_ticket_id TEXT NOT NULL,
    birth_exchange_id TEXT,
    fare INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coupons(
    coupon_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    seq_no INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    weight INTEGER NOT NULL,
    original_value INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','USED','REFUNDED','EXCHANGED')),
    parent_exchange_id TEXT,
    root_ticket_id TEXT NOT NULL,
    UNIQUE(ticket_id, seq_no)
);
CREATE TABLE IF NOT EXISTS exchanges(
    exchange_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    old_ticket_id TEXT NOT NULL,
    new_ticket_id TEXT NOT NULL,
    surcharge INTEGER NOT NULL,
    refund_amount INTEGER NOT NULL,
    new_fare INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_parents(
    new_coupon_id TEXT NOT NULL,
    parent_coupon_id TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    PRIMARY KEY(new_coupon_id, parent_coupon_id)
);
CREATE TABLE IF NOT EXISTS coupon_terminations(
    coupon_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    request_id TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    op_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- 工具函数


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_hash(op_type: str, payload: dict[str, Any]) -> str:
    body = json.dumps(
        {"op": op_type, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _norm_coupon_spec(spec: Any) -> dict[str, Any]:
    """接受 {"segment_id","weight"} 或 (segment_id, weight)。"""
    if isinstance(spec, dict):
        seg = spec.get("segment_id")
        weight = spec.get("weight")
    elif isinstance(spec, (tuple, list)) and len(spec) == 2:
        seg, weight = spec
    else:
        raise InvalidOperation(f"无法识别的票联规格: {spec!r}")
    if not isinstance(seg, str) or not seg:
        raise InvalidOperation("航段ID必须是非空字符串")
    if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
        raise InvalidOperation("分摊权重必须为正整数")
    return {"segment_id": seg, "weight": weight}


# --------------------------------------------------------------------------- 服务主体


class TicketingService:
    """票联服务。每个方法调用使用独立连接，方法级事务，可跨线程共享实例。"""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        max_tickets: int = DEFAULT_MAX_TICKETS,
        max_coupons_per_ticket: int = DEFAULT_MAX_COUPONS_PER_TICKET,
        max_operations: int = DEFAULT_MAX_OPERATIONS,
    ) -> None:
        self.path = path
        self.max_tickets = max_tickets
        self.max_coupons_per_ticket = max_coupons_per_ticket
        self.max_operations = max_operations
        # :memory: 下每次操作开新连接会丢库，改用共享缓存 URI 并保留活连接。
        self._memory = path == ":memory:"
        if self._memory:
            self._dsn = f"file:airline_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self._dsn, uri=True)
        else:
            self._dsn = path
            self._keeper = None
        self._init_schema()

    # ------------------------------------------------------------- 连接与事务

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._dsn, uri=self._memory, timeout=30
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _write_tx(self) -> sqlite3.Connection:
        """写事务：立即拿写锁，两个并发写者串行化，配合条件 UPDATE 实现竞争裁决。"""
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

    @contextmanager
    def _read_tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
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

    # ------------------------------------------------------------- 序号与幂等

    def _next_id(self, conn: sqlite3.Connection, kind: str) -> str:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"seq_{kind}",)
        ).fetchone()
        nxt = (row["value"] + 1) if row else 1
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"seq_{kind}", nxt),
        )
        prefix = {"ticket": "TKT", "coupon": "CPN", "exchange": "EXC"}[kind]
        return f"{prefix}-{nxt:06d}"

    def _begin_request(
        self, conn: sqlite3.Connection, request_id: str, op_type: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """在已开启的写事务内处理请求号。命中历史请求则返回首次响应，否则返回 None。"""
        if not isinstance(request_id, str) or not request_id:
            raise InvalidOperation("request_id 必须是非空字符串")
        h = _payload_hash(op_type, payload)
        row = conn.execute(
            "SELECT payload_hash, response_json FROM requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is not None:
            if row["payload_hash"] != h:
                raise RequestConflict(
                    f"请求号 {request_id} 已用于不同请求体（同号异文拒绝）"
                )
            return json.loads(row["response_json"])
        op_count = conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"]
        if op_count >= self.max_operations:
            raise LimitExceeded(f"操作次数已达上限 {self.max_operations}")
        return None

    def _finish_request(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        op_type: str,
        payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        h = _payload_hash(op_type, payload)
        resp_json = json.dumps(response, sort_keys=True)
        conn.execute(
            "INSERT INTO requests(request_id,payload_hash,op_type,response_json,created_at)"
            " VALUES(?,?,?,?,?)",
            (request_id, h, op_type, resp_json, _utcnow()),
        )
        conn.execute(
            "INSERT INTO journal(request_id,op_type,payload_json,response_json)"
            " VALUES(?,?,?,?)",
            (request_id, op_type, json.dumps(payload, sort_keys=True), resp_json),
        )

    # ------------------------------------------------------------- 出票

    def issue_ticket(
        self,
        request_id: str,
        coupon_specs: list[dict[str, Any] | tuple[str, int]],
        total_fare: int,
    ) -> dict[str, Any]:
        """创建 ticket_id 与有序 coupon_id 列表，按权重分摊固定总票价（整数分）。"""
        payload = {"coupon_specs": list(coupon_specs), "total_fare": total_fare}
        with self._write_tx() as conn:
            cached = self._begin_request(conn, request_id, "issue", payload)
            if cached is not None:
                return cached

            if not isinstance(total_fare, int) or isinstance(total_fare, bool) or total_fare < 0:
                raise InvalidOperation("总票价必须是非负整数分")
            specs = [_norm_coupon_spec(s) for s in coupon_specs]
            if not specs:
                raise InvalidOperation("每张票至少要有一联")
            if len(specs) > self.max_coupons_per_ticket:
                raise LimitExceeded(
                    f"每张票最多 {self.max_coupons_per_ticket} 联，收到 {len(specs)} 联"
                )
            ticket_count = conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
            if ticket_count >= self.max_tickets:
                raise LimitExceeded(f"票数已达上限 {self.max_tickets}")

            ticket_id = self._next_id(conn, "ticket")
            weighted = [(f"__pos_{i}__", s["weight"]) for i, s in enumerate(specs)]
            alloc = allocate_values(total_fare, weighted)
            now = _utcnow()
            conn.execute(
                "INSERT INTO tickets(ticket_id,root_ticket_id,birth_exchange_id,fare,created_at)"
                " VALUES(?,?,?,?,?)",
                (ticket_id, ticket_id, None, total_fare, now),
            )
            coupons_out: list[dict[str, Any]] = []
            for i, spec in enumerate(specs):
                coupon_id = self._next_id(conn, "coupon")
                value = alloc[f"__pos_{i}__"]
                conn.execute(
                    "INSERT INTO coupons(coupon_id,ticket_id,seq_no,segment_id,weight,"
                    "original_value,status,parent_exchange_id,root_ticket_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        coupon_id, ticket_id, i, spec["segment_id"], spec["weight"],
                        value, _OPEN, None, ticket_id,
                    ),
                )
                coupons_out.append(
                    {
                        "coupon_id": coupon_id,
                        "seq_no": i,
                        "segment_id": spec["segment_id"],
                        "weight": spec["weight"],
                        "original_value": value,
                        "status": _OPEN,
                    }
                )
            response = {
                "ticket_id": ticket_id,
                "root_ticket_id": ticket_id,
                "total_fare": total_fare,
                "coupons": coupons_out,
            }
            self._finish_request(conn, request_id, "issue", payload, response)
            return response

    # ------------------------------------------------------------- 使用

    def use_coupon(self, request_id: str, coupon_id: str) -> dict[str, Any]:
        """OPEN 联记录实际使用；USED/REFUNDED/EXCHANGED 不能再次终结。"""
        payload = {"coupon_id": coupon_id}
        with self._write_tx() as conn:
            cached = self._begin_request(conn, request_id, "use", payload)
            if cached is not None:
                return cached
            row = self._require_coupon(conn, coupon_id)
            # 终态裁决以条件 UPDATE 为准：并发下第二个事务 rowcount=0，整体回滚。
            cur = conn.execute(
                "UPDATE coupons SET status='USED' "
                "WHERE coupon_id=? AND status='OPEN'", (coupon_id,)
            )
            if cur.rowcount != 1:
                raise InvalidOperation(
                    f"票联 {coupon_id} 状态为 {row['status']}，不可再使用"
                )
            self._terminate(conn, coupon_id, "USED",
                            {"segment_id": row["segment_id"]}, request_id)
            response = {"coupon_id": coupon_id, "status": "USED",
                        "segment_id": row["segment_id"],
                        "original_value": row["original_value"]}
            self._finish_request(conn, request_id, "use", payload, response)
            return response

    # ------------------------------------------------------------- 退票

    def refund_coupon(self, request_id: str, coupon_id: str, fee: int) -> dict[str, Any]:
        """退 OPEN 联：退款 = 原票值 - 手续费，手续费不超过票值；USED 联不能退。"""
        payload = {"coupon_id": coupon_id, "fee": fee}
        with self._write_tx() as conn:
            cached = self._begin_request(conn, request_id, "refund", payload)
            if cached is not None:
                return cached
            if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
                raise InvalidOperation("手续费必须是非负整数分")
            row = self._require_coupon(conn, coupon_id)
            if fee > row["original_value"]:
                raise InvalidOperation(
                    f"手续费 {fee} 超过票联原票值 {row['original_value']}"
                )
            # 同样以条件 UPDATE 裁决；USED/EXCHANGED/REFUNDED 下 rowcount=0。
            cur = conn.execute(
                "UPDATE coupons SET status='REFUNDED' "
                "WHERE coupon_id=? AND status='OPEN'", (coupon_id,)
            )
            if cur.rowcount != 1:
                if row["status"] == "USED":
                    raise InvalidOperation(f"票联 {coupon_id} 已 USED，不能退票")
                raise InvalidOperation(
                    f"票联 {coupon_id} 状态为 {row['status']}，不可退票"
                )
            refund_amount = row["original_value"] - fee
            self._terminate(
                conn, coupon_id, "REFUNDED",
                {"fee": fee, "refund_amount": refund_amount,
                 "original_value": row["original_value"]},
                request_id,
            )
            response = {"coupon_id": coupon_id, "status": "REFUNDED",
                        "original_value": row["original_value"],
                        "fee": fee, "refund_amount": refund_amount}
            self._finish_request(conn, request_id, "refund", payload, response)
            return response

    # ------------------------------------------------------------- 重签

    def reissue(
        self,
        request_id: str,
        old_ticket_id: str,
        old_coupon_ids: list[str],
        new_coupon_specs: list[dict[str, Any] | tuple[str, int]],
        surcharge: int = 0,
        refund_amount: int = 0,
    ) -> dict[str, Any]:
        """一次重签：原子终结原票中若干 OPEN 联，创建新票与新 OPEN 联。"""
        payload = {
            "old_ticket_id": old_ticket_id,
            "old_coupon_ids": list(old_coupon_ids),
            "new_coupon_specs": list(new_coupon_specs),
            "surcharge": surcharge,
            "refund_amount": refund_amount,
        }
        with self._write_tx() as conn:
            cached = self._begin_request(conn, request_id, "reissue", payload)
            if cached is not None:
                return cached

            if not isinstance(surcharge, int) or isinstance(surcharge, bool) or surcharge < 0:
                raise InvalidOperation("补收必须是非负整数分")
            if not isinstance(refund_amount, int) or isinstance(refund_amount, bool) or refund_amount < 0:
                raise InvalidOperation("退还必须是非负整数分")
            if surcharge > 0 and refund_amount > 0:
                raise InvalidOperation("补收和退还不能同时为正")
            specs = [_norm_coupon_spec(s) for s in new_coupon_specs]
            if not specs:
                raise InvalidOperation("重签至少要创建一联")
            if len(specs) > self.max_coupons_per_ticket:
                raise LimitExceeded(
                    f"每张票最多 {self.max_coupons_per_ticket} 联，新票收到 {len(specs)} 联"
                )
            if not old_coupon_ids:
                raise InvalidOperation("重签必须至少选择一个旧联")
            if len(set(old_coupon_ids)) != len(old_coupon_ids):
                raise InvalidOperation("所选旧联存在重复")

            ticket_row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=?", (old_ticket_id,)
            ).fetchone()
            if ticket_row is None:
                raise InvalidOperation(f"原票 {old_ticket_id} 不存在")

            old_rows: list[sqlite3.Row] = []
            old_value_sum = 0
            for cid in old_coupon_ids:
                row = self._require_coupon(conn, cid)
                if row["ticket_id"] != old_ticket_id:
                    raise InvalidOperation(
                        f"票联 {cid} 不属于原票 {old_ticket_id}（不能跨票拼联重签）"
                    )
                if row["status"] != _OPEN:
                    raise InvalidOperation(
                        f"票联 {cid} 状态为 {row['status']}，只有 OPEN 联可参加重签"
                    )
                old_rows.append(row)
                old_value_sum += row["original_value"]

            new_fare = old_value_sum + surcharge - refund_amount
            if new_fare < 0:
                raise InvalidOperation(
                    f"新票价 {new_fare} 为负：旧联剩余票值 {old_value_sum}"
                    f" + 补收 {surcharge} - 退还 {refund_amount}"
                )

            exchange_id = self._next_id(conn, "exchange")
            new_ticket_id = self._next_id(conn, "ticket")
            root_ticket_id = ticket_row["root_ticket_id"]
            now = _utcnow()

            conn.execute(
                "INSERT INTO tickets(ticket_id,root_ticket_id,birth_exchange_id,fare,created_at)"
                " VALUES(?,?,?,?,?)",
                (new_ticket_id, root_ticket_id, exchange_id, new_fare, now),
            )
            weighted = [(f"__pos_{i}__", s["weight"]) for i, s in enumerate(specs)]
            alloc = allocate_values(new_fare, weighted)
            new_coupons_out: list[dict[str, Any]] = []
            for i, spec in enumerate(specs):
                new_cid = self._next_id(conn, "coupon")
                value = alloc[f"__pos_{i}__"]
                conn.execute(
                    "INSERT INTO coupons(coupon_id,ticket_id,seq_no,segment_id,weight,"
                    "original_value,status,parent_exchange_id,root_ticket_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        new_cid, new_ticket_id, i, spec["segment_id"], spec["weight"],
                        value, _OPEN, exchange_id, root_ticket_id,
                    ),
                )
                # 祖先链：新联 -> 本次被交换的每个旧联（同一次生命周期可追溯）。
                for old_row in old_rows:
                    conn.execute(
                        "INSERT INTO exchange_parents(new_coupon_id,parent_coupon_id,exchange_id)"
                        " VALUES(?,?,?)",
                        (new_cid, old_row["coupon_id"], exchange_id),
                    )
                new_coupons_out.append({
                    "coupon_id": new_cid, "seq_no": i,
                    "segment_id": spec["segment_id"], "weight": spec["weight"],
                    "original_value": value, "status": _OPEN,
                    "parent_coupon_ids": list(old_coupon_ids),
                })

            # 条件终结：只有仍是 OPEN 的联能被标 EXCHANGED，并发下失败者回滚整个操作。
            for cid in old_coupon_ids:
                cur = conn.execute(
                    "UPDATE coupons SET status='EXCHANGED' "
                    "WHERE coupon_id=? AND status='OPEN'",
                    (cid,),
                )
                if cur.rowcount != 1:
                    raise InvalidOperation(
                        f"票联 {cid} 在重签提交时已不再是 OPEN（竞争失败）"
                    )
                self._terminate(
                    conn, cid, "EXCHANGED",
                    {"exchange_id": exchange_id, "new_ticket_id": new_ticket_id},
                    request_id,
                )

            conn.execute(
                "INSERT INTO exchanges(exchange_id,request_id,old_ticket_id,new_ticket_id,"
                "surcharge,refund_amount,new_fare,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (exchange_id, request_id, old_ticket_id, new_ticket_id,
                 surcharge, refund_amount, new_fare, now),
            )

            response = {
                "exchange_id": exchange_id,
                "old_ticket_id": old_ticket_id,
                "new_ticket_id": new_ticket_id,
                "root_ticket_id": root_ticket_id,
                "exchanged_coupon_ids": list(old_coupon_ids),
                "old_value_sum": old_value_sum,
                "surcharge": surcharge,
                "refund_amount": refund_amount,
                "new_fare": new_fare,
                "new_coupons": new_coupons_out,
            }
            self._finish_request(conn, request_id, "reissue", payload, response)
            return response

    # ------------------------------------------------------------- 内部读取/终结

    def _require_coupon(self, conn: sqlite3.Connection, coupon_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM coupons WHERE coupon_id=?", (coupon_id,)
        ).fetchone()
        if row is None:
            raise InvalidOperation(f"票联 {coupon_id} 不存在")
        return row

    def _terminate(
        self, conn: sqlite3.Connection, coupon_id: str, status: str,
        detail: dict[str, Any], request_id: str,
    ) -> None:
        conn.execute(
            "INSERT INTO coupon_terminations(coupon_id,status,detail_json,request_id,at)"
            " VALUES(?,?,?,?,?)",
            (coupon_id, status, json.dumps(detail, sort_keys=True), request_id, _utcnow()),
        )

    # ------------------------------------------------------------- 查询与切面

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        with self._read_tx() as conn:
            t = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if t is None:
                raise InvalidOperation(f"票 {ticket_id} 不存在")
            coupons = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM coupons WHERE ticket_id=? ORDER BY seq_no", (ticket_id,)
                ).fetchall()
            ]
            return {**dict(t), "coupons": coupons}

    def get_coupon(self, coupon_id: str) -> dict[str, Any]:
        with self._read_tx() as conn:
            return dict(self._require_coupon(conn, coupon_id))

    def snapshot(self) -> dict[str, Any]:
        """发布完整票联切面（供收入仓导入）。纯读，不改变任何资格。"""
        with self._read_tx() as conn:
            tickets = [dict(r) for r in conn.execute(
                "SELECT ticket_id,root_ticket_id,birth_exchange_id,fare,created_at "
                "FROM tickets ORDER BY ticket_id").fetchall()]
            coupons = [dict(r) for r in conn.execute(
                "SELECT coupon_id,ticket_id,seq_no,segment_id,weight,original_value,"
                "status,parent_exchange_id,root_ticket_id FROM coupons ORDER BY coupon_id"
            ).fetchall()]
            exchanges = [dict(r) for r in conn.execute(
                "SELECT exchange_id,request_id,old_ticket_id,new_ticket_id,surcharge,"
                "refund_amount,new_fare,created_at FROM exchanges ORDER BY exchange_id"
            ).fetchall()]
            parents = [dict(r) for r in conn.execute(
                "SELECT new_coupon_id,parent_coupon_id,exchange_id "
                "FROM exchange_parents ORDER BY new_coupon_id,parent_coupon_id"
            ).fetchall()]
            terminations = [
                {**dict(r), "detail": json.loads(r["detail_json"])}
                for r in conn.execute(
                    "SELECT coupon_id,status,detail_json,request_id,at "
                    "FROM coupon_terminations ORDER BY coupon_id"
                ).fetchall()
            ]
            return {
                "schema": "ticketing-full-coupon-facet/v1",
                "taken_at": _utcnow(),
                "tickets": tickets,
                "coupons": coupons,
                "exchanges": exchanges,
                "exchange_parents": parents,
                "terminations": terminations,
            }

    def export_journal(self) -> list[dict[str, Any]]:
        """仅追加请求日志：事件重放的输入。"""
        with self._read_tx() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT seq,request_id,op_type,payload_json,response_json "
                "FROM journal ORDER BY seq").fetchall()]

    def operation_count(self) -> int:
        with self._read_tx() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"]


# --------------------------------------------------------------------------- 事件重放


def replay_journal(
    journal: list[dict[str, Any]],
    path: str = ":memory:",
    *,
    max_tickets: int = DEFAULT_MAX_TICKETS,
    max_coupons_per_ticket: int = DEFAULT_MAX_COUPONS_PER_TICKET,
    max_operations: int = DEFAULT_MAX_OPERATIONS,
) -> TicketingService:
    """按日志顺序重放全部操作，重建一个物化状态一致的新服务。

    重放时原 request_id 保持不变：幂等机制天然保证重复日志返回同一结果，
    重放产物的切面与原始库逐表比对即可验证。
    """
    svc = TicketingService(
        path,
        max_tickets=max_tickets,
        max_coupons_per_ticket=max_coupons_per_ticket,
        max_operations=max_operations,
    )
    for entry in sorted(journal, key=lambda e: e["seq"]):
        payload = json.loads(entry["payload_json"])
        op = entry["op_type"]
        rid = entry["request_id"]
        if op == "issue":
            svc.issue_ticket(rid, payload["coupon_specs"], payload["total_fare"])
        elif op == "use":
            svc.use_coupon(rid, payload["coupon_id"])
        elif op == "refund":
            svc.refund_coupon(rid, payload["coupon_id"], payload["fee"])
        elif op == "reissue":
            svc.reissue(
                rid,
                payload["old_ticket_id"],
                payload["old_coupon_ids"],
                payload["new_coupon_specs"],
                payload["surcharge"],
                payload["refund_amount"],
            )
        else:  # pragma: no cover - 防御性分支
            raise TicketingError(f"未知操作类型: {op}")
    return svc
