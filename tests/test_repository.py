"""End-to-end tests for the catalogue service and the repository.

These tests exercise SQLite persistence, immutable products, the exact
solver through the repository, gap/sample bookkeeping and the independent
re-validation command path.
"""

import tempfile
import unittest
from pathlib import Path

from stratcorr import synthetic
from stratcorr.catalog_service import CatalogError, CatalogService
from stratcorr.repo_db import BoreholeRepository, RepoError


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog_db = root / "catalog.db"
        self.repo_db = root / "repo.db"

    def tearDown(self):
        self.tmp.cleanup()

    def build_catalog(self, *, publish=True):
        service = CatalogService(self.catalog_db)
        service.init()
        product = synthetic.build_product()
        for horizon in product.horizons:
            service.add_horizon(
                horizon.horizon_id,
                horizon.name,
                sorted(horizon.acceptable_lithology),
                horizon.source,
            )
        for edge in product.edges:
            service.add_edge(edge.parent_id, edge.child_id, edge.source)
        if publish:
            published = service.publish("initial synthetic product")
            service.close()
            return published
        service.close()
        return product


class CatalogueServiceTest(ServiceTestBase):
    def test_self_loop_and_cycle_rejected(self):
        self.build_catalog(publish=False)
        service = CatalogService(self.catalog_db)
        with self.assertRaises(CatalogError):
            service.add_edge("H1", "H1", "bad:self")
        with self.assertRaises(CatalogError) as ctx:
            service.add_edge("H5", "H1", "bad:cycle")  # H1 -> ... -> H5 -> H1
        self.assertIn("cycle", str(ctx.exception).lower())
        service.close()

    def test_cyclic_draft_cannot_be_published_even_partially(self):
        service = CatalogService(self.catalog_db)
        service.init()
        service.add_horizon("A", "a", ["X"], "src-1")
        service.add_horizon("B", "b", ["Y"], "src-1")
        service.add_edge("A", "B", "src-1")
        # Force a cycle directly in the underlying draft tables, bypassing the
        # service guard, to prove the publish-time guard is independent.
        service.conn.execute(
            "INSERT INTO edge(parent_id, child_id, source) VALUES ('B','A','bad')"
        )
        service.conn.commit()
        with self.assertRaises(CatalogError):
            service.publish("must fail")
        self.assertEqual(service.list_products(), [])
        service.close()

    def test_products_are_versioned_immutable_and_provenanced(self):
        product = self.build_catalog()
        service = CatalogService(self.catalog_db)
        first = service.latest_product()
        self.assertEqual(first.product_id, product.product_id)
        service.add_lithology("H3", "GRAVEL", "correction-note-9")
        second = service.publish("lithology correction")
        self.assertEqual(second.version, first.version + 1)
        # The old product payload is untouched by the later correction.
        old = service.get_product(first.product_id)
        h3_old = next(h for h in old.horizons if h.horizon_id == "H3")
        self.assertNotIn("GRAVEL", h3_old.acceptable_lithology)
        provenance = service.provenance(second.product_id)
        lith = next(
            row for row in provenance["lithology"]
            if row["horizon_id"] == "H3" and row["lith_code"] == "GRAVEL"
        )
        self.assertEqual(lith["source"], "correction-note-9")
        edge_sources = {
            (row["parent_id"], row["child_id"]): row["source"]
            for row in provenance["edges"]
        }
        self.assertEqual(edge_sources[("H2", "H4")], "synthetic:log-B")
        service.close()

    def test_relation_evidence_and_sequence_queries(self):
        self.build_catalog()
        service = CatalogService(self.catalog_db)
        relation = service.query_relation("H1", "H6")
        self.assertTrue(relation["shallower_than"])
        self.assertEqual(relation["witness_path"][0], "H1")
        evidence = service.query_evidence("H3", "H4")
        self.assertTrue(evidence["incomparable"])
        sequence = service.validate_sequence(["H1", "H2", "H4", "H5"])
        self.assertTrue(sequence["valid"])
        bad = service.validate_sequence(["H4", "H3"])
        self.assertFalse(bad["valid"])
        service.close()


class RepositoryEndToEndTest(ServiceTestBase):
    def _seed_repo(self, product):
        repo = BoreholeRepository(self.repo_db)
        repo.init()
        repo.import_product(product, origin=f"catalog:{self.catalog_db}")
        boreholes = {b.borehole_id: b for b in synthetic.build_boreholes()}
        for bid in ("B1", "B2", "B3"):
            repo.import_borehole(boreholes[bid])
        for sample in synthetic.build_samples():
            repo.import_sample(sample)
        return repo, boreholes

    def test_full_correlation_pipeline_and_validation(self):
        product = self.build_catalog()
        repo, _ = self._seed_repo(product)
        result = repo.solve(product.product_id)
        self.assertEqual(result["status"], "selected")
        scheme_id = result["scheme_id"]
        # B2-S1 (SILTSTONE, weight 5) is accepted by no horizon and is the
        # single forced UNKNOWN contribution across the three boreholes.
        self.assertEqual(result["total_unknown_weight"], 5)

        assignments = {(a["borehole_id"], a["segment_id"]): a
                       for a in repo.get_assignments(scheme_id)}
        self.assertEqual(assignments[("B1", "B1-S1")]["horizon_id"], "H1")
        self.assertEqual(assignments[("B1", "B1-S5")]["horizon_id"], "H6")
        self.assertEqual(assignments[("B2", "B2-S1")]["is_unknown"], 1)
        # Fixations survive unchanged.
        self.assertEqual(assignments[("B3", "B3-S1")]["horizon_id"], "H1")
        self.assertEqual(assignments[("B3", "B3-S2")]["horizon_id"], "H4")

        # Gap attribution: same-horizon gap assigned, cross-horizon gap not.
        gaps = {g["gap_id"]: g for g in repo.get_gap_attribution(scheme_id)}
        self.assertEqual(gaps["B1-G1"]["horizon_id"], "H2")
        self.assertIsNone(gaps["B1-G2"]["horizon_id"])
        self.assertIsNone(gaps["B2-G1"]["horizon_id"])  # above first segment

        stats = {(s["borehole_id"], s["horizon_id"]): s
                 for s in repo.get_stats(scheme_id)}
        h2 = stats[("B1", "H2")]
        self.assertEqual(h2["cored_thickness"], "3/1")   # S2(2) + S3(1)
        self.assertEqual(h2["gap_thickness"], "2/1")     # only B1-G1
        self.assertEqual(h2["top_depth"], "3/1")
        self.assertEqual(h2["bottom_depth"], "8/1")

        # Sample boundary semantics and UNKNOWN exclusion.
        samples = {s["sample_id"]: s
                   for s in repo.get_sample_membership(scheme_id)}
        self.assertEqual(samples["SM1"]["status"], "HIT")
        # SM2 sits at B1-S1's end 2/1; because S2 only starts at 3/1 there is
        # an uncored (but undeclared) interval, and half-open [start,end) makes
        # this depth a miss rather than a hit in either segment.
        self.assertEqual(samples["SM2"]["status"], "MISS")
        self.assertEqual(samples["SM3"]["status"], "GAP")
        self.assertEqual(samples["SM5"]["status"], "MISS")       # deepest end
        self.assertEqual(samples["SM7"]["status"], "UNKNOWN_HIT")
        self.assertIsNone(samples["SM7"]["horizon_id"])
        self.assertEqual(samples["SM9"]["status"], "MISS")
        self.assertEqual(samples["SM10"]["status"], "AMBIGUOUS")

        # A depth exactly on a touching segment boundary belongs to the next
        # (deeper) segment under [start, end): B3-S2 starts at 2/1.
        from stratcorr.models import SampleInput

        repo.import_sample(SampleInput("SM-BOUND", "2/1", "B3"))
        # Re-solve so the new sample is evaluated by the scheme machinery.
        boundary_scheme = repo.solve(product.product_id)["scheme_id"]
        boundary = {
            s["sample_id"]: s
            for s in repo.get_sample_membership(boundary_scheme)
        }["SM-BOUND"]
        self.assertEqual(boundary["status"], "HIT")
        self.assertEqual(boundary["segment_id"], "B3-S2")
        self.assertEqual(boundary["horizon_id"], "H4")

        # Independent re-derivation must match every stored row.
        report = repo.validate(scheme_id)
        self.assertTrue(report["ok"], report["errors"])

        explanation = repo.explain(scheme_id)
        b1 = next(b for b in explanation["boreholes"] if b["borehole_id"] == "B1")
        transitions = {(t["from"], t["to"]): t["shallower_than_path"]
                       for t in b1["horizon_paths"]}
        self.assertIn(("H2", "H4"), transitions)
        self.assertEqual(transitions[("H2", "H4")], ["H2", "H4"])
        repo.close()

    def test_contradiction_is_persisted_and_never_silently_cleared(self):
        product = self.build_catalog()
        repo, boreholes = self._seed_repo(product)
        repo.import_borehole(boreholes["B4"])
        result = repo.solve(product.product_id)
        self.assertEqual(result["status"], "contradiction")
        scheme_id = result["scheme_id"]
        details = repo.get_contradiction(scheme_id)
        self.assertEqual(details[0]["kind"], "fixed_order")
        self.assertEqual(
            [f["fixed_horizon"] for f in details[0]["related_fixes"]],
            ["H4", "H1"],
        )
        self.assertIsNotNone(details[0]["relations"])
        # A contradiction scheme stores no assignments and validates as such.
        self.assertEqual(repo.get_assignments(scheme_id), [])
        self.assertTrue(repo.validate(scheme_id)["ok"])
        repo.close()

    def test_correction_changes_scheme_and_samples_but_keeps_old_fixed(self):
        product = self.build_catalog()
        repo, _ = self._seed_repo(product)
        first = repo.solve(product.product_id)
        first_id = first["scheme_id"]

        # Publish a genuine correction: H5 -> H6 disappears, H6 unreachable.
        catalog = CatalogService(self.catalog_db)
        catalog.remove_edge("H5", "H6")
        corrected = catalog.publish("remove H5->H6")
        catalog.close()
        repo.import_product(corrected, origin=f"catalog:{self.catalog_db}")
        second = repo.solve(corrected.product_id)
        self.assertEqual(second["status"], "selected")
        second_id = second["scheme_id"]
        self.assertNotEqual(first_id, second_id)

        old_assign = repo.get_assignments(first_id)
        new_assign = repo.get_assignments(second_id)
        old_map = {(a["borehole_id"], a["segment_id"]): a for a in old_assign}
        new_map = {(a["borehole_id"], a["segment_id"]): a for a in new_assign}
        # B1 LIMESTONE segment was H6 and genuinely becomes UNKNOWN.
        self.assertEqual(old_map[("B1", "B1-S5")]["horizon_id"], "H6")
        self.assertEqual(new_map[("B1", "B1-S5")]["is_unknown"], 1)
        # Sample at 12/1 was an H6 hit and now lands in an UNKNOWN segment.
        new_samples = {s["sample_id"]: s
                       for s in repo.get_sample_membership(second_id)}
        self.assertEqual(new_samples["SM4"]["status"], "UNKNOWN_HIT")
        # The old scheme remains fixed and still re-validates.
        old_samples = {s["sample_id"]: s
                       for s in repo.get_sample_membership(first_id)}
        self.assertEqual(old_samples["SM4"]["horizon_id"], "H6")
        self.assertTrue(repo.validate(first_id)["ok"])
        self.assertTrue(repo.validate(second_id)["ok"])
        # Both schemes are listed and reference distinct products.
        products = {row["product_id"] for row in repo.list_schemes()}
        self.assertEqual(products, {product.product_id, corrected.product_id})
        repo.close()

    def test_external_product_is_accepted_and_independent(self):
        product = self.build_catalog()
        external_path = Path(self.tmp.name) / "external.json"
        external_path.write_text(product.to_json(), encoding="utf-8")

        repo = BoreholeRepository(self.repo_db)
        repo.init()
        imported = repo.import_product_file(external_path)
        self.assertEqual(imported.kind, "external")
        # Re-importing the same product id is refused rather than overwritten.
        with self.assertRaises(RepoError):
            repo.import_product_file(external_path)
        provenance = repo.product_provenance(imported.product_id)
        self.assertEqual(provenance["kind"], "external")
        self.assertTrue(provenance["origin"].startswith("file:"))
        repo.close()

    def test_invalid_borehole_geometry_rejected(self):
        product = self.build_catalog()
        repo = BoreholeRepository(self.repo_db)
        repo.init()
        repo.import_product(product, origin="external")
        bad = synthetic.build_boreholes()[0]
        from stratcorr.models import BoreholeInput, SegmentInput

        broken = BoreholeInput(
            bad.borehole_id,
            bad.segments[:1]
            + (SegmentInput("BAD", "1/1", "1/1", "CLAY", 1),),
        )
        with self.assertRaises(Exception):
            repo.import_borehole(broken)
        repo.close()


class LimitsTest(ServiceTestBase):
    def test_horizon_and_borehole_limits_enforced(self):
        service = CatalogService(self.catalog_db)
        service.init()
        for i in range(12):
            service.add_horizon(f"H{i}", f"h{i}", ["X"], "src")
        with self.assertRaises(CatalogError):
            service.add_horizon("H12", "h12", ["X"], "src")
        service.close()

        product = synthetic.build_product()
        repo = BoreholeRepository(self.repo_db)
        repo.init()
        repo.import_product(product, origin="external")
        from stratcorr.models import BoreholeInput, SegmentInput

        for i in range(8):
            hole = BoreholeInput(
                f"K{i}",
                (SegmentInput(f"K{i}-S1", "0/1", "1/1", "CLAY", 1),),
            )
            repo.import_borehole(hole)
        overflow = BoreholeInput(
            "K8", (SegmentInput("K8-S1", "0/1", "1/1", "CLAY", 1),)
        )
        with self.assertRaises(RepoError):
            repo.import_borehole(overflow)
        repo.close()

    def test_per_hole_segment_limit_enforced(self):
        product = synthetic.build_product()
        repo = BoreholeRepository(self.repo_db)
        repo.init()
        repo.import_product(product, origin="external")
        from stratcorr.models import BoreholeInput, SegmentInput

        too_many = BoreholeInput(
            "BIG",
            tuple(
                SegmentInput(f"S{i}", f"{i}/1", f"{i + 1}/1", "CLAY", 1)
                for i in range(21)
            ),
        )
        with self.assertRaises(RepoError):
            repo.import_borehole(too_many)
        repo.close()


if __name__ == "__main__":
    unittest.main()
