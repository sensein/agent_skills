# /// script
# requires-python = ">=3.11"
# dependencies = ["pyoxigraph>=0.4"]
# ///
"""Load graphs into Oxigraph as named graphs, run competency queries, compare with gold.

Each source keeps its own named graph (GRAPH <name> { ... } in queries), so provenance of a
triple is never lost when graphs are merged. Queries are the .rq files of a directory; the
query name is the file stem. Gold answers are a JSON object {query_name: [row, ...]} where
each row maps variable -> string value; rows are compared as unordered sets.

Gold answers should be computed independently of the graph (for example with pandas over the
source table), otherwise a match only shows the graph agrees with itself.

Usage:
  uv run scripts/query_graph.py --graph urn:g:phenotype=phenotype.ttl \
      --graph urn:g:simple2=site_nidm.ttl --queries queries/ --gold gold.json \
      --out answers.json [--store ./oxigraph_store]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyoxigraph as ox

FORMATS = {".ttl": ox.RdfFormat.TURTLE, ".nt": ox.RdfFormat.N_TRIPLES, ".nq": ox.RdfFormat.N_QUADS,
           ".trig": ox.RdfFormat.TRIG, ".rdf": ox.RdfFormat.RDF_XML, ".jsonld": ox.RdfFormat.JSON_LD}


def load(named: dict[str, str], store_path: str | None = None) -> ox.Store:
    """Load each file into the named graph given by its key."""
    store = ox.Store(store_path) if store_path else ox.Store()
    for name, path in named.items():
        fmt = FORMATS.get(Path(path).suffix.lower(), ox.RdfFormat.TURTLE)
        store.bulk_load(path=str(path), format=fmt, to_graph=ox.NamedNode(name))
    return store


def _term(t) -> str | None:
    if t is None:
        return None
    return t.value


def run_queries(store: ox.Store, query_dir: str | Path, union: bool = False) -> dict[str, list[dict]]:
    """Run every .rq file in `query_dir`; SELECT results as lists of {var: value}.

    union=True makes triple patterns outside GRAPH { } match every named graph (Oxigraph's
    default graph is otherwise empty), which shared definitions such as tool CDE files need.
    """
    out: dict[str, list[dict]] = {}
    for qf in sorted(Path(query_dir).glob("*.rq")):
        res = store.query(qf.read_text(), use_default_graph_as_union=union)
        if isinstance(res, bool):
            out[qf.stem] = [{"ask": str(res).lower()}]
            continue
        vars_ = [v.value for v in res.variables]
        out[qf.stem] = [{v: _term(sol[v]) for v in vars_} for sol in res]
    return out


def _canon(rows: list[dict]) -> list[str]:
    return sorted(json.dumps({k: (None if v is None else str(v)) for k, v in sorted(r.items())}) for r in rows)


def compare(results: dict[str, list[dict]], gold: dict[str, list[dict]]) -> dict[str, dict]:
    """Unordered row-set comparison per query that has a gold answer."""
    report = {}
    for name, expected in gold.items():
        if name.startswith("_"):  # notes such as "_method"
            continue
        got = results.get(name)
        report[name] = {"match": got is not None and _canon(got) == _canon(expected),
                        "expected": expected, "got": got}
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", action="append", required=True, help="NAME_IRI=path (repeatable)")
    ap.add_argument("--queries", required=True, help="directory of .rq files")
    ap.add_argument("--gold", help="JSON gold answers")
    ap.add_argument("--store", help="on-disk store directory (default: in memory)")
    ap.add_argument("--union", action="store_true", help="patterns outside GRAPH {} match all named graphs")
    ap.add_argument("--out", help="write answers (and comparison) as JSON")
    a = ap.parse_args()
    named = dict(g.split("=", 1) for g in a.graph)
    store = load(named, a.store)
    results = run_queries(store, a.queries, a.union)
    out = {"graphs": named, "answers": results}
    failed = False
    if a.gold:
        cmp = compare(results, json.loads(Path(a.gold).read_text()))
        out["comparison"] = cmp
        for name, c in cmp.items():
            print(f"{'PASS' if c['match'] else 'FAIL'}  {name}")
            failed |= not c["match"]
    for name, rows in results.items():
        print(f"\n# {name}")
        for r in rows[:10]:
            print("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
        if len(rows) > 10:
            print(f"  ... {len(rows)} rows")
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
