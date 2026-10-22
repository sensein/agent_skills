"""Unit tests for the structsense AIT mapping mode's deterministic stages.

Covers scripts/ait_taxonomy.py, ait_evidence.py, ait_tables.py and ait_gene_diff.py:
the lexical evidence check (exact / offset-corrected / fuzzy / not_found, and the
quarantine it implies), the column contract, the SKOS -> match_confidence crosswalk,
the same-taxonomy exactMatch house rule, the review-sheet join and the marker-gene
diff. Standard library only, like the rest of the suite.
"""
from __future__ import annotations

import re
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
    ] + [
        # every marker gene is its own gene entity (ait_tables validate requires it)
        {"paper_id": PAPER, "mention_id": f"{PAPER}:M00{i}", "entity_type": "gene",
         "label_verbatim": g, "label_normalized": g, "n_mentions": "1",
         "species": "mouse", "species_role": "experimental", "ncbi_taxon_id": "10090",
         "assertion_type": "asserted",
         "evidence_sentence": "These cells co-expressed Sst, Chodl and Nos1.",
         "evidence_section": "Results", "evidence_chunk_id": "c1",
         "evidence_char_start": str(s1), "evidence_char_end": str(e1),
         "extraction_flag": "ok", "field_provenance": prov, "extractor_version": "v2", "run_id": "r1"}
        for i, g in ((3, "Sst"), (4, "Chodl"), (5, "Nos1"))
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


try:
    import rdflib  # noqa: F401
    HAVE_RDFLIB = True
except ImportError:
    HAVE_RDFLIB = False


@unittest.skipUnless(HAVE_RDFLIB, "rdflib not installed")
class TrustedTaxonomyNodes(unittest.TestCase):
    def test_trusted_files_exist(self):
        from scripts import ait_nodes
        for t in tax.taxonomies():
            for fname in (t.get("trusted_files") or {}).values():
                self.assertTrue((ait_nodes.trusted_dir() / fname).exists(), fname)

    def test_species_from_ait_number(self):
        from scripts import ait_nodes
        self.assertEqual(ait_nodes.resolve("AIT11.9")[1:], ("macaque", "Macaque_BG.ttl"))
        with self.assertRaises(LookupError):
            ait_nodes.resolve("CCN20250428")  # multi-species: needs --species
        with self.assertRaises(LookupError):
            ait_nodes.resolve("AIT15.3")  # no trusted file

    def test_human_bg_nodes_levels_and_markers(self):
        from scripts import ait_nodes
        t = ait_nodes.load("AIT19.5")
        self.assertEqual([lvl for lvl, _, _ in t.levels],
                         ["Neighborhood", "Class", "Subclass", "Group", "cluster_id"])
        n = t.node("CS20250428_GROUP_0033")
        self.assertEqual((n.label, n.level), ("SN GATA3-PVALB GABA", "Group"))
        self.assertEqual(len(n.ancestors), 3)
        self.assertEqual(t.markers(n.node_id, own_only=True), ["GATA3", "PVALB"])
        # curated_markers link to GeneAnnotations; symbols come from the annotation file
        g29 = t.node("CS20250428_GROUP_0029")
        self.assertTrue(all(i.startswith("urn:bkbit:") for i in g29.curated_markers))
        self.assertEqual(t.gene_file, "annotation-ENSEMBL-9606-109.ttl")
        self.assertEqual(t.markers(g29.node_id, own_only=True)[:5],
                         ["BNC2", "CALB1", "NR4A2", "SLC6A3", "TH"])
        self.assertIsNotNone(t.node("Human-147"))  # cluster level keyed by label

    def test_every_curated_marker_resolves_in_its_species_annotation(self):
        from scripts import ait_nodes
        for ait_id in ("AIT19.5", "AIT11.9", "AIT101"):
            t = ait_nodes.load(ait_id)
            self.assertTrue(any(n.curated_markers for n in t.nodes.values()), ait_id)
            self.assertEqual(t.unresolved_markers(), [], ait_id)
            for n in t.nodes.values():
                self.assertFalse([i for i in n.curated_markers if i.startswith("file:")], n.node_id)

    def test_manifest_agrees_with_the_taxonomy_files(self):
        # trusted_ontologies/ait_taxonomies.json holds only what the .ttl files do not;
        # where both say something, they must agree.
        from scripts import ait_nodes
        catalog = tax.load_catalog()
        self.assertEqual(tax.CATALOG_PATH.parent, ait_nodes.trusted_dir())
        gene_files = {e["file"] for e in catalog["gene_annotations"].values()}
        for entry in tax.taxonomies():
            for species, fname in (entry.get("trusted_files") or {}).items():
                self.assertIn(species, entry["species"], fname)
                t = ait_nodes.load(tax.preferred_id(entry, species), species)
                # the file's own title names its species, and its accessions the CCN
                self.assertIn(species, t.title.lower(), fname)
                ccn = re.findall(r"CCN(\d+)", entry["ccn"] or "")
                accessions = {n.accession_id.split("_")[0] for n in t.nodes.values() if n.accession_id}
                self.assertEqual(accessions, {f"CS{ccn[0]}"}, fname)
                # the annotation its curated_markers link into is a trusted file, and the
                # one the manifest gives that species' genes
                self.assertIn(t.gene_file, gene_files, fname)
                self.assertEqual(ait_nodes.gene_annotation_file(species), t.gene_file, fname)

    def test_every_extracted_gene_links_to_an_ensembl_or_ncbi_annotation(self):
        from scripts import ait_nodes
        row = lambda mid, sym, sp="", tx="": {"mention_id": mid, "entity_type": "gene", "label_verbatim": sym,
                                              "label_normalized": sym, "species": sp, "ncbi_taxon_id": tx}
        rows = [row("M1", "DRD1", "human", "9606"),          # ENSEMBL human
                row("M2", "DRD1", "", "9606"),                # taxon id only
                row("M3", "DRD1", "Homo sapiens"),             # scientific name
                row("M4", "DRD1"),                             # no species: the paper's experimental one
                row("M5", "TH", "rhesus macaque"),             # NCBI macaque
                row("M6", "TH", "", "9483"),                   # NCBI marmoset
                row("M7", "Drd1", "mouse"),                    # ENSEMBL mouse, mouse-style symbol
                row("M8", "Drd1", "rat", "10116"),             # no trusted annotation
                row("M9", "NOTAGENE1", "human")]               # not in the annotation
        links = {r["mention_id"]: ait_nodes.resolve_gene_row(r, ("human", "9606")) for r in rows}
        human = links["M1"].iri
        self.assertTrue(human.startswith("urn:bkbit:"))
        self.assertEqual({links[m].iri for m in ("M2", "M3", "M4")}, {human})  # M4: the paper's species
        self.assertEqual(links["M5"].file, "annotation-NCBI-9544-103.ttl")
        self.assertEqual(links["M6"].file, "annotation-NCBI-9483-105.ttl")
        self.assertEqual((links["M7"].file, links["M7"].tier), ("annotation-ENSEMBL-10090-109.ttl", "symbol"))
        self.assertTrue(links["M5"].iri and links["M6"].iri and links["M7"].iri)
        self.assertEqual(len({links[m].iri for m in ("M1", "M5", "M6", "M7")}), 4)  # one gene per species
        self.assertEqual(sum(1 for g in links.values() if g.iri), 7)
        self.assertIn("no trusted gene annotation", links["M8"].reason)
        self.assertEqual(links["M9"].reason, "no gene with this symbol or synonym")
        # as a concept row: tool provenance, the annotation file as its source
        f = ait_nodes.gene_concept_fields(links["M5"])
        self.assertEqual((f["concept_mapping_provenance"], f["mapping_source"], f["match_tier"]),
                         ("tool", "trusted:annotation-NCBI-9544-103", "exactMatch"))

    def test_search_and_validate_node(self):
        from scripts import ait_nodes
        t = ait_nodes.load("AIT19.5")
        top = ait_nodes.search(t, label="D1 MSN", regions=["putamen"], level="Group", top_n=3)
        self.assertTrue(all("MSN" in r["label"] for r in top))
        row = {"ait_id": "AIT19.5", "ait_node_id": "CS20250428_GROUP_0033",
               "ait_cell_type_label": "SN GATA3-PVALB GABA", "ait_hierarchy_level": "Group"}
        self.assertEqual(tables.check_node("mappings.csv", 1, row, None, None), [])
        bad = tables.check_node("mappings.csv", 1, {**row, "ait_node_id": "CS_NOPE"}, None, None)
        self.assertEqual([i.level for i in bad], ["error"])
        bad = tables.check_node("mappings.csv", 1, {**row, "ait_cell_type_label": "SN PVALB"}, None, None)
        self.assertEqual([i.level for i in bad], ["error"])


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

    def test_marker_gene_without_a_gene_row_is_an_error(self):
        def mutate(d):
            d["entities.csv"] = [r for r in d["entities.csv"] if r["label_verbatim"] != "Nos1"]
        errs = [m for m in self._validate_with(mutate) if "no entity_type=gene row" in m]
        self.assertEqual(len(errs), 1)
        self.assertIn("Nos1", errs[0])

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

    def test_no_paper_markers_is_na_not_zero(self):
        # the HMBA case: the taxonomy node has curated markers, the paper names none
        d = gd.gene_diff([], ["STXBP6"])
        self.assertIsNone(d["jaccard"])  # NA: no evidence either way, not disagreement
        self.assertEqual(d["genes_taxonomy_only"], ["STXBP6"])
        d = gd.gene_diff([], ["STXBP6"], panel=["STXBP6"])
        self.assertEqual(d["jaccard_panel_restricted"], "NA")
        self.assertEqual(gd.gene_diff(["DRD1"], ["STXBP6"])["jaccard"], 0.0)  # a real disagreement stays 0

    def test_marker_report_counts_from_the_cards(self):
        cards = [
            {"ait_node_id": "G1", "ait_cell_type_label": "STRd D1 Matrix MSN", "n_shared": "0",
             "n_paper_only": "0", "jaccard": "NA"},
            {"ait_node_id": "S1", "ait_cell_type_label": "STR D1 MSN", "n_shared": "0",
             "n_paper_only": "2", "jaccard": "0.0"},
            {"ait_node_id": "G2", "ait_cell_type_label": "STRv D1 MSN", "n_shared": "1",
             "n_paper_only": "0", "jaccard": "0.5"},
        ]
        rep = gd.marker_report(cards, {"G1": ["STXBP6"], "G2": ["ARHGAP6", "DRD1"], "S1": []})
        self.assertEqual([n["ait_node_id"] for n in rep["nodes_with_taxonomy_markers"]], ["G1", "G2"])
        self.assertEqual(rep["nodes_without_taxonomy_markers"], 1)
        self.assertEqual((rep["edges_where_paper_names_markers"], rep["edges_with_shared_markers"],
                          rep["edges_jaccard_na"]), (2, 1, 1))

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
