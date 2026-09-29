"""Refine a generic NER class from the ontology term an entity is mapped to.

The cns-cells extractor labels every cell `CellType`; the Named Entity Ontology has
finer classes (Neuron, Interneuron, Astrocyte, Microglia, ...). An entity mapped to a
CL term below CL:0000540 *neuron* IS a neuron, so it can be typed `ner:Neuron`
deterministically, without a model. This module builds, once, a table from every CL
class to the anchor classes above it (ttl_config.json `class_from_concept.anchors`),
from the subClassOf edges of the trusted cl.owl, and answers lookups from it.

    python -m scripts.class_anchors build      # writes the table (a few seconds)
    python -m scripts.class_anchors show CL:0000129
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

SKILL = Path(__file__).resolve().parent.parent
TTL_CONFIG = SKILL / "default_ontology" / "ttl_config.json"
OBO = "http://purl.obolibrary.org/obo/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
OWL_NS = "http://www.w3.org/2002/07/owl#"


def config(ttl_config: Path = TTL_CONFIG) -> dict:
    cfg = json.loads(Path(ttl_config).read_text()).get("class_from_concept") or {}
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def _iri(curie_or_iri: str) -> str:
    if curie_or_iri.startswith("http"):
        return curie_or_iri
    p, local = curie_or_iri.split(":", 1)
    return f"{OBO}{p}_{local}"


def build(ttl_config: Path = TTL_CONFIG) -> Path:
    cfg = config(ttl_config)
    src = SKILL / cfg.get("ontology_file", "trusted_ontologes/cl.owl")
    out = SKILL / cfg.get("table", "trusted_ontologes/lexicon/class_anchors.json")
    anchors = {_iri(k): v for k, v in (cfg.get("anchors") or {}).items()}
    parents: dict[str, set] = defaultdict(set)
    about_tag, class_tag, sub_tag = f"{{{RDF_NS}}}about", f"{{{OWL_NS}}}Class", f"{{{RDFS_NS}}}subClassOf"
    res_tag = f"{{{RDF_NS}}}resource"
    for _ev, el in ET.iterparse(str(src), events=("end",)):
        if el.tag != class_tag:
            continue
        child = el.get(about_tag)
        if child and child.startswith(OBO):
            for sub in el.findall(sub_tag):  # named superclasses only (restrictions skipped)
                sup = sub.get(res_tag)
                if sup and sup.startswith(OBO):
                    parents[child].add(sup)
        el.clear()
    table: dict[str, list[str]] = {}
    for node in list(parents) + list(anchors):
        seen, todo, hit = {node}, deque([node]), set()
        while todo:
            n = todo.popleft()
            if n in anchors:
                hit.add(anchors[n])
            for p in parents.get(n, ()):
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        if hit:
            table[node] = sorted(hit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"source": src.name, "anchors": cfg.get("anchors"), "classes": table}) + "\n")
    return out


_TABLE: Optional[dict] = None


def lookup(iri: str, ttl_config: Path = TTL_CONFIG) -> list[str]:
    """NER classes implied by a mapped concept IRI (all anchors above it), [] if none.
    Builds the table on first use when it is missing."""
    global _TABLE
    if _TABLE is None:
        cfg = config(ttl_config)
        if not cfg.get("anchors"):
            _TABLE = {}
            return []
        path = SKILL / cfg.get("table", "trusted_ontologes/lexicon/class_anchors.json")
        if not path.is_file():
            src = SKILL / cfg.get("ontology_file", "trusted_ontologes/cl.owl")
            if not src.is_file():
                _TABLE = {}
                return []
            build(ttl_config)
        _TABLE = json.loads(path.read_text()).get("classes") or {}
    return list(_TABLE.get(iri) or [])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    s = sub.add_parser("show")
    s.add_argument("term")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        out = build()
        n = len(json.loads(out.read_text())["classes"])
        print(f"wrote {out}: {n} classes under an anchor")
    else:
        print(lookup(_iri(args.term)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
