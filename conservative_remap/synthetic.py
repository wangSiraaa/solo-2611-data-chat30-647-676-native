"""Synthetic planar triangulations used by the demos and tests.

Everything lives on a fixed rectangle; vertex coordinates are exact rationals.
"""

from __future__ import annotations

from fractions import Fraction

from .geometry import GeometryPolicy, MeshSnapshot, to_fraction


def _frange(a: Fraction, b: Fraction, n: int) -> list:
    return [a + (b - a) * Fraction(i, n) for i in range(n + 1)]


def grid_mesh(
    nx: int,
    ny: int,
    domain=(0, 0, 1, 1),
    *,
    diagonal: str = "main",
    prefix: str = "g",
    policy: GeometryPolicy | None = None,
) -> MeshSnapshot:
    """Regular ``nx`` by ``ny`` quad grid, each quad split into two triangles.

    ``diagonal='main'`` uses the bottom-left/top-right diagonal, ``'anti'`` the
    top-left/bottom-right one.  Adjacent cells always share complete edges, so
    the result has no hanging nodes.
    """
    if nx < 1 or ny < 1:
        raise ValueError("nx and ny must be positive")
    if diagonal not in ("main", "anti"):
        raise ValueError("diagonal must be 'main' or 'anti'")
    x0, y0, x1, y1 = (to_fraction(v) for v in domain)
    xs = _frange(x0, x1, nx)
    ys = _frange(y0, y1, ny)
    vertices = {}
    vid = {}
    for j in range(ny + 1):
        for i in range(nx + 1):
            name = f"{prefix}v{i}_{j}"
            vid[(i, j)] = name
            vertices[name] = (xs[i], ys[j])
    cells = {}
    for j in range(ny):
        for i in range(nx):
            bl = vid[(i, j)]
            br = vid[(i + 1, j)]
            tr = vid[(i + 1, j + 1)]
            tl = vid[(i, j + 1)]
            cid = f"{prefix}c{i}_{j}"
            if diagonal == "main":
                cells[cid] = (bl, br, tr)
                cells[cid + "b"] = (bl, tr, tl)
            else:
                cells[cid] = (bl, br, tl)
                cells[cid + "b"] = (br, tr, tl)
    return MeshSnapshot(
        vertices=vertices,
        cells=cells,
        domain=(x0, y0, x1, y1),
        policy=policy or GeometryPolicy(),
    )


def fan_mesh(
    nx: int,
    ny: int,
    domain=(0, 0, 1, 1),
    *,
    prefix: str = "f",
    policy: GeometryPolicy | None = None,
) -> MeshSnapshot:
    """Fan triangulation from the rectangle centre to perimeter vertices.

    Perimeter is subdivided ``nx`` times horizontally and ``ny`` times
    vertically.  Cell edges from the centre cross the edges of a regular grid,
    which makes this a good target mesh for cross-source-edge contributions.
    """
    x0, y0, x1, y1 = (to_fraction(v) for v in domain)
    xs = _frange(x0, x1, nx)
    ys = _frange(y0, y1, ny)
    vertices = {}
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    vertices[f"{prefix}core"] = (cx, cy)
    ring = []
    for i in range(nx):
        name = f"{prefix}b{i}"
        vertices[name] = (xs[i], y0)
        ring.append(name)
    for j in range(ny):
        name = f"{prefix}r{j}"
        vertices[name] = (x1, ys[j])
        ring.append(name)
    for i in range(nx, 0, -1):
        name = f"{prefix}t{i}"
        vertices[name] = (xs[i], y1)
        ring.append(name)
    for j in range(ny, 0, -1):
        name = f"{prefix}l{j}"
        vertices[name] = (x0, ys[j])
        ring.append(name)
    cells = {}
    for k in range(len(ring)):
        cells[f"{prefix}c{k}"] = (ring[k], ring[(k + 1) % len(ring)], f"{prefix}core")
    return MeshSnapshot(
        vertices=vertices,
        cells=cells,
        domain=(x0, y0, x1, y1),
        policy=policy or GeometryPolicy(),
    )


def staggered_pair(
    domain=(0, 0, 1, 1),
    *,
    source_nx: int = 5,
    source_ny: int = 6,
) -> tuple:
    """Two grids with the same cell count but crossing diagonals.

    The target uses ``source_ny`` columns and ``source_nx`` rows, so it has the
    same number of cells but a different aspect.  Diagonals cross each other,
    exercising target cells that straddle source edges, and the 15x20 / 20x15
    pair gives 600 cells each (the maximum requested scale is 300; callers can
    shrink it).
    """
    src = grid_mesh(source_nx, source_ny, domain, diagonal="main", prefix="s")
    tgt = grid_mesh(source_ny, source_nx, domain, diagonal="anti", prefix="t")
    return src, tgt
