"""End-to-end demonstration of the range service and reservation repository.

Run from the repository root:

    python3 scripts/demo.py

It prints each notable outcome (normalization, holes, aliasing, replace across
old interval, cancel/reuse, request replay, and a two-connection race), then a
verify report.  The database is created in a temporary directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multi_range_reservation import (  # noqa: E402
    OccupancyConflict,
    RequestMismatchError,
    ReservationStore,
    difference,
    intersection,
    intersects,
    normalize,
    union,
)


def show(label: str, value: object) -> None:
    if hasattr(value, "to_text"):
        print(f"{label}: {value.to_text()}  {value.as_pairs()}")
    else:
        print(f"{label}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")


def main() -> int:
    print("== range algebra (no database) ==")
    show("(1,3]", normalize("(1,3]"))
    show("[2,4)", normalize("[2,4)"))
    show("(1,3] == [2,4)", normalize("(1,3]") == normalize("[2,4)"))
    show("[1,2) [3,4)", normalize("[1,2) [3,4)"))
    show("[1,4)      ", normalize("[1,4)"))
    show("contains 2", normalize("[1,2) [3,4)").contains(2))
    show("union", union("[1,2)", "[2,3)", "[5,6)"))
    show("intersection", intersection("[1,8)", "[3,5) [6,10)"))
    show("difference", difference("[1,8)", "[2,3) [4,5)"))
    show("intersects [1,2) [2,3)", intersects("[1,2)", "[2,3)"))

    with tempfile.TemporaryDirectory() as tmpdir:
        db = os.path.join(tmpdir, "demo.sqlite")
        store = ReservationStore(db)

        print("\n== reservations ==")
        show("create a [1,2) [3,4)", store.create("d1", "room", "a", "alice", "[1,2) [3,4)"))
        print("create b (2,3] -> conflict:")
        try:
            store.create("d2", "room", "b", "bob", "(2,3]")
        except OccupancyConflict as exc:
            show("  conflict", exc.to_dict())

        show("create b (4,6]", store.create("d3", "room", "b", "bob", "(4,6]"))
        show("query [2,5)", store.query("room", "[2,5)"))

        print("\nreplace a to [3,5) (crosses its own old [3,4)):")
        show("  replace", store.replace("d4", "room", "a", "alice", "[3,5)"))
        print("same request_id with equivalent segmentation replays:")
        show("  replay", store.replace("d4", "room", "a", "alice", "[3,4) [4,5)"))
        print("same request_id with different owner is rejected:")
        try:
            store.replace("d4", "room", "a", "mallory", "[3,5)")
        except RequestMismatchError as exc:
            print(f"  rejected: {exc}")

        print("\ncancel b, then reuse its freed [5,7) with reservation c:")
        show("  cancel", store.cancel("d5", "room", "b"))
        show("  create c", store.create("d6", "room", "c", "carol", "[5,7)"))
        print("recreating reservation b under the same id is refused (identity kept):")
        try:
            store.create("d7", "room", "b", "bob", "[8,9)")
        except Exception as exc:  # ReservationExists
            print(f"  refused: {type(exc).__name__}: {exc}")

        print("\n== two connections race for the last free point ==")
        store.create("seed", "race-room", "base", "system", "[0,99)")
        barrier = threading.Barrier(2)
        results: list[str] = []
        lock = threading.Lock()

        def contender(index: int) -> None:
            conn_store = ReservationStore(db)
            barrier.wait()
            try:
                conn_store.create(
                    f"race-{index}", "race-room", f"t{index}", "u", "[99,100)"
                )
            except OccupancyConflict:
                with lock:
                    results.append("conflict")
            else:
                with lock:
                    results.append("success")

        threads = [threading.Thread(target=contender, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        print("  outcomes:", sorted(results))

        print("\n== verify ==")
        show("verify", store.verify())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
