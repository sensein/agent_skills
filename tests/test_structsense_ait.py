"""Unit tests for the structsense AIT mapping mode's deterministic stages.

Covers scripts/ait_taxonomy.py, ait_evidence.py, ait_tables.py and ait_gene_diff.py:
the lexical evidence check (exact / offset-corrected / fuzzy / not_found, and the
quarantine it implies), the column contract, the SKOS -> match_confidence crosswalk,
the same-taxonomy exactMatch house rule, the review-sheet join and the marker-gene
diff. Standard library only, like the rest of the suite.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "structsense"))

from scripts import ait_evidence as ev  # noqa: E402
from scripts import ait_gene_diff as gd  # noqa: E402
from scripts import ait_tables as tables  # noqa: E402
from scripts import ait_taxonomy as tax  # noqa: E402

PAPER = "P1"

TEXT = (
    "Results\n"
    "We identified a population of “Sst Chodl” interneurons in layer 6 of mouse "
    "primary visual cortex. These cells co-expressed Sst, Chodl and Nos1.\n"
    "Long-range projecting inhibi-\ncells were rare across all donors.\n"
    "Methods\n"
    "Nuclei were profiled with 10x 3’ v3 and a 300-gene MERFISH panel.\n"
)


def _sentence(fragment: str) -> tuple:
    start = TEXT.index(fragment)
    return start, start + len(fragment)


def fixture_tables() -> dict:
    s1, e1 = _sentence("These cells co-expressed Sst, Chodl and Nos1.")
    prov = ("label_verbatim=llm:extract|marker_genes=llm:extract|brain_region=llm:extract|"
            "species=llm:extract")
    paper = [{"paper_id": PAPER, "title": "A test paper", "extracted_at": "2026-10-01T12:00:00Z",
              "extractor_version": "v2", "run_id": "r1", "taxonomy_deposited": "false"}]
    sources = [{"paper_id": PAPER, "source_id": "S1", "source_type": "main_text",
                "retrieved": "true", "retrieval_status": "ok"}]
    entities = [
        {"paper_id": PAPER, "mention_id": f"{PAPER}:M001", "entity_type": "cell_type",
         "label_verbatim": "Sst Chodl", "n_mentions": "2",
         "marker_genes": "Sst|Chodl|Nos1", "marker_genes_normalized": "Sst|Chodl|Nos1",
         "brain_region": "primary visual cortex", "species": "mouse", "species_role": "experimental",
         "assay_id": "A1", "hierarchy_level": "cluster", "assertion_type": "asserted",
         # second sentence paraphrased -> not_found; first is verbatim but offsets are stale
         "evidence_sentence": "These cells co-expressed Sst, Chodl and Nos1.|Sst Chodl cells are common.",
         "evidence_section": "Results; Fig. 2a|Results",
         "evidence_chunk_id": "c1|c1",
         "evidence_char_start": f"{s1 + 3}|0", "evidence_char_end": f"{e1 + 3}|10",
         "extraction_flag": "ok", "field_provenance": prov, "extractor_version": "v2", "run_id": "r1"},
        {"paper_id": PAPER, "mention_id": f"{PAPER}:M002", "entity_type": "cell_type",
         "label_verbatim": "long-range projecting cells", "n_mentions": "1",
         "species": "mouse", "species_role": "experimental", "hierarchy_level": "unspecified",
         "assertion_type": "inferred",
         "evidence_sentence": "These are entirely invented words about cells.",
         "evidence_section": "Discussion", "evidence_chunk_id": "c9",
         "evidence_char_start": "0", "evidence_char_end": "10",
         "extraction_flag": "uncertain", "field_provenance": prov, "extractor_version": "v2", "run_id": "r1"},
    ]
    methods = [{"paper_id": PAPER, "assay_id": "A1", "assay_name": "MERFISH", "platform": "MERFISH",
                "modality": "spatial", "targeted": "true", "gene_panel_name": "panel300",
                "gene_panel_size": "300",
                "evidence_sentence": "Nuclei were profiled with 10x 3' v3 and a 300-gene MERFISH panel.",
                "evidence_chunk_id": "c2", "run_id": "r1"}]
    mappings = [
        {"paper_id": PAPER, "mention_id": f"{PAPER}:M001", "ait_taxonomy_used": "Whole Mouse Brain",
         "ait_id": "AIT33", "ait_cell_type_label": "Sst Chodl", "ait_hierarchy_level": "subclass",
         "basis_for_match": "label_exact|marker_genes|species", "mapping_id": "MAP1",
         "ait_node_id": "CS20230722_SUBC_0053", "skos_relation": "skos:broadMatch",
         "mapping_evidence": "Sst, Chodl, Nos1", "mapped_by": "llm",
         "field_provenance": "ait_node_id=llm:extract;code:ontology_lookup", "run_id": "r1"},
        {"paper_id": PAPER, "mention_id": f"{PAPER}:M002", "ait_taxonomy_used": "",
         "mapping_id": "MAP2", "skos_relation": "none", "no_match_reason": "no defensible node",
         "mapped_by": "llm", "field_provenance": "ait_node_id=llm:extract", "run_id": "r1"},
    ]
    return {"paper.csv": paper, "sources.csv": sources, "entities.csv": entities,
            "methods.csv": methods, "mappings.csv": mappings}


def write_fixture(out: Path) -> dict:
    data = fixture_tables()
    for name in tables.TABLES:
        tables.write_table(out / name, name, [])
    for name, rows in data.items():
        tables.write_table(out / name, name, rows)
    (out / "paper.txt").write_text(TEXT, encoding="utf-8")
    (out / "run_report.md").write_text("# run report\n", encoding="utf-8")
    return data


def run_pipeline(out: Path) -> None:
    ev.verify_dir(out, TEXT)
    data = tables.load_dir(out)
    tables.derive(data)
    for name in ("mappings.csv", "entity_cards.csv"):
        tables.write_table(out / name, name, data.get(name, []))
    cards, _ = gd.build_cards(data, {"CS20230722_SUBC_0053": ["Sst", "Chodl", "Npy", "Pde11a"]},
                              {"panel300": ["Sst", "Chodl", "Npy"]}, namespace="MGI")
    tables.write_table(out / "entity_cards.csv", "entity_cards.csv", cards)
    data = tables.load_dir(out)
    tables.write_table(out / "review_sheet.csv", "review_sheet.csv", tables.build_review_sheet(data))


class TaxonomyCatalog(unittest.TestCase):
    def test_catalog_is_the_eight_supported_taxonomies(self):
        ids = sorted(tax.preferred_id(t) for t in tax.taxonomies())
        self.assertEqual(ids, sorted(["AIT33", "AIT19.5", "AIT15.3", "AIT102", "CCN202002270",
                                      "AIT105", "AIT2.1.1", "AIT5.1"]))
        for t in tax.taxonomies():
            self.assertTrue(t["url"].startswith("https://brain-map.org/our-research/cell-type-taxonomies/"))
            self.assertNotIn("?", t["url"])
        for ident in ("AIT106", "AIT31", "AIT21"):
            self.assertIsNone(tax.find(ident), ident)

    def test_rank_prefers_mtg_for_human_mtg(self):
        top = tax.rank(["human"], ["middle temporal gyrus", "MTG"], top_n=3)
        self.assertEqual(top[0]["ait_id"], "AIT15.3")
        self.assertTrue(all("human" in r["species_match"] for r in top))

    def test_multi_species_taxonomy_reports_the_experimental_species_number(self):
        top = {r["title"]: r["ait_id"] for r in tax.rank(["macaque"], ["putamen"], top_n=8)}
        bg = next(t for t in top if "Basal Ganglia" in t)
        self.assertEqual(top[bg], "AIT11.9")
        self.assertEqual(tax.find("AIT104")["taxonomy_name"], "20181231_Adult_CrossSpecies_LGN_SMARTseq")

    def test_regions_are_anatomical_only(self):
        for t in tax.taxonomies():
            for r in t["regions"]:
                self.assertNotRegex(r.lower(), r"alzheimer|aging|development|embryonic|postnatal", t["title"])

    def test_species_coverage(self):
        self.assertTrue(tax.species_covered("AIT15.3", "human"))
        self.assertFalse(tax.species_covered("AIT15.3", "mouse"))
        self.assertIsNone(tax.species_covered("AIT999", "mouse"))


class Normalization(unittest.TestCase):
    def test_quotes_dashes_soft_hyphen_and_line_break_hyphenation(self):
        self.assertEqual(ev.normalize("“Sst” – inter­neuron"), '"Sst" - interneuron')
        self.assertEqual(ev.normalize("inhibi-\n  tory"), "inhibitory")
        # a hyphen before a capital or a digit is real, not hyphenation
        self.assertEqual(ev.normalize("Sst-\nChodl"), "Sst- Chodl")

    def test_offsets_map_back_to_raw_text(self):
        doc = ev.Normalized(TEXT)
        q = ev.normalize("“Sst Chodl” interneurons")
        pos = doc.text.index(q)
        rs, re_ = doc.raw_span(pos, pos + len(q))
        self.assertEqual(TEXT[rs:re_], "“Sst Chodl” interneurons")


class EvidenceVerification(unittest.TestCase):
    def setUp(self):
        self.doc = ev.Normalized(TEXT)

    def test_exact_at_recorded_offsets(self):
        s, e = _sentence("These cells co-expressed Sst, Chodl and Nos1.")
        r = ev.verify_sentence("These cells co-expressed Sst, Chodl and Nos1.", self.doc, s, e)
        self.assertEqual(r["status"], "exact")

    def test_offset_corrected(self):
        s, e = _sentence("These cells co-expressed Sst, Chodl and Nos1.")
        r = ev.verify_sentence("These cells co-expressed Sst, Chodl and Nos1.", self.doc, 0, 20)
        self.assertEqual(r["status"], "exact_offset_corrected")
        self.assertEqual((r["start"], r["end"]), (s, e))

    def test_typography_differences_still_exact(self):
        r = ev.verify_sentence('We identified a population of "Sst Chodl" interneurons', self.doc, None, None,
                               has_offsets=False)
        self.assertEqual(r["status"], "exact")
        r = ev.verify_sentence("Long-range projecting inhibicells were rare", self.doc, None, None,
                               has_offsets=False)
        self.assertEqual(r["status"], "exact")

    def test_fuzzy_single_typo(self):
        r = ev.verify_sentence("These cells co-expresed Sst, Chodl and Nos1.", self.doc)
        self.assertEqual(r["status"], "fuzzy")
        self.assertGreaterEqual(r["similarity"], 0.95)

    def test_paraphrase_not_found(self):
        r = ev.verify_sentence("Sst Chodl neurons expressed nitric oxide synthase.", self.doc)
        self.assertEqual(r["status"], "not_found")
        self.assertLess(r["similarity"], 0.95)

    def test_case_is_significant(self):
        r = ev.verify_sentence("THESE CELLS CO-EXPRESSED SST, CHODL AND NOS1.", self.doc)
        self.assertEqual(r["status"], "not_found")

    def test_verify_dir_quarantines_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            report = ev.verify_dir(out, TEXT)
            ents = {e["mention_id"]: e for e in tables.load_dir(out)["entities.csv"]}
            m1, m2 = ents[f"{PAPER}:M001"], ents[f"{PAPER}:M002"]
            self.assertEqual(m1["evidence_verified"], "exact_offset_corrected|not_found")
            self.assertEqual(m1["extraction_flag"], "ok")
            self.assertEqual(m2["evidence_verified"], "not_found")
            self.assertEqual(m2["extraction_flag"], "unverified_evidence")
            self.assertIn("code:lexical_verify", m1["field_provenance"])
            self.assertEqual([q["mention_id"] for q in report["quarantined"]], [f"{PAPER}:M002"])
            meth = tables.load_dir(out)["methods.csv"][0]
            self.assertEqual(meth["evidence_verified"], "exact")


class TablesContract(unittest.TestCase):
    def test_schema_matches_prompt_column_counts(self):
        counts = {t: len(tables.columns(t)) for t in tables.TABLES}
        self.assertEqual(counts, {"paper.csv": 19, "sources.csv": 10, "entities.csv": 29,
                                  "methods.csv": 30, "mappings.csv": 22, "review_sheet.csv": 13,
                                  "entity_cards.csv": 24})
        self.assertEqual(tables.columns("mappings.csv")[:11],
                         ["paper_id", "mention_id", "cell_type_name_as_in_paper", "species_experimental",
                          "ait_taxonomy_used", "ait_id", "ait_cell_type_label", "ait_hierarchy_level",
                          "match_confidence", "basis_for_match", "notes"])

    def test_schema_matches_prompt_column_lists(self):
        import re
        prompt = (REPO_ROOT / "skills" / "structsense" / "prompts" /
                  "extractor-cell-type-ait-mapping.md").read_text(encoding="utf-8")
        for table in tables.TABLES:
            m = re.search(r"### `%s`.*?\n\n`([^`]+)`" % re.escape(table), prompt, re.S)
            self.assertIsNotNone(m, table)
            self.assertEqual([c.strip() for c in m.group(1).split(",")], tables.columns(table), table)

    def test_multivalue_escape_round_trip(self):
        values = ["| a | b |", "plain"]
        self.assertEqual(tables.split_multi(tables.join_multi(values)), values)

    def test_full_pipeline_validates_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            run_pipeline(out)
            issues = tables.validate(out)
            errors = [str(i) for i in issues if i.level == "error"]
            self.assertEqual(errors, [])
            maps = {m["mapping_id"]: m for m in tables.load_dir(out)["mappings.csv"]}
            self.assertEqual(maps["MAP1"]["match_confidence"], "partial")
            self.assertEqual(maps["MAP2"]["match_confidence"], "none")
            self.assertEqual(maps["MAP1"]["species_experimental"], "mouse")

    def test_review_sheet_keeps_only_strictly_verified_sentences(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            run_pipeline(out)
            rows = tables.load_dir(out)["review_sheet.csv"]
            self.assertEqual(len(rows), 1)  # M002 is quarantined -> no verified evidence
            self.assertEqual(rows[0]["evidence_sentence"], "These cells co-expressed Sst, Chodl and Nos1.")
            self.assertEqual(rows[0]["source_in_paper"], "Results; Fig. 2a")
            self.assertEqual(rows[0]["assay"], "MERFISH")
            self.assertEqual(rows[0]["ait_match"], "Sst Chodl")

    def _validate_with(self, mutate) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            run_pipeline(out)
            data = tables.load_dir(out)
            mutate(data)
            for name, rows in data.items():
                tables.write_table(out / name, name, rows)
            return [i.message for i in tables.validate(out) if i.level == "error"]

    def test_wrong_match_confidence_is_an_error(self):
        def mutate(d):
            d["mappings.csv"][0]["match_confidence"] = "exact"
        self.assertTrue(any("derives 'partial'" in m for m in self._validate_with(mutate)))

    def test_quarantined_entity_cannot_be_an_edge(self):
        def mutate(d):
            m = d["mappings.csv"][1]
            m.update(skos_relation="skos:relatedMatch", match_confidence="partial",
                     ait_node_id="CS_X", ait_cell_type_label="X", ait_id="AIT33", no_match_reason="")
        self.assertTrue(any("quarantined" in m for m in self._validate_with(mutate)))

    def test_exact_match_requires_same_taxonomy_basis(self):
        def mutate(d):
            d["mappings.csv"][0].update(skos_relation="skos:exactMatch", match_confidence="exact")
        self.assertTrue(any("same-taxonomy identity" in m for m in self._validate_with(mutate)))

    def test_exact_match_across_species_is_an_error(self):
        def mutate(d):
            d["mappings.csv"][0].update(skos_relation="skos:exactMatch", match_confidence="exact",
                                        ait_id="AIT15.3",
                                        basis_for_match="author_statement|label_exact")
        self.assertTrue(any("species boundary" in m for m in self._validate_with(mutate)))

    def test_hand_edited_review_sheet_is_detected(self):
        def mutate(d):
            d["review_sheet.csv"][0]["notes"] = "added by hand"
        self.assertTrue(any("deterministic join" in m for m in self._validate_with(mutate)))

    def test_reordered_header_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            run_pipeline(out)
            lines = (out / "sources.csv").read_text().splitlines()
            cols = lines[0].split(",")
            cols[0], cols[1] = cols[1], cols[0]
            (out / "sources.csv").write_text(",".join(cols) + "\n")
            msgs = [i.message for i in tables.validate(out) if i.table == "sources.csv"]
            self.assertTrue(any("out of order" in m for m in msgs))


class GeneDiff(unittest.TestCase):
    def test_untargeted(self):
        d = gd.gene_diff(["Sst", "Chodl", "Nos1"], ["Sst", "Chodl", "Npy"])
        self.assertEqual(d["genes_shared"], ["Sst", "Chodl"])
        self.assertEqual(d["genes_paper_only"], ["Nos1"])
        self.assertEqual(d["genes_taxonomy_only"], ["Npy"])
        self.assertEqual(d["jaccard"], 0.5)
        self.assertFalse(d["panel_limited"])
        self.assertIsNone(d["jaccard_panel_restricted"])

    def test_panel_restriction_discounts_unmeasurable_genes(self):
        d = gd.gene_diff(["Sst", "Chodl"], ["Sst", "Chodl", "Npy", "Pde11a"], panel=["Sst", "Chodl"])
        self.assertEqual(d["jaccard"], 0.5)
        self.assertEqual(d["jaccard_panel_restricted"], 1.0)

    def test_targeted_without_panel_list_is_na(self):
        d = gd.gene_diff(["Sst"], ["Sst"], panel=[])
        self.assertTrue(d["panel_limited"])
        self.assertEqual(d["jaccard_panel_restricted"], "NA")

    def test_case_is_not_folded(self):
        d = gd.gene_diff(["PVALB"], ["Pvalb"])
        self.assertEqual(d["n_shared"], 0)

    def test_cards_skip_quarantined_and_none_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_fixture(out)
            run_pipeline(out)
            cards = tables.load_dir(out)["entity_cards.csv"]
            self.assertEqual([c["mention_id"] for c in cards], [f"{PAPER}:M001"])
            c = cards[0]
            self.assertEqual((c["genes_shared"], c["genes_paper_only"], c["genes_taxonomy_only"]),
                             ("Sst|Chodl", "Nos1", "Npy|Pde11a"))
            self.assertEqual(c["panel_limited"], "true")
            self.assertEqual(c["jaccard"], "0.4")
            self.assertEqual(c["jaccard_panel_restricted"], "0.6667")
            self.assertEqual(c["gene_namespace"], "MGI")
            self.assertEqual(c["evidence_sentence"], "These cells co-expressed Sst, Chodl and Nos1.")
            self.assertEqual(c["evidence_verified"], "exact_offset_corrected")


if __name__ == "__main__":
    unittest.main()
