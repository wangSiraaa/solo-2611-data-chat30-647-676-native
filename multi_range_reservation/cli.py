"""Command-line entry points: range, reserve, replace, cancel, query, verify.

All output is JSON on stdout.  Exit code 0 means success; 2 means the request
was rejected (conflict, invalid input, stale request identity, ...).  Range
subcommands never touch the database, and repository subcommands never
implement set algebra themselves -- they delegate to the range service.

Range values may be passed as:

* compact text: ``"[1,3) (5,8]"`` (quote it in the shell),
* a JSON array of half-open pairs: ``'[[1,3],[5,9]]'`` -- the ``--half-open``
  flag distinguishes this from segment tuples,
* a JSON list of segment objects with ``lower``/``upper``/inclusion flags,
* a JSON object with ``"ranges"``/``"segments"``/``"half_open"``.

Examples::

    python -m multi_range_reservation.cli range normalize --value '"[1,3) [3,5)"'
    python -m multi_range_reservation.cli range union "[1,2)" "[2,3)"
    python -m multi_range_reservation.cli reserve --db r.sqlite \\
        --request-id r1 --resource room --reservation-id b1 --owner alice \\
        --ranges "[1,5)"
    python -m multi_range_reservation.cli query --db r.sqlite \\
        --resource room --ranges "[3,8)"
    python -m multi_range_reservation.cli verify --db r.sqlite
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .ranges import (
    RangeError,
    RangeValue,
    difference,
    intersection,
    intersects,
    normalize,
    union,
)
from .repository import (
    OccupancyConflict,
    RequestMismatchError,
    ReservationError,
    ReservationExists,
    ReservationNotFound,
    ReservationStateError,
    ReservationStore,
    ValidationError,
)


def _load_value(raw: str, *, half_open: bool = False) -> object:
    """Interpret a CLI value as text, JSON, or ``@file`` contents."""

    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8").strip()
    text = raw.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text  # compact range notation
    if half_open and isinstance(value, (list, tuple)):
        return {"half_open": value}
    return value


def _range_value(raw: str, *, half_open: bool = False) -> RangeValue:
    return normalize(_load_value(raw, half_open=half_open))


def _emit(payload: Any, *, code: int = 0) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return code


def _range_value_json(value: RangeValue) -> dict[str, Any]:
    return {
        "half_open": value.as_pairs(),
        "text": value.to_text(),
        "empty": value.is_empty,
    }


# --------------------------------------------------------------------------
# range subcommands
# --------------------------------------------------------------------------
def _cmd_range_normalize(args: argparse.Namespace) -> int:
    value = _range_value(args.value, half_open=args.half_open)
    return _emit({"command": "normalize", "result": _range_value_json(value)})


def _two_values(args: argparse.Namespace) -> tuple[RangeValue, RangeValue]:
    return (
        _range_value(args.left, half_open=args.half_open),
        _range_value(args.right, half_open=args.half_open),
    )


def _cmd_range_union(args: argparse.Namespace) -> int:
    values = [_range_value(v, half_open=args.half_open) for v in args.values]
    result = union(*values) if values else normalize()
    return _emit({"command": "union", "result": _range_value_json(result)})


def _cmd_range_intersect(args: argparse.Namespace) -> int:
    left, right = _two_values(args)
    result = intersection(left, right)
    return _emit({"command": "intersection", "result": _range_value_json(result)})


def _cmd_range_difference(args: argparse.Namespace) -> int:
    left, right = _two_values(args)
    result = difference(left, right)
    return _emit({"command": "difference", "result": _range_value_json(result)})


def _cmd_range_contains(args: argparse.Namespace) -> int:
    value = _range_value(args.value, half_open=args.half_open)
    return _emit(
        {"command": "contains", "point": args.point, "result": value.contains(args.point)}
    )


def _cmd_range_intersects(args: argparse.Namespace) -> int:
    left, right = _two_values(args)
    return _emit(
        {
            "command": "intersects",
            "result": intersects(left, right),
            "intersection": _range_value_json(intersection(left, right)),
        }
    )


# --------------------------------------------------------------------------
# repository subcommands
# --------------------------------------------------------------------------
def _store(args: argparse.Namespace) -> ReservationStore:
    return ReservationStore(args.db)


def _ranges_arg(args: argparse.Namespace) -> object:
    if args.ranges is None:
        raise ValidationError("--ranges is required and must be non-empty")
    return _load_value(args.ranges, half_open=args.half_open)


def _cmd_reserve(args: argparse.Namespace) -> int:
    response = _store(args).create(
        args.request_id, args.resource, args.reservation_id, args.owner, _ranges_arg(args)
    )
    return _emit(response)


def _cmd_replace(args: argparse.Namespace) -> int:
    response = _store(args).replace(
        args.request_id, args.resource, args.reservation_id, args.owner, _ranges_arg(args)
    )
    return _emit(response)


def _cmd_cancel(args: argparse.Namespace) -> int:
    response = _store(args).cancel(
        args.request_id, args.resource, args.reservation_id
    )
    return _emit(response)


def _cmd_query(args: argparse.Namespace) -> int:
    response = _store(args).query(args.resource, _ranges_arg(args))
    return _emit(response, code=0 if response["available"] else 3)


def _cmd_verify(args: argparse.Namespace) -> int:
    report = _store(args).verify()
    return _emit(report, code=0 if report["ok"] else 1)


def _cmd_get(args: argparse.Namespace) -> int:
    reservation = _store(args).get(args.resource, args.reservation_id)
    if reservation is None:
        return _emit({"error": "not_found"}, code=2)
    return _emit(reservation)


def _cmd_list(args: argparse.Namespace) -> int:
    resp = _store(args).list_reservations(
        resource=args.resource, status=args.status
    )
    return _emit({"reservations": resp})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-range-reservation",
        description="Canonical integer multi-range algebra and reservation repository.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    range_parser = sub.add_parser("range", help="standalone range algebra")
    range_sub = range_parser.add_subparsers(dest="range_command", required=True)

    def add_range_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--half-open", action="store_true", help="treat JSON arrays as [lo, hi) pairs")

    p = range_sub.add_parser("normalize")
    p.add_argument("--value", required=True)
    add_range_common(p)
    p.set_defaults(func=_cmd_range_normalize)

    p = range_sub.add_parser("union")
    p.add_argument("values", nargs="+")
    add_range_common(p)
    p.set_defaults(func=_cmd_range_union)

    for name, func in (
        ("intersect", _cmd_range_intersect),
        ("intersection", _cmd_range_intersect),
        ("difference", _cmd_range_difference),
        ("intersects", _cmd_range_intersects),
    ):
        p = range_sub.add_parser(name)
        p.add_argument("--left", required=True)
        p.add_argument("--right", required=True)
        add_range_common(p)
        p.set_defaults(func=func)

    p = range_sub.add_parser("contains")
    p.add_argument("--value", required=True)
    p.add_argument("--point", required=True, type=int)
    add_range_common(p)
    p.set_defaults(func=_cmd_range_contains)

    def add_store_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True)
        p.add_argument("--half-open", action="store_true")

    p = sub.add_parser("reserve", help="create a reservation")
    add_store_args(p)
    p.add_argument("--request-id", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--reservation-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--ranges", required=True)
    p.set_defaults(func=_cmd_reserve)

    p = sub.add_parser("replace", help="replace an active reservation occupation")
    add_store_args(p)
    p.add_argument("--request-id", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--reservation-id", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--ranges", required=True)
    p.set_defaults(func=_cmd_replace)

    p = sub.add_parser("cancel", help="cancel a reservation")
    add_store_args(p)
    p.add_argument("--request-id", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--reservation-id", required=True)
    p.set_defaults(func=_cmd_cancel)

    p = sub.add_parser("query", help="find conflicts inside a queried time set")
    add_store_args(p)
    p.add_argument("--resource", required=True)
    p.add_argument("--ranges", required=True)
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("verify", help="verify persisted invariants")
    p.add_argument("--db", required=True)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("get", help="fetch one reservation")
    p.add_argument("--db", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--reservation-id", required=True)
    p.set_defaults(func=_cmd_get)

    p = sub.add_parser("list", help="list reservations")
    p.add_argument("--db", required=True)
    p.add_argument("--resource")
    p.add_argument("--status", choices=("active", "cancelled"))
    p.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OccupancyConflict,) as exc:
        return _emit(exc.to_dict(), code=2)
    except (
        RequestMismatchError,
        ReservationExists,
        ReservationNotFound,
        ReservationStateError,
        ValidationError,
        RangeError,
    ) as exc:
        return _emit(
            {"error": type(exc).__name__, "message": str(exc)}, code=2
        )
    except ReservationError as exc:
        return _emit({"error": type(exc).__name__, "message": str(exc)}, code=2)


if __name__ == "__main__":
    sys.exit(main())
