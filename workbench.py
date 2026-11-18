"""不停库盘点工作台。

工作台不直接碰账本的库存表，只消费账本的两个公开能力：

* ``retain_snapshot(prefix, label)`` —— 开始盘点时取得并物化冻结前缀；
* ``adjust(request_no, deltas)``      —— 提交时把“盘点量 - 账本 as_of(k)”
  的差异作为新的真实库存调整原子发布。

口径要点（题面）：

* 盘点输入是冻结前缀 k 时刻的实数数量，由合成复盘资料提供，
  而不是提交时刻的当前实物量；晚录入、迟提交都不改变差异口径；
* 提交时的非负检查针对**当前**库存（冻结后正常收发会驱动它），
  任一位置会变负则整单保持待处理，绝不部分提交；
* 每个盘点位置只能属于一张尚未关闭的盘点单，重叠开始整次拒绝；
* 关闭后的同请求重试复读同一组调整身份，即使库存随后变化；
* 不同请求不得再次结算同一盘点单；
* 作废释放位置、不生成调整。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from ledger import (
    IdempotencyConflict,
    InvalidOperation,
    InventoryLedger,
    canonical_text,
    now_iso,
)


class WorkbenchError(Exception):
    """工作台层基础异常。"""


class SheetNotFound(WorkbenchError):
    pass


class SheetStateError(WorkbenchError):
    """盘点单状态不允许该操作（已关闭/已作废/缺录入等）。"""


class OverlapError(WorkbenchError):
    """盘点位置与未关闭盘点单重叠，整次拒绝。"""


class NegativeAdjustmentError(WorkbenchError):
    """提交差异会使当前库存出现负数，整单保持待处理。"""


@dataclass(frozen=True)
class CountLine:
    sku: str
    location: str
    frozen_qty: int  # 账本 as_of(k)，冻结基准


@dataclass(frozen=True)
class EntryVersion:
    version: int
    counted_qty: int
    recorder: str
    created_at: str


@dataclass(frozen=True)
class CountSheet:
    sheet_id: int
    request_no: str
    frozen_prefix: int
    snapshot_label: str
    status: str  # open / submitted / voided
    created_at: str
    closed_at: Optional[str]
    lines: tuple[CountLine, ...]
    entries: dict[tuple[str, str], EntryVersion]
    adjustments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    adjust_request_no: Optional[str] = None
    adjust_commit_seq: Optional[int] = None


_WB_DDL = """
CREATE TABLE IF NOT EXISTS wb_requests (
    request_no  TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    body_text   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_sheets (
    sheet_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no          TEXT NOT NULL UNIQUE,
    frozen_prefix       INTEGER NOT NULL,
    snapshot_label      TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('open', 'submitted', 'voided')),
    created_at          TEXT NOT NULL,
    closed_at           TEXT,
    adjust_request_no   TEXT,
    adjust_commit_seq   INTEGER
);

CREATE TABLE IF NOT EXISTS count_sheet_lines (
    sheet_id    INTEGER NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    frozen_qty  INTEGER NOT NULL CHECK (frozen_qty >= 0),
    PRIMARY KEY (sheet_id, line_no),
    UNIQUE (sheet_id, sku, location),
    FOREIGN KEY (sheet_id) REFERENCES count_sheets(sheet_id)
);

-- 位置占用表：只含尚未关闭（open）盘点单的位置
CREATE TABLE IF NOT EXISTS active_count_locations (
    sku       TEXT NOT NULL,
    location  TEXT NOT NULL,
    sheet_id  INTEGER NOT NULL,
    PRIMARY KEY (sku, location),
    FOREIGN KEY (sheet_id) REFERENCES count_sheets(sheet_id)
);

CREATE TABLE IF NOT EXISTS count_entries (
    sheet_id    INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    counted_qty INTEGER NOT NULL CHECK (counted_qty >= 0),
    recorder    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (sheet_id, sku, location),
    FOREIGN KEY (sheet_id) REFERENCES count_sheets(sheet_id)
);

CREATE TABLE IF NOT EXISTS count_entry_versions (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id    INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    counted_qty INTEGER NOT NULL CHECK (counted_qty >= 0),
    recorder    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (sheet_id, sku, location, version)
);

CREATE TABLE IF NOT EXISTS count_adjustments (
    sheet_id    INTEGER NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    location    TEXT NOT NULL,
    frozen_qty  INTEGER NOT NULL,
    counted_qty INTEGER NOT NULL,
    delta       INTEGER NOT NULL,
    PRIMARY KEY (sheet_id, line_no),
    FOREIGN KEY (sheet_id) REFERENCES count_sheets(sheet_id)
);
"""


class CountWorkbench:
    def __init__(self, ledger: InventoryLedger):
        self.ledger = ledger
        self.conn = ledger.conn  # 与账本同一物理库，事务可跨两组表原子提交
        self._lock = ledger._lock  # 复用账本的串行锁
        self.conn.executescript(_WB_DDL)

    # -- 工作台自身的幂等层 ----------------------------------------------

    def _replay(self, request_no: str) -> Optional[dict[str, Any]]:
        import json

        row = self.conn.execute(
            "SELECT result_json FROM wb_requests WHERE request_no=?", (request_no,)
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def _record_request(
        self, request_no: str, action: str, payload: Any, result: dict[str, Any]
    ) -> None:
        import json

        self.conn.execute(
            "INSERT INTO wb_requests(request_no, action, body_text, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                request_no,
                action,
                canonical_text(payload),
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                now_iso(),
            ),
        )

    def _guard_request(
        self, request_no: str, action: str, payload: Any
    ) -> Optional[dict[str, Any]]:
        body = canonical_text(payload)
        row = self.conn.execute(
            "SELECT action, body_text FROM wb_requests WHERE request_no=?",
            (request_no,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["body_text"] != body:
            raise IdempotencyConflict(
                f"请求号 {request_no} 已用于不同正文的 {row['action']} 操作"
            )
        replay = self._replay(request_no)
        assert replay is not None
        replay = dict(replay)
        replay["replayed"] = True
        return replay

    # -- 开始盘点 ---------------------------------------------------------

    def open_sheet(
        self,
        request_no: str,
        targets: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """选择一组 (sku, location) 开始盘点，取得账本实际冻结前缀 k。

        位置与任何未关闭盘点单重叠则整次拒绝（不留半成品）。
        """
        if not targets:
            raise WorkbenchError("盘点目标不能为空")
        norm_targets = sorted(set(targets))
        if len(norm_targets) != len(targets):
            raise WorkbenchError("盘点目标存在重复位置")
        payload = {"op": "open_sheet", "targets": norm_targets}

        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                replay = self._guard_request(request_no, "open_sheet", payload)
                if replay is not None:
                    self.conn.execute("ROLLBACK")
                    return replay

                overlap = self.conn.execute(
                    f"""
                    SELECT a.sku, a.location, a.sheet_id
                    FROM active_count_locations a
                    WHERE (a.sku, a.location) IN (
                        {",".join("(?, ?)" for _ in norm_targets)}
                    )
                    """,
                    [v for t in norm_targets for v in t],
                ).fetchall()
                if overlap:
                    items = ", ".join(
                        f"{r['sku']}@{r['location']}->单{r['sheet_id']}" for r in overlap
                    )
                    raise OverlapError(f"盘点位置被未关闭盘点单占用: {items}")

                # 冻结前缀 = 账本当前已提交头序号；同事务物化，之后写入不会改它
                k = self.ledger.head_seq()
                sheet_id = self.conn.execute(
                    "INSERT INTO count_sheets"
                    "(request_no, frozen_prefix, snapshot_label, status, created_at) "
                    "VALUES (?, ?, ?, 'open', ?)",
                    (request_no, k, f"count-sheet-pending-{request_no}", now_iso()),
                ).lastrowid
                label = f"count-sheet-{sheet_id}-k{k}"
                self.conn.execute(
                    "UPDATE count_sheets SET snapshot_label=? WHERE sheet_id=?",
                    (label, sheet_id),
                )
                # 只物化涉及位置的冻结基准；快照本体仍由账本接口保管
                self.ledger.retain_snapshot_in_tx(k, label)
                balances = self.ledger.as_of(k)

                for line_no, (sku, location) in enumerate(norm_targets):
                    frozen_qty = balances.get((sku, location), 0)
                    self.conn.execute(
                        "INSERT INTO count_sheet_lines"
                        "(sheet_id, line_no, sku, location, frozen_qty) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (sheet_id, line_no, sku, location, frozen_qty),
                    )
                    self.conn.execute(
                        "INSERT INTO active_count_locations(sku, location, sheet_id) "
                        "VALUES (?, ?, ?)",
                        (sku, location, sheet_id),
                    )

                result = {
                    "sheet_id": sheet_id,
                    "frozen_prefix": k,
                    "snapshot_label": label,
                    "status": "open",
                    "lines": [
                        {
                            "sku": sku,
                            "location": location,
                            "frozen_qty": balances.get((sku, location), 0),
                        }
                        for sku, location in norm_targets
                    ],
                    "replayed": False,
                }
                self._record_request(request_no, "open_sheet", payload, result)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- 录入 / 更正 ------------------------------------------------------

    def enter_count(
        self,
        request_no: str,
        sheet_id: int,
        sku: str,
        location: str,
        counted_qty: int,
        recorder: str,
    ) -> dict[str, Any]:
        """录入或更正一个位置的盘点实数；保留录入身份与全部历史版本。"""
        if not isinstance(counted_qty, int) or isinstance(counted_qty, bool) or counted_qty < 0:
            raise InvalidOperation("盘点数量必须为非负整数")
        payload = {
            "op": "enter_count",
            "sheet_id": sheet_id,
            "sku": sku,
            "location": location,
            "counted_qty": counted_qty,
            "recorder": recorder,
        }
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                replay = self._guard_request(request_no, "enter_count", payload)
                if replay is not None:
                    self.conn.execute("ROLLBACK")
                    return replay

                sheet = self._get_sheet_row(sheet_id)
                self._assert_open(sheet)
                line = self.conn.execute(
                    "SELECT 1 FROM count_sheet_lines WHERE sheet_id=? AND sku=? AND location=?",
                    (sheet_id, sku, location),
                ).fetchone()
                if line is None:
                    raise SheetNotFound(f"位置 {sku}@{location} 不属于盘点单 {sheet_id}")

                prev = self.conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM count_entry_versions "
                    "WHERE sheet_id=? AND sku=? AND location=?",
                    (sheet_id, sku, location),
                ).fetchone()
                version = int(prev["v"]) + 1
                ts = now_iso()
                self.conn.execute(
                    "INSERT INTO count_entry_versions"
                    "(sheet_id, sku, location, version, counted_qty, recorder, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sheet_id, sku, location, version, counted_qty, recorder, ts),
                )
                self.conn.execute(
                    """
                    INSERT INTO count_entries
                    (sheet_id, sku, location, version, counted_qty, recorder, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sheet_id, sku, location) DO UPDATE SET
                        version=excluded.version,
                        counted_qty=excluded.counted_qty,
                        recorder=excluded.recorder,
                        created_at=excluded.created_at
                    """,
                    (sheet_id, sku, location, version, counted_qty, recorder, ts),
                )
                result = {
                    "sheet_id": sheet_id,
                    "sku": sku,
                    "location": location,
                    "version": version,
                    "counted_qty": counted_qty,
                    "recorder": recorder,
                    "replayed": False,
                }
                self._record_request(request_no, "enter_count", payload, result)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- 提交 -------------------------------------------------------------

    def submit_sheet(self, request_no: str, sheet_id: int) -> dict[str, Any]:
        """按冻结口径计算差异并原子发布，随后关闭盘点单。

        差异 = 盘点量 - 账本 as_of(k)；非负检查针对提交时的当前库存。
        同请求重试（含响应丢失）复读同一组调整身份。
        """
        payload = {"op": "submit_sheet", "sheet_id": sheet_id}
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                replay = self._guard_request(request_no, "submit_sheet", payload)
                if replay is not None:
                    self.conn.execute("ROLLBACK")
                    return replay

                sheet = self._get_sheet_row(sheet_id)
                if sheet["status"] != "open":
                    # 不同请求号试图再次结算已关闭单据
                    raise SheetStateError(
                        f"盘点单 {sheet_id} 已 {sheet['status']}，不能再次结算"
                    )

                lines = self.conn.execute(
                    "SELECT line_no, sku, location, frozen_qty FROM count_sheet_lines "
                    "WHERE sheet_id=? ORDER BY line_no",
                    (sheet_id,),
                ).fetchall()
                entries = {
                    (r["sku"], r["location"]): r
                    for r in self.conn.execute(
                        "SELECT sku, location, version, counted_qty, recorder, created_at "
                        "FROM count_entries WHERE sheet_id=?",
                        (sheet_id,),
                    ).fetchall()
                }
                missing = [
                    f"{r['sku']}@{r['location']}"
                    for r in lines
                    if (r["sku"], r["location"]) not in entries
                ]
                if missing:
                    raise SheetStateError(
                        f"盘点单 {sheet_id} 尚有位置未录入: {', '.join(missing)}"
                    )

                k = int(sheet["frozen_prefix"])
                baseline = self.ledger.as_of(k)  # 冻结基准，独立可核对
                adjustments: list[dict[str, Any]] = []
                for r in lines:
                    sku, loc = r["sku"], r["location"]
                    frozen_qty = int(r["frozen_qty"])
                    # 账本快照侧独立核对：行内基准必须等于 as_of(k)
                    if baseline.get((sku, loc), 0) != frozen_qty:
                        raise WorkbenchError(
                            f"冻结基准内部不一致: {sku}@{loc}"
                        )
                    counted = int(entries[(sku, loc)]["counted_qty"])
                    delta = counted - frozen_qty
                    adjustments.append(
                        {
                            "line_no": int(r["line_no"]),
                            "sku": sku,
                            "location": loc,
                            "frozen_qty": frozen_qty,
                            "counted_qty": counted,
                            "delta": delta,
                        }
                    )

                # 非负检查针对“当前”库存：冻结后的正常移动参与计算
                current = self.ledger.all_balances()
                net_by_pos: dict[tuple[str, str], int] = {}
                for adj in adjustments:
                    key = (adj["sku"], adj["location"])
                    net_by_pos[key] = net_by_pos.get(key, 0) + adj["delta"]
                for (sku, loc), delta in net_by_pos.items():
                    if current.get((sku, loc), 0) + delta < 0:
                        raise NegativeAdjustmentError(
                            f"差异调整会使 {sku}@{loc} 变为负数: "
                            f"当前 {current.get((sku, loc), 0)}, 差异 {delta}；"
                            f"盘点单 {sheet_id} 保持待处理"
                        )

                # 账本调整请求号稳定，响应丢失后重试仍命中同一账本回执
                adjust_request_no = f"count-adjust:sheet{sheet_id}:{request_no}"
                receipt = self.ledger.publish_adjustment_in_tx(
                    adjust_request_no,
                    [
                        {
                            "sku": a["sku"],
                            "location": a["location"],
                            "delta": a["delta"],
                        }
                        for a in adjustments
                    ],
                    reason=f"count-sheet-{sheet_id}",
                )

                for a in adjustments:
                    self.conn.execute(
                        "INSERT INTO count_adjustments"
                        "(sheet_id, line_no, sku, location, frozen_qty, counted_qty, delta) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            sheet_id,
                            a["line_no"],
                            a["sku"],
                            a["location"],
                            a["frozen_qty"],
                            a["counted_qty"],
                            a["delta"],
                        ),
                    )
                self.conn.execute(
                    "UPDATE count_sheets SET status='submitted', closed_at=?, "
                    "adjust_request_no=?, adjust_commit_seq=? WHERE sheet_id=?",
                    (now_iso(), adjust_request_no, receipt.commit_seq, sheet_id),
                )
                # 关闭即释放位置占用
                self.conn.execute(
                    "DELETE FROM active_count_locations WHERE sheet_id=?", (sheet_id,)
                )

                result = {
                    "sheet_id": sheet_id,
                    "status": "submitted",
                    "frozen_prefix": k,
                    "adjust_request_no": adjust_request_no,
                    "adjust_commit_seq": receipt.commit_seq,
                    "adjustments": adjustments,
                    "replayed": False,
                }
                self._record_request(request_no, "submit_sheet", payload, result)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- 作废 -------------------------------------------------------------

    def void_sheet(self, request_no: str, sheet_id: int) -> dict[str, Any]:
        """作废未提交盘点单：释放位置，不生成任何调整。"""
        payload = {"op": "void_sheet", "sheet_id": sheet_id}
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                replay = self._guard_request(request_no, "void_sheet", payload)
                if replay is not None:
                    self.conn.execute("ROLLBACK")
                    return replay
                sheet = self._get_sheet_row(sheet_id)
                self._assert_open(sheet)
                self.conn.execute(
                    "UPDATE count_sheets SET status='voided', closed_at=? WHERE sheet_id=?",
                    (now_iso(), sheet_id),
                )
                self.conn.execute(
                    "DELETE FROM active_count_locations WHERE sheet_id=?", (sheet_id,)
                )
                result = {
                    "sheet_id": sheet_id,
                    "status": "voided",
                    "adjustments": [],
                    "replayed": False,
                }
                self._record_request(request_no, "void_sheet", payload, result)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- 查询与逐项解释 ---------------------------------------------------

    def _get_sheet_row(self, sheet_id: int) -> sqlite3.Row:
        sheet = self.conn.execute(
            "SELECT * FROM count_sheets WHERE sheet_id=?", (sheet_id,)
        ).fetchone()
        if sheet is None:
            raise SheetNotFound(f"盘点单 {sheet_id} 不存在")
        return sheet

    def _assert_open(self, sheet: sqlite3.Row) -> None:
        if sheet["status"] != "open":
            raise SheetStateError(
                f"盘点单 {sheet['sheet_id']} 状态为 {sheet['status']}，操作被拒绝"
            )

    def get_sheet(self, sheet_id: int) -> CountSheet:
        with self._lock:
            sheet = self._get_sheet_row(sheet_id)
            line_rows = self.conn.execute(
                "SELECT sku, location, frozen_qty FROM count_sheet_lines "
                "WHERE sheet_id=? ORDER BY line_no",
                (sheet_id,),
            ).fetchall()
            entry_rows = self.conn.execute(
                "SELECT sku, location, version, counted_qty, recorder, created_at "
                "FROM count_entries WHERE sheet_id=?",
                (sheet_id,),
            ).fetchall()
            adj_rows = self.conn.execute(
                "SELECT line_no, sku, location, frozen_qty, counted_qty, delta "
                "FROM count_adjustments WHERE sheet_id=? ORDER BY line_no",
                (sheet_id,),
            ).fetchall()
            return CountSheet(
                sheet_id=int(sheet["sheet_id"]),
                request_no=sheet["request_no"],
                frozen_prefix=int(sheet["frozen_prefix"]),
                snapshot_label=sheet["snapshot_label"],
                status=sheet["status"],
                created_at=sheet["created_at"],
                closed_at=sheet["closed_at"],
                lines=tuple(
                    CountLine(
                        sku=r["sku"], location=r["location"], frozen_qty=int(r["frozen_qty"])
                    )
                    for r in line_rows
                ),
                entries={
                    (r["sku"], r["location"]): EntryVersion(
                        version=int(r["version"]),
                        counted_qty=int(r["counted_qty"]),
                        recorder=r["recorder"],
                        created_at=r["created_at"],
                    )
                    for r in entry_rows
                },
                adjustments=tuple(dict(r) for r in adj_rows),
                adjust_request_no=sheet["adjust_request_no"],
                adjust_commit_seq=(
                    int(sheet["adjust_commit_seq"])
                    if sheet["adjust_commit_seq"] is not None
                    else None
                ),
            )

    def entry_history(
        self, sheet_id: int, sku: str, location: str
    ) -> list[EntryVersion]:
        rows = self.conn.execute(
            "SELECT version, counted_qty, recorder, created_at "
            "FROM count_entry_versions WHERE sheet_id=? AND sku=? AND location=? "
            "ORDER BY version",
            (sheet_id, sku, location),
        ).fetchall()
        return [
            EntryVersion(
                version=int(r["version"]),
                counted_qty=int(r["counted_qty"]),
                recorder=r["recorder"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def explain_sheet(self, sheet_id: int) -> dict[str, Any]:
        """逐项解释：冻结基准、录入版本、冻结后变动、最终调整。"""
        with self._lock:
            sheet = self.get_sheet(sheet_id)
            k = sheet.frozen_prefix
            # 冻结之后影响过这些位置的账本事件，用于解释“当前量为何变了”
            keys = [(ln.sku, ln.location) for ln in sheet.lines]
            post_events = self.ledger.events_touching(k, keys)

            current_balances = {
                (ln.sku, ln.location): self.ledger.balance(ln.sku, ln.location)
                for ln in sheet.lines
            }
            frozen_snapshot = self.ledger.get_snapshot(label=sheet.snapshot_label)
            return {
                "sheet_id": sheet.sheet_id,
                "status": sheet.status,
                "frozen_prefix": k,
                "snapshot_label": sheet.snapshot_label,
                "frozen_snapshot": {
                    f"{sku}@{loc}": qty
                    for (sku, loc), qty in frozen_snapshot.balances.items()
                },
                "lines": [
                    {
                        "sku": ln.sku,
                        "location": ln.location,
                        "frozen_qty": ln.frozen_qty,
                        "entry": (
                            {
                                "version": sheet.entries[(ln.sku, ln.location)].version,
                                "counted_qty": sheet.entries[
                                    (ln.sku, ln.location)
                                ].counted_qty,
                                "recorder": sheet.entries[
                                    (ln.sku, ln.location)
                                ].recorder,
                            }
                            if (ln.sku, ln.location) in sheet.entries
                            else None
                        ),
                        "current_qty": current_balances[(ln.sku, ln.location)],
                    }
                    for ln in sheet.lines
                ],
                "post_freeze_events": post_events,
                "adjustments": list(sheet.adjustments),
            }
