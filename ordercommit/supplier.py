"""供应商报价承诺服务。

职责边界：
    * 登记规则版本（包装倍数、最低量、可供上限、全量数量阶梯、有效时刻区间）；
    * 多 SKU 数量向量 → 不可变报价（请求号幂等：同号同文复读、同号异文拒绝）；
    * 接受报价 → 实际供货承诺，接受时在事务内重新核对同版本累计额度，绝不凭旧报价超卖；
    * 过期接受不产生承诺。

本服务不感知门店需求，采购员可直接使用。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from .errors import (
    DuplicateRequest,
    NotFound,
    OverCapacity,
    QuoteExpired,
    RuleViolation,
    StateConflict,
)
from .pricing import SkuRule
from .schema import SUPPLIER_SCHEMA


def _iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def _canonical_request(supplier_id: str, items: list[dict[str, Any]]) -> str:
    """请求体规范化哈希：与行顺序无关，只认供应商 + 排序后的 (SKU, 数量)。"""
    payload = json.dumps(
        {"supplier_id": supplier_id,
         "items": sorted((str(i["sku"]), int(i["qty"])) for i in items)},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SupplierService:
    """单文件 SQLite 支撑的报价服务；写操作经 RLock + BEGIN IMMEDIATE 串行化。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SUPPLIER_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 规则登记
    def register_rule_version(
        self,
        version_id: str,
        supplier_id: str,
        effective_from: str | datetime,
        effective_to: str | datetime,
        sku_rules: dict[str, SkuRule],
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """登记一个完整规则版本；版本内每个 SKU 的规则在登记时自检。"""
        frm, to = _iso(effective_from), _iso(effective_to)
        if not frm < to:
            raise RuleViolation(f"有效区间非法: [{frm}, {to})")
        if not sku_rules:
            raise RuleViolation("规则版本至少包含一个 SKU")
        for sku, rule in sku_rules.items():
            rule.validate_registration(sku)
        created = _iso(now) if now else datetime(2000, 1, 1).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO s_rule_versions(version_id, supplier_id, effective_from,"
                    " effective_to, created_at) VALUES (?,?,?,?,?)",
                    (version_id, supplier_id, frm, to, created),
                )
                for sku, rule in sorted(sku_rules.items()):
                    self._conn.execute(
                        "INSERT INTO s_sku_rules(version_id, sku, pack_multiple, min_qty,"
                        " capacity) VALUES (?,?,?,?,?)",
                        (version_id, sku, rule.pack_multiple, rule.min_qty, rule.capacity),
                    )
                    for lower_bound, unit_price in rule.tiers:
                        self._conn.execute(
                            "INSERT INTO s_tiers(version_id, sku, lower_bound, unit_price)"
                            " VALUES (?,?,?,?)",
                            (version_id, sku, lower_bound, unit_price),
                        )
            except sqlite3.IntegrityError as exc:  # 版本号重复等
                raise StateConflict(f"规则版本 {version_id} 已存在或数据冲突: {exc}") from exc
        return {"version_id": version_id, "supplier_id": supplier_id,
                "effective_from": frm, "effective_to": to,
                "skus": sorted(sku_rules)}

    def load_rules(self, version_id: str) -> dict[str, SkuRule]:
        """从原始表重建某版本的全部 SKU 规则。"""
        with self._lock:
            head = self._conn.execute(
                "SELECT 1 FROM s_rule_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if head is None:
                raise NotFound(f"规则版本不存在: {version_id}")
            rules: dict[str, SkuRule] = {}
            rows = self._conn.execute(
                "SELECT version_id, sku, pack_multiple, min_qty, capacity"
                " FROM s_sku_rules WHERE version_id=? ORDER BY sku",
                (version_id,),
            ).fetchall()
            for r in rows:
                tiers = [
                    (t["lower_bound"], t["unit_price"])
                    for t in self._conn.execute(
                        "SELECT lower_bound, unit_price FROM s_tiers"
                        " WHERE version_id=? AND sku=? ORDER BY lower_bound",
                        (version_id, r["sku"]),
                    ).fetchall()
                ]
                rules[r["sku"]] = SkuRule(
                    pack_multiple=r["pack_multiple"],
                    min_qty=r["min_qty"],
                    capacity=r["capacity"],
                    tiers=tiers,
                )
            return rules

    def active_version(self, supplier_id: str, now: str | datetime) -> str:
        """返回时刻 now 处于有效区间内的规则版本（重叠时取最新登记的一个）。"""
        ts = _iso(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT version_id FROM s_rule_versions WHERE supplier_id=?"
                " AND effective_from <= ? AND ? < effective_to"
                " ORDER BY created_at DESC, version_id DESC LIMIT 1",
                (supplier_id, ts, ts),
            ).fetchone()
        if row is None:
            raise NotFound(f"供应商 {supplier_id} 在 {ts} 没有有效规则版本")
        return row["version_id"]

    # ------------------------------------------------------------------ 报价
    def quote(
        self,
        request_no: str,
        supplier_id: str,
        items: list[dict[str, Any]],
        now: str | datetime,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """提交多 SKU 数量向量取得不可变报价。

        * 同 request_no + 同请求体：返回原报价（复读，不新建）；
        * 同 request_no + 不同请求体：抛 :class:`DuplicateRequest`；
        * 任一 SKU 违反包装/最低量/上限：抛 :class:`RuleViolation`，不生成报价。
        """
        ts = _iso(now)
        if not items:
            raise RuleViolation("报价请求至少包含一个 SKU 行")
        normalized = sorted((str(i["sku"]), int(i["qty"])) for i in items)
        if any(q < 0 for _, q in normalized):
            raise RuleViolation("采购数量不能为负")
        request_hash = _canonical_request(supplier_id, items)

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM s_quotes WHERE request_no=?", (request_no,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DuplicateRequest(
                        f"请求号 {request_no} 已用于不同请求体",
                        details={"request_no": request_no},
                    )
                return self._quote_dict(existing)

            if version_id is None:
                version_id = self.active_version(supplier_id, now)
            head = self._conn.execute(
                "SELECT * FROM s_rule_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if head is None:
                raise NotFound(f"规则版本不存在: {version_id}")
            if head["supplier_id"] != supplier_id:
                raise RuleViolation(
                    f"版本 {version_id} 不属于供应商 {supplier_id}")
            rules = self.load_rules(version_id)

            line_results: list[dict[str, Any]] = []
            total = 0
            violations: list[dict[str, Any]] = []
            for sku, qty in normalized:
                rule = rules.get(sku)
                if rule is None:
                    violations.append({"sku": sku, "qty": qty,
                                       "error": "该版本未登记此 SKU"})
                    continue
                try:
                    rule.check_purchase(qty)
                except RuleViolation as exc:
                    violations.append({"sku": sku, "qty": qty, "error": exc.message})
                    continue
                unit_price = rule.applicable_unit_price(qty)
                line_price = rule.line_price(qty)
                total += line_price
                line_results.append({
                    "sku": sku, "qty": qty, "ok": True,
                    "unit_price": unit_price, "line_price": line_price,
                    "tier_lower": max(
                        (lb for lb, _ in rule.tiers if lb <= qty), default=0),
                })
            if violations:
                raise RuleViolation(
                    "报价请求存在不合法行", details={"violations": violations})

            quote_no = f"Q-{request_no}"
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO s_quotes(quote_no, request_no, supplier_id, version_id,"
                    " request_hash, items_json, line_results_json, total_price, status,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (quote_no, request_no, supplier_id, version_id, request_hash,
                     json.dumps(normalized, ensure_ascii=False),
                     json.dumps(line_results, ensure_ascii=False), total,
                     "OPEN", ts),
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise StateConflict(f"报价落库冲突: {exc}") from exc
            row = self._conn.execute(
                "SELECT * FROM s_quotes WHERE quote_no=?", (quote_no,)).fetchone()
            return self._quote_dict(row)

    def accept(
        self,
        quote_no: str,
        commitment_no: str,
        now: str | datetime,
    ) -> dict[str, Any]:
        """接受报价建立承诺。

        事务内重新核对：报价仍 OPEN、当前时刻仍在版本窗口内、同版本各 SKU 累计
        已接受承诺 + 本次数量不超过可供上限。任一不满足则回滚，不产生承诺。
        """
        ts = _iso(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                quote = self._conn.execute(
                    "SELECT * FROM s_quotes WHERE quote_no=?", (quote_no,)
                ).fetchone()
                if quote is None:
                    raise NotFound(f"报价不存在: {quote_no}")
                if quote["status"] == "ACCEPTED":
                    if quote["commitment_no"] != commitment_no:
                        raise StateConflict(
                            f"报价 {quote_no} 已接受为 {quote['commitment_no']}，"
                            f"不能改用承诺号 {commitment_no}")
                    self._conn.execute("ROLLBACK")  # 幂等复读：无写入，结束事务
                    return self.get_commitment(commitment_no)
                if quote["status"] != "OPEN":
                    raise StateConflict(f"报价 {quote_no} 状态为 {quote['status']}，不可接受")

                window = self._conn.execute(
                    "SELECT effective_from, effective_to FROM s_rule_versions"
                    " WHERE version_id=?",
                    (quote["version_id"],),
                ).fetchone()
                if not (window["effective_from"] <= ts < window["effective_to"]):
                    raise QuoteExpired(
                        f"报价 {quote_no} 已过期：当前 {ts}，有效窗口 "
                        f"[{window['effective_from']}, {window['effective_to']})",
                        details={"quote_no": quote_no, "now": ts,
                                 "effective_from": window["effective_from"],
                                 "effective_to": window["effective_to"]})

                items = json.loads(quote["items_json"])
                rules = self.load_rules(quote["version_id"])
                # 重新核剩余额度：同规则版本下全部已接受承诺的累计件数
                for sku, qty in items:
                    if qty == 0:
                        continue
                    used = self._conn.execute(
                        "SELECT COALESCE(SUM(cl.qty),0) AS used"
                        " FROM s_commitment_lines cl"
                        " JOIN s_commitments c ON c.commitment_no=cl.commitment_no"
                        " WHERE c.version_id=? AND cl.sku=?",
                        (quote["version_id"], sku),
                    ).fetchone()["used"]
                    cap = rules[sku].capacity
                    if used + qty > cap:
                        raise OverCapacity(
                            f"SKU {sku} 额度不足：已接受 {used} + 本次 {qty} > 上限 {cap}",
                            details={"sku": sku, "used": used, "qty": qty, "capacity": cap})

                line_rows = json.loads(quote["line_results_json"])
                self._conn.execute(
                    "INSERT INTO s_commitments(commitment_no, quote_no, supplier_id,"
                    " version_id, request_json, total_price, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (commitment_no, quote_no, quote["supplier_id"],
                     quote["version_id"], quote["items_json"], quote["total_price"], ts),
                )
                for line in line_rows:
                    self._conn.execute(
                        "INSERT INTO s_commitment_lines(commitment_no, sku, qty, unit_price)"
                        " VALUES (?,?,?,?)",
                        (commitment_no, line["sku"], line["qty"],
                         line["unit_price"]),
                    )
                updated = self._conn.execute(
                    "UPDATE s_quotes SET status='ACCEPTED', accepted_at=?,"
                    " commitment_no=? WHERE quote_no=? AND status='OPEN'",
                    (ts, commitment_no, quote_no),
                )
                if updated.rowcount != 1:
                    raise StateConflict(f"报价 {quote_no} 在接受途中被并发改动")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self.get_commitment(commitment_no)

    # ------------------------------------------------------------------ 查询
    def get_quote(self, quote_no: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM s_quotes WHERE quote_no=?", (quote_no,)).fetchone()
        if row is None:
            raise NotFound(f"报价不存在: {quote_no}")
        return self._quote_dict(row)

    def get_commitment(self, commitment_no: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM s_commitments WHERE commitment_no=?",
                (commitment_no,)).fetchone()
            if row is None:
                raise NotFound(f"承诺不存在: {commitment_no}")
            lines = [
                {"sku": r["sku"], "qty": r["qty"], "unit_price": r["unit_price"],
                 "line_price": r["qty"] * r["unit_price"]}
                for r in self._conn.execute(
                    "SELECT sku, qty, unit_price FROM s_commitment_lines"
                    " WHERE commitment_no=? ORDER BY sku", (commitment_no,))
            ]
        return {
            "commitment_no": row["commitment_no"],
            "quote_no": row["quote_no"],
            "supplier_id": row["supplier_id"],
            "version_id": row["version_id"],
            "items": json.loads(row["request_json"]),
            "lines": lines,
            "total_price": row["total_price"],
            "created_at": row["created_at"],
        }

    def remaining_capacity(self, version_id: str, sku: str) -> int:
        """某版本某 SKU 当前剩余可供额度（基于已接受承诺的原始记录累计）。"""
        with self._lock:
            cap_row = self._conn.execute(
                "SELECT capacity FROM s_sku_rules WHERE version_id=? AND sku=?",
                (version_id, sku),
            ).fetchone()
            if cap_row is None:
                raise NotFound(f"规则 {version_id} 下无 SKU {sku}")
            used = self._conn.execute(
                "SELECT COALESCE(SUM(cl.qty),0) AS used"
                " FROM s_commitment_lines cl"
                " JOIN s_commitments c ON c.commitment_no=cl.commitment_no"
                " WHERE c.version_id=? AND cl.sku=?",
                (version_id, sku),
            ).fetchone()["used"]
        return cap_row["capacity"] - used

    def list_commitments(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT commitment_no FROM s_commitments ORDER BY commitment_no"
            ).fetchall()
        return [self.get_commitment(r["commitment_no"]) for r in rows]

    def _quote_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "quote_no": row["quote_no"],
            "request_no": row["request_no"],
            "supplier_id": row["supplier_id"],
            "version_id": row["version_id"],
            "items": json.loads(row["items_json"]),
            "line_results": json.loads(row["line_results_json"]),
            "total_price": row["total_price"],
            "status": row["status"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
            "commitment_no": row["commitment_no"],
        }
