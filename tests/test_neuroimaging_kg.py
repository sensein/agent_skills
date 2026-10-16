"""Unit tests for the neuroimaging-kg skill's scripts.

Covers scripts/record_activity.py (PROV for an analysis step), join_identity.py
(reviewed identity links between a local table and a NIDM graph),
validate_graph.py (core SHACL shapes and the derivation-edge mutation test) and
query_graph.py (named graphs in Oxigraph, competency queries compared to gold).

The scripts need rdflib, pyshacl and pyoxigraph. When those are not installed
(the plain standard-library CI job) the whole module is skipped; the dedicated
CI job installs them and runs it.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "neuroimaging-kg"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "neuroimaging_kg"
sys.path.insert(0, str(SKILL / "scripts"))

DEPS = all(importlib.util.find_spec(m) for m in ("rdflib", "pyshacl", "pyoxigraph"))


def _rows() -> list[dict]:
    with open(FIXTURES / "phenotype.csv", newline="") as fh:
        return list(csv.DictReader(fh))


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class RecordActivityTest(unittest.TestCase):
    def test_activity_links_inputs_outputs_software_and_agent(self):
        import record_activity as ra
        from rdflib import Literal, Namespace, RDF

        prov = Namespace("http://www.w3.org/ns/prov#")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cohort.csv"
            out.write_text("participant_id\n1\n3\n")
            g = ra.record(
                activity_id="qc-cohort",
                label="Select participants with mean FD < 0.2 mm",
                used=[str(FIXTURES / "phenotype.csv")],
                generated=[str(out)],
                software=["pandas==2.2.3"],
                params={"mean_fd_max": "0.2"},
                agent="Analyst A",
                agent_kind="person",
            )
        acts = list(g.subjects(RDF.type, prov.Activity))
        self.assertEqual(len(acts), 1)
        act = acts[0]
        used = list(g.objects(act, prov.used))
        self.assertEqual(len(used), 1)
        gen = list(g.subjects(prov.wasGeneratedBy, act))
        self.assertEqual(len(gen), 1)
        self.assertIn(used[0], set(g.objects(gen[0], prov.wasDerivedFrom)))
        sha = ra.sha256_file(FIXTURES / "phenotype.csv")
        self.assertIn(Literal(sha), set(g.objects(used[0], ra.CRYPTO.sha256)))
        agents = set(g.objects(act, prov.wasAssociatedWith))
        self.assertEqual(len(agents), 2)  # the person and the software
        kinds = {o for a in agents for o in g.objects(a, RDF.type)}
        self.assertIn(prov.SoftwareAgent, kinds)
        self.assertIn(prov.Person, kinds)
        self.assertIn(Literal("0.2"), {o for _, _, o in g.triples((None, ra.NKG.value, None))})

    def test_ai_agent_is_recorded_with_its_model(self):
        import record_activity as ra
        from rdflib import Literal

        g = ra.record(activity_id="chart", label="Chart one paper", agent="Claude Sonnet", agent_kind="ai",
                      model="claude-sonnet-5")
        self.assertIn(Literal("claude-sonnet-5"), set(g.objects(None, ra.NKG.model)))


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class JoinIdentityTest(unittest.TestCase):
    def setUp(self):
        from rdflib import Graph

        self.ext = Graph().parse(FIXTURES / "external_nidm.ttl")

    def test_counts_and_conflicts(self):
        import join_identity as ji

        _, report = ji.join(_rows(), "participant_id", self.ext, normalize="int",
                            checks={"age": "http://iri.nidash.org/age"}, reviewer="Reviewer R")
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["only_local"], ["4"])
        self.assertEqual(report["only_external"], ["0009"])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["local_id"], "3")
        self.assertEqual(report["checks_compared"], {"age": 3})

    def test_check_that_finds_nothing_reports_zero_comparisons(self):
        import join_identity as ji

        _, report = ji.join(_rows(), "participant_id", self.ext, normalize="int",
                            checks={"age": "about:http://example.org/not-a-concept"})
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["checks_compared"], {"age": 0})

    def test_check_by_concept_and_source_variable(self):
        import join_identity as ji

        for target in ("about:http://uri.interlex.org/ilx_0100400", "var:AGE_AT_SCAN"):
            _, report = ji.join(_rows(), "participant_id", self.ext, normalize="int", checks={"age": target})
            self.assertEqual([c["local_id"] for c in report["conflicts"]], ["3"], target)

    def test_links_are_reviewable_not_sameas(self):
        import join_identity as ji
        from rdflib import OWL

        g, _ = ji.join(_rows(), "participant_id", self.ext, normalize="int", reviewer="Reviewer R")
        self.assertEqual(len(list(g.triples((None, OWL.sameAs, None)))), 0)
        statuses = {str(o) for o in g.objects(None, ji.NKG.reviewStatus)}
        self.assertEqual(statuses, {"proposed"})
        self.assertEqual(len(list(g.subjects(None, ji.NKG.IdentityLink))), 3)


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class ValidateGraphTest(unittest.TestCase):
    def _graph(self, tmp: Path):
        import record_activity as ra

        out = tmp / "cohort.csv"
        out.write_text("participant_id\n1\n")
        g = ra.record(activity_id="qc", label="QC", used=[str(FIXTURES / "phenotype.csv")],
                      generated=[str(out)], software=["pandas==2.2.3"], agent="Analyst A")
        return g

    def test_recorded_activity_conforms(self):
        import validate_graph as vg

        with tempfile.TemporaryDirectory() as tmp:
            g = self._graph(Path(tmp))
        ok, n, _ = vg.validate(g)
        self.assertTrue(ok, f"{n} violations")

    def test_removing_derivation_edge_fails(self):
        import validate_graph as vg

        with tempfile.TemporaryDirectory() as tmp:
            g = self._graph(Path(tmp))
        res = vg.mutation_test(g)
        self.assertTrue(res["baseline_conforms"])
        self.assertGreater(res["removed_triples"], 0)
        self.assertFalse(res["mutated_conforms"])

    def test_no_consent_is_not_a_scope(self):
        import validate_graph as vg
        from rdflib import BNode, Graph, Literal, RDF

        g = Graph()
        d = BNode()
        g.add((d, RDF.type, vg.NKG.SensitivityProfile))
        g.add((d, vg.NKG.consentScope, Literal("none")))
        ok, _, msgs = vg.validate(g)
        self.assertFalse(ok)
        self.assertTrue(any("consent" in m.lower() for m in msgs))


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class QueryGraphTest(unittest.TestCase):
    def test_named_graph_query_matches_gold(self):
        import query_graph as qg

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "q").mkdir()
            (tmp / "q" / "n_participants.rq").write_text(
                "PREFIX ndar: <https://ndar.nih.gov/api/datadictionary/v2/dataelement/>\n"
                "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { GRAPH <urn:g:external> { ?p ndar:src_subject_id ?id } }\n")
            store = qg.load({"urn:g:external": str(FIXTURES / "external_nidm.ttl")})
            results = qg.run_queries(store, tmp / "q")
            self.assertEqual(results["n_participants"], [{"n": "4"}])
            ok = qg.compare(results, {"n_participants": [{"n": "4"}]})
            self.assertTrue(ok["n_participants"]["match"])
            bad = qg.compare(results, {"n_participants": [{"n": "5"}]})
            self.assertFalse(bad["n_participants"]["match"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class NidmVolumesTest(unittest.TestCase):
    def test_volumes_by_participant_tool_and_side_in_mm3_only(self):
        import nidm_volumes as nv

        rows = nv.extract([str(FIXTURES / "site_volumes_nidm.ttl")], [str(FIXTURES / "tool_cde.ttl")],
                          region="http://purl.obolibrary.org/obo/UBERON_0001954", unit="mm^3")
        got = sorted((r["src_subject_id"], r["tool"], r["side"], r["value"]) for r in rows)
        self.assertEqual(got, [("1001", "FSL", "Left", 3700.0), ("1001", "FreeSurfer", "Left", 4400.0),
                               ("1001", "FreeSurfer", "Right", 4300.0), ("1002", "FreeSurfer", "Left", 4000.0),
                               ("1002", "FreeSurfer", "Right", 4100.0)])

    def test_unit_filter_excludes_voxel_counts(self):
        import nidm_volumes as nv

        rows = nv.extract([str(FIXTURES / "site_volumes_nidm.ttl")], [str(FIXTURES / "tool_cde.ttl")],
                          region="http://purl.obolibrary.org/obo/UBERON_0001954", unit="voxel")
        self.assertEqual([(r["src_subject_id"], r["value"]) for r in rows], [("1001", 4410.0)])


@unittest.skipUnless(DEPS, "rdflib, pyshacl and pyoxigraph are required")
class CompareGraphsTest(unittest.TestCase):
    def setUp(self):
        import compare_graphs as cg

        self.cg = cg
        self.rep = cg.compare({"labA": [str(FIXTURES / "contributor_a.ttl")],
                               "labB": [str(FIXTURES / "contributor_b.ttl")]}, normalize="int", rtol=0.05)
        self.pair = self.rep["pairs"]["labA|labB"]

    def test_identifier_overlap(self):
        ids = self.pair["participants"]
        self.assertEqual((ids["matched"], ids["only_a"], ids["only_b"]), (2, ["3"], ["4"]))
        self.assertEqual(self.pair["publications"]["matched"], ["10.1016/j.neuroimage.2016.10.045"])
        self.assertEqual(self.pair["datasets"]["version_mismatch"],
                         [{"dataset": "urn:ds:abide_i", "a": ["1.0"], "b": ["1.1"]}])

    def test_vocabulary_alignment_and_review_candidates(self):
        voc = self.pair["vocabulary"]
        self.assertIn("http://purl.obolibrary.org/obo/UBERON_0001954", voc["shared_concepts"])
        cands = {(c["a"], c["b"]) for c in voc["mapping_candidates"]}
        self.assertIn(("urn:lab-a:hand", "urn:lab-b:hand"), cands)
        self.assertEqual({c["review_status"] for c in voc["mapping_candidates"]}, {"proposed"})

    def test_value_and_definition_conflicts(self):
        conf = self.pair["conflicts"]
        vals = {(c["participant"], c["concept"].rsplit("_", 1)[-1]) for c in conf["values"]}
        self.assertIn(("2", "0001954"), vals)       # 4100 vs 4900 beyond 5 %
        self.assertNotIn(("1", "0001954"), vals)    # 4000 vs 4010 within tolerance
        csf = [c for c in conf["values"] if c["concept"].endswith("UBERON_0001359")]
        self.assertEqual(len(csf), 1)
        self.assertGreater(csf[0]["ratio"], 100)
        self.assertTrue(csf[0]["definitions_differ"])
        self.assertEqual(conf["cohorts"], [{"cohort": "mean FD < 0.2 mm", "only_a": ["2"], "only_b": []}])

    def test_outputs_never_assert_sameas_and_keep_attribution(self):
        from rdflib import Graph, OWL, URIRef

        with tempfile.TemporaryDirectory() as tmp:
            review, attribution = Path(tmp) / "review.ttl", Path(tmp) / "contributors.ttl"
            self.cg.write_review(self.rep, review)
            self.cg.write_attribution(self.rep, attribution, {"labA": "Lab A", "labB": "Lab B"})
            rg, ag = Graph().parse(review), Graph().parse(attribution)
            md = self.cg.to_markdown(self.rep)
        self.assertEqual(len(list(rg.triples((None, OWL.sameAs, None)))), 0)
        skos = "http://www.w3.org/2004/02/skos/core#"
        self.assertEqual([t for t in rg if str(t[1]).startswith(skos)], [])
        prov = "http://www.w3.org/ns/prov#"
        self.assertEqual(len(list(ag.triples((None, URIRef(prov + "wasAttributedTo"), None)))), 2)
        self.assertIn("Only in labA", md)

    def test_elements_sharing_a_concept_are_paired_by_source_variable(self):
        import compare_graphs as cg
        from rdflib import Graph

        ttl = """@prefix prov: <http://www.w3.org/ns/prov#> . @prefix nidm: <http://purl.org/nidash/nidm#> .
        @prefix ndar: <https://ndar.nih.gov/api/datadictionary/v2/dataelement/> . @prefix x: <urn:%s:> .
        x:p ndar:src_subject_id "7" .
        x:dsm nidm:isAbout <http://ncitt.ncit.nih.gov/Diagnosis> ; nidm:sourceVariable "DSM_IV_TR" .
        x:dx nidm:isAbout <http://ncitt.ncit.nih.gov/Diagnosis> ; nidm:sourceVariable "DX_GROUP" .
        x:act prov:qualifiedAssociation [ prov:agent x:p ] . x:e prov:wasGeneratedBy x:act ; x:dsm %s ; x:dx 1 ."""
        with tempfile.TemporaryDirectory() as tmp:
            fa, fb = Path(tmp) / "a.ttl", Path(tmp) / "b.ttl"
            fa.write_text(ttl % ("a", "1"))
            fb.write_text(ttl % ("b", "2"))
            pair = cg.compare({"a": [str(fa)], "b": [str(fb)]})["pairs"]["a|b"]
        self.assertEqual(pair["conflicts"]["ambiguous_concepts"], [])
        self.assertEqual([(c["a"], c["b"]) for c in pair["conflicts"]["values"]], [("1", "2")])
