"""Resource extraction -> resource KG (BrainKB Resource Ontology). Offline: no LLM,
no network; concept mapping uses a stub mapper with the ConceptMapper interface."""
import json
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))

from json_to_ttl import result_to_ttl  # noqa: E402
from resource_kg import build, conform, ground, load_config, prepare, to_records, validate_graph  # noqa: E402

BKR = Namespace("https://brainkb.org/resource/")
BKRLS = Namespace("https://brainkb.org/resource/lifesci/")
NER = Namespace("https://brainkb.org/ner/")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
SCHEMA = Namespace("https://schema.org/")
MOUSE = "http://purl.obolibrary.org/obo/NCBITaxon_10090"

TEXT = ("We introduce CellMapper, a Python package for mapping single-cell profiles onto a "
        "reference atlas. CellMapper version 2.1.0 is available at https://github.com/example/cellmapper "
        "under the MIT license. CellMapper requires raw integer counts as input. "
        "We benchmarked CellMapper on mouse cortex, where it reached an accuracy of 0.93. "
        "On human tissue it was not evaluated. When the reference lacks a cell type, CellMapper "
        "silently assigns the nearest type. We used Scanpy for preprocessing.")
META = {"doi": "10.1234/cellmapper.2026", "paper_title": "CellMapper"}


def record(**extra):
    rec = {
        "record_id": "r1", "extracted_type": "software_library", "name": "CellMapper",
        "description": "A Python package for mapping single-cell profiles onto a reference atlas.",
        "url": "https://github.com/example/cellmapper", "license": "MIT license",
        "versions": [{"version": "2.1.0"}],
        "applicability": {
            "validated": [{"statement": "mouse cortex", "species": [{"label": "mouse"}],
                           "anatomical_structures": [{"label": "cortex"}],
                           "evidence": [{"quote": "We benchmarked CellMapper on mouse cortex, where it reached an accuracy of 0.93."}]}],
            "declared": [{"statement": "human tissue", "species": [{"label": "human"}],
                          "evidence": [{"quote": "On human tissue it was not evaluated."}]}]},
        "assumptions": [{"statement": "Input must be raw integer counts.", "kind": "preprocessing",
                         "criticality": "critical",
                         "consequence": "Normalised input gives wrong assignments.", "evidence": [{"quote": "CellMapper requires raw integer counts as input."}]}],
        "failure_modes": [{"statement": "Assigns the nearest type when the reference lacks one.",
                           "condition": "reference lacks a cell type", "silent": True,
                           "evidence": [{"quote": "When the reference lacks a cell type, CellMapper silently assigns the nearest type."}]}],
        "benchmark_evidence": [{"metric": "accuracy", "value": 0.93,
                                "evidence": [{"quote": "it reached an accuracy of 0.93"}]}],
        "mentions": [{"name": "Scanpy", "extracted_type": "software_library"}],
        "provenance": {"field_evidence": [{"field": "versions[0]", "quote": "CellMapper version 2.1.0 is available"}]},
        "not_found_fields": ["access"],
    }
    rec.update(extra)
    return rec


def res(*records, meta=META):
    return {"task_type": "resource", "extracted_resources": list(records), "source_metadata": dict(meta)}


class StubMapper:
    """ConceptMapper.map_items stand-in: exact for mouse, a related synonym for cortex,
    ambiguous for human."""

    def map_items(self, items, key):
        for it in items:
            term = it[key].lower()
            if term == "mouse":
                it.update(ontology_id=MOUSE, ontology_label="Mus musculus", ontology="NCBITaxon",
                          concept_mapping_provenance="tool", alignment_method="trusted_ontology",
                          mapping_source="trusted:ncbitaxon", ontology_match_type="exact_synonym",
                          match_tier="exactMatch")
            elif term == "cortex":
                it.update(ontology_id="http://purl.obolibrary.org/obo/UBERON_0001851", ontology_label="cortex",
                          ontology="UBERON", concept_mapping_provenance="tool", alignment_method="trusted_ontology",
                          mapping_source="trusted:uberon", ontology_match_type="related_synonym",
                          match_tier="relatedMatch")
            elif term == "human":
                it.update(concept_mapping_provenance="unmapped",
                          trusted_ambiguous=[{"ontology_id": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
                                              "ontology_label": "Homo sapiens"},
                                             {"ontology_id": "http://example.org/Human", "ontology_label": "human"}])
            else:
                it["concept_mapping_provenance"] = "unmapped"

    def meta(self):
        return {"mapper_used": "stub"}


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "cellmapper.txt"
    p.write_text(TEXT, encoding="utf-8")
    return p


def graph_of(ttl):
    return Graph().parse(data=ttl, format="turtle")


def test_build_is_valid_bkr_and_deterministic(src):
    ttl, rep = build(res(record()), source_path=src, mapper=StubMapper())
    ttl2, _ = build(res(record()), source_path=src, mapper=StubMapper())
    assert ttl == ttl2
    g = graph_of(ttl)
    report = validate_graph(g)
    assert report["ok"], report["violations"] + report["problems"]
    assert report["components"] == 1
    tool = next(g.subjects(BKR.resourceName, Literal("CellMapper")))
    assert (tool, RDF.type, BKR.SoftwareLibrary) in g or (tool, RDF.type, BKR.Resource) in g
    assert (tool, BKR.rightsStatement, Literal("MIT license")) in g  # stated, not an invented IRI
    assert (tool, BKR.hasValidatedScope, None) in g and (tool, BKR.hasDeclaredScope, None) in g
    fm = g.value(tool, BKR.hasLimitation)
    assert any(g.value(n, BKR.isSilentFailure) == Literal(True) for n in g.objects(tool, BKR.hasLimitation))
    assert fm is not None
    assert rep["counts"]["stubs_kept"] == 1  # Scanpy: named, not described


def test_shared_publication_with_ner_graph(src):
    ttl, rep = build(res(record()), source_path=src)
    i = TEXT.index("Scanpy")
    ner_ttl, _ = result_to_ttl({"task_type": "ner", "source_metadata": dict(META), "entities": [
        {"entity": "Scanpy", "label": "Software", "start": i, "end": i + 6,
         "sentence": "We used Scanpy for preprocessing."}]}, source_path=src)
    pub_r = set(graph_of(ttl).subjects(RDF.type, NER.SourceDocument))
    pub_n = set(graph_of(ner_ttl).subjects(RDF.type, NER.SourceDocument))
    assert pub_r == pub_n == {URIRef(rep["publication"])}
    g = graph_of(ttl)
    assert all((r, DCTERMS.isReferencedBy, URIRef(rep["publication"])) in g
               for r in g.subjects(BKR.hasRecord, None))


def test_grounding_removes_what_the_text_does_not_state():
    rec = record(url="https://cellmapper.example.org", stable_identifiers=[{"value": "RRID:SCR_999999", "scheme": "RRID"}])
    rec["versions"].append({"version": "2.1"})          # a prefix of 2.1.0 is not stated
    rec["assumptions"][0]["evidence"][0]["quote"] = "CellMapper needs counts."  # paraphrase
    rec["provenance"]["field_evidence"][0].update(start=3, end=40)
    rec["mentions"].append({"name": "Seurat"})
    records, _ = to_records(res(rec))
    rep = ground(records, TEXT, load_config())
    r = records[0]
    reasons = {(x["field"].split("[")[0], x["reason"]) for x in rep["removed"]}
    assert "url" not in r and "stable_identifiers" not in r
    assert [v["version"] for v in r["versions"]] == ["2.1.0"]
    assert r["assumptions"][0]["evidence"] == []
    assert ("assumptions", "quote not found in the source") in reasons
    assert "start" not in r["provenance"]["field_evidence"][0]
    assert [m["name"] for m in r["mentions"]] == ["Scanpy"]
    assert {"url", "stable_identifiers"} <= set(r["not_found_fields"])


def test_validated_scope_without_evidence_is_declared():
    rec = record(benchmark_evidence=[])
    rec["applicability"]["validated"][0]["evidence"] = [{"quote": "an invented validation sentence"}]
    records, _ = to_records(res(rec))
    ground(records, TEXT, load_config())
    app = records[0]["applicability"]
    assert app["validated"] == []
    assert any(s["statement"] == "mouse cortex" for s in app["declared"])


def test_llm_mapping_never_publishes_an_iri(src):
    rec = record()
    rec["applicability"]["declared"][0]["species"] = [{"label": "human", "mapped": [
        {"concept_iri": "http://purl.obolibrary.org/obo/NCBITaxon_9606", "status": "accepted",
         "method": "llm_judgment", "provenance_raw": "llm_knowledge"}]}]
    ttl, _ = build(res(rec), source_path=src)
    g = graph_of(ttl)
    assert not list(g.subjects(BKRLS.appliesToTaxon, URIRef("http://purl.obolibrary.org/obo/NCBITaxon_9606")))
    assert not list(g.subjects(NER.conceptIRI, Literal("http://purl.obolibrary.org/obo/NCBITaxon_9606",
                                                       datatype=URIRef("http://www.w3.org/2001/XMLSchema#anyURI"))))


def test_tool_mapping_status_by_tier(src):
    ttl, rep = build(res(record()), source_path=src, mapper=StubMapper())
    g = graph_of(ttl)
    assert rep["mapping"]["accepted"] == 1 and rep["mapping"]["proposed"] == 1 and rep["mapping"]["ambiguous"] == 1
    # accepted exact: asserted on the scope, plus a SKOS shortcut
    assert list(g.subjects(BKRLS.appliesToTaxon, URIRef(MOUSE)))
    assert list(g.subjects(SKOS.exactMatch, URIRef(MOUSE)))
    # proposed related synonym: a recorded decision, not an assertion
    uberon = URIRef("http://purl.obolibrary.org/obo/UBERON_0001851")
    assert not list(g.subjects(BKRLS.appliesToAnatomicalStructure, uberon))
    assert any(str(o) == str(uberon) for o in g.objects(None, NER.conceptIRI))
    statuses = {str(o).rsplit("/", 1)[-1] for o in g.objects(None, NER.mappingStatus)}
    assert {"accepted", "proposed", "ambiguous"} <= statuses


def test_one_resource_node_across_papers(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("We used Scanpy version 1.9.3 to cluster cells.", encoding="utf-8")
    b.write_text("Clustering was done in SCANPY version 1.10.0.", encoding="utf-8")
    scan = lambda v: {"record_id": "r1", "extracted_type": "software_library", "name": "Scanpy",
                      "versions": [{"version": v}], "applicability": {"observed": [{"statement": "clustering"}]}}
    ga = graph_of(build(res(scan("1.9.3"), meta={"doi": "10.1/a"}), source_path=a)[0])
    gb = graph_of(build(res(dict(scan("1.10.0"), name="SCANPY"), meta={"doi": "10.1/b"}), source_path=b)[0])
    corpus = ga + gb
    nodes = {s for s in corpus.subjects(RDF.type, BKR.Resource) if (s, BKR.hasRecord, None) in corpus}
    assert len(nodes) == 1
    node = nodes.pop()
    assert len(set(corpus.objects(node, BKR.hasRecord))) == 2
    assert len(set(corpus.objects(node, DCTERMS.isReferencedBy))) == 2
    assert {str(corpus.value(v, BKR.versionIdentifier)) for v in corpus.objects(node, BKR.hasVersion)} == {"1.9.3", "1.10.0"}


def test_scope_gap_fails_unless_declared(src):
    rec = record(applicability={}, benchmark_evidence=[])
    g = graph_of(build(res(rec), source_path=src)[0])
    rep = validate_graph(g)
    assert not rep["ok"] and any(v["shape"] == "ScopedResourceShape" for v in rep["violations"])
    rec = record(applicability={}, benchmark_evidence=[], not_found_fields=["applicability", "access"])
    rep = validate_graph(graph_of(build(res(rec), source_path=src)[0]))
    assert rep["ok"] and any(f["shape"] == "ScopedResourceShape" for f in rep["source_silence_findings"])


def test_licence_silence_is_a_finding(src):  # noqa: D103
    rec = record()
    rec.pop("license")
    rep = validate_graph(graph_of(build(res(rec), source_path=src)[0]))
    assert rep["ok"]
    assert any(f["shape"] == "CitableResourceShape" for f in rep["source_silence_findings"])


def test_prepare_then_build_without_source_keeps_grounded_values(src):
    result = res(record())
    prepare(result, TEXT)
    assert result["resource_grounding"]["grounded"]
    ttl, rep = result_to_ttl(json.loads(json.dumps(result)))  # e.g. json_to_ttl with no --source
    g = graph_of(ttl)
    assert rep["grounding"].get("grounded_earlier")
    assert {str(o) for o in g.objects(None, BKR.versionIdentifier)} == {"2.1.0"}


def test_off_schema_values_are_removed_not_fatal():
    schema = json.loads(Path(load_config()["schema"]).read_text())
    bad = record(extracted_type="Library", colour="blue")
    bad["assumptions"][0]["criticality"] = "high"
    nameless = {"record_id": "r2", "extracted_type": "tool"}
    unknown = {"record_id": "r3", "extracted_type": "spaceship", "name": "X"}
    kept, notes = conform([bad, nameless, unknown], schema)
    assert [r["name"] for r in kept] == ["CellMapper"]
    assert kept[0]["extracted_type"] == "software_library"
    assert "colour" not in kept[0] and "criticality" not in kept[0]["assumptions"][0]
    assert any("record dropped" in n for n in notes) and any("without a name" in n for n in notes)


def test_validate_ttl_dispatches_resource_graphs(src, tmp_path):
    from validate_ttl import validate_file
    out = tmp_path / "r.ttl"
    out.write_text(build(res(record()), source_path=src)[0], encoding="utf-8")
    rep = validate_file(out)
    assert rep["kind"] == "resource_kg" and rep["ok"]


def test_bundled_example_converts_and_validates():
    example = json.loads((SKILL / "examples" / "resource-bkr-example.json").read_text())
    ttl, rep = build(example, source_text=None)
    assert rep["counts"]["resources"] >= 1
    assert validate_graph(graph_of(ttl))["ok"]


def test_resource_cqs_parse_and_answer(src, tmp_path):
    pytest.importorskip("owlrl")
    import subprocess
    out = tmp_path / "r.ttl"
    out.write_text(build(res(record()), source_path=src, mapper=StubMapper())[0], encoding="utf-8")
    report = tmp_path / "cq.json"
    subprocess.run([sys.executable, str(SKILL / "cqs" / "run_cqs.py"), str(out), "--cqs",
                    str(SKILL / "cqs" / "brainkb_resource_ontology_CQs.md"), "--with-ontology",
                    str(SKILL / "default_ontology" / "brainkb_resource_ontology.owl"), "--entail",
                    "--engine", "rdflib", "--report", str(report)], check=True, capture_output=True)
    results = {r["cq"]: r for r in json.loads(report.read_text())["results"]}
    assert len(results) == 21 and not [r for r in results.values() if r["status"] == "error"]
    for cq in ("CQ02", "CQ04", "CQ10", "CQ17", "CQ19"):  # scope split, assumptions, mapping audit, failure, work
        assert results[cq]["status"] == "answered", cq
