"""AIT mapping tables -> Turtle (scripts/ait_ttl.py), and the identity-key rule it relies on."""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDFS, SKOS

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
from json_to_ttl import result_to_ttl  # noqa: E402
from group_by_entity import mention_groups  # noqa: E402

NER = Namespace("https://brainkb.org/ner/")
PY = sys.executable
PID = "test_paper"
RUN = "test-run"
VER = "structsense-test/model-x"


def test_identity_key_is_not_rejoined_by_an_alias(tmp_path):
    # "D2MSN" is an alias of STR D2 MSN; the asserted key must not become str_d2msn
    text = "STR D2 MSNs express DRD2 here. D2MSN silencers differ there."
    a, b = text.index("STR D2 MSN"), text.index("D2MSN")
    items = [dict(entity="STR D2 MSN", label="CellType", start=a, end=a + 10, sentence=text[:30],
                  identity_key="str_d2_msn"),
             dict(entity="D2MSN", label="CellType", start=b, end=b + 5, sentence=text[31:],
                  identity_key="str_d2_msn")]
    result = {"source_metadata": {"source_id": "note:x"}, "entities": items}
    plan = {"entities": {g["id"]: {"normalized_key": "str_d2_msn"} for g in mention_groups(result)}}
    ttl, _ = result_to_ttl(result, kg_plan=plan)
    g = Graph().parse(data=ttl, format="turtle")
    keys = {str(o) for o in g.objects(None, NER.normalizedEntityKey)}
    assert keys == {"str_d2_msn"}


def _write(path, table, rows):
    schema = json.loads((SKILL / "schemas" / "ait-mapping-columns.json").read_text())
    cols = [c["name"] for c in schema["tables"][table]["columns"]]
    with open(path / table, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _run(*args):
    p = subprocess.run([PY, "-m", *args], cwd=SKILL, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


@pytest.mark.skipif(not (SKILL / "trusted_ontologies" / "Human_BG.ttl").is_file(), reason="needs Human_BG.ttl")
def test_ait_tables_to_validated_ttl(tmp_path):
    s1 = "Nuclei were mapped with MapMyCells to the HMBA consensus basal ganglia taxonomy."
    s2 = "The STR D1 MSN group expressed _DRD1_ strongly in human putamen."
    text = f"{s1} {s2}\n"
    (tmp_path / "paper.txt").write_text(text)
    i2 = text.index(s2)
    base = dict(paper_id=PID, extractor_version=VER, run_id=RUN, evidence_verified="not_found",
                extraction_flag="ok", n_mentions="1", assertion_type="asserted",
                field_provenance="label_verbatim=llm:extract|marker_genes=llm:extract|brain_region=llm:extract|species=llm:extract")
    ev = dict(evidence_sentence=s2, evidence_char_start=str(i2), evidence_char_end=str(i2 + len(s2)))
    _write(tmp_path, "paper.csv", [dict(paper_id=PID, title="A test paper", extracted_at="2026-10-15T00:00:00+00:00",
                                        extractor_version=VER, run_id=RUN)])
    _write(tmp_path, "sources.csv", [dict(paper_id=PID, source_id="S1", source_type="main_text",
                                          retrieved="true", retrieval_status="ok")])
    _write(tmp_path, "entities.csv", [
        {**base, **ev, "mention_id": f"{PID}:M001", "entity_type": "cell_type", "label_verbatim": "STR D1 MSN",
         "label_normalized": "STR D1 MSN", "marker_genes": "DRD1", "marker_genes_normalized": "DRD1",
         "species": "human", "species_role": "experimental", "ncbi_taxon_id": "9606", "assay_id": "A1",
         "hierarchy_level": "subclass"},
        {**base, **ev, "mention_id": f"{PID}:M002", "entity_type": "gene", "label_verbatim": "DRD1",
         "label_normalized": "DRD1", "species": "human", "species_role": "experimental"},
        {**base, **ev, "mention_id": f"{PID}:M003", "entity_type": "species", "label_verbatim": "human",
         "label_normalized": "Homo sapiens", "species": "human", "species_role": "experimental",
         "ncbi_taxon_id": "9606"},
        {**base, **ev, "mention_id": f"{PID}:M004", "entity_type": "brain_region", "label_verbatim": "putamen",
         "label_normalized": "putamen", "brain_region": "putamen"},
    ])
    _write(tmp_path, "methods.csv", [dict(paper_id=PID, assay_id="A1", assay_name="snRNA-seq", targeted="false",
                                          evidence_sentence=s1, evidence_verified="not_found", run_id=RUN)])
    _write(tmp_path, "mappings.csv", [dict(
        paper_id=PID, mention_id=f"{PID}:M001", ait_id="AIT19.5", ait_taxonomy_used="HMBA BG",
        ait_node_id="CS20250428_SUBCL_0028", ait_cell_type_label="STR D1 MSN", ait_hierarchy_level="Subclass",
        skos_relation="skos:exactMatch", basis_for_match="author_statement|label_exact", mapping_id=f"{PID}:MAP001",
        mapped_by="llm", field_provenance="ait_node_id=code:ontology_lookup", run_id=RUN,
        notes="Node read from trusted_ontologies/Human_BG.ttl.")])
    _write(tmp_path, "entity_cards.csv", [])
    _write(tmp_path, "review_sheet.csv", [])
    _run("scripts.ait_evidence", str(tmp_path), "--text", str(tmp_path / "paper.txt"))
    _run("scripts.ait_tables", "derive", str(tmp_path))
    (tmp_path / "markers.json").write_text(json.dumps({"CS20250428_SUBCL_0028": []}))
    _run("scripts.ait_gene_diff", str(tmp_path), "--markers", str(tmp_path / "markers.json"), "--namespace", "HGNC")
    _run("scripts.ait_tables", "review-sheet", str(tmp_path))
    _run("scripts.ait_tables", "validate", str(tmp_path))
    # --offline: the fallback map stage uses the trusted files only (no network in tests)
    out = _run("scripts.ait_ttl", str(tmp_path), "--text", str(tmp_path / "paper.txt"), "--offline")
    assert "VALID" in out
    g = Graph().parse(tmp_path / f"{PID}.ttl")
    cell = g.value(predicate=NER.normalizedEntityKey, object=Literal("str_d1_msn"))
    gene = g.value(predicate=NER.normalizedEntityKey, object=Literal("drd1"))
    node_iri = [o for o in g.objects(cell, SKOS.exactMatch)]
    assert len(node_iri) == 1 and str(node_iri[0]).startswith("urn:bkbit:")
    assert (cell, URIRef("http://purl.obolibrary.org/obo/RO_0002292"), gene) in g
    # the italic _DRD1_ is grounded as the bare symbol
    assert {str(o) for m in g.objects(gene, NER.hasMention) for o in g.objects(m, NER.surfaceForm)} == {"DRD1"}
    # the gene is linked to its GeneAnnotation in the human annotation (catalog gene_annotations)
    from scripts import ait_nodes
    want = ait_nodes.gene_lookup("human").resolve("DRD1")
    assert want.tier == "symbol" and want.iri
    assert [str(o) for o in g.objects(gene, SKOS.exactMatch)] == [want.iri]
    concept = g.value(gene, NER.resolvedToConcept)
    assert str(g.value(concept, NER.conceptIRI)) == want.iri
    assert str(g.value(concept, NER.conceptIdentifier)) == "BKBIT:" + want.iri.split(":")[-1]
    # extraction -> concept mapping against the trusted ontologies: species and region
    species = g.value(predicate=NER.normalizedEntityKey, object=Literal("homo_sapien"))
    region = g.value(predicate=NER.normalizedEntityKey, object=Literal("putamen"))
    for ent, iri in ((species, "http://purl.obolibrary.org/obo/NCBITaxon_9606"),
                     (region, "http://purl.obolibrary.org/obo/UBERON_0001874")):
        concept = g.value(ent, NER.resolvedToConcept)
        assert str(g.value(concept, NER.conceptIRI)) == iri
    # no concept_mappings.csv yet: ait_ttl ran the map stage (trusted files only) and wrote it
    assert "ran the concept map stage" in out
    assert "mapped brain_region 1, gene 1, species 1" in out
    rows = {r["mention_id"]: r for r in csv.DictReader(open(tmp_path / "concept_mappings.csv"))}
    assert rows[f"{PID}:M002"]["mapping_source"] == "trusted:annotation-ENSEMBL-9606-109"
    # identifiers are never cut: no label is a shortened CURIE
    assert not [str(o) for o in g.objects(None, RDFS.label) if "…" in str(o) and ":" in str(o)]


def test_long_identifier_names_a_decision_by_its_label():
    from types import SimpleNamespace
    from json_to_ttl import TurtleBuilder
    b = SimpleNamespace(cfg=SimpleNamespace(label_max_length=60))  # id_name reads only the label cap
    b.id_name = TurtleBuilder.id_name.__get__(b)
    long_curie = "BKBIT:" + "0" * 64
    assert b.id_name(long_curie, "ABC1", "BKBIT") == "ABC1"
    assert b.id_name("CL:0000617", "GABAergic neuron", "CL") == "CL:0000617"  # fits: the CURIE stays
    assert b.id_name(long_curie, None, "BKBIT") == "BKBIT concept"


@pytest.mark.skipif(not (SKILL / "trusted_ontologies" / "Human_BG.ttl").is_file(), reason="needs Human_BG.ttl")
def test_a_correction_in_concept_mappings_reaches_the_graph(tmp_path):
    """The Turtle is written from concept_mappings.csv, so a judge/curator correction
    there (here: a demoted region) is what the graph says — nothing re-maps it."""
    test_ait_tables_to_validated_ttl(tmp_path)
    p = tmp_path / "concept_mappings.csv"
    rows = list(csv.DictReader(open(p)))
    for r in rows:
        if r["entity_type"] == "brain_region":
            r.update(ontology_id="", ontology_label="", ontology="", mapping_source="", match_tier="",
                     concept_mapping_provenance="unmapped", mapped_by="human", notes="curator: demoted")
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    out = _run("scripts.ait_ttl", str(tmp_path), "--text", str(tmp_path / "paper.txt"))
    assert "ran the concept map stage" not in out and "VALID" in out
    g = Graph().parse(tmp_path / f"{PID}.ttl")
    region = g.value(predicate=NER.normalizedEntityKey, object=Literal("putamen"))
    iris = {str(g.value(c, NER.conceptIRI)) for c in g.objects(region, NER.resolvedToConcept)}
    assert "http://purl.obolibrary.org/obo/UBERON_0001874" not in iris
    assert any("curator: demoted" in str(o) for o in g.objects(region, RDFS.comment))
