"""End-to-end demonstration of the mesh service and field warehouse.

Run with::

    python3 -m conservative_remap.demo

Everything is synthetic and deterministic.  The script walks through:

1. a hand-computed rectangle split into two triangles,
2. a shared diagonal split with traceable lineage,
3. a cross-source-edge target with per-target contribution matrices,
4. positive/negative cancellation and boundary touching,
5. a missing-value field that cannot be published,
6. a real source refinement that invalidates the old contribution matrix,
7. persistence and a rectangle-subregion integration.
"""

from __future__ import annotations

import os
import tempfile
from fractions import Fraction

from .field import FieldWarehouse, IncompleteFieldError
from .mesh import MeshService
from .synthetic import grid_mesh


def _fmt(value):
    if isinstance(value, Fraction) and value.denominator == 1:
        return int(value)
    return str(value)


def _hr(title):
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="conservative-remap-")
    mesh_path = os.path.join(tmpdir, "mesh.db")
    field_path = os.path.join(tmpdir, "field.db")

    # ------------------------------------------------------------------
    _hr("1. Hand example: rectangle -> two triangles")
    mesh = MeshService(mesh_path)
    src_v = mesh.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
        note="hand example source",
    )
    report = mesh.report(src_v)
    print(f"  domain           {mesh.domain}")
    print(f"  cell areas       {({c: _fmt(a) for c, a in report['cell_areas'].items()})}")
    print(f"  total/domain     {_fmt(report['area_total'])} / {_fmt(report['domain_area'])}")
    print(f"  edge adjacency   {mesh.edge_adjacency()}")

    # ------------------------------------------------------------------
    _hr("2. Preview then publish a shared-edge split")
    preview = mesh.preview_split(edge=("A", "C"), t=Fraction(1, 2))
    print(f"  preview only, current version still {mesh.current_version()}")
    print(f"  would replace    {preview.replaced_cells}")
    print(f"  would create     {preview.new_cells} at new vertex {preview.new_vertex_id}")
    result = mesh.split_edge(edge=("A", "C"), t=Fraction(1, 2))
    tgt_v = result.version_id
    print(f"  published version {tgt_v}, cells {mesh.cell_ids(tgt_v)}")
    for cid in mesh.cell_ids(tgt_v):
        chain = [step["cell_id"] for step in mesh.cell_provenance(cid)["chain"]]
        print(f"    {cid:6} lineage {' -> '.join(chain)}")

    # ------------------------------------------------------------------
    _hr("3. Conservative remap with contribution matrix")
    wh = FieldWarehouse(field_path)
    src_id = wh.register_mesh(mesh.export(src_v), role="source", label="hand source")
    tgt_id = wh.register_mesh(mesh.export(tgt_v), role="target", label="split target")
    ds = wh.add_density(src_id, {"lower": 3, "upper": -2})
    run = wh.remap(src_id, ds, tgt_id, label="hand remap")
    print(f"  source integral  {_fmt(run.source_integral)}")
    print(f"  target integral  {_fmt(run.target_integral)}  gap {_fmt(run.conservation_gap)}")
    for cid in mesh.cell_ids(tgt_v):
        trace = wh.trace(run.run_id, cid)
        contribs = ", ".join(
            f"{c['source_cell_id']}:{_fmt(c['overlap_area'])}*{_fmt(c['source_density'])}"
            for c in trace["contributions"]
        )
        print(f"    {cid:6} rho={_fmt(trace['target']['density']):>4}  [{contribs}]")

    # ------------------------------------------------------------------
    _hr("4. Cancellation, mixed-sign targets and zero-area touching")
    # Four source columns across x in [0, 2]; densities alternate sign so the
    # two target halves each mix + and - contributions.
    s = grid_mesh(4, 1, (0, 0, 2, 1), diagonal="main", prefix="s")
    t = grid_mesh(2, 1, (0, 0, 2, 1), diagonal="anti", prefix="t")
    s_id = wh.register_mesh(s, role="source")
    t_id = wh.register_mesh(t, role="target")
    signs = {0: 1, 1: -1, 2: 2, 3: -2}
    values = {}
    for cid in s.cells:
        col = int(cid.split("c")[1].split("_")[0])
        values[cid] = signs[col]
    s_ds = wh.add_density(s_id, values)
    s_run = wh.remap(s_id, s_ds, t_id, label="cancellation")
    multi = [cid for cid in t.cells if len(wh.contributions(s_run.run_id, cid)) > 1]
    print(f"  target cells with multiple source contributions: {len(multi)}")
    for cid in sorted(t.cells):
        cs = wh.contributions(s_run.run_id, cid)
        print(f"    {cid}: " + ", ".join(
            f"{c['source_cell_id']} {_fmt(c['overlap_area'])}*{_fmt(c['source_density'])}"
            for c in cs
        ) + f" -> rho {_fmt(wh.target_cell(s_run.run_id, cid)['density'])}")
    print(f"  zero-area contribution rows stored: "
          f"{sum(1 for c in wh.contributions(s_run.run_id) if c['overlap_area'] == 0)}"
          f" (edge/vertex touching never contributes)")
    from .geometry import intersection_area, to_fraction

    touch_a = [(to_fraction(0), to_fraction(0)), (to_fraction(2), to_fraction(0)),
               (to_fraction(1), to_fraction(1))]
    touch_b = [(to_fraction(0), to_fraction(0)), (to_fraction(1), to_fraction(1)),
               (to_fraction(0), to_fraction(2))]
    print(f"  explicit edge-touch overlap area: {intersection_area(touch_a, touch_b)}")
    print(f"  global integral {_fmt(s_run.source_integral)} -> {_fmt(s_run.target_integral)}"
          f" (gap {_fmt(s_run.conservation_gap)})")

    # ------------------------------------------------------------------
    _hr("5. Missing source value cannot be published")
    partial = {cid: 1 for cid in sorted(s.cells)[:-1]}
    bad_ds = wh.add_density(s_id, partial, complete=False)
    print(f"  density set complete={wh.density_set_info(bad_ds)['complete']}")
    try:
        wh.remap(s_id, bad_ds, t_id)
    except IncompleteFieldError as exc:
        print(f"  remap refused: {exc}")

    # ------------------------------------------------------------------
    _hr("6. Real refinement invalidates the old area matrix")
    base_src = mesh.import_mesh(
        vertices={"A": (0, 0), "B": (2, 0), "C": (2, 1), "D": (0, 1)},
        cells={"lower": ("A", "B", "C"), "upper": ("A", "C", "D")},
        domain=(0, 0, 2, 1),
        note="refinement source v1",
    )
    target_mesh = grid_mesh(2, 2, (0, 0, 2, 1), diagonal="anti", prefix="x")
    b_src_id = wh.register_mesh(mesh.export(base_src), role="source")
    x_tgt_id = wh.register_mesh(target_mesh, role="target")
    b_ds = wh.add_density(b_src_id, {"lower": 4, "upper": 1})
    run1 = wh.remap(b_src_id, b_ds, x_tgt_id, label="before refinement")
    old_matrix = {cid: wh.contributions(run1.run_id, cid) for cid in target_mesh.cells}

    refined_v = mesh.split_edge(edge=("A", "C"), t=Fraction(1, 2)).version_id
    refined = mesh.export(refined_v)
    refined_id = wh.register_mesh(refined, role="source")
    refined_ds = wh.add_density(
        refined_id,
        {
            cid: (4 if cid.startswith("lower/") else 1)
            for cid in refined.cells
        },
    )
    run2 = wh.remap(refined_id, refined_ds, x_tgt_id, label="after refinement")
    new_matrix = {cid: wh.contributions(run2.run_id, cid) for cid in target_mesh.cells}
    stale = wh.is_run_stale(run1.run_id, source=refined, target=target_mesh)
    print(f"  old run source fp  {run1.source_fingerprint[:12]}…")
    print(f"  new run source fp  {run2.source_fingerprint[:12]}…")
    print(f"  old run stale for refined source: {stale['source_stale']}")
    print(f"  old matrix preserved: {old_matrix == {cid: wh.contributions(run1.run_id, cid) for cid in target_mesh.cells}}")
    print(f"  new matrix differs:   {new_matrix != old_matrix}")
    print(f"  old run integral {_fmt(run1.target_integral)}, new run integral {_fmt(run2.target_integral)}")

    # ------------------------------------------------------------------
    _hr("7. Subregion integration and persistence")
    rect = (0.25, 0.1, 1.5, 0.9)
    result = wh.integrate(run1.run_id, rect)
    direct = wh.integrate_source(b_ds, rect)
    print(f"  query            {rect}")
    print(f"  covered area     {_fmt(result.covered_area)}")
    print(f"  product integral {_fmt(result.integral)}")
    print(f"  direct source    {_fmt(direct.integral)} (subregion values need not")
    print("                    match; only the full-domain total is conserved)")

    wh.close()
    mesh.close()

    reopened_mesh = MeshService(mesh_path)
    reopened_wh = FieldWarehouse(field_path)
    trace = reopened_wh.trace(run1.run_id, sorted(target_mesh.cells)[0])
    print(f"  after reopen: version {reopened_mesh.current_version()} "
          f"has {len(reopened_mesh.cell_ids())} cells")
    print(f"  after reopen: {trace['target_cell_id'] if 'target_cell_id' in trace else trace['target']['cell_id']}"
          f" still has {trace['source_count']} source contribution(s)")
    reopened_wh.close()
    reopened_mesh.close()


if __name__ == "__main__":
    main()
