#!/usr/bin/env python3
"""Run full-text-inclusion SPARQL queries against a SynthScholar RDF export.

Usage:
    python query_sparql.py review.ttl --query included-full-text
    python query_sparql.py review.jsonld --query status-all --format csv
    python query_sparql.py review.ttl --sparql ./my_query.rq
    python query_sparql.py --list

Named queries mirror references/sparql_queries.md. Requires `rdflib`.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

PREFIXES = """
PREFIX slr:     <https://w3id.org/slr-ontology/>
PREFIX rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX bibo:    <http://purl.org/ontology/bibo/>
PREFIX fabio:   <http://purl.org/spar/fabio/>
PREFIX prov:    <http://www.w3.org/ns/prov#>
"""

QUERIES: dict[str, str] = {
    "included-full-text": """
        SELECT DISTINCT ?pub ?title ?doi ?pmid ?bytes ?hash ?text WHERE {
          ?review a slr:SystematicReview ; slr:included_sources ?pub .
          ?pub a slr:IncludedSource ; dcterms:title ?title .
          OPTIONAL { ?pub bibo:doi  ?doi }
          OPTIONAL { ?pub bibo:pmid ?pmid }
          ?pub slr:full_text_artifact ?art .
          ?art slr:content_text ?text .
          OPTIONAL { ?art slr:content_size_bytes ?bytes }
          OPTIONAL { ?art slr:content_hash ?hash }
        } ORDER BY ?title
    """,
    "included-full-text-brief": """
        SELECT DISTINCT ?pub ?title ?doi ?pmid ?bytes ?hash WHERE {
          ?review a slr:SystematicReview ; slr:included_sources ?pub .
          ?pub a slr:IncludedSource ; dcterms:title ?title .
          OPTIONAL { ?pub bibo:doi  ?doi }
          OPTIONAL { ?pub bibo:pmid ?pmid }
          ?pub slr:full_text_artifact ?art .
          OPTIONAL { ?art slr:content_size_bytes ?bytes }
          OPTIONAL { ?art slr:content_hash ?hash }
        } ORDER BY ?title
    """,
    "abstract-only": """
        SELECT DISTINCT ?pub ?title ?doi ?abstract WHERE {
          ?review a slr:SystematicReview ; slr:included_sources ?pub .
          ?pub a slr:IncludedSource ; dcterms:title ?title .
          OPTIONAL { ?pub bibo:doi ?doi }
          OPTIONAL { ?pub dcterms:abstract ?abstract }
          FILTER NOT EXISTS { ?pub slr:full_text_artifact ?art }
        } ORDER BY ?title
    """,
    "status-all": """
        SELECT ?title (IF(BOUND(?art), "full_text", "abstract_only") AS ?status)
               ?doi ?bytes WHERE {
          ?review a slr:SystematicReview ; slr:included_sources ?pub .
          ?pub a slr:IncludedSource ; dcterms:title ?title .
          OPTIONAL { ?pub bibo:doi ?doi }
          OPTIONAL { ?pub slr:full_text_artifact ?art .
                     OPTIONAL { ?art slr:content_size_bytes ?bytes } }
        } ORDER BY ?status ?title
    """,
    "counts": """
        SELECT (COUNT(DISTINCT ?pub) AS ?included)
               (COUNT(DISTINCT ?ft)  AS ?full_text)
               (COUNT(DISTINCT ?pub) - COUNT(DISTINCT ?ft) AS ?abstract_only) WHERE {
          ?review a slr:SystematicReview ; slr:included_sources ?pub .
          ?pub a slr:IncludedSource .
          OPTIONAL { ?pub slr:full_text_artifact ?art . BIND(?pub AS ?ft) }
        }
    """,
    "provenance": """
        SELECT ?title ?source ?hash ?bytes ?media WHERE {
          ?pub a slr:IncludedSource ; dcterms:title ?title ;
               slr:full_text_artifact ?art .
          OPTIONAL { ?art slr:full_text_source ?source }
          OPTIONAL { ?art slr:content_hash ?hash }
          OPTIONAL { ?art slr:content_size_bytes ?bytes }
          OPTIONAL { ?art slr:media_type ?media }
        } ORDER BY ?source ?title
    """,
    "source-counts": """
        SELECT ?source (COUNT(?art) AS ?n) WHERE {
          ?pub a slr:IncludedSource ; slr:full_text_artifact ?art .
          OPTIONAL { ?art slr:full_text_source ?src }
          BIND(COALESCE(?src, "(unknown)") AS ?source)
        } GROUP BY ?source ORDER BY DESC(?n)
    """,
    "retrieval-chain": """
        SELECT ?title ?source ?when ?hash WHERE {
          ?pub a slr:IncludedSource ; dcterms:title ?title ;
               slr:full_text_artifact ?art .
          ?art prov:wasGeneratedBy ?act .
          ?art prov:wasDerivedFrom ?used .
          OPTIONAL { ?act slr:tool_name ?source }
          OPTIONAL { ?act prov:endedAtTime ?when }
          OPTIONAL { ?act slr:result_hash ?hash }
        } ORDER BY ?when ?title
    """,
    # ── Retrieval + screening basis (what the review actually read) ──
    "reading-basis": """
        SELECT ?retrieved ?not_retrieved ?on_full_text ?on_abstract_only
               ?included_full ?included_abstract WHERE {
          ?rev a slr:SystematicReview .
          OPTIONAL { ?rev slr:full_text_retrieved       ?retrieved }
          OPTIONAL { ?rev slr:not_retrieved             ?not_retrieved }
          OPTIONAL { ?rev slr:assessed_on_full_text     ?on_full_text }
          OPTIONAL { ?rev slr:assessed_on_abstract_only ?on_abstract_only }
          OPTIONAL { ?rev slr:included_with_full_text   ?included_full }
          OPTIONAL { ?rev slr:included_abstract_only    ?included_abstract }
        }
    """,
    "retrieval-routes": """
        SELECT ?route ?reports WHERE {
          ?rev a slr:SystematicReview ; slr:full_text_route_record ?rec .
          ?rec slr:full_text_source ?route ; slr:report_count ?reports .
        } ORDER BY DESC(?reports)
    """,
    "ezproxy-articles": """
        SELECT ?title ?doi ?when WHERE {
          ?pub a slr:IncludedSource ; slr:full_text_artifact ?art .
          ?art slr:full_text_source "ezproxy_pdf" .
          OPTIONAL { ?pub dcterms:title ?title }
          OPTIONAL { ?pub bibo:doi ?doi }
          OPTIONAL { ?art slr:created_at_time ?when }
        } ORDER BY ?title
    """,
    "exclusion-reasons": """
        SELECT ?stage ?reason ?reports WHERE {
          { ?rev slr:exclusion_reason_title_abstract ?rec .
            BIND("title_abstract" AS ?stage) }
          UNION
          { ?rev slr:exclusion_reason_full_text ?rec .
            BIND("eligibility" AS ?stage) }
          ?rec slr:exclusion_reason ?reason ; slr:report_count ?reports .
        } ORDER BY ?stage DESC(?reports)
    """,
    "screening-decisions": """
        SELECT ?pmid ?stage ?decision ?assessed_on ?route ?reason WHERE {
          ?rev slr:screening_decision_record ?rec .
          ?rec slr:screening_stage ?stage ; slr:decision ?decision ;
               slr:assessed_on ?assessed_on .
          OPTIONAL { ?rec bibo:pmid ?pmid }
          OPTIONAL { ?rec slr:full_text_source ?route }
          OPTIONAL { ?rec slr:decision_rationale ?reason }
        } ORDER BY ?stage ?decision ?pmid
    """,
    "abstract-only-inclusions": """
        SELECT ?pmid ?reason WHERE {
          ?rev slr:screening_decision_record ?rec .
          ?rec slr:screening_stage "full_text" ;
               slr:assessed_on "abstract_only" ;
               slr:decision ?decision .
          FILTER(LCASE(STR(?decision)) = "include")
          OPTIONAL { ?rec bibo:pmid ?pmid }
          OPTIONAL { ?rec slr:decision_rationale ?reason }
        } ORDER BY ?pmid
    """,
    "research-questions": """
        SELECT ?qid ?title (COUNT(?a) AS ?answers) WHERE {
          ?q a slr:ResearchQuestion ; slr:has_answer ?a .
          OPTIONAL { ?q slr:question_id ?qid }
          OPTIONAL { ?q dcterms:title ?title }
        } GROUP BY ?qid ?title ORDER BY ?qid
    """,
    # Every study's answer to one question. Edit the id, or pass your own file
    # with --sparql for a different one.
    "question-answers": """
        SELECT ?pmid ?source_id ?answer WHERE {
          ?q slr:question_id "RQ1.1" ; slr:has_answer ?a .
          ?a slr:answer_text ?answer .
          OPTIONAL { ?a slr:source_id ?source_id }
          OPTIONAL { ?a slr:about_source/bibo:pmid ?pmid }
        } ORDER BY ?source_id
    """,
}

_FORMAT_BY_EXT = {
    ".ttl": "turtle", ".turtle": "turtle", ".nt": "nt",
    ".jsonld": "json-ld", ".json": "json-ld", ".rdf": "xml", ".xml": "xml",
}


def _guess_format(path: str) -> str:
    for ext, fmt in _FORMAT_BY_EXT.items():
        if path.lower().endswith(ext):
            return fmt
    return "turtle"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("graph", nargs="?", help="RDF export file (.ttl/.jsonld/...)")
    ap.add_argument("--query", choices=sorted(QUERIES), help="named query to run")
    ap.add_argument("--sparql", help="path to a .rq file OR an inline SPARQL string")
    ap.add_argument("--format", choices=["table", "csv", "json"], default="table")
    ap.add_argument("--list", action="store_true", help="list named queries and exit")
    args = ap.parse_args()

    if args.list:
        for name in sorted(QUERIES):
            print(name)
        return 0
    if not args.graph:
        ap.error("graph file is required (unless --list)")
    if not args.query and not args.sparql:
        ap.error("provide --query NAME or --sparql FILE|STRING")

    try:
        from rdflib import Graph
    except ImportError:
        print("rdflib is required: pip install rdflib", file=sys.stderr)
        return 2

    if args.sparql:
        import os
        body = (open(args.sparql).read() if os.path.isfile(args.sparql)
                else args.sparql)
        query = body if "PREFIX" in body.upper() else PREFIXES + body
    else:
        query = PREFIXES + QUERIES[args.query]

    g = Graph()
    g.parse(args.graph, format=_guess_format(args.graph))
    result = g.query(query)
    cols = [str(v) for v in result.vars]
    rows = [[("" if row[i] is None else str(row[i]))
             for i in range(len(cols))] for row in result]

    if args.format == "json":
        print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2))
    elif args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
    else:
        _print_table(cols, rows)
    return 0


def _print_table(cols: list[str], rows: list[list[str]]) -> None:
    def trunc(s: str, n: int = 60) -> str:
        s = s.replace("\n", " ")
        return s if len(s) <= n else s[: n - 1] + "…"

    disp = [[trunc(c) for c in r] for r in rows]
    widths = [max(len(cols[i]), *(len(r[i]) for r in disp)) if disp else len(cols[i])
              for i in range(len(cols))]
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(cols))))
    for r in disp:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(cols))))
    print(f"\n({len(rows)} rows)")


if __name__ == "__main__":
    raise SystemExit(main())
