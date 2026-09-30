"""Identity basis: normalization, grounding, mapping assessment and TTL representation."""
import sys
from pathlib import Path

import rdflib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from identity import assess, candidate_queries, grounded_mask, normalize  # noqa: E402
from json_to_ttl import result_to_ttl  # noqa: E402
from validate_ttl import validate_file  # noqa: E402

NER = rdflib.Namespace("https://brainkb.org/ner/")
TEXT = "Pvalb-positive GABAergic interneurons in layer 5 of the motor cortex fire fast. Pvalb is a protein."
SPAN = "Pvalb-positive GABAergic interneurons"


def item(**ib):
    return {"entity": SPAN, "label": "CellType", "start": 0, "end": len(SPAN),
            "sentence": TEXT.split(". ")[0] + ".", "source_model": "llm_ner:test",
            "identity_basis": ib or {
                "canonical_candidate": "Pvalb GABAergic interneuron",
                "features": [
                    {"kind": "hierarchy", "value": "interneuron", "role": "supporting", "source": "explicit_text"},
                    {"kind": "molecular_marker", "value": "PVALB", "target": "Pvalb", "polarity": "positive",
                     "role": "defining", "source": "explicit_text", "quote": "Pvalb-positive"},
                    {"kind": "neurotransmitter", "value": "GABA", "role": "supporting", "source": "explicit_text",
                     "quote": "GABAergic"},
                    {"kind": "anatomical", "value": "motor cortex", "role": "contextual", "source": "explicit_text"},
                    {"kind": "morphological", "value": "chandelier", "role": "defining", "source": "explicit_text",
                     "quote": "chandelier axon"}]}}


def test_keyed_form_is_normalized():
    ib = normalize({"base_cell_type": {"value": "interneuron", "support": "explicit"},
                    "molecular_marker": [{"value": "PVALB", "polarity": "positive", "role": "defining"}],
                    "anatomical_location": [{"value": "motor cortex", "support": "context"}]})
    kinds = {(f["kind"], f["role"], f["source"]) for f in ib["features"]}
    assert ("hierarchy", "supporting", "explicit_text") in kinds
    assert ("molecular_marker", "defining", "explicit_text") in kinds
    assert ("anatomical", "contextual", "surrounding_context") in kinds


def test_ungrounded_feature_is_dropped():
    _ib, mask = grounded_mask(item(), TEXT)
    assert mask == [True, True, True, True, False]  # "chandelier axon" is not in the text


def test_contextual_features_never_justify_and_contradiction_is_found():
    it = item()
    a = assess(it, "pvalb GABAergic interneuron", ["Interneuron", "InhibitoryNeuron", "Neuron"])
    assert set(a["justified_by"]) == {0, 1, 2} and a["contradicted_by"] == []
    b = assess(it, "glutamatergic neuron", ["ExcitatoryNeuron", "Neuron"])
    assert 2 in b["contradicted_by"]


def test_candidate_queries_start_with_canonical():
    qs = candidate_queries(item(), SPAN)
    assert qs[0] == "Pvalb GABAergic interneuron" and any("interneuron" in q for q in qs[1:])


def test_ttl_carries_identity_and_validates(tmp_path):
    it = item()
    it.update({"ontology_id": "http://purl.obolibrary.org/obo/CL_4023018", "ontology_label": "pvalb GABAergic interneuron",
               "ontology": "CL", "concept_mapping_provenance": "tool", "alignment_method": "trusted_ontology",
               "mapping_source": "trusted:cl", "match_tier": "exactMatch",
               "identity_mapping": {"query": "Pvalb GABAergic interneuron", "basis": "identity",
                                    "justified_by": [0, 1, 2], "contradicted_by": []}})
    gene = {"entity": "Pvalb", "label": "Gene", "start": TEXT.index("Pvalb is"), "end": TEXT.index("Pvalb is") + 5,
            "sentence": "Pvalb is a protein.", "source_model": "llm_ner:test"}
    res = {"source_metadata": {"paper_title": "t", "doi": "10.9999/x"}, "task_type": "ner",
           "entities": [it, gene], "key_terms": []}
    src = tmp_path / "t.txt"
    src.write_text(TEXT)
    ttl, rep = result_to_ttl(res, source_path=src)
    out = tmp_path / "t.ttl"
    out.write_text(ttl)
    assert validate_file(out)["ok"]
    g = rdflib.Graph().parse(out)
    feats = set(g.subjects(NER.featureRole, None))
    assert len(feats) == 4 and rep["counts"]["identity_features_ungrounded"] == 1
    dec = next(g.subjects(rdflib.RDF.type, NER.ConceptMappingDecision))
    assert len(set(g.objects(dec, NER.justifiedByIdentityFeature))) == 3
    assert any(g.value(f, NER.featureEntity) is not None for f in feats)  # PVALB -> the Pvalb gene entity


def test_salience_attribution_and_r5(tmp_path):
    """Salience / passage focus / attribution / definitional role land on the mention;
    document focus on the publication; a cited finding is never defining (R5)."""
    cited = item()
    cited.update({"salience": "background_citation", "passage_focus": "cns_cells",
                  "attributed_to": "cited_work", "definitional_role": "uses", "specificity": "cell_phenotype"})
    res = {"source_metadata": {"paper_title": "t", "doi": "10.9999/r5",
                               "source_relevance": {"document_focus": "subject", "evidence": ["Abstract"]}},
           "task_type": "ner", "entities": [cited], "key_terms": []}
    src = tmp_path / "t.txt"
    src.write_text(TEXT)
    ttl, _ = result_to_ttl(res, source_path=src)
    out = tmp_path / "t.ttl"
    out.write_text(ttl)
    assert validate_file(out)["ok"]
    g = rdflib.Graph().parse(out)
    m = next(g.subjects(rdflib.RDF.type, NER.EntityMention))
    assert g.value(m, NER.mentionSalience) == NER["salience/background_citation"]
    assert g.value(m, NER.attributedTo) == NER["attribution/cited_work"]
    assert g.value(m, NER.definitionalRole) == NER["definitional-role/uses"]
    pub = next(g.subjects(rdflib.RDF.type, NER.Publication))
    assert g.value(pub, NER.documentFocus) == NER["document-focus/subject"]
    roles = {str(r) for r in g.objects(None, NER.featureRole)}
    assert str(NER["feature-role/defining"]) not in roles  # PVALB downgraded to supporting


def test_shacl_rejects_cited_defining(tmp_path):
    """The gate itself enforces R5, whatever wrote the graph."""
    bad = tmp_path / "bad.ttl"
    it = item()
    it.update({"attributed_to": "this_study"})
    res = {"source_metadata": {"paper_title": "t", "doi": "10.9999/r5b"}, "task_type": "ner",
           "entities": [it], "key_terms": []}
    src = tmp_path / "t.txt"
    src.write_text(TEXT)
    ttl, _ = result_to_ttl(res, source_path=src)
    ttl = ttl.replace("attribution/this_study", "attribution/cited_work")  # forge a cited + defining pair
    bad.write_text(ttl)
    rep = validate_file(bad)
    assert not rep["ok"] and any("R5" in k for k in rep["violations"])
