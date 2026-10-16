# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib>=7", "pyshacl>=0.26"]
# ///
"""Validate a knowledge graph with SHACL, and prove the checks can fail.

Runs the skill's core shapes (assets/core_shapes.ttl) plus any extra shape files — normally
SHACL generated from the PyNIDM LinkML schema with `linkml gen-shacl` (see SKILL.md) — over
the data files given.

Apply the core shapes to the layers you author (analysis provenance, identity links, cohorts,
claims). Third-party NIDM written by other tools does not follow them (sessions without agents,
tool outputs without prov:wasDerivedFrom); validate those layers with the LinkML-generated
shapes instead.

--mutation-test then deletes every prov:wasDerivedFrom edge from outputs of activities that
used inputs and validates again. It only counts as caught when the unmutated graph conforms
and the mutated one does not; otherwise the checks are not shown to detect anything.

Exit code: 0 if the data conforms (and, with --mutation-test, the mutation is caught), else 1.

Usage:
  uv run scripts/validate_graph.py --data graph.ttl --data provenance.ttl \
      --shapes nidm_linkml_shapes.ttl --mutation-test --report validation.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import Graph, Namespace

PROV = Namespace("http://www.w3.org/ns/prov#")
NKG = Namespace("urn:neuroimaging-kg:")
SH = Namespace("http://www.w3.org/ns/shacl#")
CORE_SHAPES = Path(__file__).resolve().parents[1] / "assets" / "core_shapes.ttl"


def _shapes(extra: list[Graph] | None) -> Graph:
    s = Graph().parse(CORE_SHAPES)
    for g in extra or []:
        s += g
    return s


def validate(data: Graph, extra_shapes: list[Graph] | None = None) -> tuple[bool, int, list[str]]:
    """Return (conforms, number of results, messages)."""
    ok, report, _ = shacl_validate(data, shacl_graph=_shapes(extra_shapes), inference="none",
                                   allow_warnings=True, advanced=True)
    msgs = sorted({str(m) for m in report.objects(None, SH.resultMessage)})
    n = len(list(report.subjects(None, SH.ValidationResult)))
    return bool(ok), n, msgs


def mutation_test(data: Graph, extra_shapes: list[Graph] | None = None) -> dict:
    """Remove derivation edges from derived outputs; the result must not conform."""
    base_ok, base_n, _ = validate(data, extra_shapes)
    mutated = Graph()
    mutated += data
    removed = 0
    rows = data.query("""PREFIX prov: <http://www.w3.org/ns/prov#>
        SELECT DISTINCT ?out ?src WHERE { ?out prov:wasGeneratedBy ?act ; prov:wasDerivedFrom ?src . ?act prov:used ?x }""")
    for r in rows:
        mutated.remove((r.out, PROV.wasDerivedFrom, r.src))
        removed += 1
    mut_ok, mut_n, msgs = validate(mutated, extra_shapes)
    return {"baseline_conforms": base_ok, "baseline_results": base_n, "removed_triples": removed,
            "mutated_conforms": mut_ok, "mutated_results": mut_n, "mutated_messages": msgs[:5],
            "caught": base_ok and removed > 0 and not mut_ok}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", action="append", required=True, help="Turtle data file (repeatable)")
    ap.add_argument("--shapes", action="append", default=[], help="extra SHACL shapes file (repeatable)")
    ap.add_argument("--mutation-test", action="store_true")
    ap.add_argument("--report", help="JSON report path")
    ap.add_argument("--warn-only", action="store_true",
                    help="report but exit 0 (for third-party layers whose violations are findings, not build errors)")
    a = ap.parse_args()
    data = Graph()
    for p in a.data:
        data.parse(p)
    extra = [Graph().parse(p) for p in a.shapes]
    ok, n, msgs = validate(data, extra)
    out = {"data": a.data, "shapes": [str(CORE_SHAPES)] + a.shapes, "triples": len(data),
           "conforms": ok, "results": n, "messages": msgs[:20]}
    if a.mutation_test:
        out["mutation_test"] = mutation_test(data, extra)
    if a.report:
        Path(a.report).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k not in ("data", "shapes")}, indent=1))
    failed = not ok or (a.mutation_test and not out["mutation_test"]["caught"])
    sys.exit(1 if failed and not a.warn_only else 0)


if __name__ == "__main__":
    main()
