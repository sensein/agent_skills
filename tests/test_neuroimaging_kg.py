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
