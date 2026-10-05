#!/usr/bin/env python3
"""Resolve mention stubs in a converted BKR graph onto full resource records.

Vendored from the BrainKB Resource Ontology bundle (bkr-0.5.6,
scripts/resolve_mention_stubs.py); scripts/resource_kg.py runs it after conversion.
Run it again over a merged corpus graph to join a stub in one paper onto the full
record another paper wrote.

extraction_to_bkr.py mints a stub resource for every entry in a record's
``mentions`` list, keyed ``mentioned/<slug>``. When the same resource also has
its own extraction record in the batch, the graph ends up with two nodes for one
resource: a named stub with no metadata, and the full record. This pass rewires
every reference to the stub onto the full record and deletes the stub, so
``bkr:mentions`` becomes a real dependency edge between catalogue entries.

Matching is on the normalised resource name only. A stub whose name matches no
full record is left alone: it is a resource this document names but does not
describe, and deleting it would lose that fact.

    python -m scripts.bkr_stubs in.ttl --out out.ttl
"""
from __future__ import annotations

import argparse
import re
import sys

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import DCTERMS

BKR  = Namespace("https://brainkb.org/resource/")
ADMS = Namespace("http://www.w3.org/ns/adms#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")


PREFIX = re.compile(r"^(geo|github|nemo|rrid|doi|accession)\s*:?\s*", re.I)
SUFFIX = re.compile(r"\s+(r package|package|pipeline|function|tool|software)$", re.I)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\- ]", "", str(s).lower())).strip()


def keys_for(name: str) -> list[str]:
    """Normalised lookup keys for one resource name, most specific first.

    Handles the surface variation a document uses for the same resource:
    parenthetical glosses ("scMOSAIC (single cell multi-omic ...)"), repository
    prefixes ("GEO: GSE344678"), comma tails ("Human Brain Collection Core,
    NIMH"), and library suffixes ("multimode R package").
    """
    out, base = [], str(name)
    cands = {base,
             re.sub(r"\s*\([^)]*\)", "", base),          # drop parenthetical gloss
             base.split(",")[0],                          # drop comma tail
             PREFIX.sub("", base),
             SUFFIX.sub("", re.sub(r"\s*\([^)]*\)", "", base))}
    for c in cands:
        k = norm(SUFFIX.sub("", PREFIX.sub("", c)))
        if k and k not in out:
            out.append(k)
    return out


def resolve(g: Graph, alias: dict[str, str] | None = None) -> tuple[int, int, list[str]]:
    alias = {norm(k): norm(v) for k, v in (alias or {}).items()}
    stubs, full = {}, {}
    for node, ident in g.subject_objects(DCTERMS.identifier):
        name = g.value(node, BKR.resourceName)
        if name is None:
            continue
        if str(ident).startswith("mentioned/"):
            stubs.setdefault(norm(name), []).append(node)
        else:
            for k in keys_for(name):
                full.setdefault(k, node)
            # an identifier value is also a lookup key: documents cite resources
            # by accession or repository URL as often as by name
            # identifier values are a lookup key: documents cite resources by accession or
            # repository URL as often as by name. Read both the ADMS terms the current
            # converter emits and the BKR terms deprecated in 0.5.2, so older graphs resolve too.
            for idprop in (ADMS.identifier, BKR.primaryIdentifier, BKR.alternateIdentifier, BKR.hasIdentifier):
                for idnode in g.objects(node, idprop):
                    for v in list(g.objects(idnode, SKOS.notation)) + list(g.objects(idnode, BKR.identifierValue)):
                        full.setdefault(norm(v), node)
            for urlprop in (DCAT.accessURL, BKR.accessURL, DCAT.landingPage):
                for v in g.objects(node, urlprop):
                    full.setdefault(norm(v), node)

    merged, unmatched = 0, []
    for key, nodes in stubs.items():
        name = str(g.value(nodes[0], BKR.resourceName))
        target = next((full[k] for k in keys_for(name) if k in full), None)
        if target is None and alias.get(key):
            target = full.get(alias[key])
        if target is None:
            unmatched.append(key)
            continue
        for stub in nodes:
            if stub == target:
                continue
            for s, p, o in list(g.triples((None, None, stub))):
                g.remove((s, p, o))
                g.add((s, p, target))
            for s, p, o in list(g.triples((stub, None, None))):
                g.remove((s, p, o))
            merged += 1
    return merged, len(stubs), sorted(unmatched)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--out", required=True)
    ap.add_argument("--alias", help="JSON file mapping a mention name to the resource name it denotes, "
                                    "for variants the generic rules cannot match")
    args = ap.parse_args()

    alias = {}
    if args.alias:
        import json
        alias = json.load(open(args.alias))

    g = Graph()
    g.parse(args.input, format="turtle")
    before = len(g)
    merged, n_stub_names, unmatched = resolve(g, alias)
    g.serialize(destination=args.out, format="turtle")
    print(f"{merged} stub(s) merged onto full records; {len(unmatched)} mentioned resource(s) "
          f"kept as stubs (named but not described in the source); "
          f"{before} -> {len(g)} triples", file=sys.stderr)
    if unmatched:
        print("kept as stubs: " + ", ".join(unmatched), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
