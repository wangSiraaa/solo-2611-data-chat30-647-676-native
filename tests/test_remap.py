import math
import pytest

from conservative_remap.geometry import (
    MeshValidationError,
    intersection_area,
)
from conservative_remap.mesh import MeshOperationError, MeshService
from conservative_remap.field import (
    FieldError,
    FieldWarehouse,
    IncompleteFieldError,
    RemapError,
)
from conservative_remap.synthetic import fan_mesh, grid_mesh, staggered_pair


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def unit_square_two_triangles():
    """Rectangle (0,0)-(2,1) split by the main diagonal into two triangles."""
    vertices = {"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)}
    cells = {"lower": ("A", "B", "C"), "upper": ("A", "C", "D")}
    return vertices, cells, (0, 0, 2, 1)


def make_services(tmp_path):
    mesh_path = str(tmp_path / "mesh.db")
    field_path = str(tmp_path / "field.db")
    return MeshService(mesh_path), FieldWarehouse(field_path), mesh_path, field_path


def correlation_ok(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ---------------------------------------------------------------------------
# Geometry / hand calculation
# ---------------------------------------------------------------------------


def test_hand_rectangle_contribution_matrix():
    """Rectangle split into two triangles; target splits each source edge.

    Source: lower = A(0,0) B(2,0) C(2,1), upper = A(0,0) C(2,1) D(0,1).
    Target: split shared diagonal A-C at M(1, 0.5), producing four triangles:
      lower-left  A B M   area 1/2
      lower-right M B C   area 1/2
      upper-left  A M D   area 1/2
      upper-right M C D   area 1/2
    Every target triangle lies wholly inside exactly one source triangle.
    Contributions are therefore one term of 1/2 each.
    """
    svc = MeshService()
    src_version = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    split = svc.split_edge(edge=("A", "C"), t=0.5)
    tgt_version = split.version_id
    target_cells = set(svc.cell_ids(tgt_version))
    assert len(target_cells) == 4

    wh = FieldWarehouse()
    src = wh.register_mesh(svc.export(src_version))
    tgt = wh.register_mesh(svc.export(tgt_version))
    ds = wh.add_density(src, {"lower": 3, "upper": -2})
    run = wh.remap(src, ds, tgt)
    assert run.published
    assert run.conservation_gap == 0

    # Map each target triangle back to its source via its provenance.
    contributions = {}
    for cid in target_cells:
        trace = wh.trace(run.run_id, cid)
        [(contrib)] = trace["contributions"]
        contributions[cid] = (
            contrib["source_cell_id"],
            contrib["overlap_area"],
            contrib["source_density"],
        )
        assert contrib["target_cell_id"] == cid

    # Each split piece covers exactly half of its parent, so density is copied.
    for cid, (scid, area, dens) in contributions.items():
        assert area == pytest.approx(0.5)
        if scid == "lower":
            assert dens == 3
        else:
            assert dens == -2

    densities = wh.target_densities(run.run_id)
    assert sorted(densities.values()) == [-2, -2, 3, 3]
    assert run.source_integral == pytest.approx(3 * 1.0 + (-2) * 1.0)
    assert run.target_integral == pytest.approx(1.0)


def test_hand_overlap_fractions_are_exact():
    """Two rectangles overlapping in a quarter of a unit square."""
    a = [(0, 0), (1, 0), (1, 1), (0, 1)]
    b = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]
    from conservative_remap.geometry import to_fraction

    ar = [(to_fraction(x), to_fraction(y)) for x, y in a]
    br = [(to_fraction(x), to_fraction(y)) for x, y in b]
    assert intersection_area(ar, br) == to_fraction("0.25")


def test_validate_rejects_hanging_node_and_overlap_and_gap():
    # Hanging node: a vertex on an edge that is not used by the neighbour.
    with pytest.raises(MeshValidationError):
        MeshService().import_mesh(
            vertices={
                "A": (0, 0),
                "B": (2, 0),
                "C": (2, 1),
                "D": (0, 1),
                "M": (1, 1),
            },
            cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "M")},
            domain=(0, 0, 2, 1),
        )
    # Positive overlap.
    with pytest.raises(MeshValidationError):
        MeshService().import_mesh(
            vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1), "E": (1, 0.5)},
            cells={"a": ("A", "B", "C"), "b": ("A", "C", "E")},
            domain=(0, 0, 2, 1),
        )
    # Gap: union does not fill the rectangle.
    with pytest.raises(MeshValidationError):
        MeshService().import_mesh(
            vertices={
                "A": (0, 0),
                "B": (1, 0),
                "C": (1, 1),
                "D": (0, 1),
                "E": (2, 0),
                "F": (2, 1),
            },
            cells={"a": ("A", "B", "C"), "b": ("A", "C", "D"), "c": ("B", "E", "F")},
            domain=(0, 0, 2, 1),
        )
    # Degenerate triangle.
    with pytest.raises(MeshValidationError):
        MeshService().import_mesh(
            vertices={"A": (0, 0), "B": (2, 0), "C": (1, 0)},
            cells={"a": ("A", "B", "C")},
            domain=(0, 0, 2, 1),
        )
    # Hanging node with correct total area: split only one side of the shared
    # diagonal A-C at M, leaving upper with an unsplit long edge A-C.
    with pytest.raises(MeshValidationError):
        MeshService().import_mesh(
            vertices={
                "A": (0, 0),
                "B": (2, 0),
                "C": (2, 1),
                "D": (0, 1),
                "M": (1, 0.5),
            },
            cells={
                "lower_a": ("A", "B", "M"),
                "lower_b": ("M", "B", "C"),
                "upper": ("A", "C", "D"),
            },
            domain=(0, 0, 2, 1),
        )


# ---------------------------------------------------------------------------
# Mesh service behaviour
# ---------------------------------------------------------------------------


def test_split_both_sides_and_lineage_trace(tmp_path):
    svc = MeshService(str(tmp_path / "m.db"))
    v1 = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    svc.preview_split(edge=("A", "C"))
    # Preview is not persisted: current version unchanged.
    assert svc.current_version() == v1
    result = svc.split_edge(edge=("A", "C"))
    assert result.version_id != v1
    # Both source cells were replaced, no old side left unsplit.
    assert set(result.preview.replaced_cells) == {"lower", "upper"}
    adjacency = svc.edge_adjacency()
    # New shared edges connect the four new cells across the split point.
    new_cells = set(svc.cell_ids())
    assert new_cells == set(result.preview.cell_ids())
    for cid, nbrs in adjacency.items():
        assert nbrs, f"{cid} lost all neighbours"
    # Lineage: every new cell traces back to its old parent.
    for cid in new_cells:
        chain = svc.cell_provenance(cid)["chain"]
        assert chain[0]["cell_id"] in ("lower", "upper")
        assert chain[-1]["cell_id"] == cid
        assert chain[0]["origin_cid"] == chain[-1]["origin_cid"]
    # Area unchanged.
    assert svc.report()["area_total"] == 2
    svc.close()


def test_boundary_split_preserves_outer_boundary(tmp_path):
    svc = MeshService()
    svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    split = svc.split_edge(edge=("A", "B"), t=0.25)
    assert set(split.preview.replaced_cells) == {"lower"}
    rep = svc.report()
    assert rep["area_total"] == 2 and rep["domain_area"] == 2
    loop = [tuple(map(float, svc.get_vertex(v))) for v in rep["boundary_loop"]]
    # New boundary vertex lies on the bottom edge; boundary is still a rectangle.
    xs = sorted({p[0] for p in loop})
    ys = sorted({p[1] for p in loop})
    assert xs == [0.0, 0.5, 2.0]
    assert ys == [0.0, 1.0]
    svc.close()


def test_rejected_split_leaves_no_half_mesh(tmp_path):
    path = str(tmp_path / "m.db")
    svc = MeshService(path)
    v1 = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "B"), t=0.0)
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "B"), t=1.0)
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "D"), point=(0.2, 0.2))
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "B"), point=(0.0, 0.5))  # not collinear
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "B"), point=(3, 0))  # outside edge
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "nope"))
    # Nothing was persisted beyond the import.
    assert svc.current_version() == v1
    assert svc.cell_ids() == ("lower", "upper")
    assert [o["kind"] for o in svc.operation_log()] == ["import"]
    svc.close()
    # Reopening still shows the untouched mesh.
    svc2 = MeshService(path)
    assert svc2.cell_ids() == ("lower", "upper")
    assert svc2.current_version() == v1
    svc2.close()


def test_mesh_service_has_no_field_dependency(tmp_path):
    svc = MeshService()
    svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    assert svc.edge_adjacency()["lower"] and svc.edge_adjacency()["upper"]
    # No field API is reachable on the service.
    assert not hasattr(svc, "remap") and not hasattr(svc, "add_density")


# ---------------------------------------------------------------------------
# Field warehouse: conservation, cancellation, touching, missing values
# ---------------------------------------------------------------------------


def test_constant_field_is_preserved_under_remap():
    svc = MeshService()
    src_v = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    tgt_v = svc.split_edge(edge=("A", "C")).version_id
    tgt_mesh = svc.export(tgt_v)
    wh = FieldWarehouse()
    src = wh.register_mesh(svc.export(src_v))
    tgt = wh.register_mesh(tgt_mesh)
    ds = wh.add_density(src, {"lower": 7, "upper": 7})
    run = wh.remap(src, ds, tgt)
    assert all(d == 7 for d in wh.target_densities(run.run_id).values())
    assert run.target_integral == pytest.approx(7 * 2)
    assert run.conservation_gap == 0


def test_positive_negative_cancellation():
    """A 2x1 rectangle with +1 over the left half and -2 over the right half."""
    src = grid_mesh(2, 1, (0, 0, 2, 1), diagonal="main", prefix="s")
    # Cells: sc0_0, sc0_0b in left square; sc1_0, sc1_0b in right square.
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    vals = {cid: (1 if cid.startswith("sc0_") else -2) for cid in src.cells}
    ds = wh.add_density(src_id, vals)
    tgt = grid_mesh(1, 1, (0, 0, 2, 1), diagonal="anti", prefix="t")
    tgt_id = wh.register_mesh(tgt)
    run = wh.remap(src_id, ds, tgt_id)
    # Left area 1 at +1, right area 1 at -2 => integral -1 over area 2.
    assert run.source_integral == pytest.approx(-1)
    assert run.target_integral == pytest.approx(-1)
    # The anti-diagonal target cell straddles x=1, so its density is mixed; the
    # point is that opposite-sign contributions cancel and the total survives.
    densities = wh.target_densities(run.run_id)
    assert run.conservation_gap == 0
    assert sum(
        densities[cid] * wh.target_cell(run.run_id, cid)["area"] for cid in tgt.cells
    ) == pytest.approx(-1)
    assert any(d < 0 for d in densities.values())
    # The two diagonal target cells each record both a positive and a negative
    # source contribution; their weighted terms cancel partially.
    for cid in tgt.cells:
        signs = {c["source_density"] > 0 for c in wh.contributions(run.run_id, cid)}
        assert signs == {True, False}
        total = sum(c["weighted"] for c in wh.contributions(run.run_id, cid))
        assert total == pytest.approx(
            densities[cid] * wh.target_cell(run.run_id, cid)["area"]
        )


def test_zero_area_touch_does_not_contribute():
    """Cells that only share an edge must not appear in each other's traces."""
    src = grid_mesh(2, 1, (0, 0, 2, 1), diagonal="main", prefix="s")
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    ds = wh.add_density(src_id, {cid: 1 for cid in src.cells})
    # Target is the same 2x1 rectangle with the opposite diagonal.  Each target
    # triangle lies inside one half-square, so it overlaps only the source cells
    # of that half; the other half's cells merely touch its boundary.
    tgt = grid_mesh(2, 1, (0, 0, 2, 1), diagonal="anti", prefix="t")
    tgt_id = wh.register_mesh(tgt)
    run = wh.remap(src_id, ds, tgt_id)
    for cid in tgt.cells:
        left = cid.startswith("tc0_")
        sources = {c["source_cell_id"] for c in wh.contributions(run.run_id, cid)}
        expected_prefix = "sc0_" if left else "sc1_"
        assert sources and all(s.startswith(expected_prefix) for s in sources)
        other = "sc1_" if left else "sc0_"
        assert not any(s.startswith(other) for s in sources)


def test_missing_source_value_blocks_publication():
    src = grid_mesh(2, 2, (0, 0, 1, 1), diagonal="main", prefix="s")
    tgt = grid_mesh(1, 1, (0, 0, 1, 1), diagonal="anti", prefix="t")
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    cids = sorted(src.cells)
    partial = {cid: 1 for cid in cids[:-1]}
    ds = wh.add_density(src_id, partial, complete=False)
    info = wh.density_set_info(ds)
    assert not info["complete"] and info["missing_cells"] == [cids[-1]]
    with pytest.raises(IncompleteFieldError):
        wh.remap(src_id, ds, tgt_id)
    # A draft run can be built but cannot be published and has no densities.
    run = wh.remap(src_id, ds, tgt_id, allow_incomplete=True, publish=False)
    assert not run.published
    assert run.incomplete_targets >= 1
    with pytest.raises(IncompleteFieldError):
        wh.target_densities(run.run_id)
    with pytest.raises(IncompleteFieldError):
        wh.publish_run(run.run_id)
    with pytest.raises(IncompleteFieldError):
        wh.integrate(run.run_id, (0, 0, 1, 1))
    # Density set declared complete but missing a value is rejected on insert.
    with pytest.raises(IncompleteFieldError):
        wh.add_density(src_id, partial, complete=True)


def test_field_accepts_external_mesh_without_split_history():
    """Target mesh built independently (never touched the mesh service)."""
    src = grid_mesh(3, 2, (0, 0, 3, 2), diagonal="main", prefix="s")
    tgt = fan_mesh(3, 2, (0, 0, 3, 2), prefix="f")
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    ds = wh.add_density(src_id, {cid: 0.5 for cid in src.cells})
    run = wh.remap(src_id, ds, tgt_id)
    assert run.published
    assert all(d == pytest.approx(0.5) for d in wh.target_densities(run.run_id).values())


# ---------------------------------------------------------------------------
# Integration and conservation against direct source integration
# ---------------------------------------------------------------------------


def test_full_domain_integral_matches_direct_source():
    src, tgt = staggered_pair(source_nx=5, source_ny=6)
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    import random

    rng = random.Random(11)
    vals = {cid: rng.uniform(-4, 4) for cid in src.cells}
    ds = wh.add_density(src_id, vals)
    run = wh.remap(src_id, ds, tgt_id)
    direct = wh.integrate_source(ds, (0, 0, 1, 1))
    target = wh.integrate(run.run_id, (0, 0, 1, 1))
    assert direct.covered_area == pytest.approx(1)
    assert target.covered_area == pytest.approx(1)
    assert correlation_ok(float(direct.integral), float(target.integral))
    assert correlation_ok(float(run.source_integral), float(run.target_integral))


def test_subregion_integration_uses_actual_intersections():
    """Subregion products integrate target∩query exactly and partition additively.

    Area-weighted remapping conserves the *global* integral exactly; a
    subregion integral is not expected to equal the source subregion integral
    because target densities are averages over each target cell.  What must
    hold is that the product integral is built from actual target∩query areas,
    that covered area tracks the query, and that subregions partition the
    domain integral exactly.
    """
    src, tgt = staggered_pair(source_nx=4, source_ny=5)
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    import random

    rng = random.Random(5)
    vals = {cid: rng.uniform(-2, 3) for cid in src.cells}
    ds = wh.add_density(src_id, vals)
    run = wh.remap(src_id, ds, tgt_id)
    densities = wh.target_densities(run.run_id)

    rects = [
        (0, 0, 1, 1),
        (0.1, 0.2, 0.65, 0.9),
        (0.35, 0.05, 0.5, 0.45),
        (0.75, 0.6, 1.2, 1.3),  # partially outside
        (2, 2, 3, 3),  # entirely outside
    ]
    for rect in rects:
        target = wh.integrate(run.run_id, rect)
        # covered area equals the clipped query area when inside the domain.
        if target.clipped_query is not None:
            cx0, cy0, cx1, cy1 = target.clipped_query
            assert target.covered_area == pytest.approx((cx1 - cx0) * (cy1 - cy0))
        # Recompute the integral independently from target∩query intersections.
        tgt_snap = wh.mesh_snapshot(tgt_id)
        q = target.clipped_query
        if q is not None:
            from conservative_remap.geometry import to_fraction

            qring = [
                (to_fraction(q[0]), to_fraction(q[1])),
                (to_fraction(q[2]), to_fraction(q[1])),
                (to_fraction(q[2]), to_fraction(q[3])),
                (to_fraction(q[0]), to_fraction(q[3])),
            ]
            expected = sum(
                densities[cid]
                * intersection_area(
                    [
                        (to_fraction(tgt_snap.vertices[v][0]), to_fraction(tgt_snap.vertices[v][1]))
                        for v in tgt_snap.cells[cid]
                    ],
                    qring,
                )
                for cid in tgt_snap.cells
            )
            assert target.integral == expected
    # Outside the domain: zero integral, zero coverage, nothing touched.
    outside = wh.integrate(run.run_id, (2, 2, 3, 3))
    assert outside.integral == 0 and outside.covered_area == 0
    assert outside.clipped_query is None and outside.cell_count == 0

    # Subregions tile the full domain, so their integrals add to the total.
    parts = [(0, 0, 0.5, 1), (0.5, 0, 1, 0.5), (0.5, 0.5, 1, 1)]
    part_sum = sum(wh.integrate(run.run_id, r).integral for r in parts)
    assert float(part_sum) == pytest.approx(float(run.target_integral))
    assert float(run.target_integral) == pytest.approx(float(run.source_integral))
    # Partial-outside queries report the out-of-domain area separately.
    partial = wh.integrate(run.run_id, (0.75, 0.6, 1.2, 1.3))
    assert partial.outside_area == pytest.approx(0.45 * 0.7 - 0.25 * 0.4)


def test_cross_source_edge_target_cells_have_multiple_contributions():
    """Anti-diagonal target cells straddle main-diagonal source cells."""
    src = grid_mesh(2, 2, (0, 0, 2, 2), diagonal="main", prefix="s")
    tgt = grid_mesh(2, 2, (0, 0, 2, 2), diagonal="anti", prefix="t")
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    ds = wh.add_density(src_id, {cid: float(i + 1) for i, cid in enumerate(sorted(src.cells))})
    run = wh.remap(src_id, ds, tgt_id)
    multi = [
        cid
        for cid in tgt.cells
        if len(wh.contributions(run.run_id, cid)) >= 2
    ]
    assert multi, "expected target cells crossing source edges"
    # Each target's source-area sum equals its own area.
    for cid in tgt.cells:
        info = wh.target_cell(run.run_id, cid)
        assert info["source_area_sum"] == info["area"]
    assert run.conservation_gap == 0


# ---------------------------------------------------------------------------
# Persistence, immutability of runs, and stale contribution matrices
# ---------------------------------------------------------------------------


def test_reopen_preserves_mesh_and_field_trace(tmp_path):
    mesh_path = str(tmp_path / "mesh.db")
    field_path = str(tmp_path / "field.db")
    svc = MeshService(mesh_path)
    src_v = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    tgt_v = svc.split_edge(edge=("A", "C")).version_id
    src_snap = svc.export(src_v)
    tgt_snap = svc.export(tgt_v)
    wh = FieldWarehouse(field_path)
    src = wh.register_mesh(src_snap)
    tgt = wh.register_mesh(tgt_snap)
    ds = wh.add_density(src, {"lower": 4, "upper": -1})
    run = wh.remap(src, ds, tgt)
    before = wh.trace_all(run.run_id)
    wh.close()
    svc.close()

    svc2 = MeshService(mesh_path)
    wh2 = FieldWarehouse(field_path)
    assert svc2.cell_ids(tgt_v) == tuple(before.keys())
    assert svc2.report(tgt_v)["area_total"] == 2
    after = wh2.trace_all(run.run_id)
    assert after.keys() == before.keys()
    for cid in before:
        assert after[cid]["target"] == before[cid]["target"]
        assert after[cid]["contributions"] == before[cid]["contributions"]
    assert wh2.run_summary(run.run_id) == run
    wh2.close()
    svc2.close()


def test_real_refinement_invalidates_old_run_area_matrix():
    """Refine the true source mesh; old run stays fixed, new run differs."""
    svc = MeshService()
    src_v1 = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    tgt = grid_mesh(2, 2, (0, 0, 2, 1), diagonal="anti", prefix="t")
    wh = FieldWarehouse()
    src1 = wh.register_mesh(svc.export(src_v1))
    tgt_id = wh.register_mesh(tgt)
    ds1 = wh.add_density(src1, {"lower": 3, "upper": -2})
    run1 = wh.remap(src1, ds1, tgt_id)
    old_matrix = {
        cid: wh.contributions(run1.run_id, cid) for cid in tgt.cells
    }
    old_fp = run1.source_fingerprint

    # Real refinement of the source mesh: split shared diagonal A-C.
    src_v2 = svc.split_edge(edge=("A", "C")).version_id
    src2 = wh.register_mesh(svc.export(src_v2))
    # Reuse the same cell-constant density; the refined cells have new ids, so a
    # corrected density set is required.
    refined_densities = {
        cid: (3 if cid.startswith("lower/") else -2) for cid in svc.cell_ids(src_v2)
    }
    ds2 = wh.add_density(src2, refined_densities)
    run2 = wh.remap(src2, ds2, tgt_id)

    # Old run's pinned source identity and area matrix are unchanged.
    assert wh.run_summary(run1.run_id).source_fingerprint == old_fp
    assert {
        cid: wh.contributions(run1.run_id, cid) for cid in tgt.cells
    } == old_matrix
    # The mesh identity changed, so the old run is stale for the new source.
    stale = wh.is_run_stale(run1.run_id, source=svc, target=tgt)
    assert stale["source_stale"] and stale["stale"]
    # The new run has a different source identity and its own matrix.
    assert run2.source_fingerprint != run1.source_fingerprint
    new_matrix = {cid: wh.contributions(run2.run_id, cid) for cid in tgt.cells}
    assert new_matrix != old_matrix
    # Both runs still conserve their own totals.
    for run in (run1, run2):
        assert wh.integrate(run.run_id, (0, 0, 2, 1)).integral == pytest.approx(
            wh.integrate_source(
                run.density_set_id, (0, 0, 2, 1)
            ).integral
        )
    # Correcting a density produces a new run; the old values cannot change.
    ds3 = wh.add_density(src1, {"lower": 1, "upper": 1})
    run3 = wh.remap(src1, ds3, tgt_id)
    assert wh.density_values(ds1) == wh.density_values(
        run1.density_set_id
    )
    assert wh.density_values(ds1)["lower"] == 3
    assert wh.density_values(ds3)["lower"] == 1
    assert run3.run_id != run1.run_id


# ---------------------------------------------------------------------------
# Scale test: 300 source, 300 target, 50 queries
# ---------------------------------------------------------------------------


def test_scale_300_300_50_queries():
    src, tgt = staggered_pair(source_nx=15, source_ny=10, domain=(0, 0, 3, 2))
    assert len(src.cells) == 300
    assert len(tgt.cells) == 300
    wh = FieldWarehouse()
    src_id = wh.register_mesh(src)
    tgt_id = wh.register_mesh(tgt)
    import random

    rng = random.Random(3)
    vals = {cid: rng.uniform(-5, 5) for cid in src.cells}
    ds = wh.add_density(src_id, vals)
    run = wh.remap(src_id, ds, tgt_id)
    assert run.published
    assert correlation_ok(float(run.source_integral), float(run.target_integral))
    x0, y0, x1, y1 = 0.0, 0.0, 3.0, 2.0
    for _ in range(50):
        ax, bx = sorted(rng.uniform(x0, x1) for _ in range(2))
        ay, by = sorted(rng.uniform(y0, y1) for _ in range(2))
        rect = (ax, ay, bx, by)
        remapped = wh.integrate(run.run_id, rect)
        # Covered area equals the query area for an in-domain rectangle, and the
        # product integral is finite and built from actual intersections.
        assert float(remapped.covered_area) == pytest.approx((bx - ax) * (by - ay))
        assert remapped.cell_count > 0
    # A query split in two along x must give exactly additive integrals.
    for _ in range(10):
        ax, bx = sorted(rng.uniform(x0, x1) for _ in range(2))
        ay, by = sorted(rng.uniform(y0, y1) for _ in range(2))
        mid = rng.uniform(ax, bx)
        whole = wh.integrate(run.run_id, (ax, ay, bx, by)).integral
        left = wh.integrate(run.run_id, (ax, ay, mid, by)).integral
        right = wh.integrate(run.run_id, (mid, ay, bx, by)).integral
        assert left + right == whole
    # Global conservation against the source at full scale.
    direct_total = wh.integrate_source(ds, (x0, y0, x1, y1))
    target_total = wh.integrate(run.run_id, (x0, y0, x1, y1))
    assert correlation_ok(float(direct_total.integral), float(target_total.integral), 1e-9)


# ---------------------------------------------------------------------------
# Trace entry after reopen for a split/refined product
# ---------------------------------------------------------------------------


def test_trace_returns_source_contributions_after_reopen(tmp_path):
    mesh_path = str(tmp_path / "m.db")
    field_path = str(tmp_path / "f.db")
    svc = MeshService(mesh_path)
    src_v = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
    )
    tgt_v = svc.split_edge(edge=("A", "C")).version_id
    wh = FieldWarehouse(field_path)
    src = wh.register_mesh(svc.export(src_v))
    tgt = wh.register_mesh(svc.export(tgt_v))
    ds = wh.add_density(src, {"lower": 2, "upper": 5})
    run = wh.remap(src, ds, tgt)
    wh.close()
    wh2 = FieldWarehouse(field_path)
    for cid in svc.cell_ids(tgt_v):
        trace = wh2.trace(run.run_id, cid)
        assert trace["source_count"] == 1
        assert trace["contributions"][0]["overlap_area"] == pytest.approx(0.5)
        assert trace["contributions"][0]["source_density"] in (2, 5)
        assert trace["target"]["density"] in (2, 5)
    wh2.close()
    svc.close()


# ---------------------------------------------------------------------------
# Draft workflow, domain checks, and API independence
# ---------------------------------------------------------------------------


def test_draft_import_publish_and_discard(tmp_path):
    svc = MeshService()
    draft = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
        publish=False,
    )
    # A draft cannot be used as a split base or exported as "current".
    from conservative_remap.mesh import MeshOperationError

    with pytest.raises(MeshOperationError):
        svc.current_version()
    with pytest.raises(MeshOperationError):
        svc.split_edge(edge=("A", "C"))
    svc.publish(draft)
    assert svc.current_version() == draft
    # A second draft can be discarded without touching the published version.
    second = svc.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
        publish=False,
    )
    svc.discard(second)
    assert svc.current_version() == draft
    with pytest.raises(MeshOperationError):
        svc.discard(draft)  # published versions are immutable


def test_field_rejects_domain_mismatch_and_incomplete_publish():
    wh = FieldWarehouse()
    a = grid_mesh(2, 2, (0, 0, 1, 1), prefix="a")
    b = grid_mesh(2, 2, (0, 0, 2, 2), prefix="b")
    a_id = wh.register_mesh(a)
    b_id = wh.register_mesh(b)
    ds = wh.add_density(a_id, {cid: 1 for cid in a.cells})
    with pytest.raises(RemapError):
        wh.remap(a_id, ds, b_id)
    # Density set bound to a different mesh cannot be used with another source.
    with pytest.raises(FieldError):
        wh.remap(b_id, ds, a_id)


def test_field_rejects_unknown_and_duplicate_density_cells():
    wh = FieldWarehouse()
    m = grid_mesh(2, 2, (0, 0, 1, 1), prefix="a")
    mid = wh.register_mesh(m)
    with pytest.raises(FieldError):
        wh.add_density(mid, {"nope": 1})
    with pytest.raises(FieldError):
        wh.add_density(mid, {next(iter(m.cells)): float("nan")})
    with pytest.raises(FieldError):
        wh.add_density(mid, {next(iter(m.cells)): float("inf")})


def test_no_split_history_in_field_tables(tmp_path):
    """The warehouse schema has no mesh-editing table or operation log."""
    path = str(tmp_path / "f.db")
    wh = FieldWarehouse(path)
    tables = {
        row[0]
        for row in wh._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    wh.close()
    assert not any("split" in t or "operation" in t or "version" in t for t in tables)
    assert {"mesh", "mesh_cell", "density_set", "run", "contribution"} <= tables
