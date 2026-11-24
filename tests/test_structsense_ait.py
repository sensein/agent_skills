"""Unit tests for the structsense AIT mapping mode's deterministic stages.

Covers scripts/ait_taxonomy.py, ait_evidence.py, ait_tables.py, ait_gene_diff.py,
doc_layout.py, text_quality.py and ait_export.py: the lexical evidence check (exact /
offset-corrected / fuzzy / not_found, and the quarantine it implies), the column
contract, the SKOS -> match_confidence crosswalk, the same-taxonomy exactMatch house
rule, the review-sheet join, the marker-gene diff, the layout map (sections, pages,
figures, tables), the parse-artifact check and the Turtle export. Standard library
only, like the rest of the suite; the Turtle tests skip without rdflib/pyshacl.
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
from scripts import doc_layout as dl  # noqa: E402
from scripts import text_quality as tq  # noqa: E402

PAPER = "P1"

TEXT = (
    "Results\n"
    "We identified a population of “Sst Chodl” interneurons in layer 6 of mouse "
    "primary visual cortex. These cells co-expressed Sst, Chodl and Nos1.\n"
    "Long-range projecting inhibi-\ncells were rare across all donors.\n"
    "Methods\n"
    "Nuclei were profiled with 10x 3’ v3 and a 300-gene MERFISH panel.\n"
)


NAMED = "We identified a population of “Sst Chodl” interneurons in layer 6 of mouse primary visual cortex."


def _sentence(fragment: str) -> tuple:
    start = TEXT.index(fragment)
    return start, start + len(fragment)


def fixture_tables() -> dict:
    s1, e1 = _sentence(NAMED)
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
         # second sentence paraphrased -> not_found; first is verbatim but offsets are stale.
         # The first names the entity ("Sst Chodl", in curly quotes in the source): the
         # returned evidence must contain the phrase.
         "evidence_sentence": NAMED + "|Sst Chodl cells are common.",
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
        self.assertEqual(counts, {"paper.csv": 19, "sources.csv": 10, "entities.csv": 33,
                                  "methods.csv": 34, "mappings.csv": 22, "review_sheet.csv": 13,
                                  "entity_cards.csv": 25})
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
            self.assertEqual(rows[0]["evidence_sentence"], NAMED)
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
            self.assertEqual(c["evidence_sentence"], NAMED)
            self.assertEqual(c["evidence_verified"], "exact_offset_corrected")


# --------------------------------------------------------------------------- #
# layout, parse artifacts, evidence location, Turtle export
# --------------------------------------------------------------------------- #

LAYOUT_TEXT = (
    "A single-cell atlas of the mouse cortex\n\n"
    "Abstract\nWe profiled interneurons.\n\n"
    "1. Introduction\nSst Chodl cells are long-range projecting.\n\n"
    "Results\nPvalb interneurons were abundant in layer 4, as shown in\n"
    "Fig. 3b, Pvalb cells were also found in layer 5.\n\n"
    "Figure 3. Sst Chodl cells. (a) UMAP of 10x data. (b) Pvalb expression.\n\n"
    "Table 1. Marker genes per cluster.\n"
    "| Cluster | Markers |\n| --- | --- |\n| Sst Chodl | Sst, Chodl, Nos1 |\n\n"
    "Methods\nCells were sequenced with 10x v3.\n\n"
    "References\n1. Tasic B et al. Shared and distinct transcriptomic cell types. Nature 2018.\n\n"
    "[SUPPLEMENTARY FILE: Table S2.xlsx]\nSupplementary Table S2. Full cluster list.\n| a | b |\n"
)


def _where(layout, text, fragment):
    start = text.index(fragment)
    return dl.locate(layout, start, start + len(fragment))


class Layout(unittest.TestCase):
    def setUp(self):
        self.layout = dl.build_layout(LAYOUT_TEXT)

    def test_sections(self):
        self.assertEqual([s["label"] for s in self.layout["sections"]],
                         ["Front matter", "Abstract", "Introduction", "Results", "Methods", "References",
                          "Supplementary: Table S2.xlsx"])

    def test_figure_table_reference_and_supplement_blocks(self):
        loc = _where(self.layout, LAYOUT_TEXT, "(b) Pvalb expression")
        self.assertEqual((loc["section"], loc["locator_type"], loc["locator"]), ("Results", "figure_caption", "Fig. 3"))
        loc = _where(self.layout, LAYOUT_TEXT, "| Sst Chodl | Sst, Chodl, Nos1 |")
        self.assertEqual((loc["locator_type"], loc["locator"]), ("table", "Table 1"))
        self.assertEqual(_where(self.layout, LAYOUT_TEXT, "Tasic B et al")["locator_type"], "reference_list")
        loc = _where(self.layout, LAYOUT_TEXT, "Full cluster list")
        self.assertEqual((loc["section"], loc["locator"]), ("Supplementary: Table S2.xlsx", "Table S2"))

    def test_wrapped_figure_reference_is_body_not_a_caption(self):
        loc = _where(self.layout, LAYOUT_TEXT, "Pvalb cells were also found in layer 5.")
        self.assertEqual((loc["section"], loc["locator_type"], loc["locator"]), ("Results", "body", "NA"))

    def test_no_pages_without_pdf_or_form_feeds(self):
        self.assertEqual(_where(self.layout, LAYOUT_TEXT, "Pvalb interneurons")["page"], "NA")

    def test_pages_from_pdf_page_texts(self):
        cut = LAYOUT_TEXT.index("Figure 3.")
        layout = dl.build_layout(LAYOUT_TEXT, page_texts=[LAYOUT_TEXT[:cut], LAYOUT_TEXT[cut:]])
        self.assertEqual(layout["page_method"], "pdf_line_alignment")
        self.assertEqual(_where(layout, LAYOUT_TEXT, "Sst Chodl cells are long-range")["page"], "1")
        self.assertEqual(_where(layout, LAYOUT_TEXT, "Full cluster list")["page"], "2")

    def test_pages_from_form_feeds_and_page_ranges(self):
        text = "Results\nfirst page text here\fsecond page text here\fthird"
        layout = dl.build_layout(text)
        self.assertEqual(layout["page_method"], "form_feed")
        start = text.index("text here\fsecond")
        self.assertEqual(dl.locate(layout, start, start + 16)["page"], "1-2")
        self.assertEqual(dl.format_source("Results", "Fig. 3", "1-2"), "Results; Fig. 3; pp. 1-2")
        self.assertEqual(dl.format_source("NA", "NA", "NA"), "NA")

    def test_layout_for_other_text_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.layout.json"
            import json
            path.write_text(json.dumps(self.layout))
            dl.load_layout(path, LAYOUT_TEXT)
            with self.assertRaises(ValueError):
                dl.load_layout(path, LAYOUT_TEXT + " edited")


CLEAN = ("Pvalb interneurons were abundant in layer 4 of the mouse primary visual cortex, and Sst Chodl "
         "cells expressed Nos1. ") * 30
GARBLED = ("T h e c e l l s w e r e (cid:12)(cid:13) sorted â€™ by FACS Ã© into tubes ") * 40


class TextQuality(unittest.TestCase):
    def test_clean_text_and_number_tables_are_ok(self):
        table = "| Cluster | n | UMI |\n| --- | --- | --- |\n" + "| Sst_1 | 1203 | 4512.3 |\n" * 40
        pdf_table = "Sst_1 1203 4512.3 2311 0.05\n" * 80
        for text in (CLEAN, CLEAN + "\n\n" + table, CLEAN + "\n\n" + pdf_table):
            self.assertEqual(tq.document_report(text)["verdict"], "ok")

    def test_garbled_text_is_unusable(self):
        report = tq.document_report(GARBLED)
        self.assertEqual(report["verdict"], "unusable")
        self.assertIn("glyph_or_mojibake", report["junk_regions"][0]["junk_reasons"])
        self.assertTrue(report["advice"])

    def test_run_together_columns_are_junk(self):
        fragments = "\n".join(["Pv", "al", "b", "in", "ter", "ne", "ur", "on"] * 40)
        self.assertIn("fragmented_lines", tq.document_report(fragments)["junk_regions"][0]["junk_reasons"])

    def test_junk_is_scored_per_paragraph(self):
        report = tq.document_report(CLEAN + "\n\n" + GARBLED)
        self.assertTrue(all(start >= len(CLEAN) for start, _ in report["junk_spans"]))

    def test_label_checks(self):
        ok = [("Sst Chodl", "cell_type"), ("L2/3 IT neurons", "cell_type"), ("Type 3 inhibitory neuron", "cell_type"),
              ("Pvalb+", "gene"), ("H2-K1", "gene")]
        for label, kind in ok:
            self.assertEqual(tq.label_issues(label, kind), [], label)
        bad = {("Pv(cid:3)alb", "cell_type"): "label_glyph_artifact", ("â€™Sst", "cell_type"): "label_mojibake",
               ("P v a l b", "cell_type"): "label_letter_spaced", ("Sst (Chodl", "cell_type"): "label_unbalanced_brackets",
               ("Pvalb expression", "gene"): "label_not_gene_symbol", ("12 3 45", "cell_type"): "label_low_alpha",
               ("cells were sorted. Then we sequenced them", "cell_type"): "label_sentence_like"}
        for (label, kind), code in bad.items():
            self.assertIn(code, tq.label_issues(label, kind), label)


JUNK_TEXT = (
    "Results\n"
    "We identified a population of Sst Chodl interneurons in layer 6 of mouse primary visual cortex. "
    "These cells co-expressed Sst, Chodl and Nos1.\n\n"
    "Figure 2. Sst Chodl cells in V1. (a) UMAP of all GABAergic nuclei.\n\n"
    "Methods\nNuclei were profiled with a 300-gene MERFISH panel in mouse visual cortex.\n\n"
    "T h e c e l l s (cid:12)(cid:13) w e r e (cid:14) Pv(cid:3)alb s o r t e d.\n"
)
JUNK_PAGES = [JUNK_TEXT[:150], JUNK_TEXT[150:]]
PROV = "label_verbatim=llm:extract|marker_genes=llm:extract|brain_region=llm:extract|species=llm:extract"


def _span(text, start_frag, end_frag):
    a = text.index(start_frag)
    return a, text.index(end_frag, a) + len(end_frag)


def junk_fixture(out: Path) -> None:
    s1, e1 = _span(JUNK_TEXT, "We identified", "cortex.")
    s2, e2 = _span(JUNK_TEXT, "Figure 2.", "nuclei.")
    sj, ej = _span(JUNK_TEXT, "T h e", "d.")
    base = {"paper_id": PAPER, "field_provenance": PROV, "extractor_version": "v2", "run_id": "r1",
            "extraction_flag": "ok"}
    entities = [
        {**base, "mention_id": "P1:M001", "entity_type": "cell_type", "label_verbatim": "Sst Chodl", "n_mentions": "2",
         "marker_genes": "Sst|Chodl|Nos1", "species": "mouse", "species_role": "experimental", "assay_id": "A1",
         "hierarchy_level": "cluster", "assertion_type": "asserted",
         "evidence_sentence": JUNK_TEXT[s1:e1] + "|" + JUNK_TEXT[s2:e2],
         "evidence_section": "Results|Results", "evidence_char_start": f"{s1}|{s2}", "evidence_char_end": f"{e1}|{e2}"},
        # verbatim, so the lexical check passes it as `exact` — but it is parse junk
        {**base, "mention_id": "P1:M002", "entity_type": "cell_type", "label_verbatim": "Pv(cid:3)alb", "n_mentions": "1",
         "species": "mouse", "species_role": "experimental", "evidence_sentence": JUNK_TEXT[sj:ej],
         "evidence_char_start": str(sj), "evidence_char_end": str(ej)},
        # a "repaired" label: readable, but nowhere in the paper
        {**base, "mention_id": "P1:M003", "entity_type": "cell_type", "label_verbatim": "Pvalb basket cells",
         "n_mentions": "1", "evidence_sentence": "These cells co-expressed Sst, Chodl and Nos1."},
        {**base, "mention_id": "P1:M004", "entity_type": "gene", "label_verbatim": "Nos1", "n_mentions": "1",
         "evidence_sentence": "These cells co-expressed Sst, Chodl and Nos1."},
    ]
    data = {
        "paper.csv": [{"paper_id": PAPER, "doi": "10.1234/test.5678", "title": "A test paper",
                       "extracted_at": "2026-10-01T12:00:00Z", "extractor_version": "v2", "run_id": "r1"}],
        "sources.csv": [{"paper_id": PAPER, "source_id": "S1", "source_type": "main_text", "retrieved": "true",
                         "retrieval_status": "ok"}],
        "entities.csv": entities,
        "methods.csv": [{"paper_id": PAPER, "assay_id": "A1", "assay_name": "MERFISH", "targeted": "true",
                         "gene_panel_name": "panel300", "gene_panel_size": "300", "run_id": "r1",
                         "evidence_sentence": "Nuclei were profiled with a 300-gene MERFISH panel in mouse visual cortex."}],
        "mappings.csv": [{"paper_id": PAPER, "mention_id": "P1:M001", "ait_taxonomy_used": "Whole Mouse Brain",
                          "ait_id": "AIT33", "ait_cell_type_label": "Sst Chodl", "ait_hierarchy_level": "subclass",
                          "basis_for_match": "label_exact|marker_genes|species", "mapping_id": "MAP1",
                          "ait_node_id": "CS20230722_SUBC_0053", "skos_relation": "skos:broadMatch",
                          "mapping_evidence": "Sst, Chodl, Nos1", "mapped_by": "llm",
                          "field_provenance": "ait_node_id=llm:extract", "run_id": "r1"}],
    }
    for name in tables.TABLES:
        tables.write_table(out / name, name, [])
    for name, rows in data.items():
        tables.write_table(out / name, name, rows)
    (out / "paper.txt").write_text(JUNK_TEXT, encoding="utf-8")
    (out / "run_report.md").write_text("# run report\n", encoding="utf-8")


def run_junk_pipeline(out: Path) -> dict:
    report = ev.verify_dir(out, JUNK_TEXT, layout=dl.build_layout(JUNK_TEXT, page_texts=JUNK_PAGES))
    data = tables.load_dir(out)
    tables.derive(data)
    tables.write_table(out / "mappings.csv", "mappings.csv", data["mappings.csv"])
    cards, _ = gd.build_cards(data, {"CS20230722_SUBC_0053": ["Sst", "Chodl", "Npy"]},
                              {"panel300": ["Sst", "Chodl", "Npy"]}, namespace="MGI")
    tables.write_table(out / "entity_cards.csv", "entity_cards.csv", cards)
    data = tables.load_dir(out)
    tables.write_table(out / "review_sheet.csv", "review_sheet.csv", tables.build_review_sheet(data))
    return report


class ParseArtifactsAndLocation(unittest.TestCase):
    def run_it(self, tmp):
        out = Path(tmp)
        junk_fixture(out)
        report = run_junk_pipeline(out)
        return out, report, {e["mention_id"]: e for e in tables.load_dir(out)["entities.csv"]}

    def test_verbatim_junk_is_held_back_with_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, report, ents = self.run_it(tmp)
            junk = ents["P1:M002"]
            self.assertEqual(junk["evidence_verified"], "exact")  # the lexical check alone passes it
            self.assertEqual(junk["extraction_flag"], "parse_artifact")
            issues = tables.split_multi(junk["quality_issues"])
            self.assertIn("label_glyph_artifact", issues)
            self.assertIn("all_evidence_junk", issues)
            self.assertIn("quality_issues=code:text_quality", junk["field_provenance"])
            self.assertEqual({a["mention_id"] for a in report["parse_artifacts"]}, {"P1:M002", "P1:M003"})
            self.assertEqual(report["text_quality"]["verdict"], "degraded")  # one junk paragraph of five

    def test_repaired_label_that_is_not_in_the_paper_is_held_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, ents = self.run_it(tmp)
            self.assertEqual(ents["P1:M003"]["extraction_flag"], "parse_artifact")
            self.assertIn("label_not_in_text", ents["P1:M003"]["quality_issues"])

    def test_clean_entities_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, ents = self.run_it(tmp)
            self.assertEqual((ents["P1:M001"]["extraction_flag"], ents["P1:M001"]["quality_issues"]), ("ok", ""))
            self.assertEqual(ents["P1:M004"]["extraction_flag"], "ok")
            self.assertEqual(ents["P1:M004"]["quality_issues"], "")

    def test_evidence_location_is_computed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, ents = self.run_it(tmp)
            e = ents["P1:M001"]
            self.assertEqual(e["evidence_section"], "Results|Results")
            self.assertEqual(e["evidence_page"], "1|2")
            self.assertEqual(e["evidence_locator_type"], "body|figure_caption")
            self.assertEqual(e["evidence_locator"], "NA|Fig. 2")
            self.assertIn("evidence_page=code:layout_lookup", e["field_provenance"])
            m = tables.load_dir(out)["methods.csv"][0]
            self.assertEqual((m["evidence_section"], m["evidence_page"], m["evidence_locator_type"]),
                             ("Methods", "2", "body"))
            sheet = tables.load_dir(out)["review_sheet.csv"]
            self.assertEqual(sheet[0]["source_in_paper"], "Results; p. 1|Results; Fig. 2; p. 2")
            self.assertEqual(tables.load_dir(out)["entity_cards.csv"][0]["source_in_paper"],
                             "Results; p. 1|Results; Fig. 2; p. 2")
            self.assertEqual([i for i in tables.validate(out) if i.level == "error"], [])

    def test_parse_artifact_cannot_be_an_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.run_it(tmp)
            data = tables.load_dir(out)
            data["mappings.csv"].append({**data["mappings.csv"][0], "mention_id": "P1:M002", "mapping_id": "MAP9",
                                         "cell_type_name_as_in_paper": "Pv(cid:3)alb"})
            tables.write_table(out / "mappings.csv", "mappings.csv", data["mappings.csv"])
            msgs = [i.message for i in tables.validate(out) if i.level == "error"]
            self.assertTrue(any("parse artifact" in m for m in msgs), msgs)

    def test_hard_issue_without_flag_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.run_it(tmp)
            data = tables.load_dir(out)
            for e in data["entities.csv"]:
                if e["mention_id"] == "P1:M002":
                    e["extraction_flag"] = "ok"
            tables.write_table(out / "entities.csv", "entities.csv", data["entities.csv"])
            msgs = [i.message for i in tables.validate(out) if i.level == "error"]
            self.assertTrue(any("not flagged parse_artifact" in m for m in msgs), msgs)

    def test_fixed_entity_is_reset_to_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.run_it(tmp)
            data = tables.load_dir(out)
            for e in data["entities.csv"]:
                if e["mention_id"] == "P1:M003":  # a curator's correction of label and evidence
                    e["label_verbatim"] = "Sst Chodl"
                    e["evidence_sentence"] = "We identified a population of Sst Chodl interneurons"
            tables.write_table(out / "entities.csv", "entities.csv", data["entities.csv"])
            ev.verify_dir(out, JUNK_TEXT, layout=dl.build_layout(JUNK_TEXT, page_texts=JUNK_PAGES))
            e = {x["mention_id"]: x for x in tables.load_dir(out)["entities.csv"]}["P1:M003"]
            self.assertEqual(e["extraction_flag"], "uncertain")

    def test_evidence_that_does_not_contain_the_phrase_is_held_back(self):
        # Reported in a real run (doi 10.64898/2026.02.03.703645): "STR SST GABA" was
        # returned with evidence that is figure text and does not contain the phrase.
        evidence = ("It is made ** **** *** ** available under a * ***** *** ****** ***** ****** ****** "
                    "****** ****** CC-BY-NC-ND 4.0 International license **** ****** ****** "
                    "SchizophreniaNeuroticism . ****** ****** ****** ****** ****** ****** ****** ****** "
                    "****** SchizophreniaNeuroticism<br>*** *** * ** ** * * Bipolar disorderMajor "
                    "depressive disorder *** *** *** *** *** ***")
        verdict = tq.entity_quality(label="STR SST GABA", aliases=[], entity_type="cell_type",
                                    sentences=[evidence], spans=[(None, None)], statuses=["exact"],
                                    locator_types=[], junk_spans=[], boiler_spans=[])
        self.assertTrue(verdict["hold_back"])
        self.assertIn("label_not_in_evidence", verdict["issues"])

    def test_phrase_match_ignores_case_typography_and_accepts_aliases(self):
        kw = dict(entity_type="cell_type", spans=[(None, None)], statuses=["exact"], locator_types=[],
                  junk_spans=[], boiler_spans=[])
        for label, aliases, sentence in (
                ("STR SST GABA", [], "STR Sst GABA neurons were enriched in the putamen."),
                ("Sst Chodl", [], "a population of “Sst Chodl” interneurons"),
                ("L5 extratelencephalic", ["L5 ET"], "L5 ET neurons project to the brainstem.")):
            verdict = tq.entity_quality(label=label, aliases=aliases, sentences=[sentence], **kw)
            self.assertEqual(verdict["issues"], [], label)

    def test_sentences_without_the_phrase_are_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            junk_fixture(out)
            data = tables.load_dir(out)
            s, e = _span(JUNK_TEXT, "These cells", "Nos1.")
            for ent in data["entities.csv"]:
                if ent["mention_id"] == "P1:M001":  # add a verified sentence that does not name it
                    for col, val in (("evidence_sentence", JUNK_TEXT[s:e]), ("evidence_section", "Results"),
                                     ("evidence_char_start", str(s)), ("evidence_char_end", str(e))):
                        ent[col] = ent[col] + "|" + val
            tables.write_table(out / "entities.csv", "entities.csv", data["entities.csv"])
            run_junk_pipeline(out)
            data = tables.load_dir(out)
            m1 = {x["mention_id"]: x for x in data["entities.csv"]}["P1:M001"]
            self.assertEqual(m1["extraction_flag"], "ok")
            self.assertIn("evidence_without_label", m1["quality_issues"])
            for row in (data["review_sheet.csv"][0], data["entity_cards.csv"][0]):
                self.assertNotIn("These cells co-expressed", row["evidence_sentence"])
                self.assertEqual(len(tables.split_multi(row["evidence_sentence"])), 2)

    def test_validate_rejects_unflagged_entity_whose_evidence_lacks_the_phrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.run_it(tmp)
            data = tables.load_dir(out)
            for e in data["entities.csv"]:
                if e["mention_id"] == "P1:M004":  # gene Nos1, but evidence that does not name it
                    e["evidence_sentence"] = "Nuclei were profiled with a 300-gene MERFISH panel in mouse visual cortex."
                    e["evidence_verified"] = "exact"
            tables.write_table(out / "entities.csv", "entities.csv", data["entities.csv"])
            msgs = [i.message for i in tables.validate(out) if i.level == "error"]
            self.assertTrue(any("must contain the phrase" in m for m in msgs), msgs)

    def test_without_layout_location_columns_are_left_and_validate_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            junk_fixture(out)
            ev.verify_dir(out, JUNK_TEXT)
            self.assertEqual(tables.load_dir(out)["entities.csv"][0]["evidence_page"], "")
            warnings = [i.message for i in tables.validate(out) if i.level == "warning"]
            self.assertTrue(any("no evidence location computed" in w for w in warnings))


def _have_rdf() -> bool:
    try:
        import pyshacl  # noqa: F401
        import rdflib  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_rdf(), "rdflib and pyshacl are needed for the Turtle export")
class TurtleExport(unittest.TestCase):
    def export(self, tmp):
        from scripts import ait_export
        out = Path(tmp)
        junk_fixture(out)
        run_junk_pipeline(out)
        ttl, report = ait_export.export(out, JUNK_TEXT, text_path=out / "paper.txt")
        path = out / "P1.ttl"
        path.write_text(ttl, encoding="utf-8")
        return path, report

    def test_export_validates_and_holds_back_parse_artifacts(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "structsense" / "scripts"))
        from validate_ttl import validate_file
        with tempfile.TemporaryDirectory() as tmp:
            path, report = self.export(tmp)
            gate = validate_file(path)
            self.assertTrue(gate["ok"], gate["violations"])
            skipped = {s["mention_id"]: s["reason"] for s in report["skipped_entities"]}
            self.assertEqual(skipped, {"P1:M002": "parse_artifact", "P1:M003": "parse_artifact"})
            self.assertNotIn("cid:", path.read_text(encoding="utf-8"))
            self.assertEqual(report["ait"]["taxonomy_mappings"], 1)

    def test_reverse_lookup_from_taxonomy_node_with_location(self):
        import rdflib
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.export(tmp)
            g = rdflib.Graph().parse(path)
            rows = g.query("""
                PREFIX ner: <https://brainkb.org/ner/> PREFIX prov: <http://www.w3.org/ns/prov#>
                SELECT ?rel ?by ?page ?part WHERE {
                  ?n ner:taxonomyNodeIdentifier ?id . FILTER (STR(?id) = "CS20230722_SUBC_0053")
                  ?a ner:mappingTarget ?n ; ner:taxonomyMappingRelation ?rel ; ner:mappedByRaw ?by ;
                     prov:hadPrimarySource ?pub ; ner:hasTaxonomyMappingEvidence ?m .
                  ?m ner:onPage ?page . OPTIONAL { ?m ner:inDocumentPart/ner:documentPartLabel ?part }
                }""")
            got = sorted((str(r[0]).rsplit("/", 1)[1], str(r[1]), int(r[2]), str(r[3]) if r[3] else None) for r in rows)
            self.assertEqual(got, [("broader", "llm", 1, None), ("broader", "llm", 2, "Fig. 2")])

    def test_invalid_tables_are_refused(self):
        from scripts import ait_export
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            junk_fixture(out)
            run_junk_pipeline(out)
            (out / "paper.csv").unlink()
            with self.assertRaises(ValueError):
                ait_export.export(out, JUNK_TEXT)


if __name__ == "__main__":
    unittest.main()
