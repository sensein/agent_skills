# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7"]
# ///
"""Join a local participant table to participants in a NIDM graph, as reviewable links.

Matches a column of a local CSV/TSV against an identifier property on agents in an RDF graph
(default ndar:src_subject_id, which PyNIDM writes), after a stated normalisation (`int` strips
leading zeros, `strip` trims whitespace, `none` compares verbatim). Each match becomes an
nkg:IdentityLink with reviewStatus "proposed", the rule, and the activity that made it —
never owl:sameAs, because a shared ID string is evidence for identity, not proof.

Optional --check col=PREDICATE_IRI compares a local column with a value the graph holds for
the same participant (NIDM pattern: acquisition object -> generating acquisition -> qualified
association -> agent). Disagreements are reported as conflicts for a reviewer.

Prints and writes a report: matched, only_local, only_external, duplicates, conflicts, and
checks_compared (how many links each check could actually compare: 0 conflicts with 0 comparisons
means the check found nothing to compare, not that the sources agree).

Usage:
  uv run scripts/join_identity.py --local phenotype.csv --id-col SUB_ID --graph site_nidm.ttl \
      --normalize int --check AGE_AT_SCAN=http://uri.interlex.org/ilx_0100400 \
      --reviewer "Your Name" --out links.ttl --report join_report.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rdflib import RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef

PROV = Namespace("http://www.w3.org/ns/prov#")
NKG = Namespace("urn:neuroimaging-kg:")
SRC_SUBJECT_ID = "https://ndar.nih.gov/api/datadictionary/v2/dataelement/src_subject_id"
DEFAULT_BASE = "urn:neuroimaging-kg:study:"


def normalise(value: str, how: str) -> str:
    v = str(value).strip()
    if how == "int":
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    if how == "strip":
        return v
    return str(value)


def _external_ids(graph: Graph, id_property: str) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for agent, ident in graph.subject_objects(URIRef(id_property)):
        out[str(ident)].append(agent)
    return out


def _graph_values(graph: Graph, agent, target: str) -> set[str]:
    """Values the graph holds for `agent`.

    `target` is a predicate IRI, `about:<concept IRI>` (data elements with that nidm:isAbout, the
    way PyNIDM and ReproNim files encode variables under per-file element IRIs), or `var:<name>`
    (data elements with that nidm:sourceVariable).
    """
    if target.startswith("about:"):
        pred_pattern = "?pred <http://purl.org/nidash/nidm#isAbout> ?target ."
        target_term = URIRef(target[6:])
    elif target.startswith("var:"):
        pred_pattern = "?pred <http://purl.org/nidash/nidm#sourceVariable> ?target ."
        target_term = Literal(target[4:])
    else:
        pred_pattern = "FILTER (?pred = ?target)"
        target_term = URIRef(target)
    q = f"""
    PREFIX prov: <http://www.w3.org/ns/prov#>
    SELECT ?v WHERE {{
      {{ ?ao prov:wasGeneratedBy ?acq . ?acq prov:qualifiedAssociation ?as . ?as prov:agent ?agent . ?ao ?pred ?v }}
      UNION {{ ?agent ?pred ?v }}
      {pred_pattern}
    }}"""
    rows = graph.query(q, initBindings={"agent": agent, "target": target_term})
    return {str(r.v) for r in rows}


def join(rows: list[dict], id_col: str, graph: Graph, id_property: str = SRC_SUBJECT_ID, normalize: str = "int",
         checks: dict[str, str] | None = None, reviewer: str | None = None, base: str = DEFAULT_BASE,
         numeric_tolerance: float = 0.0) -> tuple[Graph, dict]:
    """Return (links graph, report)."""
    ext_raw = _external_ids(graph, id_property)
    ext: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for raw, agents in ext_raw.items():
        for a in agents:
            ext[normalise(raw, normalize)].append((raw, a))
    local: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        local[normalise(r[id_col], normalize)].append(r)

    g = Graph()
    g.bind("prov", PROV)
    g.bind("nkg", NKG)
    act = URIRef(f"{base}activity/identity-join")
    g.add((act, RDF.type, PROV.Activity))
    g.add((act, RDFS.label, Literal(f"Identity join {id_col} -> <{id_property}> (normalise: {normalize})")))
    g.add((act, PROV.endedAtTime, Literal(datetime.now(timezone.utc).isoformat(timespec="seconds"), datatype=XSD.dateTime)))
    rule = f"normalise_{normalize}(local.{id_col}) == normalise_{normalize}(<{id_property}>)"
    g.add((act, NKG.rule, Literal(rule)))
    if reviewer:
        who = URIRef(f"{base}agent/{reviewer.replace(' ', '-')}")
        g.add((who, RDF.type, PROV.Person))
        g.add((who, RDFS.label, Literal(reviewer)))
        g.add((act, PROV.wasAssociatedWith, who))

    report = {"rule": rule, "local_rows": len(rows), "external_ids": sum(len(v) for v in ext.values()),
              "matched": 0, "only_local": [], "only_external": [], "duplicates": [], "conflicts": [],
              "checks_compared": {col: 0 for col in (checks or {})}}
    for key in sorted(set(local) | set(ext), key=lambda k: (len(k), k)):
        lrows, erows = local.get(key, []), ext.get(key, [])
        if not erows:
            report["only_local"].extend(r[id_col] for r in lrows)
            continue
        if not lrows:
            report["only_external"].extend(raw for raw, _ in erows)
            continue
        if len(lrows) > 1 or len(erows) > 1:
            report["duplicates"].append({"key": key, "local": len(lrows), "external": len(erows)})
            continue
        row, (raw, agent) = lrows[0], erows[0]
        report["matched"] += 1
        link = URIRef(f"{base}identity/{key}")
        g.add((link, RDF.type, NKG.IdentityLink))
        g.add((link, NKG.localId, Literal(row[id_col])))
        g.add((link, NKG.externalSubject, agent))
        g.add((link, NKG.externalId, Literal(raw)))
        g.add((link, NKG.reviewStatus, Literal("proposed")))
        g.add((link, PROV.wasGeneratedBy, act))
        for col, pred in (checks or {}).items():
            graph_vals = _graph_values(graph, agent, pred)
            if not graph_vals or row.get(col) in (None, ""):
                continue
            report["checks_compared"][col] += 1
            if not any(_same(row[col], gv, numeric_tolerance) for gv in graph_vals):
                c = BNode()
                g.add((link, NKG.conflict, c))
                g.add((c, RDFS.label, Literal(col)))
                g.add((c, NKG.localValue, Literal(row[col])))
                g.add((c, NKG.externalValue, Literal(sorted(graph_vals)[0])))
                report["conflicts"].append({"local_id": row[id_col], "external_id": raw, "field": col,
                                            "local": row[col], "external": sorted(graph_vals)})
    return g, report


def _same(a: str, b: str, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except ValueError:
        return str(a).strip().lower() == str(b).strip().lower()


def read_table(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        dialect = "excel-tab" if path.endswith((".tsv", ".tab")) else "excel"
        return list(csv.DictReader(fh, dialect=dialect))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--local", required=True, help="CSV/TSV with one row per participant")
    ap.add_argument("--id-col", required=True)
    ap.add_argument("--graph", action="append", required=True, help="Turtle file(s) holding the external participants")
    ap.add_argument("--id-property", default=SRC_SUBJECT_ID)
    ap.add_argument("--normalize", choices=["int", "strip", "none"], default="int")
    ap.add_argument("--check", action="append", default=[], help="local_col=PREDICATE_IRI (repeatable)")
    ap.add_argument("--tolerance", type=float, default=0.0, help="numeric tolerance for --check")
    ap.add_argument("--reviewer")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", required=True, help="Turtle file for the identity links")
    ap.add_argument("--report", help="JSON report path")
    a = ap.parse_args()
    graph = Graph()
    for gpath in a.graph:
        graph.parse(gpath)
    checks = dict(c.split("=", 1) for c in a.check)
    links, report = join(read_table(a.local), a.id_col, graph, a.id_property, a.normalize, checks, a.reviewer,
                         a.base, a.tolerance)
    links.serialize(a.out, format="turtle")
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=1))
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in report.items() if k != "rule"}
    for col, n in report["checks_compared"].items():
        if n == 0:
            print(f"WARNING: check {col!r} compared nothing (no values found in the graph)", file=sys.stderr)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
