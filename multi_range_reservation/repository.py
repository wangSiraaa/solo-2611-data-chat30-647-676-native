"""SQLite-backed reservation repository.

Design notes
------------
* **Canonical semantics only.** Every range value that enters the store is
  passed through ``range_service.normalize`` at write time, and the service's
  *produced* canonical half-open pairs are persisted.  Reads reconstruct those
  exact pairs as a ``RangeValue`` (a value read back from storage is not fed
  through input normalization again, which would compound a non-idempotent
  service bug).  The contract for an injected service is therefore: pure,
  idempotent ``normalize`` returning an already-canonical ``RangeValue``
  unchanged.  Algebra (intersection, union, ...) is applied to the
  reconstructed values, so conflict decisions consume the produced semantics
  rather than the caller's raw segmentation.
* **Transactions.** Every mutation runs in a real ``BEGIN IMMEDIATE``
  transaction on SQLite.  The conflict check and the write are inside that one
  transaction, so no other connection can commit between them.  ``replace``
  updates the existing row in place: there is never a committed or even
  visible window where the old occupation was deleted and the new one not yet
  inserted.
* **Request identity.** A successful request stores its canonical request
  fingerprint and its exact response JSON.  Replaying the same request_id with
  the same canonical payload returns the stored response verbatim; a different
  payload is rejected.  Errors roll back, including the receipt insert, so a
  failed request consumes no identity.
* **Conflict strategy.** For each write we rescan the active rows of the one
  affected resource and intersect the candidate canonical value against each.
  At the required scale (<= 50 resources, <= 1000 reservations, <= 20 segments
  each) this is cheap, and it is *representation independent*: correctness does
  not depend on keeping an auxiliary interval index in sync across equivalent
  segmentations.  It also makes the update cost symmetric -- a replace costs
  one scan, not a delete-plus-rebuild of a shared index.  An interval index
  could be added later, but it would have to be rebuilt from canonical ranges
  in the same transaction to preserve these guarantees.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from .ranges import DEFAULT_RANGE_SERVICE, RangeValue

__all__ = [
    "OccupancyConflict",
    "RequestMismatchError",
    "ReservationError",
    "ReservationExists",
    "ReservationNotFound",
    "ReservationStateError",
    "ReservationStore",
    "ValidationError",
]

ACTIVE = "active"
CANCELLED = "cancelled"
STATUSES = (ACTIVE, CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id  TEXT    PRIMARY KEY,
    resource        TEXT    NOT NULL,
    owner           TEXT    NOT NULL,
    ranges_json     TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('active', 'cancelled')),
    seq             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_resource_status
    ON reservations(resource, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_seq
    ON reservations(seq);
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    action          TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    resource        TEXT NOT NULL,
    reservation_id  TEXT
);
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO metadata (key, value) VALUES ('next_seq', '1');
"""


class ReservationError(Exception):
    """Base class for repository failures."""


class ValidationError(ReservationError):
    """Invalid input such as a missing field or an empty occupation."""


class OccupancyConflict(ReservationError):
    """The requested occupation overlaps an active reservation."""

    def __init__(self, resource: str, conflicts: Sequence[Mapping[str, Any]]) -> None:
        self.resource = resource
        self.conflicts = [dict(item) for item in conflicts]
        summary = ", ".join(
            f"{item['reservation_id']} [{item['intersection']['text']}]"
            for item in self.conflicts
        )
        super().__init__(
            f"occupation conflict on resource {resource!r} with: {summary}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "occupancy_conflict",
            "resource": self.resource,
            "conflicts": self.conflicts,
        }


class RequestMismatchError(ReservationError):
    """A request_id was reused with a different canonical payload."""

    def __init__(self, request_id: str, action: str) -> None:
        self.request_id = request_id
        self.action = action
        super().__init__(
            f"request_id {request_id!r} was already used for a different request"
        )


class ReservationExists(ReservationError):
    def __init__(self, resource: str, reservation_id: str) -> None:
        self.resource = resource
        self.reservation_id = reservation_id
        super().__init__(
            f"reservation {reservation_id!r} already exists on resource {resource!r}"
        )


class ReservationNotFound(ReservationError):
    def __init__(self, resource: str, reservation_id: str) -> None:
        self.resource = resource
        self.reservation_id = reservation_id
        super().__init__(
            f"reservation {reservation_id!r} not found on resource {resource!r}"
        )


class ReservationStateError(ReservationError):
    pass


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _require_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    if len(value) > 256:
        raise ValidationError(f"{field} must be at most 256 characters")
    return value


def _ranges_payload(value: RangeValue) -> list[list[int]]:
    return [list(pair) for pair in value.pairs]


class ReservationStore:
    """Persistent reservation repository.

    ``range_service`` must provide ``normalize`` returning a canonical
    :class:`RangeValue`; by default the module-level range algebra is used.
    Injecting a service is how tests verify that the repository consumes range
    semantics rather than input segmentation.
    """

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 20.0,
        range_service: Any = DEFAULT_RANGE_SERVICE,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.ranges = range_service
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # -- connections -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None gives us explicit transaction control.
        conn = sqlite3.connect(
            self.path, timeout=self.timeout, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- input helpers ---------------------------------------------------
    def _occupation(self, value: object, field: str = "ranges") -> RangeValue:
        try:
            occupation = self.ranges.normalize(value)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid {field}: {exc}") from exc
        if not isinstance(occupation, RangeValue):
            raise ValidationError(
                f"range service returned {type(occupation).__name__}, expected RangeValue"
            )
        if occupation.is_empty:
            raise ValidationError(f"{field} must contain at least one integer")
        return occupation

    def _deserialize(self, raw: str) -> RangeValue:
        """Reconstruct a stored value from its produced canonical pairs.

        ``RangeValue`` validates and normalizes the pairs; because the stored
        value was already canonical, this is a lossless round-trip.  The
        service's algebra (not its input parser) is what acts on the result.
        """

        pairs = json.loads(raw)
        try:
            return RangeValue.from_half_open(pairs)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"corrupt stored occupation: {exc}") from exc

    # -- serialization helpers ------------------------------------------
    def _reservation_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        occupation = self._deserialize(row["ranges_json"])
        return {
            "resource": row["resource"],
            "reservation_id": row["reservation_id"],
            "owner": row["owner"],
            "status": row["status"],
            "occupation": occupation.to_dict(),
            "seq": row["seq"],
        }

    def _conflicts(
        self,
        conn: sqlite3.Connection,
        resource: str,
        candidate: RangeValue,
        *,
        exclude_reservation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM reservations "
            "WHERE resource = ? AND status = 'active'"
        )
        params: list[Any] = [resource]
        if exclude_reservation_id is not None:
            sql += " AND reservation_id <> ?"
            params.append(exclude_reservation_id)
        sql += " ORDER BY seq"
        conflicts: list[dict[str, Any]] = []
        for row in conn.execute(sql, params):
            other = self._deserialize(row["ranges_json"])
            overlap = self.ranges.intersection(candidate, other)
            if not overlap.is_empty:
                conflicts.append(
                    {
                        "resource": row["resource"],
                        "reservation_id": row["reservation_id"],
                        "owner": row["owner"],
                        "occupation": other.to_dict(),
                        "intersection": overlap.to_dict(),
                    }
                )
        return conflicts

    def _lookup_receipt(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        action: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT action, fingerprint, response_json FROM requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["fingerprint"] != fingerprint:
            raise RequestMismatchError(request_id, action)
        return json.loads(row["response_json"])

    def _store_receipt(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        action: str,
        fingerprint: str,
        payload: Mapping[str, Any],
        response: Mapping[str, Any],
        resource: str,
        reservation_id: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO requests "
            "(request_id, action, fingerprint, request_json, response_json, "
            " resource, reservation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                action,
                fingerprint,
                _canonical_payload(payload),
                _canonical_payload(response),
                resource,
                reservation_id,
            ),
        )

    def _next_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'next_seq'"
        ).fetchone()
        seq = int(row["value"])
        conn.execute(
            "UPDATE metadata SET value = ? WHERE key = 'next_seq'", (str(seq + 1),)
        )
        return seq

    # -- mutations -------------------------------------------------------
    def create(
        self,
        request_id: str,
        resource: str,
        reservation_id: str,
        owner: str,
        ranges: object,
    ) -> dict[str, Any]:
        request_id = _require_name(request_id, "request_id")
        resource = _require_name(resource, "resource")
        reservation_id = _require_name(reservation_id, "reservation_id")
        owner = _require_name(owner, "owner")
        occupation = self._occupation(ranges)
        payload = {
            "action": "create",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
            "owner": owner,
            "ranges": _ranges_payload(occupation),
        }
        fingerprint = _fingerprint(payload)

        with self._write_transaction() as conn:
            replay = self._lookup_receipt(
                conn, request_id, "create", fingerprint
            )
            if replay is not None:
                return replay

            existing = conn.execute(
                "SELECT resource FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                raise ReservationExists(resource, reservation_id)

            conflicts = self._conflicts(conn, resource, occupation)
            if conflicts:
                raise OccupancyConflict(resource, conflicts)

            seq = self._next_seq(conn)
            conn.execute(
                "INSERT INTO reservations "
                "(resource, reservation_id, owner, ranges_json, status, seq) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (
                    resource,
                    reservation_id,
                    owner,
                    json.dumps(_ranges_payload(occupation), separators=(",", ":")),
                    seq,
                ),
            )
            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            response = {
                "request_id": request_id,
                "action": "create",
                "status": "success",
                "reservation": self._reservation_dict(row),
            }
            self._store_receipt(
                conn,
                request_id,
                "create",
                fingerprint,
                payload,
                response,
                resource,
                reservation_id,
            )
            return response

    def replace(
        self,
        request_id: str,
        resource: str,
        reservation_id: str,
        owner: str,
        ranges: object,
    ) -> dict[str, Any]:
        request_id = _require_name(request_id, "request_id")
        resource = _require_name(resource, "resource")
        reservation_id = _require_name(reservation_id, "reservation_id")
        owner = _require_name(owner, "owner")
        occupation = self._occupation(ranges)
        payload = {
            "action": "replace",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
            "owner": owner,
            "ranges": _ranges_payload(occupation),
        }
        fingerprint = _fingerprint(payload)

        with self._write_transaction() as conn:
            replay = self._lookup_receipt(
                conn, request_id, "replace", fingerprint
            )
            if replay is not None:
                return replay

            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["resource"] != resource:
                raise ReservationNotFound(resource, reservation_id)
            if row["status"] != ACTIVE:
                raise ReservationStateError(
                    f"cannot replace reservation {reservation_id!r}: "
                    f"status is {row['status']!r}"
                )

            # Check against the *final* occupation, excluding this row, but do
            # not delete the old row first.  The update below is the only write.
            conflicts = self._conflicts(
                conn,
                resource,
                occupation,
                exclude_reservation_id=reservation_id,
            )
            if conflicts:
                raise OccupancyConflict(resource, conflicts)

            conn.execute(
                "UPDATE reservations SET owner = ?, ranges_json = ? "
                "WHERE reservation_id = ?",
                (
                    owner,
                    json.dumps(_ranges_payload(occupation), separators=(",", ":")),
                    reservation_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            response = {
                "request_id": request_id,
                "action": "replace",
                "status": "success",
                "reservation": self._reservation_dict(row),
            }
            self._store_receipt(
                conn,
                request_id,
                "replace",
                fingerprint,
                payload,
                response,
                resource,
                reservation_id,
            )
            return response

    def cancel(
        self,
        request_id: str,
        resource: str,
        reservation_id: str,
    ) -> dict[str, Any]:
        request_id = _require_name(request_id, "request_id")
        resource = _require_name(resource, "resource")
        reservation_id = _require_name(reservation_id, "reservation_id")
        payload = {
            "action": "cancel",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
        }
        fingerprint = _fingerprint(payload)

        with self._write_transaction() as conn:
            replay = self._lookup_receipt(
                conn, request_id, "cancel", fingerprint
            )
            if replay is not None:
                return replay

            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["resource"] != resource:
                raise ReservationNotFound(resource, reservation_id)

            if row["status"] == ACTIVE:
                conn.execute(
                    "UPDATE reservations SET status = 'cancelled' "
                    "WHERE reservation_id = ?",
                    (reservation_id,),
                )
                row = conn.execute(
                    "SELECT * FROM reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()

            response = {
                "request_id": request_id,
                "action": "cancel",
                "status": "success",
                "reservation": self._reservation_dict(row),
            }
            self._store_receipt(
                conn,
                request_id,
                "cancel",
                fingerprint,
                payload,
                response,
                resource,
                reservation_id,
            )
            return response

    # -- queries ---------------------------------------------------------
    def query(self, resource: str, ranges: object) -> dict[str, Any]:
        resource = _require_name(resource, "resource")
        try:
            requested = self.ranges.normalize(ranges)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid query ranges: {exc}") from exc
        if not isinstance(requested, RangeValue):
            raise ValidationError("range service did not return a RangeValue")

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT * FROM reservations "
                "WHERE resource = ? AND status = 'active' ORDER BY seq",
                (resource,),
            ).fetchall()
            conflicts: list[dict[str, Any]] = []
            for row in rows:
                occupation = self._deserialize(row["ranges_json"])
                overlap = self.ranges.intersection(requested, occupation)
                if not overlap.is_empty:
                    conflicts.append(
                        {
                            "resource": row["resource"],
                            "reservation_id": row["reservation_id"],
                            "owner": row["owner"],
                            "occupation": occupation.to_dict(),
                            "intersection": overlap.to_dict(),
                        }
                    )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

        return {
            "resource": resource,
            "query": requested.to_dict(),
            "conflict_count": len(conflicts),
            "available": not conflicts,
            "conflicts": conflicts,
        }

    def get(self, resource: str, reservation_id: str) -> dict[str, Any] | None:
        reservation = self.get_by_id(reservation_id)
        if reservation is None or reservation["resource"] != resource:
            return None
        return reservation

    def get_by_id(self, reservation_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else self._reservation_dict(row)

    def list_reservations(
        self, *, resource: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if resource is not None:
            clauses.append("resource = ?")
            params.append(resource)
        if status is not None:
            if status not in STATUSES:
                raise ValidationError(f"unknown status {status!r}")
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM reservations {where} ORDER BY seq", params
            ).fetchall()
        finally:
            conn.close()
        return [self._reservation_dict(row) for row in rows]

    # -- verification ----------------------------------------------------
    def verify(self) -> dict[str, Any]:
        """Cross-check persisted invariants and receipts.

        Returns a report rather than raising, so callers can inspect every
        problem at once.  The query path never relies on these checks; they
        exist to prove the stored state is consistent after crashes/tests.
        """

        errors: list[str] = []
        info: dict[str, Any] = {}
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            reservations = conn.execute(
                "SELECT * FROM reservations ORDER BY seq"
            ).fetchall()
            requests = conn.execute(
                "SELECT * FROM requests ORDER BY request_id"
            ).fetchall()
            meta = conn.execute(
                "SELECT value FROM metadata WHERE key = 'next_seq'"
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

        parsed: dict[str, RangeValue] = {}
        resource_of: dict[str, str] = {}
        active_by_resource: dict[str, list[tuple[sqlite3.Row, RangeValue]]] = {}
        normalized_equivalents = 0
        max_seq = 0

        for row in reservations:
            key = row["reservation_id"]
            try:
                stored = json.loads(row["ranges_json"])
                occupation = RangeValue.from_half_open(stored)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"{key}: unparseable ranges_json: {exc}")
                continue
            if not isinstance(occupation, RangeValue):
                errors.append(f"{key}: range service returned a non-RangeValue")
                continue
            parsed[key] = occupation
            resource_of[key] = row["resource"]
            max_seq = max(max_seq, row["seq"])
            if _ranges_payload(occupation) != stored:
                # A row can be non-canonical only if it was written outside
                # this repository (or before a fix).  It is not a failure: the
                # repository reconstructs the same point set from it.
                normalized_equivalents += 1
            if row["status"] not in STATUSES:
                errors.append(f"{key}: invalid status {row['status']!r}")
            if row["status"] == ACTIVE and occupation.is_empty:
                errors.append(f"{key}: active reservation has empty occupation")
            if row["status"] == ACTIVE:
                active_by_resource.setdefault(row["resource"], []).append(
                    (row, occupation)
                )

        for resource, entries in active_by_resource.items():
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    left = entries[i][1]
                    right = entries[j][1]
                    if self.ranges.intersects(left, right):
                        overlap = self.ranges.intersection(left, right)
                        errors.append(
                            f"resource {resource!r}: active reservations "
                            f"{entries[i][0]['reservation_id']!r} and "
                            f"{entries[j][0]['reservation_id']!r} overlap at "
                            f"{overlap.to_text()}"
                        )

        for row in requests:
            action = row["action"]
            try:
                payload = json.loads(row["request_json"])
                response = json.loads(row["response_json"])
            except json.JSONDecodeError as exc:
                errors.append(f"request {row['request_id']!r}: bad JSON: {exc}")
                continue
            if _fingerprint(payload) != row["fingerprint"]:
                errors.append(
                    f"request {row['request_id']!r}: fingerprint does not match payload"
                )
            if payload.get("action") != action:
                errors.append(
                    f"request {row['request_id']!r}: action/payload mismatch"
                )
            if response.get("action") != action or response.get("status") != "success":
                errors.append(
                    f"request {row['request_id']!r}: receipt is not a successful {action}"
                )
            if response.get("request_id") != row["request_id"]:
                errors.append(
                    f"request {row['request_id']!r}: receipt request_id mismatch"
                )
            reservation = response.get("reservation") or {}
            key = reservation.get("reservation_id")
            if action in ("create", "replace", "cancel") and key not in parsed:
                errors.append(
                    f"request {row['request_id']!r}: receipt references an unknown reservation {key!r}"
                )
            elif key in parsed and reservation.get("resource") != resource_of.get(key):
                errors.append(
                    f"request {row['request_id']!r}: receipt resource does not match reservation {key!r}"
                )

        if meta is None:
            errors.append("metadata next_seq is missing")
        elif int(meta["value"]) <= max_seq:
            errors.append(
                f"metadata next_seq {meta['value']} is not greater than max seq {max_seq}"
            )

        info.update(
            {
                "reservation_count": len(reservations),
                "active_count": sum(1 for r in reservations if r["status"] == ACTIVE),
                "cancelled_count": sum(
                    1 for r in reservations if r["status"] == CANCELLED
                ),
                "request_count": len(requests),
                "resources": len(active_by_resource),
                "normalized_equivalent_rows": normalized_equivalents,
            }
        )
        return {"ok": not errors, "errors": errors, "info": info}
