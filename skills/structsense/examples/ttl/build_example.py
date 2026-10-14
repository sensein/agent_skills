"""Build the Hu et al. 2026 worked example from the curated reference TTL.

Inputs (all shipped in this directory):
  hu2026_curated.ttl   a human-curated NER representation of the paper
                       (hu2026_pdp_manifolds_ner_v2.ttl): entities, keys, classes,
                       OLS4-verified mappings, RO/SKOS/PROV edges, causal claims.
  paper.txt            the paper's text (CC BY 4.0, see README.md), whitespace-
                       normalised; every offset below points into this file.

Outputs (the pipeline's inputs):
  paper_final.json     the aligned working JSON as an extractor would leave it:
                       EVERY occurrence of each curated surface form in paper.txt,
                       with real offsets, context sentence and section, labelled
                       with the neuroscience extractor's taxonomy. No mappings yet —
                       `concept_mapping map` adds them from the trusted ontologies.
  curated_mappings.json the curated file's OLS4-verified mappings, used only as the
                       reference the mapping judge compares against.
  kg_plan.json         keys, specific classes, paper-stated edges, causal claims.
  reviews/*.json       reference judge reviews derived from the curated file
                       (model "reference:hu2026_curated.ttl", mode "deterministic"):
                       they stand in for the LLM judges so the example is
                       reproducible, and they say so.

    python examples/ttl/build_example.py            # from the skill root
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import PROV, RDF, RDFS, SKOS

HERE = Path(__file__).resolve().parent
NER = Namespace("https://brainkb.org/ner/")
OBO = "http://purl.obolibrary.org/obo/"
REF = "reference:hu2026_curated.ttl"

# ontology class of the curated entity -> the label the neuroscience extractor
# (prompts/extractor-ner-neuroscience.md) would emit; kg_plan keeps the class.
CLASS_TO_LABEL = {
    "Species": "Species", "Strain": "TransgenicLine", "LifeStage": "DevelopmentalStage",
    "BrainRegion": "BrainRegion", "CorticalArea": "BrainRegion", "NeuralTract": "NervousSystemPart",
    "Neuron": "CellType", "ExcitatoryNeuron": "CellType", "InhibitoryNeuron": "CellType",
    "Interneuron": "CellType", "ProjectionNeuron": "CellType", "NeuralPopulation": "CellType",
    "AminoAcid": "Chemical", "ChemicalEntity": "Chemical", "Protein": "Protein",
    "IonChannel": "IonChannel", "Promoter": "Gene", "RegulatoryRegion": "Gene",
    "LearningProcess": "Phenomenon", "MemoryProcess": "Phenomenon", "SynapticProcess": "Phenomenon",
    "NeuralActivity": "Phenomenon", "Behavior": "Phenotype", "NeuroscienceEntity": "Other",
    "NeuroimagingModality": "Method", "ElectrophysiologyModality": "Method", "Stimulation": "Stimulus",
    "Assay": "BehavioralAssay", "ComputationalMethod": "Method", "Algorithm": "Method",
    "StatisticalMethod": "Method", "SoftwareEntity": "Software", "Measurement": "Measurement",
}
# RO/BFO IRIs in the curated file -> kg_plan short names (ttl_config.json relation_predicates)
RELATIONS = {"BFO_0000050": "part_of", "BFO_0000051": "has_part", "RO_0001025": "located_in",
             "RO_0000057": "has_participant", "RO_0000056": "participates_in"}
TIER = {str(SKOS.exactMatch): "exactMatch", str(SKOS.closeMatch): "closeMatch",
        str(SKOS.broadMatch): "broadMatch", str(SKOS.narrowMatch): "narrowMatch",
        str(SKOS.relatedMatch): "relatedMatch"}
# curated entities with no mention of their own: the words the paper uses for them
EXTRA_SURFACES = {"amino_acid_odor_class": ["amino acids"], "bile_acid_odor_class": ["bile acids"]}
SECTIONS = ("Results", "Discussion", "Methods", "Online content", "References")


def normalise(raw: str) -> str:
    t = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", raw)     # line-break hyphenation
    return re.sub(r"\s+", " ", t).strip()


def sections_of(text: str) -> list[tuple[int, str]]:
    marks = [(0, "Abstract / Introduction")]
    for name in SECTIONS:
        for m in re.finditer(rf" {re.escape(name)} (?=[A-Z])", text):
            marks.append((m.start(), name))
    return sorted(marks)


def section_at(marks, pos: int) -> str:
    cur = marks[0][1]
    for start, name in marks:
        if start > pos:
            break
        cur = name
    return cur


def sentence_at(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("? ", 0, start)) + 2
    right = text.find(". ", end)
    right = len(text) if right < 0 else right + 1
    left = max(left, start - 400)
    right = min(right, end + 400)
    return text[left if left > 1 else 0:right].strip()


def occurrences(text: str, surface: str) -> list[tuple[int, int]]:
    """Every occurrence as a whole token; case-sensitive for short symbols."""
    flags = 0 if len(surface) <= 5 or not surface.islower() else re.I
    # a trailing digit is a citation superscript run into the word ("imaging48")
    pat = r"(?<![A-Za-z0-9])" + re.escape(surface) + r"(?![A-Za-z])"
    return [(m.start(), m.end()) for m in re.finditer(pat, text, flags)]


def main() -> int:
    text_path = HERE / "paper.txt"
    raw = text_path.read_text()
    text = normalise(raw)
    if text != raw:
        text_path.write_text(text)
    marks = sections_of(text)
    g = Graph()
    g.parse(HERE / "hu2026_curated.ttl")

    ents = [s for s in g.subjects(RDF.type, NER.NamedEntity)]
    by_node: dict = {}
    entities, curated, plan_entities, missing = [], {}, {}, []
    for e in sorted(ents, key=str):
        key = str(g.value(e, NER.normalizedEntityKey))
        label_text = str(g.value(e, NER.normalizedEntityLabel) or key)
        classes = [str(t)[len(str(NER)):] for t in g.objects(e, RDF.type)
                   if str(t).startswith(str(NER)) and t != NER.NamedEntity]
        cls = classes[0] if classes else "NamedEntity"
        label = CLASS_TO_LABEL.get(cls, "Other")
        surfaces = [str(g.value(m, NER.surfaceForm)) for m in g.objects(e, NER.hasMention)]
        surfaces += EXTRA_SURFACES.get(key, [])
        # find the words actually in the text: the surface as curated, else its core
        # (curated mentions sometimes add a gloss: "zebrafish (Danio rerio)")
        # "olfactory bulb (OB)" is two mentions to an extractor: the long form and the
        # abbreviation, each emitted on its own (prompts/extractor-ner-*.md).
        split = []
        for sf in surfaces:
            m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*(.*)$", sf)
            if m and not m.group(3):
                split += [m.group(1), m.group(2).rstrip(".")]
            else:
                split.append(sf)
        surfaces = list(dict.fromkeys(x for x in split if x))
        spans: list[tuple[int, int, str]] = []
        for sf in surfaces:
            curly = sf.replace("'", "\u2018", 1).replace("'", "\u2019", 1)
            cands = [sf, curly, re.sub(r"\s*\(.*?\)\s*", " ", sf).strip(), sf.split(" (")[0]]
            for c in dict.fromkeys(x for x in cands if x):
                hits = occurrences(text, c)
                if hits:
                    spans += [(a, b, c) for a, b in hits]
                    break
        spans = sorted(set(spans))
        if not spans:
            missing.append(f"{key}: none of {surfaces} occurs in paper.txt")
            continue
        # one group per distinct surface (long form, abbreviation); all share the key,
        # which is how kg_plan says they are one entity
        forms = list(dict.fromkeys(sf for _, _, sf in spans))
        gids = [f"{text[a:b]}|{label}" for a, b, sf in
                (next(x for x in spans if x[2] == f) for f in forms)]
        gid = gids[0]
        by_node[e] = {"key": key, "id": gid}
        for a, b, sf in spans:
            entities.append({"entity": text[a:b], "label": label, "start": a, "end": b,
                             "sentence": sentence_at(text, a, b), "paper_location": section_at(marks, a),
                             "source_model": "llm_ner:claude"})
        for x in gids:
            plan_entities[x] = {"normalized_key": key, "normalized_label": label_text, "class": cls}
        note = g.value(e, RDFS.comment)
        concept = g.value(e, NER.resolvedToConcept)
        if concept is not None:
            iri = str(g.value(concept, NER.conceptIRI))
            tier = next((TIER[str(p)] for p, o in g.predicate_objects(e) if str(p) in TIER and str(o) == iri),
                        "closeMatch")
            for x in gids:
                curated[x] = {"ontology_id": iri, "ontology_label": str(g.value(concept, NER.preferredLabel) or ""),
                              "tier": tier, "note": str(note) if note else None}
            curated[gid] = {"ontology_id": iri, "ontology_label": str(g.value(concept, NER.preferredLabel) or ""),
                            "tier": tier, "note": str(note) if note else None}

    # paper-stated edges between curated entities
    key_of = {n: v["key"] for n, v in by_node.items()}
    edges = defaultdict(lambda: defaultdict(list))
    for s, p, o in g:
        if s not in key_of or o not in key_of:
            continue
        pid = str(p).rsplit("/", 1)[-1]
        gid = by_node[s]["id"]
        if pid in RELATIONS:
            edges[gid]["relations"].append({"predicate": RELATIONS[pid], "target_key": key_of[o]})
        elif p == SKOS.broader:
            edges[gid]["broader_key"] = key_of[o]
        elif p == SKOS.related:
            edges[gid]["related_keys"].append(key_of[o])
        elif p == RDFS.seeAlso:
            edges[gid]["see_also_keys"].append(key_of[o])
        elif p == PROV.used:
            edges[gid]["uses_keys"].append(key_of[o])
        elif p in (PROV.wasDerivedFrom, PROV.wasInfluencedBy):
            edges[gid]["derived_from_keys"].append(key_of[o])
    for gid, fields in edges.items():
        for k, v in fields.items():
            plan_entities[gid][k] = sorted(v, key=json.dumps) if isinstance(v, list) else v

    causal = []
    for i, rel in enumerate(sorted(g.subjects(RDF.type, NER.CausalRelation), key=str), 1):
        v = g.value(rel, NER.hasCurrentCausalRelationVersion)
        cause, effect = g.value(rel, NER.hasCause), g.value(rel, NER.hasEffect)
        hyp = bool(g.value(v, NER.causalHypothetical).toPython())
        comment = str(g.value(v, RDFS.comment) or "")
        cr = {"id": f"rel-{i}", "cause_key": key_of[cause], "effect_key": key_of[effect],
              "polarity": "positive", "hypothetical": hyp,
              "negated": bool(g.value(v, NER.causalNegated).toPython()),
              "evidence_basis": ["experimental_intervention"] if not hyp else ["observational_unadjusted"],
              "type": "promotes" if not hyp else "contributes_to",
              "modality": "asserted" if not hyp else "probable",
              "evidence": comment}
        est = g.value(v, NER.hasEffectEstimate)
        if est is not None:
            cr["effect_estimate"] = {"measure": str(g.value(est, NER.effectMeasure)),
                                     "value": float(g.value(est, NER.effectValue)),
                                     "p_value": float(g.value(est, NER.pValue)),
                                     "sample_size": int(g.value(est, NER.sampleSize))}
        causal.append(cr)
    plan = {"entities": plan_entities, "causal_relations": causal,
            "chains": [{"id": "chain-1", "label": "odor training -> manifold capacity -> discrimination behaviour",
                        "relation_ids": [c["id"] for c in causal]}]}

    result = {"source_metadata": {"paper_title": str(next(g.objects(None, NER.title))),
                                  "doi": str(next(g.objects(None, NER.doi))),
                                  "source_path": "paper.txt", "license": "CC BY 4.0",
                                  # the PDF prints a placeholder ("Published online: xx xx xxxx"), so
                                  # only the year the curated citation states is recorded — no day
                                  **({"year": m.group(1)} if (m := re.search(r"Nature Neuroscience \((\d{4})\)",
                                      (HERE / "hu2026_curated.ttl").read_text())) else {})},
              "task_type": "ner", "entities": entities, "key_terms": []}
    (HERE / "paper_final.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    (HERE / "kg_plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    (HERE / "curated_mappings.json").write_text(json.dumps(curated, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(plan_entities)} entities, {len(entities)} mentions, {len(curated)} curated mappings, "
          f"{sum(len(v.get('relations', [])) for v in plan_entities.values())} RO edges, {len(causal)} causal",
          file=sys.stderr)
    for m in missing:
        print(f"  skipped {m}", file=sys.stderr)
    return 0


def curated_fallback(result_path: Path) -> int:
    """Stand-in for the remote end of the cascade (local hybrid -> BioPortal), so the
    example is reproducible offline: an item the trusted ontologies left unmapped
    takes the curated file's OLS4-verified mapping, and says that is where it came from."""
    result = json.loads(result_path.read_text())
    curated = json.loads((HERE / "curated_mappings.json").read_text())
    n = 0
    for it in result.get("entities") or []:
        ref = curated.get(f"{it['entity']}|{it.get('label')}")
        if ref and it.get("concept_mapping_provenance") != "tool":
            prefix = ref["ontology_id"].rsplit("/", 1)[-1].split("_")[0]
            it.update({"ontology_id": ref["ontology_id"], "ontology_label": ref["ontology_label"],
                       "ontology": prefix, "concept_mapping_provenance": "tool",
                       "alignment_method": "direct_tool_call",
                       "mapping_source": "ols4 (curated, verified 2026-09-24; hu2026_curated.ttl)"})
            n += 1
    alignment = result.setdefault("stats", {}).setdefault("alignment", {})
    alignment.setdefault("mapped_by_source", {})["ols4_curated"] = n
    alignment["mapper_used"] = "trusted+ols4_curated"
    result_path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(f"curated OLS4 fallback mapped {n} mention(s) the trusted ontologies did not", file=sys.stderr)
    return 0


def write_reviews(result_path: Path, out_dir: Path) -> int:
    """Reference reviews against the MAPPED result (run after `concept_mapping map`)."""
    result = json.loads(result_path.read_text())
    curated = json.loads((HERE / "curated_mappings.json").read_text())
    plan = json.loads((HERE / "kg_plan.json").read_text())
    sys.path.insert(0, str(HERE.parent.parent / "scripts"))
    from group_by_entity import mention_groups
    out_dir.mkdir(parents=True, exist_ok=True)
    base = {"model": REF, "mode": "deterministic"}
    labeling, mapping = [], []
    for grp in mention_groups(result):
        gid = grp["id"]
        labeling.append({"id": gid, "verdict": "pass", "confidence": 0.9,
                         "reason": "label agrees with the curated class"})
        it = next((i for i in grp["items"] if i.get("concept_mapping_provenance") == "tool"), None)
        ref = curated.get(gid)
        if it is None:
            continue
        if ref is None:
            mapping.append({"id": gid, "verdict": "flag", "confidence": 0.5,
                            "reason": f"{it['ontology_id']} not in the curated file; tier left to closeMatch",
                            "suggestion": {"tier": "closeMatch"}})
        elif ref["ontology_id"] == it["ontology_id"]:
            if ref["tier"] == "exactMatch":
                mapping.append({"id": gid, "verdict": "pass", "confidence": 0.95,
                                "reason": f"same concept as curated ({ref['ontology_label']})"})
            else:
                mapping.append({"id": gid, "verdict": "flag", "confidence": 0.85,
                                "reason": f"curated as {ref['tier']}: {ref.get('note') or ref['ontology_label']}"[:300],
                                "suggestion": {"tier": ref["tier"]}})
        else:
            mapping.append({"id": gid, "verdict": "fail", "confidence": 0.8,
                            "reason": f"curated mapping is {ref['ontology_id']} ({ref['ontology_label']}), "
                                      f"not {it['ontology_id']}"})
    keys = []
    for gid, e in plan["entities"].items():
        k = e["normalized_key"]
        if k in ("neuron",):
            keys.append({"id": gid, "verdict": "fail", "confidence": 0.9,
                         "reason": "bare 'neuron' is on the guardrail list; the paper means pDp neurons",
                         "suggestion": {"normalized_key": "neuron_pdp"}})
        else:
            keys.append({"id": gid, "verdict": "pass", "confidence": 0.9, "reason": "curated key"})
    claims = []
    for gid, e in plan["entities"].items():
        for r in e.get("relations") or []:
            claims.append({"id": f"{e['normalized_key']}--{r['predicate']}--{r['target_key']}",
                           "verdict": "pass", "confidence": 0.85, "reason": "curated paper-stated edge"})
        if e.get("broader_key"):
            claims.append({"id": f"{e['normalized_key']}--broader--{e['broader_key']}",
                           "verdict": "pass", "confidence": 0.85, "reason": "curated in-paper hierarchy"})
    for c in plan["causal_relations"]:
        claims.append({"id": c["id"], "verdict": "pass", "confidence": 0.85, "reason": c["evidence"][:190]})
    for ch in plan["chains"]:
        claims.append({"id": ch["id"], "verdict": "pass", "confidence": 0.8, "reason": "order follows the paper"})
    claims = list({c["id"]: c for c in claims}.values())
    for judge, items in (("labeling", labeling), ("mapping", mapping), ("kg-keys", keys), ("claims", claims)):
        (out_dir / f"{judge}-001.json").write_text(json.dumps({"judge": judge, **base, "items": items},
                                                              indent=1, ensure_ascii=False) + "\n")
    print(f"reference reviews: labeling {len(labeling)}, mapping {len(mapping)}, kg-keys {len(keys)}, "
          f"claims {len(claims)} -> {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fallback":
        raise SystemExit(curated_fallback(Path(sys.argv[2])))
    if len(sys.argv) > 1 and sys.argv[1] == "reviews":
        raise SystemExit(write_reviews(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(main())
