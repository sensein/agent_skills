"""Entity-focused views of a validated extraction graph (not an ingestion graph).

The JSON nests occurrences and evidence under each global entity. The Turtle
projection contains entity nodes, positive entity-to-entity edges and, for graph
viewing, ONE node per distinct surface form of an entity ("Na+ channels") linked to
every sentence it occurs in — instead of one node per occurrence, which is what the
canonical graph keeps (an EntityMention is one span). Audit records stay in the
canonical extraction graph. No ontology hierarchy is invented.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import uuid
from rdflib import Graph, Namespace, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD

NER = Namespace("https://brainkb.org/ner/")
PROV = Namespace("http://www.w3.org/ns/prov#")

def entity_views(graph: Graph) -> tuple[Graph, dict]:
    entities = set(graph.subjects(RDF.type, NER.NamedEntity))
    view = Graph()
    for prefix, ns in graph.namespaces():
        view.bind(prefix, ns)
    rows = []
    def values(node, predicate):
        return sorted(str(x) for x in graph.objects(node, predicate))
    def value(node, predicate):
        vals = values(node, predicate)
        return vals[0] if vals else None
    for entity in sorted(entities, key=str):
        mentions = []
        for mention in sorted(set(graph.objects(entity, NER.hasMention)), key=str):
            version = graph.value(mention, NER.partOfDocumentVersion)
            sentence = graph.value(mention, NER.inSentence)
            a, b = graph.value(mention, NER.documentStartOffset), graph.value(mention, NER.documentEndOffset)
            mentions.append({"iri": str(mention), "surface": value(mention, NER.surfaceForm),
                             "start": int(a) if a is not None else None, "end": int(b) if b is not None else None,
                             "source": str(graph.value(version, NER.versionOfDocument)) if version else None,
                             "document_version": str(version) if version else None,
                             "sentence": value(sentence, NER.sentenceText) if sentence else None})
        mentions.sort(key=lambda m: (m["source"] or "", m["start"] if m["start"] is not None else -1, m["iri"]))
        assertions = []
        for assertion in sorted(set(graph.subjects(NER.assertionSubject, entity)), key=str):
            neg = graph.value(assertion, NER.assertionNegated)
            assertions.append({"iri": str(assertion), "predicate": value(assertion, NER.assertionPredicate),
                               "target": value(assertion, NER.assertionObject), "evidence": value(assertion, NER.evidenceText),
                               "evidence_mentions": values(assertion, NER.hasEvidenceMention),
                               "source": value(assertion, PROV.hadPrimarySource),
                               "negated": bool(neg.toPython()) if neg is not None else False,
                               "modality": value(assertion, NER.assertionModality),
                               "context": values(assertion, NER.assertionContext)})
        mappings = [{"relation": str(p), "iri": str(o)} for p in
                    (SKOS.exactMatch, SKOS.closeMatch, SKOS.broadMatch, SKOS.narrowMatch, SKOS.relatedMatch)
                    for o in graph.objects(entity, p)]
        for p in (RDF.type, RDFS.label, NER.normalizedEntityKey, NER.normalizedEntityLabel):
            for o in graph.objects(entity, p): view.add((entity, p, o))
        # External mappings and provenance are literal details in this projection,
        # so generic viewers do not draw them as additional entity nodes.
        view.add((entity, NER.mentionCount, Literal(len(mentions), datatype=XSD.nonNegativeInteger)))
        for p, o in graph.predicate_objects(entity):
            if o in entities and p != RDF.type: view.add((entity, p, o))
        # one node per surface form, linked to all its sentences (view only)
        by_surface: dict = {}
        for mention in graph.objects(entity, NER.hasMention):
            surf = " ".join(str(graph.value(mention, RDFS.label) or graph.value(mention, NER.surfaceForm) or "").split())
            by_surface.setdefault(surf, []).append(mention)
        for surf, ms in sorted(by_surface.items()):
            node = URIRef(f"{str(entity).rsplit('/', 1)[0]}/{uuid.uuid5(uuid.NAMESPACE_URL, f'surface|{entity}|{surf}')}")
            view.add((node, RDF.type, NER.SurfaceFormView))
            view.add((node, RDFS.label, Literal(surf)))
            view.add((node, NER.refersToEntity, entity))
            view.add((node, NER.mentionCount, Literal(len(ms), datatype=XSD.nonNegativeInteger)))
            for m in ms:
                sent = graph.value(m, NER.inSentence)
                if sent is not None:
                    view.add((node, NER.inSentence, sent))
                    for p in (RDFS.label, NER.sentenceText):
                        for o in graph.objects(sent, p):
                            view.add((sent, p, o))
        rows.append({"iri": str(entity), "key": value(entity, NER.normalizedEntityKey),
                     "label": value(entity, NER.normalizedEntityLabel), "types": values(entity, RDF.type),
                     "sources": values(entity, PROV.hadPrimarySource), "mention_count": len(mentions),
                     "mentions": mentions, "mappings": sorted(mappings, key=lambda x: (x['iri'], x['relation'])),
                     "relations": assertions,
                     "direct_relations": [{"predicate": str(p), "target": str(o)}
                                          for p, o in graph.predicate_objects(entity) if o in entities]})
    return view, {"view": "entities", "entities": rows, "entity_count": len(rows),
                  "mention_count": sum(r["mention_count"] for r in rows)}

def write_entity_views(ttl: str, output: Path) -> dict:
    graph = Graph().parse(data=ttl, format="turtle")
    view, data = entity_views(graph)
    tpath, jpath = output.with_suffix(".entities.ttl"), output.with_suffix(".entities.json")
    view.serialize(destination=str(tpath), format="turtle")
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"entity_view": str(tpath), "entity_index": str(jpath)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ttl", type=Path)
    parser.add_argument("--out", type=Path, help="Output basename; defaults to input")
    args = parser.parse_args()
    print(json.dumps(write_entity_views(args.ttl.read_text(), args.out or args.ttl)))
