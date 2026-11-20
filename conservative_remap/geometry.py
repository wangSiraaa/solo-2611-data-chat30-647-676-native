"""Exact rational geometry for planar triangle meshes.

All geometric predicates needed by the mesh service and the remap warehouse are
evaluated in exact rational arithmetic.  A polygon is represented as a closed
ring of ``Fraction`` coordinate pairs with no repeated closing vertex.

The clipping routine is Sutherland-Hodgman restricted to a convex clip polygon.
Every source triangle and every query rectangle is convex, so that is all we
need.  Intersections are computed with a parameter that is structurally in
``[0, 1]`` before use, which keeps the result numerically well behaved even when
a vertex lies almost on a clip edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
from typing import Iterable, Sequence

Point = tuple[Fraction, Fraction]
Polygon = list[Point]


class GeometryError(ValueError):
    """Raised when a polygon or snapshot cannot be interpreted geometrically."""


class MeshValidationError(GeometryError):
    """A mesh snapshot violates one of the declared structural invariants."""


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------


def to_fraction(value) -> Fraction:
    """Convert int/float/str/Fraction to an exact Fraction.

    Floats are converted through their ``str`` form so that a vertex written as
    ``0.1`` maps to ``1/10`` rather than to the closest binary double.  This
    makes float input deterministic and keeps area tolerances meaningful.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise GeometryError("bool is not a coordinate")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise GeometryError(f"non-finite coordinate: {value!r}")
        return Fraction(str(value))
    if isinstance(value, str):
        return Fraction(value)
    raise GeometryError(f"unsupported coordinate type: {type(value).__name__}")


def _clean_num(value: Fraction):
    """Return an int when the Fraction is integral, else the Fraction."""
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    return value


def id_token(value) -> str:
    """A stable, type-preserving key for an int/str cell or vertex id."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise GeometryError(
            f"cell and vertex ids must be int or str, got {type(value).__name__}"
        )
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def id_from_token(token: str):
    return json.loads(token)


def fraction_text(value: Fraction) -> str:
    f = to_fraction(value)
    return f"{f.numerator}/{f.denominator}"


def fraction_from_text(text: str) -> Fraction:
    return Fraction(text)



def signed_area(ring: Sequence[Point]) -> Fraction:
    """Twice the signed area of a ring (positive for counter-clockwise)."""
    if len(ring) < 3:
        return Fraction(0)
    total = Fraction(0)
    x0, y0 = ring[0]
    for i in range(1, len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    return total


def area2(ring: Sequence[Point]) -> Fraction:
    """Twice the (unsigned) area of a ring."""
    return abs(signed_area(ring))


def polygon_area(ring: Sequence[Point]) -> Fraction:
    """Unsigned area of a ring."""
    return area2(ring) / 2


# ---------------------------------------------------------------------------
# Mesh validation report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeometryPolicy:
    """Declared convention and tolerances for a mesh/run.

    Geometry is exact rational, so ``area_tolerance`` and ``coverage_tolerance``
    default to zero.  They are exposed because callers that build snapshots from
    their own floating point data may want to declare a small acceptance
    window; the values are only ever compared against exact rational areas.
    """

    orientation: str = "exact-rational-ccw"
    #: Two cell areas are considered equal inside this absolute tolerance.
    area_tolerance: float = 0.0
    #: Domain coverage may differ from the declared rectangle by this much.
    coverage_tolerance: float = 0.0
    #: Sum of intersection contributions is conserved to this relative amount.
    conservation_tolerance: float = 1e-12


@dataclass(frozen=True)
class MeshSnapshot:
    """An immutable, self-describing mesh handed to the warehouse.

    ``vertices`` maps vertex id -> ``(x, y)``; ``cells`` maps cell id ->
    three vertex ids in a ring.  ``domain`` is ``(xmin, ymin, xmax, ymax)``.
    """

    vertices: dict
    cells: dict
    domain: tuple
    policy: GeometryPolicy = field(default_factory=GeometryPolicy)
    adjacency: dict | None = None


def coerce_snapshot(mesh) -> MeshSnapshot:
    """Accept a MeshSnapshot, an object with ``to_snapshot`` or a mapping."""
    if isinstance(mesh, MeshSnapshot):
        return mesh
    if hasattr(mesh, "to_snapshot"):
        return mesh.to_snapshot()
    if isinstance(mesh, dict):
        vertices = mesh.get("vertices")
        cells = mesh.get("cells")
        domain = mesh.get("domain")
        if vertices is None or cells is None or domain is None:
            raise GeometryError("mapping snapshot needs vertices, cells and domain")
        policy = mesh.get("policy", GeometryPolicy())
        if isinstance(policy, dict):
            policy = GeometryPolicy(**policy)
        return MeshSnapshot(
            vertices=dict(vertices),
            cells=dict(cells),
            domain=tuple(domain),
            policy=policy,
            adjacency=mesh.get("adjacency"),
        )
    raise GeometryError(f"cannot interpret mesh of type {type(mesh).__name__}")


# ---------------------------------------------------------------------------
# Intersection area (exact)
# ---------------------------------------------------------------------------


def _cross(o: Point, a: Point, b: Point) -> Fraction:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _clip_convex(subject: Polygon, clip: Polygon) -> Polygon:
    """Sutherland-Hodgman clip of ``subject`` by convex ``clip``.

    Both rings are supplied counter-clockwise.  Output keeps CCW orientation and
    may be empty.  Collinear edge fragments are kept; the caller filters by
    area.
    """
    output = list(subject)
    n = len(clip)
    for i in range(n):
        if not output:
            break
        a = clip[i]
        b = clip[(i + 1) % n]
        inp = output
        output = []
        m = len(inp)
        for j in range(m):
            cur = inp[j]
            prev = inp[j - 1]
            cur_in = _cross(a, b, cur) >= 0
            prev_in = _cross(a, b, prev) >= 0
            if cur_in:
                if not prev_in:
                    output.append(_edge_intersection(prev, cur, a, b))
                output.append(cur)
            elif prev_in:
                output.append(_edge_intersection(prev, cur, a, b))
    return output


def _edge_intersection(p: Point, q: Point, a: Point, b: Point) -> Point:
    """Proper line-line intersection, computed in a well-conditioned form.

    ``p`` is known to be strictly inside the clip half-plane and ``q`` outside
    (or vice versa), so the interpolation parameter lies in ``(0, 1]``.
    """
    dp = _cross(a, b, p)
    dq = _cross(a, b, q)
    t = dp / (dp - dq)  # Fractions: dp >= 0 >= dq, dp != dq
    return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))


def clipped_polygon(poly_a: Sequence[Point], poly_b: Sequence[Point]) -> Polygon:
    """Intersection polygon of two convex rings (CCW), exactly."""
    a = list(poly_a)
    b = list(poly_b)
    if signed_area(a) < 0:
        a.reverse()
    if signed_area(b) < 0:
        b.reverse()
    result = _clip_convex(a, b)
    if len(result) < 3:
        return []
    if signed_area(result) <= 0:
        return []
    return result


def intersection_area(poly_a: Sequence[Point], poly_b: Sequence[Point]) -> Fraction:
    """Exact overlap area of two convex rings."""
    return polygon_area(clipped_polygon(poly_a, poly_b))


# ---------------------------------------------------------------------------
# Mesh validation: every invariant the services rely on
# ---------------------------------------------------------------------------


def _boundary_edges(all_edges: dict) -> list:
    """Canonical edges -> directed uses; return the uses that occur exactly once.

    Each canonical edge maps to a list of directed ``(u, v)`` occurrences.  An
    edge traversed twice (once per direction by two cells) is interior; an edge
    traversed once is on the boundary.
    """
    boundary = []
    for key, uses in all_edges.items():
        if len(uses) > 2:
            raise GeometryError(f"edge {key} used by more than two cells")
        if len(uses) == 1:
            u, v = uses[0]
            if u == v:
                raise GeometryError("degenerate self-loop edge")
            boundary.append((u, v))
    return boundary


def _order_boundary_loop(boundary_edges: list) -> list:
    """Chain the directed boundary edges into a single closed loop."""
    nxt = {}
    indeg = {}
    for u, v in boundary_edges:
        if u in nxt:
            raise GeometryError(f"vertex {u} has more than one outgoing boundary edge")
        nxt[u] = v
        indeg[v] = indeg.get(v, 0) + 1
    for v, d in indeg.items():
        if d != 1:
            raise GeometryError(f"vertex {v} has more than one incoming boundary edge")
    if not boundary_edges:
        return []
    start = boundary_edges[0][0]
    loop = [start]
    cur = nxt[start]
    while cur != start:
        if cur not in nxt or len(loop) > len(boundary_edges):
            raise GeometryError("boundary edges do not form a simple loop")
        loop.append(cur)
        cur = nxt[cur]
    if len(loop) != len(boundary_edges):
        raise GeometryError("boundary has multiple components")
    return loop


def validate_mesh(
    vertices: dict,
    cells: dict,
    domain: Sequence,
    *,
    area_tolerance: float = 0.0,
    coverage_tolerance: float = 0.0,
) -> dict:
    """Validate a planar triangle mesh and return a normalised report.

    Checks, in order: finite rational vertices, three distinct non-collinear
    vertices per cell with strictly positive exact area, complete edge sharing
    (no hanging nodes anywhere, including the rectangle boundary), no positive
    area overlap, connected boundary equal to the declared rectangle, and exact
    union area equal to the rectangle area.
    """
    if not cells:
        raise GeometryError("mesh has no cells")
    if len(domain) != 4:
        raise GeometryError("domain must be (xmin, ymin, xmax, ymax)")
    xmin, ymin, xmax, ymax = (to_fraction(v) for v in domain)
    if not (xmin < xmax and ymin < ymax):
        raise GeometryError("domain rectangle is degenerate")

    # Vertices used by cells must exist; unused stray vertices are ignored but
    # replicated into the cleaned map because they may belong to a larger build.
    used = set()
    for cid, tri in cells.items():
        ids = list(tri)
        if len(ids) != 3:
            raise GeometryError(f"cell {cid!r} does not have exactly 3 vertices")
        if len(set(ids)) != 3:
            raise GeometryError(f"cell {cid!r} repeats a vertex")
        for vid in ids:
            if vid not in vertices:
                raise GeometryError(f"cell {cid!r} references missing vertex {vid!r}")
            used.add(vid)

    coords: dict = {}
    for vid in used:
        xy = vertices[vid]
        try:
            x = to_fraction(xy[0])
            y = to_fraction(xy[1])
        except (TypeError, IndexError, GeometryError) as exc:
            raise GeometryError(f"vertex {vid!r} has invalid coordinates") from exc
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            raise GeometryError(
                f"vertex {vid!r} lies outside the domain rectangle"
            )
        coords[vid] = (x, y)
    for vid in vertices:
        if vid not in coords:
            xy = vertices[vid]
            coords[vid] = (to_fraction(xy[0]), to_fraction(xy[1]))

    # Per-cell exact areas and edge directions.
    edge_dirs: dict = {}  # (min,max) -> list of (cell, direction)
    area_sum = Fraction(0)
    cell_rings: dict = {}
    cell_order: dict = {}
    for cid, tri in cells.items():
        ring = [coords[vid] for vid in tri]
        order = list(tri)
        s = signed_area(ring)
        if s == 0:
            raise GeometryError(f"cell {cid!r} is degenerate (zero area)")
        if s < 0:
            # Keep the canonical CCW representation; input rings may be either.
            ring = list(reversed(ring))
            order.reverse()
            s = -s
        # Rotate the CCW ring so it starts at the lexicographically smallest
        # vertex token; the cycle is unchanged.
        k = min(range(3), key=lambda i: id_token(order[i]))
        ring = ring[k:] + ring[:k]
        order = order[k:] + order[:k]
        cell_rings[cid] = ring
        cell_order[cid] = tuple(order)
        area_sum += s / 2
        for k2 in range(3):
            u, v = order[k2], order[(k2 + 1) % 3]
            key = (u, v) if id_token(u) < id_token(v) else (v, u)
            edge_dirs.setdefault(key, []).append((cid, (u, v)))

    # Complete edge sharing: every shared edge has exactly two owners, each
    # traversing it in the opposite direction.
    for key, uses in edge_dirs.items():
        if len(uses) == 1:
            continue
        if len(uses) > 2:
            raise GeometryError(f"edge {key} is shared by more than two cells")
        (c0, d0), (c1, d1) = uses
        if d0 == d1:
            raise GeometryError(f"edge {key} is traversed in the same direction twice")
    all_dirs = {k: [d for _, d in v] for k, v in edge_dirs.items()}
    boundary = _boundary_edges(all_dirs)
    loop_ids = _order_boundary_loop(boundary)

    # No positive-area overlap.  A watertight triangulation cannot produce any;
    # the explicit check guards against malformed snapshots with self-touching
    # cells that still pass the edge parity test.
    _check_no_overlap(cells, cell_rings, area_tolerance)

    # Boundary is exactly the rectangle.
    rect_area = (xmax - xmin) * (ymax - ymin)
    tol_cov = to_fraction(coverage_tolerance)
    gap = rect_area - area_sum
    if gap < -tol_cov or gap > tol_cov:
        raise GeometryError(
            f"cell union area {float(area_sum):.17g} differs from domain area "
            f"{float(rect_area):.17g}"
        )
    loop_area = signed_area([coords[vid] for vid in loop_ids])
    if abs(loop_area) / 2 != rect_area:
        # Boundary loop encloses a hole (negative orientation component) or a
        # shape other than the rectangle.
        if abs(abs(loop_area) / 2 - rect_area) > tol_cov:
            raise GeometryError(
                "mesh boundary does not enclose exactly the declared rectangle"
            )
    for vid in loop_ids:
        x, y = coords[vid]
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            raise GeometryError(f"boundary vertex {vid!r} lies outside the domain")
        if not (x in (xmin, xmax) or y in (ymin, ymax)):
            raise GeometryError(
                f"boundary vertex {vid!r} at ({_clean_num(x)}, {_clean_num(y)}) "
                "is not on the rectangle boundary (hanging boundary node?)"
            )

    # Deterministic references for area/exactness.  Rebuild loop in CCW order.
    if signed_area([coords[vid] for vid in loop_ids]) < 0:
        loop_ids = list(reversed(loop_ids))

    return {
        "coords": coords,
        "cell_rings": cell_rings,
        "area_total": area_sum,
        "domain_area": rect_area,
        "boundary_loop": loop_ids,
        "adjacency": _adjacency_from_edges(edge_dirs),
    }


def _adjacency_from_edges(edge_dirs: dict) -> dict:
    adj = {}
    for uses in edge_dirs.values():
        if len(uses) == 2:
            (c0, d0), (c1, d1) = uses
            adj.setdefault(c0, {})[tuple(d0)] = c1
            adj.setdefault(c1, {})[tuple(d1)] = c0
    return adj


def _check_no_overlap(cells: dict, rings: dict, tol: float) -> None:
    """Exact pairwise overlap check, accelerated by bounding boxes.

    Only positive-area overlaps are rejected; edge/vertex touching is allowed.
    ``tol`` must be zero for exact snapshots; it is clamped to zero here because
    any positive overlap is a hard failure regardless of declared tolerance.
    """
    items = list(cells.items())
    boxes = {}
    for cid, ring in rings.items():
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        boxes[cid] = (min(xs), min(ys), max(xs), max(ys))
    for i in range(len(items)):
        ci = items[i][0]
        bi = boxes[ci]
        for j in range(i + 1, len(items)):
            cj = items[j][0]
            bj = boxes[cj]
            if bi[2] <= bj[0] or bj[2] <= bi[0]:
                continue
            if bi[3] <= bj[1] or bj[3] <= bi[1]:
                continue
            if intersection_area(rings[ci], rings[cj]) > 0:
                raise GeometryError(f"cells {ci!r} and {cj!r} overlap with positive area")


# ---------------------------------------------------------------------------
# Small helpers used elsewhere
# ---------------------------------------------------------------------------


def bbox(ring: Iterable[Point]) -> tuple:
    xs = []
    ys = []
    for x, y in ring:
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_overlap(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _json_number(value) -> object:
    """JSON-safe exact numeric: int when integral, else ``"p/q"`` string."""
    f = to_fraction(value)
    if f.denominator == 1:
        return f.numerator
    return f"{f.numerator}/{f.denominator}"


def canonical_geometry(vertices: dict, cells: dict, domain: Sequence) -> dict:
    """Canonical JSON-ready geometry independent of input ordering."""
    verts = {}
    for vid in sorted(vertices, key=lambda v: id_token(v)):
        x, y = vertices[vid]
        verts[str(id_token(vid))] = [_json_number(x), _json_number(y)]
    tris = {}
    for cid in sorted(cells, key=lambda c: id_token(c)):
        f = to_fraction
        pts = []
        for vid in cells[cid]:
            x, y = vertices[vid]
            pts.append((f(x), f(y)))
        # Normalise winding and rotate so the lexicographically smallest vertex
        # is first: identical triangles with different input order hash equal.
        s = signed_area(pts)
        if s < 0:
            pts.reverse()
        elif s == 0:
            raise GeometryError(f"cell {cid!r} has zero area")
        k = min(range(3), key=lambda i: (pts[i][0], pts[i][1]))
        pts = pts[k:] + pts[:k]
        tris[str(id_token(cid))] = [[_json_number(x), _json_number(y)] for x, y in pts]
    return {
        "domain": [_json_number(v) for v in domain],
        "vertices": verts,
        "cells": tris,
    }


def geometry_fingerprint(vertices: dict, cells: dict, domain: Sequence) -> str:
    """SHA-256 over canonical geometry; identical geometry hashes equal."""
    payload = canonical_geometry(vertices, cells, domain)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def orient_cells_ccw(vertices: dict, cells: dict) -> dict:
    """Return a copy of ``cells`` with each ring oriented counter-clockwise."""
    out = {}
    for cid, tri in cells.items():
        pts = [(to_fraction(vertices[vid][0]), to_fraction(vertices[vid][1])) for vid in tri]
        if signed_area(pts) < 0:
            out[cid] = tuple(reversed(tuple(tri)))
        else:
            out[cid] = tuple(tri)
    return out

