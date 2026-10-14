"""The review loop (judge + human feedback) across NER, ABCD and AIT, and the OLS MCP
client. Offline: a stub mapper with the ConceptMapper interface stands in for the
cascade (trusted -> local -> OLS MCP -> BioPortal); no LLM, no network."""
import copy
import json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL))

import human_feedback as hf  # noqa: E402
import record_judge as rj  # noqa: E402
import review_loop as rl  # noqa: E402
from judge_combine import combine  # noqa: E402
from ols_mcp_map import OlsMcpMapper  # noqa: E402

TEXT = ("Zebrafish pDp neurons showed attractor dynamics. The hippocampus was not recorded. "
        "BDNF was elevated in pDp.")


class StubMapper:
    """ConceptMapper interface: map_items(items, surface_key) in place; a fixed lexicon."""
    sources = ["trusted"]
    LEX = {"ammon's horn": ("http://purl.obolibrary.org/obo/UBERON_0001954", "Ammon's horn", "UBERON"),
           "piriform cortex": ("http://purl.obolibrary.org/obo/UBERON_0004725", "piriform cortex", "UBERON"),
           "working memory": ("http://www.cognitiveatlas.org/ontology/cogat.owl#trm_4a3fd79d0b5a7",
                              "working memory", "COGAT"),
           "mus musculus": ("http://purl.obolibrary.org/obo/NCBITaxon_10090", "Mus musculus", "NCBITaxon")}

    def __init__(self):
        self.calls = []

    def map_items(self, items, surface_key, only_unmapped=False):
        for it in items:
            q = (it.get("mapping_query") or it[surface_key]).lower()
            self.calls.append(q)
            hit = self.LEX.get(q)
            if hit:
                it.update({"ontology_id": hit[0], "ontology_label": hit[1], "ontology": hit[2],
                           "concept_mapping_provenance": "tool", "mapping_source": "trusted:stub",
                           "match_tier": "exactMatch"})
            else:
                it.update({"ontology_id": None, "concept_mapping_provenance": "unmapped",
                           "mapping_sources_tried": self.sources})

    def meta(self):
        return {"sources_priority": self.sources, "mapped_by_source": {}, "trusted_ontologies": []}


def mention(surface, label, **kw):
    s = TEXT.index(surface)
    return {"entity": surface, "label": label, "start": s, "end": s + len(surface),
            "sentence": TEXT, **kw}


@pytest.fixture
def ner_result():
    return {"entities": [
        mention("pDp", "BrainRegion", ontology_id="http://purl.obolibrary.org/obo/ZFA_0000181",
                ontology_label="telencephalic area pDp", ontology="ZFA", concept_mapping_provenance="tool"),
        mention("hippocampus", "BrainRegion", concept_mapping_provenance="unmapped"),
        mention("attractor dynamics", "Phenomenon"),
        mention("BDNF", "Gene")]}


def gid(result, surface):
    return next(r["id"] for r in rl.records("ner", result) if r["surface"] == surface)


# --------------------------------------------------------------------------- NER

def test_ner_judge_remaps_through_the_tool_and_keeps_drops_restorable(ner_result):
    pdp, hip, att = gid(ner_result, "pDp"), gid(ner_result, "hippocampus"), gid(ner_result, "attractor dynamics")
    reviews = {
        "mapping": {"judge": "mapping", "items": [
            {"id": pdp, "verdict": "fail", "confidence": 0.9, "reason": "homolog", "suggestion": {"query": "piriform cortex"}},
            {"id": hip, "verdict": "flag", "confidence": 0.7, "reason": "searchable", "suggestion": {"query": "Ammon's horn"}}]},
        "grounding": {"judge": "grounding", "items": [
            {"id": att, "verdict": "fail", "confidence": 0.9, "reason": "test"}]}}
    cfg = json.loads((SKILL / "judges_config.json").read_text())
    out, _, report = combine(ner_result, reviews, cfg, None, remapper=StubMapper())
    got = {r["id"]: r["to"] for r in report["remapped"]}
    assert got[pdp].endswith("UBERON_0004725") and got[hip].endswith("UBERON_0001954")
    assert all(not m.get("mapping_tier") for m in out["entities"] if m["entity"] in ("pDp", "hippocampus"))
    assert att in out["review_loop"]["dropped"] and all(m["entity"] != "attractor dynamics" for m in out["entities"])

    log = hf.apply_feedback("ner", out, [{"id": att, "action": "restore", "reason": "it is in the text"}])
    assert log[0]["applied"] and any(m["entity"] == "attractor dynamics" for m in out["entities"])
    assert att not in out["review_loop"]["dropped"]


def test_ner_human_ops_follow_the_same_rules(ner_result):
    bdnf = gid(ner_result, "BDNF")
    tools = rl.Tools(mapper=StubMapper())
    log = hf.apply_feedback("ner", ner_result, [
        {"id": bdnf, "action": "set", "field": "label", "value": "Protein"},
        {"id": bdnf, "action": "set", "field": "ontology_id", "value": "http://example.org/x"},
        {"id": gid(ner_result, "hippocampus"), "action": "remap", "value": "Ammon's horn"},
        {"id": gid(ner_result, "pDp"), "action": "remap", "value": "not a term anywhere"},
        {"id": "nope|Gene", "action": "drop"},
        {"id": gid(ner_result, "pDp"), "action": "restore"}], tools=tools, by="tester")
    applied = [e["applied"] for e in log]
    assert applied == [True, False, True, False, False, False]
    assert "not correctable" in log[1]["rejected_because"]
    assert "no tool mapping" in log[3]["rejected_because"]
    assert "restore never adds" in log[5]["rejected_because"]
    hip = next(m for m in ner_result["entities"] if m["entity"] == "hippocampus")
    assert hip["ontology_id"].endswith("UBERON_0001954") and hip["alignment_method"] == "human_remapped"
    rnd = ner_result["review_loop"]["rounds"][-1]
    assert rnd["actor"] == "human" and rnd["by"] == "tester" and rnd["applied"] == 2


def test_queue_lists_escalations_drops_and_remaps(ner_result):
    pdp = gid(ner_result, "pDp")
    ner_result["judge_ensemble"] = {"combiner": {"escalated": [{"id": pdp, "question": "pDp or piriform?"}]}}
    ner_result["review_loop"] = {"rounds": [], "dropped": {"x|Gene": {"mode": "ner", "key": "entities",
                                                                     "items": [mention("BDNF", "Gene")]}}}
    q = hf.build_queue("ner", ner_result)
    assert q[0]["id"] == pdp and q[0]["why"][0].startswith("escalated")
    assert any(i["kind"] == "dropped" and i["id"] == "x|Gene" for i in q)


# --------------------------------------------------------------------------- ABCD

def abcd_doc():
    def ev(q):
        return {"quote": q, "start": 0, "end": len(q), "anchor_method": "as_reported", "verified": True}
    return {"paper_id": "p1", "provenance": {}, "verification": {"rejected_total": 0}, "rejected": [],
            "variables": [{"name": "fes_y_ss_fc", "mention_as_written": "Family conflict", "role": "predictor",
                           "dictionary_status": "verified", "nda_or_nbdc_table": "abcd_sscey01",
                           "dictionary_match": {"label": "conflict subscale"},
                           "evidence": ev("Family conflict was entered as a covariate in all models")}],
            "constructs": [{"construct": "working memory", "mapping_provenance": "unmapped",
                            "evidence": ev("We modelled working memory with the list sorting task")}],
            "models": [],
            "findings": [{"statement": "screen time and sleep", "direction": "positive", "role": "predictor",
                          "variables": [], "evidence": ev("screen time was negatively associated with sleep "
                                                          "(beta = -0.12)")}]}


def test_abcd_concept_map_judge_and_feedback():
    doc = abcd_doc()
    tools = rl.Tools(mapper=StubMapper())
    rl.map_abcd(doc, tools)
    assert doc["constructs"][0]["ontology_id"].endswith("trm_4a3fd79d0b5a7")
    assert doc["constructs"][0]["ontology_mapping_source"] == "trusted:stub"
    ids = {r["kind"]: r["id"] for r in rl.records("abcd", doc)}
    assert ids == {r["kind"]: r["id"] for r in rl.records("abcd", copy.deepcopy(doc))}  # stable ids

    reviews = {
        "labeling": {"judge": "labeling", "items": [
            {"id": ids["variable"], "verdict": "flag", "confidence": 0.9, "reason": "covariate",
             "suggestion": {"set": {"role": "covariate"}}}]},
        "claims": {"judge": "claims", "items": [
            {"id": ids["finding"], "verdict": "flag", "confidence": 0.9, "reason": "sign",
             "suggestion": {"set": {"direction": "negative", "effect_size": "-0.12"}}}]},
        "mapping": {"judge": "mapping", "items": [
            {"id": ids["construct"], "verdict": "flag", "confidence": 0.8, "reason": "broader",
             "suggestion": {"tier": "broadMatch"}}]},
        "grounding": {"judge": "grounding", "items": []}}
    s = rj.combine("abcd", doc, reviews, tools=tools)
    assert s["applied"] == 4 and s["rejected"] == 0
    assert doc["variables"][0]["role"] == "covariate"
    assert doc["findings"][0]["direction"] == "negative" and doc["findings"][0]["effect_size"] == "-0.12"
    assert doc["constructs"][0]["ontology_match_tier"] == "broadMatch"

    log = hf.apply_feedback("abcd", doc, [
        {"id": ids["finding"], "action": "drop", "reason": "cited work"},
        {"id": ids["variable"], "action": "set", "field": "timepoint", "value": "year 2"},
        {"id": ids["finding"], "action": "restore"}])
    assert [e["applied"] for e in log] == [True, False, True]
    assert "evidence quote" in log[1]["rejected_because"]
    assert len(doc["findings"]) == 1 and doc["rejected"] == []


def test_abcd_judge_gate_moves_item_to_rejected():
    doc = abcd_doc()
    fid = next(r["id"] for r in rl.records("abcd", doc) if r["kind"] == "finding")
    rj.combine("abcd", doc, {"claims": {"judge": "claims", "items": [
        {"id": fid, "verdict": "fail", "confidence": 0.9, "reason": "Smith et al. reported it"}]}})
    assert doc["findings"] == [] and doc["rejected"][0]["reason"].startswith("judge:claims")
    assert doc["verification"]["rejected_by_judge"] == 1


# --------------------------------------------------------------------------- AIT

def ait_dir(tmp_path):
    from scripts import ait_tables as at
    p = "P1"
    prov = "label_verbatim=llm:extract|marker_genes=llm:extract|brain_region=llm:extract|species=llm:extract"

    def ent(mid, typ, lab, ver):
        return {"paper_id": p, "mention_id": f"{p}:{mid}", "entity_type": typ, "label_verbatim": lab,
                "label_normalized": lab, "n_mentions": "1", "species": "mouse", "species_role": "experimental",
                "hierarchy_level": "subclass", "assertion_type": "asserted", "evidence_sentence": f"{lab} were seen.",
                "evidence_verified": ver, "extraction_flag": "ok", "field_provenance": prov,
                "extractor_version": "v1", "run_id": "r1"}
    data = {"paper.csv": [{"paper_id": p, "title": "t", "run_id": "r1", "extracted_at": "2026-10-08T00:00:00Z",
                           "extractor_version": "v1"}],
            "sources.csv": [], "methods.csv": [],
            "entities.csv": [ent("M001", "cell_type", "Sst interneurons", "exact"),
                             ent("M002", "species", "Mus musculus", "exact"),
                             ent("M003", "cell_type", "ghost cells", "fuzzy")],
            "mappings.csv": [{"paper_id": p, "mention_id": f"{p}:M001", "ait_taxonomy_used": "AIT33", "ait_id": "AIT33",
                              "ait_node_id": "CS_SUBC_050", "ait_cell_type_label": "Pvalb Gaba",
                              "ait_hierarchy_level": "subclass", "skos_relation": "skos:closeMatch",
                              "basis_for_match": "label_normalized", "mapping_id": f"{p}:M001:A1",
                              "mapped_by": "llm", "field_provenance": "ait_node_id=llm:extract", "run_id": "r1"}],
            "entity_cards.csv": []}
    at.derive(data)
    data["review_sheet.csv"] = at.build_review_sheet(data)
    for t in at.TABLES:
        at.write_table(tmp_path / t, t, data.get(t, []))
    (tmp_path / "run_report.md").write_text("x\n")
    return tmp_path


def test_ait_loop_keeps_the_table_contract(tmp_path):
    from scripts import ait_tables as at
    out = ait_dir(tmp_path)
    tools = rl.Tools(mapper=StubMapper())
    data = rl.load("ait", out)
    rl.map_ait(data, tools)
    cm = {c["mention_id"]: c for c in data[rl.CONCEPT_TABLE]}
    assert cm["P1:M002"]["ontology_id"].endswith("NCBITaxon_10090")
    assert next(e for e in data["entities.csv"] if e["mention_id"] == "P1:M002")["ncbi_taxon_id"] == "10090"

    s = rj.combine("ait", data, {
        "mapping": {"judge": "mapping", "items": [
            {"id": "P1:M001:A1", "verdict": "fail", "confidence": 0.95, "reason": "Sst is not Pvalb"}]},
        "grounding": {"judge": "grounding", "items": [
            {"id": "P1:M003", "verdict": "fail", "confidence": 0.9, "reason": "not in the paper"}]}}, tools=tools)
    assert s["applied"] == 2
    rl.save("ait", data, out)
    assert not [i for i in at.validate(out) if i.level == "error"]
    edge = at.current_mappings(data["mappings.csv"])
    head = next(m for m in edge if m["mention_id"] == "P1:M001")
    assert head["skos_relation"] == "none" and head["supersedes_mapping_id"] == "P1:M001:A1"

    log = hf.apply_feedback("ait", data, [
        {"id": head["mapping_id"], "action": "remap",
         "value": {"ait_node_id": "CS_SUBC_051", "ait_cell_type_label": "Sst Gaba", "skos_relation": "skos:closeMatch"}},
        {"id": "P1:M002:C", "action": "set", "field": "tier", "value": "sameAs"}], tools=tools)
    assert [e["applied"] for e in log] == [True, False]
    rl.save("ait", data, out)
    assert not [i for i in at.validate(out) if i.level == "error"]
    new = next(m for m in at.current_mappings(data["mappings.csv"]) if m["mention_id"] == "P1:M001")
    assert new["mapped_by"] == "human" and new["ait_node_id"] == "CS_SUBC_051" and not new["no_match_reason"]


def test_a_judge_cannot_pin_an_ait_node(tmp_path):
    data = rl.load("ait", ait_dir(tmp_path))
    log = rl.apply_ops("ait", data, [{"id": "P1:M001:A1", "action": "remap",
                                      "value": {"ait_node_id": "X", "ait_cell_type_label": "x",
                                                "skos_relation": "skos:closeMatch"}}], actor="judge")
    assert not log[0]["applied"] and "only be re-pointed by a human" in log[0]["rejected_because"]


# --------------------------------------------------------------------------- OLS MCP

def test_ols_mcp_accepts_only_an_exact_label(monkeypatch):
    m = OlsMcpMapper()
    items = {"items": [
        {"iri": "http://purl.obolibrary.org/obo/UBERON_0003881", "curie": "UBERON:0003881",
         "label": ["CA1 field of hippocampus"], "isObsolete": False},
        {"iri": "http://purl.obolibrary.org/obo/UBERON_0001954", "curie": "UBERON:0001954",
         "label": ["Ammon's horn"], "isObsolete": False}]}
    monkeypatch.setattr(m, "call", lambda tool, args: items)
    hit = m.map_one("Ammon's horn", ["UBERON"])
    assert hit["ontology_id"].endswith("UBERON_0001954") and hit["ontology"] == "UBERON"
    assert m.map_one("hippocampus", ["UBERON"])["concept_mapping_provenance"] == "unmapped"
    assert m.map_one("Ammon's horn", ["UBERON"], accept=lambda iri: False)["concept_mapping_provenance"] == "unmapped"


def test_default_cascade_order():
    cfg = json.loads((SKILL / "concept_mapping.json").read_text())
    assert cfg["sources_priority"] == ["trusted", "local_hybrid", "ols", "bioportal"]
    assert cfg["remote"]["ols_mcp_url"] == "https://www.ebi.ac.uk/ols4/api/mcp"
