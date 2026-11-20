"""Geometry: exact 2D placement with rotation, touching allowed."""
from coldchain.geometry import Rect, find_canonical_placement, validate_explicit


def test_area_sufficient_but_shape_infeasible():
    # 5x5 box (area 25), two 4x3 items (area 24 <= 25): cannot fit.
    assert find_canonical_placement([("A", 4, 3), ("B", 4, 3)], 5, 5) is None


def test_rotation_required():
    # 5x2 item only fits the 3x5 box when rotated 90 degrees.
    res = find_canonical_placement([("A", 5, 2)], 3, 5)
    assert res == (("A", 0, 0, 1),)
    # without rotation it would stick out
    assert validate_explicit([("A", 0, 0, 0, 5, 2)], 3, 5) != []


def test_touching_edges_allowed():
    res = find_canonical_placement([("A", 2, 2), ("B", 2, 2)], 4, 2)
    assert res is not None
    rects = {uid: Rect(x, y, 2, 2) for uid, x, y, _r in res}
    # they share an edge but do not overlap
    assert not rects["A"].overlaps(rects["B"])


def test_overlap_rejected_in_explicit_validation():
    v = validate_explicit([("A", 0, 0, 0, 2, 2), ("B", 1, 1, 0, 2, 2)], 4, 4)
    assert any("overlap" in msg for msg in v)


def test_negative_and_out_of_bounds_rejected():
    assert validate_explicit([("A", -1, 0, 0, 2, 2)], 4, 4) != []
    assert validate_explicit([("A", 3, 0, 0, 2, 2)], 4, 4) != []


def test_canonical_is_lexicographically_minimal():
    # two identical items: canonical placement puts first item at (0,0)
    res = find_canonical_placement([("B", 1, 2), ("A", 1, 2)], 3, 2)
    assert res[0][0] == "A" and res[0][1:3] == (0, 0)
    # determinism
    assert res == find_canonical_placement([("A", 1, 2), ("B", 1, 2)], 3, 2)


def test_single_item_exact_fit():
    assert find_canonical_placement([("A", 4, 4)], 4, 4) == (("A", 0, 0, 0),)
    assert find_canonical_placement([("A", 4, 5)], 4, 4) is None
