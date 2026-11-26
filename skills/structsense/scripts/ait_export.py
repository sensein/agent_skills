"""AIT mapping mode -> Turtle, for loading into the knowledge graph.

The AIT mode's deliverable is seven CSV tables (`prompts/extractor-cell-type-ait-
mapping.md`). This turns a validated output directory into one Turtle file built
on the same Named Entity Ontology as the NER mode, through the same converter
(`scripts/json_to_ttl.py`) and gated by the same validator (`scripts/validate_ttl.py`).
Nothing is written by hand, and nothing reaches the graph that the tables'
own checks held back.

What goes in:

* the paper, its text and run, exactly as `json_to_ttl` writes them for NER;
* one `ner:NamedEntity` per entity (`CellType`, `Gene`, `Species`, `BrainRegion`)
  with one `ner:EntityMention` per strictly verified evidence sentence (`exact` or
  `exact_offset_corrected`), anchored on the label (or an alias) inside that
  sentence. Cell types are paper-local (a paper's "cluster 14" is not another
  paper's); papers meet at shared concept and taxonomy nodes;
* each mention's location, from the layout lookup Pass 2a wrote: `ner:inSection`,
  `ner:onPage` (physical PDF page), `ner:inDocumentPart` (a `ner:Figure` or
  `ner:Table` labelled `Fig. 3` / `Table 2`) and `ner:evidenceLocatorTypeRaw`;
* tool-backed concept mappings from `concept_mappings.csv` (review loop), as for NER;
* each current AIT mapping edge as a `ner:TaxonomyMappingAssertion`: entity ->
  `ner:TaxonomyNode` (shared across papers, so the reverse lookup "which papers
  map to this AIT node" is one hop), the SKOS relation, who mapped it, its basis
  and evidence text, and the verified mentions that support it.

What stays out (and is listed in the report): entities flagged
`unverified_evidence`, `parse_artifact`, `generic_phrase` or `duplicate_merged`;
entities with no strictly verified evidence sentence containing their label;
superseded mapping rows; `skos_relation = none` rows (a recorded failure, not an
edge). Assays, entity cards and the gene diff stay in the CSV tables.

    python -m scripts.ait_export out/ --text paper.txt --source paper.pdf   # -> out/<paper_id>.ttl, validated
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))  # json_to_ttl and validate_ttl import their siblings by bare name

from scripts import ait_tables as tables  # noqa: E402
from scripts import ait_taxonomy  # noqa: E402

VARIANT = "ait:cell-type-mapping"
ENTITY_LABEL = {"cell_type": "CellType", "gene": "Gene", "species": "Species", "brain_region": "BrainRegion"}
SKIP_FLAGS = tables.HELD_BACK_FLAGS + ("generic_phrase", "duplicate_merged")
SKOS_RELATION = {"skos:exactMatch": "exact", "skos:closeMatch": "close", "skos:broadMatch": "broader",
                 "skos:narrowMatch": "narrower", "skos:relatedMatch": "related"}
CONCEPT_TABLE = "concept_mappings.csv"
PART_CLASS = {"figure_caption": "Figure", "table": "Table"}


def _int(v: str) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pages(value: str) -> List[int]:
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", value or "")
    if not m:
        return []
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return [p for p in range(a, b + 1) if p >= 1]


def _aligned(row: Dict[str, str], col: str, n: int) -> List[str]:
    vals = tables.split_multi(row.get(col, ""))
    return vals + [""] * (n - len(vals))


_SEP_CLASS = r"[\s\-/\u2010-\u2015\u2212]+"


def find_surface(text: str, start: int, end: int, surfaces: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Raw [start, end) of the first surface found inside text[start:end]; exact case
    first, then case-insensitive (a label capitalised at the start of a sentence), then
    with space, hyphen and slash interchangeable ("Sst Chodl" finds "Sst-Chodl"). The
    span returned is the paper's own characters, so the mention's surface form is what
    the paper printed."""
    span = text[start:end]
    for surf in surfaces:
        i = span.find(surf)
        if i >= 0:
            return start + i, start + i + len(surf)
    low = span.lower()
    for surf in surfaces:
        i = low.find(surf.lower())
        if i >= 0 and span[i:i + len(surf)].lower() == surf.lower():
            return start + i, start + i + len(surf)
    for surf in surfaces:
        tokens = [t for t in re.split(_SEP_CLASS, surf) if t]
        if not tokens:
            continue
        m = re.search(r"(?<![^\W_])" + _SEP_CLASS.join(re.escape(t) for t in tokens) + r"(?![^\W_])",
                      span, re.IGNORECASE)
        if m:
            return start + m.start(), start + m.end()
    return None


def load_concepts(out_dir: Path) -> Dict[str, Dict[str, str]]:
    path = Path(out_dir) / CONCEPT_TABLE
    if not path.exists():
        return {}
    return {r.get("mention_id", ""): r for r in tables.read_table(path)[1]
            if r.get("concept_mapping_provenance") == "tool" and r.get("ontology_id")}


def build_result(data: Dict[str, List[Dict[str, str]]], text: str,
                 concepts: Optional[Dict[str, Dict[str, str]]] = None) -> Tuple[dict, List[dict], dict]:
    """(NER-shaped result for json_to_ttl, anchored evidence refs, skip report)."""
    paper = (data.get("paper.csv") or [{}])[0]
    concepts = concepts or {}
    items: List[dict] = []
    refs: List[dict] = []
    skipped: Dict[str, List[dict]] = {"entities": [], "evidence_without_label": []}
    for e in data.get("entities.csv", []):
        mid = e.get("mention_id", "")
        label = ENTITY_LABEL.get(e.get("entity_type", ""))
        if e.get("extraction_flag") in SKIP_FLAGS or tables.is_quarantined(e):
            skipped["entities"].append({"mention_id": mid, "reason": e.get("extraction_flag") or "quarantined"})
            continue
        if not label:
            skipped["entities"].append({"mention_id": mid, "reason": f"entity_type {e.get('entity_type')!r}"})
            continue
        surfaces = list(dict.fromkeys(s for s in [e.get("label_verbatim", "")] +
                                      tables.split_multi(e.get("aliases", "")) if s))
        sentences = tables.split_multi(e.get("evidence_sentence", ""))
        n = len(sentences)
        statuses, starts, ends = (_aligned(e, c, n) for c in
                                  ("evidence_verified", "evidence_char_start", "evidence_char_end"))
        sections, pages, ltypes, locators = (_aligned(e, c, n) for c in
                                             ("evidence_section", "evidence_page",
                                              "evidence_locator_type", "evidence_locator"))
        concept = concepts.get(mid)
        anchored = 0
        for i in range(n):
            s, t = _int(starts[i]), _int(ends[i])
            if statuses[i] not in tables.VERIFIED or s is None or t is None or not (0 <= s < t <= len(text)):
                continue
            hit = find_surface(text, s, t, surfaces)
            if hit is None:
                skipped["evidence_without_label"].append({"mention_id": mid, "evidence_index": i})
                continue
            a, b = hit
            item = {"entity": text[a:b], "label": label, "sentence": text[s:t], "start": a, "end": b}
            if sections[i] not in ("", "NA"):
                item["paper_location"] = sections[i]
            if e.get("entity_type") == "cell_type":
                # a paper's cell type is its own until a taxonomy or concept links it
                item["referent_id"] = mid
            if concept:
                item.update({"ontology_id": concept["ontology_id"],
                             "ontology_label": concept.get("ontology_label", ""),
                             "ontology": concept.get("ontology", ""),
                             "mapping_source": concept.get("mapping_source", ""),
                             "mapping_tier": concept.get("match_tier") or "exactMatch",
                             "concept_mapping_provenance": "tool"})
            items.append(item)
            refs.append({"mention_id": mid, "evidence_index": i, "start": a, "end": b, "surface": text[a:b],
                         "pages": _pages(pages[i]), "locator_type": ltypes[i], "locator": locators[i]})
            anchored += 1
        if not anchored:
            skipped["entities"].append({"mention_id": mid, "reason": "no strictly verified evidence containing the label"})
    doi = paper.get("doi", "")
    meta = {"paper_title": paper.get("title", ""), "doi": doi, "journal": paper.get("journal", ""),
            "pmid": paper.get("pmid", ""), "publication_date": paper.get("publication_date", "")}
    if not doi:
        meta["source_id"] = paper.get("paper_id", "")
    result = {"task_type": "ait", "source_metadata": {k: v for k, v in meta.items() if v},
              "run_metadata": {"variant": VARIANT, "run_id": paper.get("run_id", "")},
              "entities": items}
    return result, refs, skipped


class AitExtension:
    """Adds locations and taxonomy mappings to the json_to_ttl builder's graph."""

    def __init__(self, data: Dict[str, List[Dict[str, str]]], refs: List[dict], catalog: Optional[Path] = None):
        self.data, self.refs, self.catalog = data, refs, catalog
        self.counts: Dict[str, int] = {}
        self.skipped_mappings: List[dict] = []

    def _count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def __call__(self, b) -> None:
        from rdflib import Literal, URIRef
        from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, XSD
        from json_to_ttl import NER

        by_span: Dict[Tuple[int, int], List] = {}
        for (key, a, z), node in b.mention_nodes.items():
            if isinstance(a, int):
                by_span.setdefault((a, z), []).append(node)
        mentions: Dict[str, List] = {}
        parts: Dict[str, URIRef] = {}
        for ref in self.refs:
            node = next((m for m in by_span.get((ref["start"], ref["end"]), [])
                         if str(b.g.value(m, NER.surfaceForm)) == ref["surface"]), None)
            if node is None:  # dropped by json_to_ttl's non-entity filter
                self._count("evidence_not_emitted")
                continue
            if node not in mentions.setdefault(ref["mention_id"], []):
                mentions[ref["mention_id"]].append(node)
            for p in ref["pages"]:
                b.add(node, NER.onPage, Literal(p, datatype=XSD.positiveInteger))
            if ref["locator_type"] not in ("", "NA"):
                b.add(node, NER.evidenceLocatorTypeRaw, Literal(ref["locator_type"], datatype=XSD.string))
            cls = PART_CLASS.get(ref["locator_type"])
            if cls and ref["locator"] not in ("", "NA"):
                part = parts.get(ref["locator"])
                if part is None:
                    part = b.mint("document_part", f"{b.docv}|{ref['locator']}")
                    b.add(part, RDF.type, NER[cls])
                    b.add(part, NER.documentPartLabel, Literal(ref["locator"], datatype=XSD.string))
                    b.add(part, NER.partOfDocumentVersion, b.docv)
                    b.label(part, ref["locator"])
                    parts[ref["locator"]] = part
                    self._count("document_parts")
                b.add(node, NER.inDocumentPart, part)
            self._count("located_mentions")

        entities = {e.get("mention_id"): e for e in self.data.get("entities.csv", [])}
        taxa: Dict[str, URIRef] = {}
        for m in tables.current_mappings(self.data.get("mappings.csv", [])):
            mid, node_id, skos = m.get("mention_id", ""), m.get("ait_node_id", ""), m.get("skos_relation", "")
            rel = SKOS_RELATION.get(skos)
            if not node_id or rel is None:
                self.skipped_mappings.append({"mapping_id": m.get("mapping_id"), "reason": "no edge (skos_relation none)"})
                continue
            ms = mentions.get(mid)
            if not ms:
                self.skipped_mappings.append({"mapping_id": m.get("mapping_id"),
                                              "reason": "entity not exported (held back or no verified mention)"})
                continue
            ent = b.g.value(ms[0], NER.refersToEntity)
            ait_id = m.get("ait_id") or m.get("ait_taxonomy_used") or "unknown-taxonomy"
            tax = taxa.get(ait_id)
            if tax is None:
                tax = b.mint_global("taxonomy", id=ait_id)
                entry = ait_taxonomy.find(ait_id, self.catalog)
                b.add(tax, RDF.type, NER.CellTypeTaxonomy)
                b.add(tax, NER.taxonomyIdentifier, Literal(ait_id, datatype=XSD.string))
                b.label(tax, (entry or {}).get("title") or m.get("ait_taxonomy_used") or ait_id)
                if entry and entry.get("url"):
                    b.add(tax, RDFS.seeAlso, URIRef(entry["url"]))
                taxa[ait_id] = tax
            tn = b.mint_global("taxonomy_node", taxonomy=ait_id, node=node_id)
            b.add(tn, RDF.type, NER.TaxonomyNode)
            b.add(tn, NER.taxonomyNodeIdentifier, Literal(node_id, datatype=XSD.string))
            b.add(tn, NER.inTaxonomy, tax)
            b.label(tn, m.get("ait_cell_type_label") or node_id)
            if m.get("ait_hierarchy_level"):
                b.add(tn, NER.taxonomyLevel, Literal(m["ait_hierarchy_level"], datatype=XSD.string))

            a = b.mint("taxonomy_mapping", m.get("mapping_id") or f"{mid}|{node_id}")
            b.add(a, RDF.type, NER.TaxonomyMappingAssertion)
            b.add(a, NER.mappingSubject, ent)
            b.add(a, NER.mappingTarget, tn)
            b.add(a, NER.taxonomyMappingRelation, NER[f"mapping-relation/{rel}"])
            b.add(a, NER.mappedByRaw, Literal(m.get("mapped_by") or "llm", datatype=XSD.string))
            for basis in tables.split_multi(m.get("basis_for_match", "")):
                b.add(a, NER.mappingBasisRaw, Literal(basis, datatype=XSD.string))
            if m.get("mapping_evidence"):
                b.add(a, NER.mappingEvidenceText, Literal(m["mapping_evidence"], datatype=XSD.string))
            if m.get("mapping_id"):
                b.add(a, DCTERMS.identifier, Literal(m["mapping_id"], datatype=XSD.string))
            if m.get("notes"):
                b.add(a, RDFS.comment, Literal(m["notes"]))
            for mention in ms:
                b.add(a, NER.hasTaxonomyMappingEvidence, mention)
            b.add(a, PROV.hadPrimarySource, b.pub)
            b.add(a, PROV.wasGeneratedBy, b.run)
            name = (entities.get(mid) or {}).get("label_verbatim") or mid
            b.label(a, f"{name} {skos} {m.get('ait_cell_type_label') or node_id}")
            self._count("taxonomy_mappings")


def export(out_dir: Path, text: str, *, source: Optional[Path] = None, text_path: Optional[Path] = None,
           catalog: Optional[Path] = None) -> Tuple[str, dict]:
    """(turtle, report). Raises ValueError if the tables do not validate."""
    errors = [i for i in tables.validate(out_dir) if i.level == "error"]
    if errors:
        raise ValueError(f"{len(errors)} validation error(s) in {out_dir}; run "
                         f"`python -m scripts.ait_tables validate {out_dir}` and fix them first:\n"
                         + "\n".join(str(i) for i in errors[:20]))
    from json_to_ttl import result_to_ttl
    from scripts.doc_layout import sha256_text

    data = tables.load_dir(out_dir)
    result, refs, skipped = build_result(data, text, load_concepts(out_dir))
    meta = result["source_metadata"]
    if source is not None:
        meta["source_path"] = str(source)
    if text_path is not None:
        meta["text_path"] = str(text_path)
        meta["ingestion"] = {"text_sha256": sha256_text(text), "chars": len(text)}
    ext = AitExtension(data, refs, catalog)
    run_id = (data.get("paper.csv") or [{}])[0].get("run_id") or None
    ttl, report = result_to_ttl(result, source_path=source or text_path, source_text=text,
                                variant=VARIANT, run_id=run_id, extend=ext)
    report.update({"ait": ext.counts, "skipped_entities": skipped["entities"],
                   "evidence_without_label": skipped["evidence_without_label"],
                   "skipped_mappings": ext.skipped_mappings,
                   "entities_in": len(data.get("entities.csv", [])), "mentions_in": len(refs)})
    return ttl, report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path, help="the AIT output directory (validated tables)")
    ap.add_argument("--text", type=Path, required=True, help="the text file evidence offsets index (paper.txt)")
    ap.add_argument("--source", type=Path, help="the document the text was extracted from (paper.pdf)")
    ap.add_argument("--out", type=Path, help="default: <out_dir>/<paper_id>.ttl")
    ap.add_argument("--report", type=Path, help="default: <out>.report.json")
    ap.add_argument("--no-validate", action="store_true", help="skip the validate_ttl gate (not for handoff)")
    args = ap.parse_args(argv)

    text = args.text.read_text(encoding="utf-8")
    try:
        ttl, report = export(args.out_dir, text, source=args.source, text_path=args.text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    paper_id = (tables.load_dir(args.out_dir).get("paper.csv") or [{}])[0].get("paper_id") or "paper"
    out = args.out or args.out_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id) + ".ttl")
    out.write_text(ttl, encoding="utf-8")
    ok = True
    if not args.no_validate:
        from validate_ttl import validate_file
        gate = validate_file(out)
        ok = bool(gate.get("ok"))
        report["validate_ttl"] = {k: gate.get(k) for k in ("ok", "violation_count", "warning_count",
                                                           "violations", "warnings") if k in gate}
    rpath = args.report or out.with_suffix(".report.json")
    rpath.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    c = report.get("counts", {})
    print(f"{out}: {report.get('triples')} triples; entities {c.get('entities', 0)}, mentions {c.get('mentions', 0)}, "
          f"taxonomy mappings {report['ait'].get('taxonomy_mappings', 0)}, "
          f"located mentions {report['ait'].get('located_mentions', 0)}")
    print(f"held out: {len(report['skipped_entities'])} entities, {len(report['skipped_mappings'])} mapping rows "
          f"(see {rpath})")
    if not args.no_validate:
        print(f"validate_ttl: {'ok' if ok else 'FAILED — do not load this file'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
