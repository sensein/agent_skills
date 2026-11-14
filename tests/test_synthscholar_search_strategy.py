"""
No-network regression checks for SynthScholar's search-strategy engine
(skills/synthscholar/scripts/search_strategy/): closure-mode facet voting,
deterministic MeSH closure, and coverage-gap recovery.

The voting / heuristic tests need nothing. The closure and coverage-gap tests
use the real local MeSH index (a static, offline fixture built with
``generate_search_strategy.py --build-index``); they skip when it is absent,
as on CI.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "synthscholar" / "scripts"))

try:
    import requests  # noqa: F401
except ImportError:
    # The engine only touches requests when it calls a model or PubMed, which
    # these tests never do; a placeholder lets the modules import on CI.
    sys.modules["requests"] = types.ModuleType("requests")

from search_strategy import closure, facet_pipeline, facets  # noqa: E402
from search_strategy.mesh_index import DEFAULT_DB, get_index  # noqa: E402

HAVE_INDEX = os.path.exists(DEFAULT_DB) and os.path.getsize(DEFAULT_DB) > 0
needs_index = unittest.skipUnless(
    HAVE_INDEX, "MeSH index not built (generate_search_strategy.py --build-index)")


class HeuristicFacetTests(unittest.TestCase):
    def test_splits_on_conjunctions_and_guesses_roles(self):
        q = "Effect of RNA sequencing on hippocampal neurons in Alzheimer Disease patients"
        names = [f["name"] for f in facets.heuristic_facets(q)]
        self.assertTrue(any("RNA sequencing" in n for n in names))
        self.assertTrue(any("Alzheimer Disease" in n for n in names))

    def test_deterministic_across_runs(self):
        q = "Effect of metformin on cardiovascular outcomes in type 2 diabetes patients"
        first = facets.heuristic_facets(q)
        for _ in range(5):
            self.assertEqual(facets.heuristic_facets(q), first)


class VotingTests(unittest.TestCase):
    def test_majority_span_wins_over_minority(self):
        # 2 of 3 runs agree on the same (start,end); the 3rd run disagrees
        # slightly -- majority should win, and agreement count is exact.
        runs = [
            [{"name": "RNA sequencing", "role": "method", "start": 5, "end": 6}],
            [{"name": "RNA sequencing", "role": "method", "start": 5, "end": 6}],
            [{"name": "the RNA sequencing step", "role": "method", "start": 4, "end": 7}],
        ]
        voted = facets._cluster_and_vote(runs, min_runs_agree=2)
        self.assertEqual(len(voted), 1)
        self.assertEqual((voted[0]["start"], voted[0]["end"]), (5, 6))
        self.assertEqual(voted[0]["agreement"], 3)  # all 3 runs overlap this span

    def test_below_threshold_cluster_is_dropped(self):
        runs = [
            [{"name": "a", "role": "other", "start": 0, "end": 0}],
            [],
            [],
        ]
        self.assertEqual(facets._cluster_and_vote(runs, min_runs_agree=2), [])

    def test_role_normalizer_rejects_echoed_enum(self):
        self.assertEqual(facets._normalize_pico_role(
            "population|intervention|comparator|outcome|method|context"), "other")
        self.assertEqual(facets._normalize_pico_role("Population"), "population")
        self.assertEqual(facets._normalize_pico_role("garbage"), "other")

    def test_no_key_falls_back_to_heuristic(self):
        q = "Does metformin reduce cardiovascular mortality in patients with type 2 diabetes?"
        old = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            seg = facets.segment(q, model="anthropic/claude-haiku-4.5", k=3)
        finally:
            if old is not None:
                os.environ["OPENROUTER_API_KEY"] = old
        self.assertEqual(seg["mode"], "heuristic")
        self.assertEqual(seg["facets"], facets.heuristic_facets(q))


class AgentSamplesTests(unittest.TestCase):
    """The host agent as the model: its answers go through the same parse and
    vote as an OpenRouter reply (facets.segment_from_samples)."""

    Q = "Does metformin reduce cardiovascular mortality in patients with type 2 diabetes?"
    ANSWER = ('{"facets": [{"text": "metformin", "role": "intervention"}, '
              '{"text": "cardiovascular mortality", "role": "outcome"}, '
              '{"text": "patients with type 2 diabetes", "role": "population"}]}')

    def test_samples_vote_like_model_replies(self):
        parsed = [facets._parse_facet_response(self.Q, self.ANSWER)] * 3
        expected = facets._vote_or_fallback(self.Q, parsed, [], 0.5)
        got = facets.segment_from_samples(self.Q, [self.ANSWER] * 3)
        self.assertEqual(got, expected)
        self.assertEqual(got["mode"], "llm_voted")
        self.assertEqual([f["name"] for f in got["facets"]],
                         ["metformin", "cardiovascular mortality",
                          "patients with type 2 diabetes"])

    def test_malformed_answer_is_a_failed_sample(self):
        got = facets.segment_from_samples(
            self.Q, ['{"facets": [', "no json here", self.ANSWER])
        self.assertEqual(got["mode"], "llm_voted")
        self.assertEqual(got["runs"], 1)
        self.assertEqual(len(got["errors"]), 2)

    def test_null_facets_is_a_failed_sample(self):
        got = facets.segment_from_samples(self.Q, ['{"facets": null}', self.ANSWER])
        self.assertEqual((got["mode"], got["runs"], len(got["errors"])), ("llm_voted", 1, 1))

    def test_transport_error_is_one_failed_sample(self):
        def boom(*a, **k):
            raise OSError("connection refused")      # requests' errors are OSErrors
        old = getattr(facets.requests, "post", None)
        facets.requests.post = boom
        try:
            with self.assertRaises(facets.FacetLLMError):
                facets._one_llm_pass(self.Q, model="ollama/x", api_key=None, seed=1)
        finally:
            if old is None:
                del facets.requests.post
            else:
                facets.requests.post = old

    def test_llm_answer_shapes(self):
        from search_strategy import openrouter_client as oc
        self.assertEqual(oc._strs("Anxiety"), ["Anxiety"])
        with self.assertRaises(oc.OpenRouterError):
            oc.parse_proposal('{"concepts": "oops"}', "m")

    def test_prompt_is_the_one_sent_to_openrouter(self):
        messages = facets.facet_messages(self.Q)
        self.assertEqual(messages[0], {"role": "system", "content": facets.FACET_SYSTEM_PROMPT})
        self.assertEqual(messages[1], {"role": "user", "content": self.Q})


class IndexBuildTests(unittest.TestCase):
    """The MeSH index builds from the compressed dump, and the automatic
    path leaves nothing half-built behind when it fails."""

    # real lines from mesh2026.nt (D000001 Calcimycin), plus one altLabel
    TRIPLES = """\
<http://id.nlm.nih.gov/mesh/2026/D000001> <http://www.w3.org/2000/01/rdf-schema#label> "Calcimycin"@en .
<http://id.nlm.nih.gov/mesh/2026/D000001> <http://id.nlm.nih.gov/mesh/vocab#treeNumber> <http://id.nlm.nih.gov/mesh/2026/D03.633.100.221.173> .
<http://id.nlm.nih.gov/mesh/2026/D000001> <http://id.nlm.nih.gov/mesh/vocab#preferredConcept> <http://id.nlm.nih.gov/mesh/2026/M0000001> .
<http://id.nlm.nih.gov/mesh/2026/M0000001> <http://id.nlm.nih.gov/mesh/vocab#preferredTerm> <http://id.nlm.nih.gov/mesh/2026/T000002> .
<http://id.nlm.nih.gov/mesh/2026/T000002> <http://id.nlm.nih.gov/mesh/vocab#prefLabel> "Calcimycin"@en .
<http://id.nlm.nih.gov/mesh/2026/T000002> <http://id.nlm.nih.gov/mesh/vocab#altLabel> "A23187"@en .
"""

    def setUp(self):
        from search_strategy import build_index
        self.build_index = build_index
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _gz(self) -> Path:
        path = self.dir / "mesh_fixture.nt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(self.TRIPLES)
        return path

    def test_builds_from_gzipped_dump(self):
        db = str(self.dir / "mesh.sqlite")
        with contextlib.redirect_stdout(io.StringIO()):
            self.build_index.build(str(self._gz()), db)
        self.assertEqual(self.build_index.index_descriptors(db), 1)
        con = sqlite3.connect(db)
        try:
            terms = {r[0] for r in con.execute("SELECT term FROM entry_term")}
            trees = [r[0] for r in con.execute("SELECT tree FROM tree")]
        finally:
            con.close()
        self.assertEqual(terms, {"Calcimycin", "A23187"})
        self.assertEqual(trees, ["D03.633.100.221.173"])

    def test_builds_to_a_bare_filename(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.build_index.build(str(self._gz()), "bare.sqlite")
            self.assertEqual(self.build_index.index_descriptors("bare.sqlite"), 1)
        finally:
            os.chdir(old)

    def test_failed_write_leaves_no_partial_file(self):
        db = self.dir / "mesh.sqlite"
        real = sqlite3.connect

        class Full:                      # a connection whose writes fail, like a full disk
            def __init__(self, path):
                self._c = real(path)

            def executescript(self, sql):
                raise sqlite3.OperationalError("database or disk is full")

            def close(self):
                self._c.close()

        self.build_index.sqlite3.connect = Full
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sqlite3.Error):
                self.build_index.build(str(self._gz()), str(db))
        finally:
            self.build_index.sqlite3.connect = real
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["mesh_fixture.nt.gz"])

    def test_missing_or_partial_index_counts_as_absent(self):
        self.assertEqual(self.build_index.index_descriptors(str(self.dir / "none.sqlite")), 0)
        empty = self.dir / "empty.sqlite"
        empty.write_bytes(b"")
        self.assertEqual(self.build_index.index_descriptors(str(empty)), 0)

    def test_ensure_rejects_a_too_small_dump_and_cleans_up(self):
        db = self.dir / "mesh.sqlite"
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.build_index.ensure(str(db), url=self._gz().as_uri())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["mesh_fixture.nt.gz"])

    def test_ensure_failed_download_leaves_nothing(self):
        db = self.dir / "mesh.sqlite"
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.build_index.ensure(str(db), url=(self.dir / "absent.nt.gz").as_uri())
        self.assertEqual(list(self.dir.iterdir()), [])


class ScreeningTests(unittest.TestCase):
    """screen_records.py: search results -> agent decisions -> corpus.json,
    screening log and PRISMA counts."""

    def setUp(self):
        import screen_records
        self.sr = screen_records
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rows = [{"pmid": str(100 + i), "doi": f"10.1/x{i}", "title": f"Trial {i}",
                 "authors": "Doe J; Roe R", "journal": "J", "year": "2024",
                 "abstract": "MBSR and anxiety.", "pub_types": "Journal Article",
                 "mesh_terms": "Anxiety; Mindfulness"} for i in range(5)]
        rows.append(dict(rows[0]))                       # duplicate PMID: dropped
        self.results = self.dir / "results.jsonl"
        self.results.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self.protocol = self.dir / "protocol.json"
        self.protocol.write_text(json.dumps({
            "objective": "MBSR for anxiety", "inclusion_criteria": "RCTs of MBSR",
            "exclusion_criteria": "reviews"}), encoding="utf-8")
        self.work = self.dir / "screening"
        self.prov = self.dir / "search_provenance.json"
        self.prov.write_text(json.dumps({"searches": [], "total_identified": 5}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv) -> int:
        old = sys.argv
        sys.argv = ["screen_records.py", *map(str, argv)]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return self.sr.main()
        finally:
            sys.argv = old

    def _plan(self):
        return self._run("plan", self.results, "--protocol", self.protocol,
                         "--workdir", self.work, "--batch-size", 2)

    def _decide(self, n: int, rows: list[dict]):
        (self.work / f"decisions_{n:03d}.json").write_text(
            json.dumps({"decisions": rows}), encoding="utf-8")

    def _decide_all(self):
        self._decide(1, [{"pmid": "100", "decision": "include", "reason": "RCT of MBSR"},
                         {"pmid": "101", "decision": "exclude", "reason": "exclusion: review"}])
        self._decide(2, [{"pmid": "102", "decision": "unsure", "reason": ""},
                         {"pmid": "103", "decision": "exclude", "reason": "inclusion: not MBSR"}])
        self._decide(3, [{"pmid": "104", "decision": "include", "reason": "RCT"}])

    def test_plan_batches_records_once(self):
        self.assertEqual(self._plan(), 0)
        plan = json.loads((self.work / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["n_records"], plan["n_batches"]), (5, 3))
        self.assertEqual(self._run("status", "--workdir", self.work), 1)   # nothing decided

    def test_plan_needs_eligibility_criteria(self):
        self.protocol.write_text(json.dumps({"objective": "x"}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._plan()

    def test_invalid_decisions_are_rejected(self):
        self._plan()
        bad = {
            "missing pmid": [{"pmid": "100", "decision": "include", "reason": ""}],
            "unknown pmid": [{"pmid": "999", "decision": "include", "reason": ""}],
            "bad value": [{"pmid": "100", "decision": "maybe", "reason": ""},
                          {"pmid": "101", "decision": "include", "reason": ""}],
            "exclude without reason": [{"pmid": "100", "decision": "exclude", "reason": ""},
                                       {"pmid": "101", "decision": "include", "reason": ""}],
        }
        for label, rows in bad.items():
            with self.subTest(label):
                self._decide(1, rows)
                state, _ = self.sr._check_batch(self.work, 1)
                self.assertTrue(state.startswith("invalid"), state)

    def test_apply_keeps_include_and_unsure_and_counts(self):
        self._plan()
        self._decide_all()
        self.assertEqual(self._run("status", "--workdir", self.work), 0)
        corpus = self.dir / "corpus.json"
        self.assertEqual(self._run("apply", "--workdir", self.work, "--corpus-out", corpus,
                                   "--provenance", self.prov, "--model-name", "test-model"), 0)
        data = json.loads(corpus.read_text(encoding="utf-8"))
        self.assertEqual([i["pmid"] for i in data["items"]], ["100", "102", "104"])
        self.assertEqual(data["provenance"], "pubmed_search_screened")
        self.assertEqual(data["items"][0]["source"], "PubMed")
        self.assertEqual(data["items"][0]["mesh_terms"], ["Anxiety", "Mindfulness"])
        prov = json.loads(self.prov.read_text(encoding="utf-8"))
        self.assertEqual((prov["records_screened"], prov["records_excluded_title_abstract"],
                          prov["reports_sought"]), (5, 2, 3))
        log = [json.loads(line) for line in
               (self.work / "screening_log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(log), 5)
        self.assertTrue(all(r["screened_by"] == "test-model" for r in log))

    def test_integer_pmids_and_odd_rows(self):
        rows = [{"pmid": 555, "title": "Int PMID", "abstract": "a"},
                {"pmid": " 556 ", "title": "Spaced", "abstract": "b"}]
        self.results.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self._plan()
        self._decide(1, ["not an object"])
        self.assertTrue(self.sr._check_batch(self.work, 1)[0].startswith("invalid"))
        self._decide(1, [{"pmid": "555", "decision": "include", "reason": ""},
                         {"pmid": 556, "decision": "exclude", "reason": "inclusion: x"}])
        self.assertEqual(self.sr._check_batch(self.work, 1)[0], "done")
        corpus = self.dir / "corpus.json"
        self.assertEqual(self._run("apply", "--workdir", self.work, "--corpus-out", corpus), 0)
        items = json.loads(corpus.read_text(encoding="utf-8"))["items"]
        self.assertEqual([i["pmid"] for i in items], ["555"])

    def test_apply_from_another_directory(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            self._run("plan", "results.jsonl", "--protocol", "protocol.json",
                      "--workdir", "screening", "--batch-size", 2)
        finally:
            os.chdir(old)
        self._decide_all()
        self.assertEqual(self._run("apply", "--workdir", self.work,
                                   "--corpus-out", self.dir / "c.json"), 0)

    def test_apply_refuses_incomplete_or_changed_input(self):
        self._plan()
        self._decide(1, [{"pmid": "100", "decision": "include", "reason": ""},
                         {"pmid": "101", "decision": "include", "reason": ""}])
        with self.assertRaises(SystemExit):           # batches 2 and 3 not decided
            self._run("apply", "--workdir", self.work, "--corpus-out", self.dir / "c.json")
        self._decide_all()
        with self.results.open("a", encoding="utf-8") as fh:
            fh.write("\n" + json.dumps({"pmid": "999", "title": "late"}))
        with self.assertRaises(SystemExit):           # results changed since plan
            self._run("apply", "--workdir", self.work, "--corpus-out", self.dir / "c.json")


class DateSlicingTests(unittest.TestCase):
    """PubMed's history server refuses retstart > 9,998, so a bigger search is
    split by publication date. Checked against a fake PubMed: no network."""

    def _fake(self, dates):
        import re as _re
        from datetime import date as _date
        from search_strategy.pubmed import PubMed

        class Fake(PubMed):
            def __init__(self):            # no HTTP session
                pass

            def count(self, query):
                found = _re.findall(r'"(\d{4})/(\d{2})/(\d{2})"', query)
                if not found:
                    return len(dates)
                lo, hi = (_date(*map(int, f)) for f in found)
                return sum(1 for d in dates if lo <= d <= hi)
        return Fake()

    def test_slices_are_disjoint_complete_and_under_the_limit(self):
        from datetime import date, timedelta
        dates = [date(2010, 1, 1) + timedelta(days=i % 4000) for i in range(40_000)]
        dates += [date(2021, 5, 5)] * 12_000      # one day over the limit on its own
        slices = self._fake(dates).date_partitions("q")
        self.assertEqual(sum(s["count"] for s in slices), len(dates))
        spans = [(s["from"], s["to"]) for s in slices]
        self.assertEqual(spans, sorted(spans))
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            self.assertLess(b1, a2)                # disjoint, in order
        over = [s for s in slices if s["count"] > 9_999]
        self.assertEqual([(s["from"], s["truncated"]) for s in over], [("2021/05/05", True)])
        self.assertTrue(all(not s["truncated"] for s in slices if s["count"] <= 9_999))

    def test_unexpected_ncbi_replies_are_runtime_errors(self):
        from search_strategy.pubmed import PubMed

        class Reply:
            def __init__(self, text):
                self.text = text

            def json(self):
                return json.loads(self.text)
        for body in ('{"header": {}}', "<html>Service unavailable</html>", '{"esearchresult": []}'):
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                PubMed._esearch(Reply(body))
        with self.assertRaises(RuntimeError):
            PubMed._parse(b"<html><body>error</body>")

    def test_slicing_is_deterministic(self):
        from datetime import date, timedelta
        dates = [date(2000, 1, 1) + timedelta(days=(i * 7) % 9000) for i in range(25_000)]
        pm = self._fake(dates)
        self.assertEqual(pm.date_partitions("q"), pm.date_partitions("q"))


class SearchProvenanceTests(unittest.TestCase):
    def test_provenance_from_a_run(self):
        import generate_search_strategy as gss
        filters = {"date_from": "2015", "date_to": "2024/06/30", "languages": ["english"],
                   "article_types_include": [], "article_types_exclude": ["Review"],
                   "species": "humans", "exclude_terms": []}
        prov = gss._search_provenance({
            "mode": "closure", "provider": "agent", "model": "claude-opus-5-5",
            "query": "(x) AND (y)", "hash": "abc123", "count": 78, "filters": filters,
            "fetch": {"fetched": 78, "total_count": 78, "capped": False,
                      "records_unaccounted": 0}})
        s = prov["searches"][0]
        self.assertEqual((s["database"], s["query"], s["records_identified"]),
                         ("PubMed", "(x) AND (y)", 78))
        self.assertIn("abc123", s["notes"])
        self.assertIn("agent claude-opus-5-5", s["notes"])
        self.assertIn("excluding publication type: Review", s["filters"])
        self.assertEqual((prov["total_identified"], prov["duplicates_removed"]), (78, 0))
        self.assertEqual((prov["date_range_start"], prov["date_range_end"]),
                         ("2015", "2024-06-30"))


@needs_index
class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.ix = get_index()

    def test_generic_standalone_term_excluded(self):
        q = "Alzheimer Disease patients"
        facet = {"name": q, "role": "population", "start": 0, "end": 2}
        block = closure.build_facet(self.ix, q, facet)
        labels = [m["options"][0]["label"] for m in block["mesh"]]
        self.assertIn("Alzheimer Disease", labels)
        self.assertNotIn("Patients", labels)  # generic-term guard

    def test_epidemiological_generic_term_does_not_dilute_outcome(self):
        # "incidence" resolving to its own MeSH heading OR'd alongside
        # "Diarrhea" turned a specific outcome facet into
        # "Diarrhea OR Incidence-of-anything", diluting precision.
        q = "incidence of antibiotic-associated diarrhea"
        toks = closure.tokenize(q)
        facet = {"name": q, "role": "outcome", "start": 0, "end": len(toks) - 1}
        block = closure.build_facet(self.ix, q, facet)
        labels = [m["options"][0]["label"] for m in block["mesh"]]
        self.assertEqual(labels, ["Diarrhea"])

    def test_subsumption_pruning_keeps_broader_only(self):
        d_broad = self.ix.exact("Nervous System")
        d_narrow = self.ix.exact("Neurons")
        if not d_broad or not d_narrow:
            self.skipTest("index fixture missing expected descriptors")
        kept_labels = {d.label for d in closure.prune_subsumed(d_broad + d_narrow)}
        if closure._is_descendant(d_narrow[0], d_broad[0]):
            self.assertNotIn("Neurons", kept_labels)
            self.assertIn("Nervous System", kept_labels)

    def test_deterministic_repeat(self):
        q = "RNA sequencing of hippocampal neurons"
        facet = {"name": q, "role": "method", "start": 0, "end": 5}
        first = closure.build_facet(self.ix, q, facet)
        for _ in range(3):
            again = closure.build_facet(self.ix, q, facet)
            self.assertEqual(first["mesh"], again["mesh"])
            self.assertEqual(first["freetext"], again["freetext"])


@needs_index
class CoverageGapTests(unittest.TestCase):
    """closure.py is only as exhaustive as the union of facet spans it is
    given, so a phrase that never lands in any voted facet must be detected,
    not silently lost."""

    def setUp(self):
        self.ix = get_index()

    def test_detects_text_outside_every_facet(self):
        q = "Effect of RNA sequencing in Alzheimer Disease"
        toks = closure.tokenize(q)
        self.assertEqual(toks[2:4], ["RNA", "sequencing"])
        voted = [{"name": "RNA sequencing", "role": "method", "start": 2, "end": 3}]
        gaps = facet_pipeline.coverage_gaps(q, voted)
        self.assertEqual(len(gaps), 1)
        phrase = " ".join(toks[gaps[0]["start"]:gaps[0]["end"] + 1])
        self.assertIn("Alzheimer Disease", phrase)

    def test_fully_covered_question_has_no_gaps(self):
        q = "RNA sequencing of hippocampal neurons"
        toks = closure.tokenize(q)
        voted = [{"name": q, "role": "other", "start": 0, "end": len(toks) - 1}]
        self.assertEqual(facet_pipeline.coverage_gaps(q, voted), [])

    def test_gap_of_only_stopwords_is_not_reported(self):
        q = "RNA sequencing of the hippocampal neurons"
        # cover everything except "of the" in the middle -- not worth a gap
        voted = [{"start": 0, "end": 1}, {"start": 4, "end": 5}]
        self.assertEqual(facet_pipeline.coverage_gaps(q, voted), [])

    def test_resolve_coverage_gaps_recovers_missed_mesh_heading(self):
        q = "Effect of RNA sequencing in Alzheimer Disease"
        voted = [{"name": "RNA sequencing", "role": "method", "start": 2, "end": 3}]
        blocks = facet_pipeline.resolve_coverage_gaps(self.ix, q, voted)
        self.assertEqual(len(blocks), 1)
        labels = [m["options"][0]["label"] for m in blocks[0]["mesh"]]
        self.assertIn("Alzheimer Disease", labels)
        self.assertIn("coverage gap", blocks[0]["rationale"])

    def test_build_appends_gap_blocks_and_reports_them(self):
        q = "Effect of RNA sequencing in Alzheimer Disease"
        build = facet_pipeline._finish(
            q, self.ix, {"facets": [{"name": "RNA sequencing", "role": "method",
                                     "start": 2, "end": 3}], "mode": "heuristic", "runs": 0},
            model=None, min_agreement=0.5, explode=True, max_freetext=None)
        self.assertEqual(len(build["coverage_gaps"]), 1)
        self.assertTrue(any("Alzheimer" in c["name"] for c in build["concepts"]))
        self.assertIn("coverage gap", build["notes"])


@needs_index
class SegmentationFallbackTests(unittest.TestCase):
    """A closure run whose samples all fail falls back to the offline heuristic;
    that must never be recorded as model or agent output."""

    Q = "Does metformin reduce cardiovascular mortality in patients with type 2 diabetes?"

    def setUp(self):
        import generate_search_strategy as gss
        self.gss = gss
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["generate_search_strategy.py", *map(str, argv)]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.gss.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def test_unusable_agent_answers_stop_the_run(self):
        samples = self.dir / "samples.json"
        samples.write_text(json.dumps([[1, 2], "not json"]), encoding="utf-8")
        code, out, err = self._main(self.Q, "--facet-samples", samples, "--model-name", "m", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("None of the answers", err)

    def test_failed_model_calls_are_recorded_as_a_fallback(self):
        def fail(*a, **k):
            raise facets.FacetLLMError("LLM 401: bad key")
        old_pass, old_key = facets._one_llm_pass, os.environ.get("OPENROUTER_API_KEY")
        facets._one_llm_pass = fail
        os.environ["OPENROUTER_API_KEY"] = "test"
        try:
            code, out, err = self._main(self.Q, "--json")
        finally:
            facets._one_llm_pass = old_pass
            if old_key is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old_key
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["provider"], "none (fallback)")
        self.assertEqual(result["segmentation"]["mode"], "heuristic_fallback")
        self.assertEqual(len(result["fallback"]["errors"]), 3)
        self.assertIn("WARNING: model segmentation failed", err)
        result.update(count=1, filters=result["filters"])
        notes = self.gss._search_provenance(result)["searches"][0]["notes"]
        self.assertIn("model segmentation FAILED", notes)

    def test_no_query_is_a_clear_error(self):
        code, out, err = self._main("xqzzy blorft", "--mode", "mesh_only")
        self.assertEqual(code, 1)
        self.assertIn("No query could be compiled", err)

    def test_pubmed_failure_is_a_clear_error(self):
        class Down:
            def search(self, query):
                raise RuntimeError("E-utilities request failed after retries")
        old = self.gss._pubmed
        self.gss._pubmed = lambda: Down()
        try:
            code, out, err = self._main(self.Q, "--mode", "mesh_only", "--count")
        finally:
            self.gss._pubmed = old
        self.assertEqual(code, 3)
        self.assertIn("PubMed request failed", err)

    def test_unwritable_outdir_is_not_a_pubmed_error(self):
        class Up:
            def search(self, query):
                return {"count": 1, "translation": "", "warnings": {}, "errors": {}}

            def fetch_query(self, query, max_records=None):
                return {"articles": [], "count": 1, "fetched": 0, "slices": [{}],
                        "capped": False, "missing": 1}
        blocker = self.dir / "a_file"
        blocker.write_text("x", encoding="utf-8")       # --outdir under a file: mkdir fails
        old = self.gss._pubmed
        self.gss._pubmed = lambda: Up()
        try:
            code, out, err = self._main(self.Q, "--mode", "mesh_only", "--fetch", "0",
                                        "--outdir", blocker / "sub")
        finally:
            self.gss._pubmed = old
        self.assertEqual(code, 4)
        self.assertIn("fetch succeeded but could not write", err)
        self.assertNotIn("PubMed request failed", err)


@needs_index
class SelfTest(unittest.TestCase):
    def test_engine_selftest_passes(self):
        from search_strategy import selftest

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = selftest.main()
        self.assertEqual(code, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
