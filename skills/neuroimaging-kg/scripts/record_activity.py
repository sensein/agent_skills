# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Record one analysis step as W3C PROV: inputs, outputs, software, parameters, agent.

Each file input or output becomes a prov:Entity identified by its SHA-256 content hash, so
the same file reached by two paths is one node, and a changed file is a different node.
Outputs are prov:wasGeneratedBy the activity and prov:wasDerivedFrom every input.
The activity is associated with the responsible agent (a person, or an AI agent with its
model id) and with each software package at its version.

Appends to an existing Turtle file when --out already exists, so a pipeline can call this
once per step and accumulate one provenance graph.

Usage:
  uv run scripts/record_activity.py --id qc-cohort --label "Select mean FD < 0.2 mm" \
      --used phenotype.csv --generated cohort.csv --software pandas==2.2.3 \
      --param mean_fd_max=0.2 --agent "Analyst A" --out provenance.ttl
"""
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from rdflib import RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef

PROV = Namespace("http://www.w3.org/ns/prov#")
NKG = Namespace("urn:neuroimaging-kg:")
CRYPTO = Namespace("http://id.loc.gov/vocabulary/preservation/cryptographicHashFunctions#")
NFO = Namespace("http://www.semanticdesktop.org/ontologies/2007/03/22/nfo#")
DEFAULT_BASE = "urn:neuroimaging-kg:study:"


def sha256_file(path: str | Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "x"


def _entity(g: Graph, ref: str) -> URIRef:
    """A file path becomes a content-addressed entity; anything else is taken as an IRI."""
    p = Path(ref)
    if p.is_file():
        digest = sha256_file(p)
        node = URIRef(f"urn:sha256:{digest}")
        g.add((node, RDF.type, PROV.Entity))
        g.add((node, CRYPTO.sha256, Literal(digest)))
        g.add((node, NFO.fileName, Literal(p.name)))
        return node
    node = URIRef(ref)
    g.add((node, RDF.type, PROV.Entity))
    return node


def record(activity_id: str, label: str, used: list[str] | None = None, generated: list[str] | None = None,
           software: list[str] | None = None, params: dict[str, str] | None = None, agent: str | None = None,
           agent_kind: str = "person", model: str | None = None, started: str | None = None,
           ended: str | None = None, base: str = DEFAULT_BASE, graph: Graph | None = None) -> Graph:
    """Add one activity and its qualified context to `graph` (a new graph if None)."""
    g = graph if graph is not None else Graph()
    g.bind("prov", PROV)
    g.bind("nkg", NKG)
    g.bind("crypto", CRYPTO)
    act = URIRef(f"{base}activity/{_slug(activity_id)}")
    g.add((act, RDF.type, PROV.Activity))
    g.add((act, RDFS.label, Literal(label)))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    g.add((act, PROV.startedAtTime, Literal(started or now, datatype=XSD.dateTime)))
    g.add((act, PROV.endedAtTime, Literal(ended or now, datatype=XSD.dateTime)))

    inputs = [_entity(g, u) for u in (used or [])]
    for e in inputs:
        g.add((act, PROV.used, e))
    for ref in generated or []:
        out = _entity(g, ref)
        g.add((out, PROV.wasGeneratedBy, act))
        for e in inputs:
            g.add((out, PROV.wasDerivedFrom, e))

    for spec in software or []:
        name, _, version = spec.partition("==")
        sw = URIRef(f"{base}software/{_slug(name)}/{_slug(version or 'unversioned')}")
        g.add((sw, RDF.type, PROV.SoftwareAgent))
        g.add((sw, RDFS.label, Literal(name)))
        if version:
            g.add((sw, NKG.version, Literal(version)))
        g.add((act, PROV.wasAssociatedWith, sw))

    if agent:
        who = URIRef(f"{base}agent/{_slug(agent)}")
        kinds = {"person": PROV.Person, "organization": PROV.Organization, "ai": PROV.SoftwareAgent}
        g.add((who, RDF.type, PROV.Agent))
        g.add((who, RDF.type, kinds.get(agent_kind, PROV.Agent)))
        g.add((who, RDFS.label, Literal(agent)))
        if agent_kind == "ai":
            g.add((who, RDF.type, NKG.AIAgent))
            if model:
                g.add((who, NKG.model, Literal(model)))
        assoc = BNode()
        g.add((act, PROV.qualifiedAssociation, assoc))
        g.add((assoc, RDF.type, PROV.Association))
        g.add((assoc, PROV.agent, who))
        g.add((assoc, PROV.hadRole, NKG.ResponsibleAgent))
        g.add((act, PROV.wasAssociatedWith, who))

    for key, value in (params or {}).items():
        p = BNode()
        g.add((act, NKG.parameter, p))
        g.add((p, RDF.type, NKG.Parameter))
        g.add((p, RDFS.label, Literal(key)))
        g.add((p, NKG.value, Literal(str(value))))
    return g


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", required=True, help="activity id (slugged into the IRI)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--used", action="append", default=[], help="input file path or IRI (repeatable)")
    ap.add_argument("--generated", action="append", default=[], help="output file path or IRI (repeatable)")
    ap.add_argument("--software", action="append", default=[], help="name==version (repeatable)")
    ap.add_argument("--param", action="append", default=[], help="key=value (repeatable)")
    ap.add_argument("--agent", help="responsible agent's name or ORCID")
    ap.add_argument("--agent-kind", choices=["person", "organization", "ai"], default="person")
    ap.add_argument("--model", help="model id when --agent-kind ai")
    ap.add_argument("--base", default=DEFAULT_BASE, help="IRI prefix for minted nodes")
    ap.add_argument("--out", required=True, help="Turtle file; appended to if it exists")
    a = ap.parse_args()
    g = Graph()
    if Path(a.out).exists():
        g.parse(a.out)
    params = dict(p.split("=", 1) for p in a.param)
    record(a.id, a.label, a.used, a.generated, a.software, params, a.agent, a.agent_kind, a.model,
           base=a.base, graph=g)
    g.serialize(a.out, format="turtle")
    print(f"{a.out}: {len(g)} triples")


if __name__ == "__main__":
    main()
