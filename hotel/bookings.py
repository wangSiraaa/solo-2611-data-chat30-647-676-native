"""Versioned subscription book.

This module owns booking records only.  It knows nothing about rooms; it
produces an immutable booking slice at a caller-chosen sequence number.  The
fulfilment module consumes both a state product and one of these slices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database, utcnow
from .errors import (
    NotFound,
    RecordRevoked,
    RevisionConflict,
    StaleRevision,
    ValidationError,
)
from .identity import canonical_json, digest


_ACTIVE_SQL = """
SELECT e.booking_id, e.revision, e.arrival, e.departure, e.party_size,
       e.min_level, e.max_level
FROM booking_events AS e
JOIN (
    SELECT booking_id, MAX(seq) AS max_seq
    FROM booking_events
    WHERE seq <= ?
    GROUP BY booking_id
) AS h ON e.booking_id = h.booking_id AND e.seq = h.max_seq
WHERE e.action = 'active'
ORDER BY e.booking_id
"""


@dataclass(frozen=True)
class BookingRecord:
    booking_id: str
    revision: int
    arrival: int
    departure: int
    party_size: int
    min_level: int
    max_level: int

    @property
    def nights(self) -> int:
        return self.departure - self.arrival

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "revision": self.revision,
            "arrival": self.arrival,
            "departure": self.departure,
            "party_size": self.party_size,
            "min_level": self.min_level,
            "max_level": self.max_level,
        }


@dataclass(frozen=True)
class BookingSlice:
    """Immutable, order-independent set of active bookings at one watermark.

    ``records`` is sorted by ``booking_id`` so downstream optimisation never
    inherits caller submission order.
    """

    watermark: int | None
    records: tuple[BookingRecord, ...]
    digest: str
    slice_json: str

    def booking_ids(self) -> list[str]:
        return [record.booking_id for record in self.records]

    def by_id(self) -> dict[str, BookingRecord]:
        return {record.booking_id: record for record in self.records}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "booking_slice",
            "watermark": self.watermark,
            "bookings": [record.to_dict() for record in self.records],
        }


class BookingStore:
    """Ingest bookings with monotonic revisions and immutable history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- sequence / revision ----------------------------------------------

    def current_watermark(self) -> int:
        return int(self.db.scalar("SELECT MAX(seq) FROM booking_events", default=0))

    @staticmethod
    def _content(
        booking_id: str,
        revision: int,
        action: str,
        arrival: int | None,
        departure: int | None,
        party_size: int | None,
        min_level: int | None,
        max_level: int | None,
    ) -> dict[str, Any]:
        return {
            "kind": "booking",
            "booking_id": booking_id,
            "revision": int(revision),
            "action": action,
            "arrival": None if arrival is None else int(arrival),
            "departure": None if departure is None else int(departure),
            "party_size": None if party_size is None else int(party_size),
            "min_level": None if min_level is None else int(min_level),
            "max_level": None if max_level is None else int(max_level),
        }

    def commit(
        self,
        booking_id: str,
        revision: int,
        action: str,
        arrival: int | None = None,
        departure: int | None = None,
        party_size: int | None = None,
        min_level: int | None = None,
        max_level: int | None = None,
    ) -> dict[str, Any]:
        """Append a booking event.

        * same ``(booking_id, revision)`` with identical content: no-op replay;
        * same revision with different content: rejected;
        * older revision than the head: rejected;
        * modification after cancellation: rejected (no resurrection).
        """

        if not isinstance(booking_id, str) or not booking_id.strip():
            raise ValidationError("booking_id must be a non-empty string")
        revision = int(revision)
        if revision < 1:
            raise ValidationError("revision must be a positive integer")
        action = str(action).lower()
        if action not in {"active", "cancelled"}:
            raise ValidationError("action must be 'active' or 'cancelled'")

        if action == "active":
            if None in (arrival, departure, party_size, min_level, max_level):
                raise ValidationError(
                    "active bookings require arrival, departure, party_size,"
                    " min_level and max_level"
                )
            arrival, departure = int(arrival), int(departure)
            party_size, min_level, max_level = (
                int(party_size), int(min_level), int(max_level)
            )
            if arrival >= departure:
                raise ValidationError("booking must satisfy arrival < departure")
            if party_size < 1:
                raise ValidationError("party_size must be >= 1")
            if min_level < 1:
                raise ValidationError("min_level must be >= 1")
            if max_level < min_level:
                raise ValidationError("max_level must be >= min_level")
        elif action == "cancelled":
            # For explicit cancels, content fields are inherited from the head
            # when omitted; if supplied they must match to keep the receipt
            # unambiguous.  A cancel is never allowed to change the stay.
            head = self.db.one(
                "SELECT * FROM booking_head WHERE booking_id = ?", (booking_id,)
            )
            if head is None:
                raise NotFound(f"booking {booking_id!r} does not exist")
            for supplied, column in (
                (arrival, "arrival"),
                (departure, "departure"),
                (party_size, "party_size"),
                (min_level, "min_level"),
                (max_level, "max_level"),
            ):
                if supplied is not None and int(supplied) != int(head[column]):
                    raise ValidationError(
                        f"a cancel cannot change {column}; omit it or repeat the head value"
                    )
            arrival = int(head["arrival"])
            departure = int(head["departure"])
            party_size = int(head["party_size"])
            min_level = int(head["min_level"])
            max_level = int(head["max_level"])

        content = self._content(
            booking_id, revision, action, arrival, departure,
            party_size, min_level, max_level,
        )
        content_hash = digest(content)

        with self.db.tx():
            existing = self.db.one(
                "SELECT * FROM booking_events WHERE booking_id = ? AND revision = ?",
                (booking_id, revision),
            )
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise RevisionConflict(
                        f"booking {booking_id!r} revision {revision} already"
                        " committed with different content"
                    )
                return self._receipt(existing, replayed=True)

            head = self.db.one(
                "SELECT * FROM booking_head WHERE booking_id = ?", (booking_id,)
            )
            if head is None:
                if revision != 1:
                    raise ValidationError("a new booking must start at revision 1")
            else:
                head_revision = int(head["revision"])
                if revision < head_revision:
                    raise StaleRevision(
                        f"booking {booking_id!r} revision {revision} is older"
                        f" than head revision {head_revision}"
                    )
                if head["action"] == "cancelled":
                    raise RecordRevoked(
                        f"booking {booking_id!r} is cancelled and cannot be revived"
                    )

            self.db.execute(
                "INSERT INTO booking_events(booking_id, revision, action, arrival,"
                " departure, party_size, min_level, max_level, content_hash,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    booking_id, revision, action, arrival, departure,
                    party_size, min_level, max_level, content_hash, utcnow(),
                ),
            )
            self.db.execute(
                "INSERT INTO booking_head(booking_id, revision, action, arrival,"
                " departure, party_size, min_level, max_level, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(booking_id) DO UPDATE SET"
                " revision=excluded.revision, action=excluded.action,"
                " arrival=excluded.arrival, departure=excluded.departure,"
                " party_size=excluded.party_size, min_level=excluded.min_level,"
                " max_level=excluded.max_level, content_hash=excluded.content_hash",
                (
                    booking_id, revision, action, arrival, departure,
                    party_size, min_level, max_level, content_hash,
                ),
            )
            row = self.db.one(
                "SELECT * FROM booking_events WHERE booking_id = ? AND revision = ?",
                (booking_id, revision),
            )
            assert row is not None
            return self._receipt(row, replayed=False)

    def submit(
        self,
        booking_id: str,
        revision: int,
        arrival: int,
        departure: int,
        party_size: int,
        min_level: int,
        max_level: int,
    ) -> dict[str, Any]:
        return self.commit(
            booking_id, revision, "active", arrival, departure,
            party_size, min_level, max_level,
        )

    def correct(
        self,
        booking_id: str,
        revision: int,
        arrival: int,
        departure: int,
        party_size: int,
        min_level: int,
        max_level: int,
    ) -> dict[str, Any]:
        return self.commit(
            booking_id, revision, "active", arrival, departure,
            party_size, min_level, max_level,
        )

    def cancel(self, booking_id: str, revision: int) -> dict[str, Any]:
        return self.commit(booking_id, revision, "cancelled")

    @staticmethod
    def _receipt(row: Any, replayed: bool) -> dict[str, Any]:
        return {
            "booking_id": row["booking_id"],
            "revision": int(row["revision"]),
            "action": row["action"],
            "arrival": int(row["arrival"]),
            "departure": int(row["departure"]),
            "party_size": int(row["party_size"]),
            "min_level": int(row["min_level"]),
            "max_level": int(row["max_level"]),
            "seq": int(row["seq"]),
            "replayed": replayed,
        }

    # -- reading -----------------------------------------------------------

    def history(self, booking_id: str) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM booking_events WHERE booking_id = ? ORDER BY revision",
            (booking_id,),
        )
        if not rows:
            raise NotFound(f"booking {booking_id!r} does not exist")
        return [
            {
                **self._receipt(row, replayed=False),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def head(self, booking_id: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM booking_head WHERE booking_id = ?", (booking_id,)
        )
        if row is None:
            raise NotFound(f"booking {booking_id!r} does not exist")
        return {
            "booking_id": row["booking_id"],
            "revision": int(row["revision"]),
            "action": row["action"],
            "arrival": int(row["arrival"]),
            "departure": int(row["departure"]),
            "party_size": int(row["party_size"]),
            "min_level": int(row["min_level"]),
            "max_level": int(row["max_level"]),
        }

    def snapshot(self, watermark: int | None = None) -> BookingSlice:
        """Return the immutable booking slice as of *watermark*.

        ``None`` means "current".  A slice is fully determined by the sequence
        watermark, so it is reproducible after later corrections arrive.
        """

        current = self.current_watermark()
        if watermark is None:
            watermark = current
        watermark = int(watermark)
        if watermark < 0 or watermark > current:
            raise NotFound(f"booking watermark {watermark} is outside 0..{current}")

        rows = self.db.all(_ACTIVE_SQL, (watermark,))
        records = tuple(
            BookingRecord(
                booking_id=row["booking_id"],
                revision=int(row["revision"]),
                arrival=int(row["arrival"]),
                departure=int(row["departure"]),
                party_size=int(row["party_size"]),
                min_level=int(row["min_level"]),
                max_level=int(row["max_level"]),
            )
            for row in rows
        )
        payload = canonical_json(
            {
                "kind": "booking_slice",
                "watermark": watermark,
                "bookings": [record.to_dict() for record in records],
            }
        )
        return BookingSlice(
            watermark=watermark,
            records=records,
            digest=digest(payload),
            slice_json=payload,
        )
