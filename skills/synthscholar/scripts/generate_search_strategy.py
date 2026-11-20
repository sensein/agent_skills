#!/usr/bin/env python3
"""Generate a deterministic PubMed search strategy from a research question.

The skill-side entry point to the search-strategy engine vendored from
``sensein/search-strategy-generator`` (``scripts/search_strategy/``). It runs the
same question -> concept blocks -> PubMed query pipeline the web app ran, without
the web app:

1. **Map** the question to MeSH-grounded concept blocks (``pipeline.build``).
2. **Compile** the blocks plus filters into a PubMed boolean query.
3. **Count** the hits on PubMed (``--count``), and optionally **fetch** the
   records to CSV / JSONL with an audit ``protocol.json`` (``--fetch``).

Three mapping modes (``--mode``):

  closure    (default) the LLM only marks WHERE each PICO-style concept sits in
             the question, sampled k times and majority-voted; vocabulary is then
             resolved from the MeSH index alone. Most model-independent;
             replaces search-strategy-generator's ``hybrid`` mode.
  llm        the LLM proposes blocks and headings freely (comparison only).
  mesh_only  no LLM at all: every maximal MeSH match becomes a block.

MeSH index: built locally from NLM's RDF dump and not committed. The first run
that needs it downloads the compressed dump (~120 MB, 1-2 minutes) and builds
it automatically; ``--ensure-index`` does that up front and exits. To build
from a dump you already have instead:

    python generate_search_strategy.py --build-index /path/to/mesh2026.nt[.gz]

Usage:
    python generate_search_strategy.py "vocal biomarkers for depression"
    python generate_search_strategy.py "..." --mode mesh_only --count
    python generate_search_strategy.py "..." --date-from 2015 --species humans \\
        --languages english --count
    python generate_search_strategy.py "..." --fetch 5000 --outdir searches/
    python generate_search_strategy.py "..." --json > strategy.json

The host agent as the model (Claude Code, Codex, ...: no API key). The script
prints the exact prompt it would send; the agent answers it itself and hands
the answers back, and they go through the same parsing and voting:

    python generate_search_strategy.py "..." --emit-prompt > prompt.json
    #   closure: answer prompt.json's messages k times, independently, and save
    #   the k JSON answers as a list in samples.json
    python generate_search_strategy.py "..." --facet-samples samples.json \\
        --model-name <your model id> --count
    #   llm mode: --mode llm --emit-prompt, one answer, then --llm-response FILE

Environment:
    OPENROUTER_API_KEY   only when the script calls the model itself; not needed
                         with --facet-samples / --llm-response or --mode mesh_only
    OPENROUTER_MODEL     default model (else anthropic/claude-3.5-sonnet)
    NCBI_API_KEY         optional; raises the E-utilities rate limit
    NCBI_EMAIL           optional; NCBI asks for a contact address
    SYNTHSCHOLAR_MESH_DB optional; use an existing MeSH index at this path

Requires ``requests``.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_strategy import build_index, facets, pipeline, query_builder  # noqa: E402
from search_strategy.mesh_index import DEFAULT_DB  # noqa: E402
from search_strategy.openrouter_client import OpenRouterError, propose_messages  # noqa: E402
from search_strategy.pubmed import PubMed, to_csv, to_jsonl  # noqa: E402


def _emit_prompt(args: argparse.Namespace) -> dict:
    """What the host agent answers when it is the model (--emit-prompt)."""
    if args.mode == "closure":
        messages, n, flag = facets.facet_messages(args.question), args.facet_runs, "--facet-samples"
        save = (f"a JSON list of the {n} answers, each answered independently "
                "(a fresh context per sample, e.g. one subagent each)")
    else:
        messages, n, flag = (propose_messages(args.question, args.domains or None,
                                              args.extra_context), 1, "--llm-response")
        save = "the one answer, as a JSON file"
    return {
        "mode": args.mode,
        "question": args.question,
        "samples_needed": n,
        "messages": messages,
        "answer": "Answer the messages as the model would: the JSON object only, no prose.",
        "save_as": save,
        "next": (f'generate_search_strategy.py "<question>" --mode {args.mode} {flag} FILE '
                 "--model-name <your model id>"),
    }


def _ensure_index() -> bool:
    """Build the MeSH index if it is missing. Progress goes to stderr, so a
    --json result on stdout stays parseable. False if it could not be built."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            built = build_index.ensure(DEFAULT_DB)
    except RuntimeError as e:
        print(f"MeSH index not available: {e}\n"
              f"With a dump already on disk: generate_search_strategy.py --build-index "
              f"/path/to/mesh{build_index.MESH_YEAR}.nt[.gz]", file=sys.stderr)
        return False
    if built:
        print(f"MeSH index ready: {DEFAULT_DB}", file=sys.stderr)
    return True


def _load_samples(path: str) -> list[str]:
    """--facet-samples: a JSON list of answers (objects or JSON strings), or
    {"samples": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("samples")
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: expected a non-empty JSON list of answers")
    return [s if isinstance(s, str) else json.dumps(s) for s in data]


def _filters(args: argparse.Namespace) -> dict:
    return {
        "date_from": args.date_from,
        "date_to": args.date_to,
        "languages": args.languages,
        "article_types_include": args.include_type,
        "article_types_exclude": args.exclude_type,
        "species": args.species,
        "exclude_terms": args.exclude_term,
    }


def _pubmed() -> PubMed:
    return PubMed(api_key=os.environ.get("NCBI_API_KEY", ""),
                  email=os.environ.get("NCBI_EMAIL", ""))


class _OutputError(Exception):
    """A local write failed (unwritable --outdir, full disk): not a PubMed error,
    so it must not be reported as one -- retrying the network would not help."""


def _fetch(pm: PubMed, query: str, search: dict, max_records: int, outdir: Path) -> dict:
    """Mirror of the web app's /api/search: records + an audit protocol.json."""
    # fetch_query, not fetch_all: past 9,999 records NCBI's history server refuses
    # retstart, so the query has to be split by publication date.
    fetched = pm.fetch_query(query, max_records=max_records or None)
    articles = fetched["articles"]
    qhash = query_builder.query_hash(query)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = outdir / f"{stamp}_{qhash}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "results.csv").write_text(to_csv(articles), encoding="utf-8")
        (folder / "results.jsonl").write_text(to_jsonl(articles), encoding="utf-8")
    except OSError as e:
        raise _OutputError(f"could not write {len(articles)} records to {folder}: {e}") from e
    protocol = {
        "query": query,
        "query_hash": qhash,
        "pubmed_translation": search["translation"],
        "total_count": search["count"],
        "fetched": len(articles),
        "capped": fetched["capped"],
        # audit trail for a >9,999-hit search: which date slices were walked, and
        # whether any records fell outside them
        "date_slices": [{k: s[k] for k in ("from", "to", "count", "truncated")}
                        for s in fetched["slices"]] if len(fetched["slices"]) > 1 else [],
        "records_unaccounted": fetched["missing"],
        "timestamp": stamp,
    }
    try:
        (folder / "protocol.json").write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    except OSError as e:
        raise _OutputError(f"could not write {folder / 'protocol.json'}: {e}") from e
    return {**protocol, "folder": str(folder)}


def _iso(d: str) -> str:
    """Filter date ("YYYY" or "YYYY/MM/DD") -> the protocol's form ("YYYY" or ISO)."""
    return d.replace("/", "-") if d else ""


def _filters_text(f: dict) -> str:
    parts = []
    if f["date_from"] or f["date_to"]:
        parts.append(f"publication date {f['date_from'] or '...'} to {f['date_to'] or 'present'}")
    if f["languages"]:
        parts.append("language: " + ", ".join(f["languages"]))
    if f["species"]:
        parts.append(f"species: {f['species']}")
    if f["article_types_include"]:
        parts.append("publication type: " + ", ".join(f["article_types_include"]))
    if f["article_types_exclude"]:
        parts.append("excluding publication type: " + ", ".join(f["article_types_exclude"]))
    if f["exclude_terms"]:
        parts.append("excluding terms: " + ", ".join(f["exclude_terms"]))
    return "; ".join(parts)


def _search_provenance(result: dict) -> dict:
    """search_provenance.json for run_local_review.py / update_provenance.py, from
    this run instead of from the user. Screening counts are left at 0 for
    screen_records.py to fill in."""
    f = result["filters"]
    who = (f"{result['provider']} {result['model']}".strip()
           if result["mode"] != "mesh_only" else "no model")
    notes = (f"generated by generate_search_strategy.py: {result['mode']} mode ({who}), "
             f"MeSH {build_index.MESH_YEAR} index, query hash {result['hash']}")
    seg = result.get("segmentation") or {}
    if seg.get("mode") == "llm_voted":
        notes += f"; concept spans voted from {seg.get('runs', 0)} model sample(s)"
    elif seg.get("mode") in ("heuristic", "heuristic_fallback"):
        notes += "; concept spans from the offline heuristic segmenter"
    if result.get("fallback"):
        fb = result["fallback"]
        notes += (f" because model segmentation FAILED ({fb['requested_provider']} "
                  f"{fb['requested_model']}: {len(fb['errors'])} failed sample(s))")
    fetch = result.get("fetch")
    if fetch:
        notes += f"; {fetch['fetched']} of {fetch['total_count']} records fetched"
        if fetch["capped"] or fetch["records_unaccounted"]:
            notes += " (NOT all records were fetched)"
    return {
        "searches": [{
            "database": "PubMed",
            "query": result["query"],
            "date_searched": time.strftime("%Y-%m-%d"),
            "records_identified": result["count"],
            "filters": _filters_text(f),
            "notes": notes,
        }],
        "duplicates_removed": 0,          # one database, PMIDs are unique
        "total_identified": result["count"],
        "records_screened": 0,
        "records_excluded_title_abstract": 0,
        "reports_sought": 0,
        "reports_not_retrieved": 0,
        "date_range_start": _iso(f["date_from"]),
        "date_range_end": _iso(f["date_to"]),
        "grey_literature": "",
        "search_notes": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("question", nargs="?", default="", help="the research question")
    ap.add_argument("--mode", default="closure", choices=pipeline.MODES,
                    help="mapping mode (default: closure)")
    ap.add_argument("--model", default=None, help="model id (default: $OPENROUTER_MODEL)")
    ap.add_argument("--facet-runs", type=int, default=3, metavar="K",
                    help="closure mode: samples for self-consistency voting (default 3)")
    ap.add_argument("--domains", nargs="*", default=[],
                    help="domain-vocabulary clusters from data/domain_terms.json")
    ap.add_argument("--extra-context", default="", help="extra guidance for the model")
    ap.add_argument("--no-strict", action="store_true",
                    help="keep the model's free-text terms instead of index-derived synonyms")
    # filters
    ap.add_argument("--date-from", default="", help="YYYY or YYYY/MM/DD")
    ap.add_argument("--date-to", default="", help="YYYY or YYYY/MM/DD")
    ap.add_argument("--languages", nargs="*", default=[], help="e.g. english")
    ap.add_argument("--species", default="", choices=("", "humans", "animals"))
    ap.add_argument("--include-type", action="append", default=[], metavar="PT",
                    help="publication type to require, e.g. 'Randomized Controlled Trial'")
    ap.add_argument("--exclude-type", action="append", default=[], metavar="PT",
                    help="publication type to exclude, e.g. Review")
    ap.add_argument("--exclude-term", action="append", default=[], metavar="TERM",
                    help="Title/Abstract term to NOT")
    # PubMed
    ap.add_argument("--count", action="store_true", help="count hits on PubMed")
    ap.add_argument("--fetch", type=int, default=None, metavar="N",
                    help="fetch up to N records (0 = all) into --outdir")
    ap.add_argument("--outdir", default="searches", help="where --fetch writes (default searches/)")
    ap.add_argument("--provenance-out", default="", metavar="FILE",
                    help="write search_provenance.json for the review from this run (implies --count)")
    # outputs
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--build-index", default="", metavar="MESH_NT",
                    help="build the local MeSH index from an NLM mesh.nt(.gz) dump and exit")
    ap.add_argument("--ensure-index", action="store_true",
                    help="download and build the MeSH index if it is missing, and exit")
    # the host agent as the model
    ap.add_argument("--emit-prompt", action="store_true",
                    help="print the prompt for the host agent to answer, and exit")
    ap.add_argument("--facet-samples", default="", metavar="FILE",
                    help="closure: the agent's k answers to --emit-prompt (JSON list)")
    ap.add_argument("--llm-response", default="", metavar="FILE",
                    help="llm mode: the agent's answer to --emit-prompt")
    ap.add_argument("--model-name", default="", metavar="ID",
                    help="the model that wrote --facet-samples / --llm-response (provenance)")
    args = ap.parse_args()

    if args.build_index:
        build_index.build(args.build_index, DEFAULT_DB)
        return 0
    if args.ensure_index:
        if not _ensure_index():
            return 2
        print(f"MeSH index: {DEFAULT_DB} "
              f"({build_index.index_descriptors(DEFAULT_DB):,} descriptors)")
        return 0
    if not args.question.strip():
        ap.error("a question is required (or use --build-index / --ensure-index)")
    if args.emit_prompt:
        if args.mode == "mesh_only":
            ap.error("--mode mesh_only uses no model; there is no prompt to answer")
        print(json.dumps(_emit_prompt(args), indent=2))
        return 0
    if args.facet_samples and args.mode != "closure":
        ap.error("--facet-samples is for --mode closure")
    if args.llm_response and args.mode != "llm":
        ap.error("--llm-response is for --mode llm")
    agent = bool(args.facet_samples or args.llm_response)
    if not _ensure_index():
        return 2

    try:
        samples = _load_samples(args.facet_samples) if args.facet_samples else None
        response = (Path(args.llm_response).read_text(encoding="utf-8")
                    if args.llm_response else None)
        built = pipeline.build(
            args.question, domains=args.domains or None, mode=args.mode, model=args.model,
            extra_context=args.extra_context,
            strict=not args.no_strict, facet_runs=args.facet_runs,
            facet_samples=samples, llm_response=response,
        )
    except OpenRouterError as e:
        if agent:
            print(f"Could not parse the answer in {args.llm_response}: {e}", file=sys.stderr)
        else:
            print(f"LLM call failed: {e}\n(--mode mesh_only needs no LLM; inside an agent, "
                  "use --emit-prompt and answer it yourself)", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        what = "read the agent's answers" if agent else "build the concept blocks"
        print(f"Could not {what}: {e}", file=sys.stderr)
        return 1

    seg = built["segmentation"] if built["mode"] == "closure" else {}
    no_model = seg.get("mode") == "heuristic"
    # every sample failed (or none survived the vote): the facets are the offline
    # heuristic's, whatever was asked for -- never record that as model output
    fallback = seg.get("mode") == "heuristic_fallback"
    if no_model:
        print("note: no model was called (OPENROUTER_API_KEY not set); facets come from the "
              "offline heuristic segmenter.\nInside an agent, run --emit-prompt and answer it "
              "yourself (--facet-samples).", file=sys.stderr)
    if fallback:
        why = "; ".join(seg.get("errors") or []) or "no span was agreed by enough samples"
        if agent:
            print(f"None of the answers in {args.facet_samples} could be used ({seg.get('runs', 0)} "
                  f"parsed): {why}\nRe-answer --emit-prompt: JSON only, every \"text\" a verbatim "
                  "substring of the question.", file=sys.stderr)
            return 1
        print(f"WARNING: model segmentation failed ({why}); facets come from the offline "
              "heuristic segmenter and are recorded as such.", file=sys.stderr)
    if built["mode"] == "mesh_only" or no_model:
        provider, model = "none", ""
    elif fallback:
        provider, model = "none (fallback)", ""
    elif agent:
        provider, model = "agent", args.model_name or "unspecified"
    else:
        provider, model = "openrouter", args.model or facets.DEFAULT_MODEL

    filters = _filters(args)
    try:
        compiled = query_builder.compile_search(built["concepts"], filters)
    except ValueError as e:
        print(f"No query could be compiled: {str(e).rstrip('.')}. No concept in the question matched MeSH "
              "or yielded search terms; rephrase it with the concepts named explicitly"
              + (", or re-answer --emit-prompt" if agent else "") + ".", file=sys.stderr)
        return 1
    result = {
        "question": args.question,
        "mode": built["mode"],
        "provider": provider,
        "model": model,
        "notes": built["notes"],
        "concepts": built["concepts"],
        "dropped": built["dropped"],
        "filters": filters,
        **compiled,
    }
    if built["mode"] == "closure":
        # the voted spans the vocabulary was resolved from (and how many samples agreed)
        result["segmentation"] = built["segmentation"]
    if fallback:
        result["fallback"] = {"requested_provider": "openrouter",
                              "requested_model": args.model or facets.DEFAULT_MODEL,
                              "errors": seg.get("errors") or []}

    if args.count or args.fetch is not None or args.provenance_out:
        pm = _pubmed()
        try:
            search = pm.search(compiled["query"])
            result.update(count=search["count"], translation=search["translation"],
                          warnings=search["warnings"], errors=search["errors"])
            if args.fetch is not None and search["count"]:
                result["fetch"] = _fetch(pm, compiled["query"], search, args.fetch,
                                         Path(args.outdir))
        except _OutputError as e:
            print(f"The PubMed fetch succeeded but {e}.\nFree disk space or pass a writable "
                  "--outdir, then run again.", file=sys.stderr)
            return 4
        except (OSError, RuntimeError) as e:     # requests errors are OSErrors
            print(f"PubMed request failed: {e}\nThe query compiled (hash {compiled['hash']}); "
                  "retry later, or set NCBI_API_KEY for a higher rate limit.", file=sys.stderr)
            return 3
    if args.provenance_out:
        try:
            Path(args.provenance_out).write_text(
                json.dumps(_search_provenance(result), indent=2), encoding="utf-8")
        except OSError as e:
            print(f"Could not write {args.provenance_out}: {e}", file=sys.stderr)
            return 4
        result["provenance_out"] = args.provenance_out

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"mode: {result['mode']}   provider: {result['provider']}   "
          f"model: {result['model'] or '-'}")
    if result["notes"]:
        print(f"notes: {result['notes']}")
    for f in result.get("segmentation", {}).get("facets", []):
        votes = f" ({f['agreement']}/{f['of']} samples)" if "agreement" in f else ""
        print(f"  span [{f['role']}] {f['name']!r}{votes}")
    for i, c in enumerate(result["concepts"], 1):
        mesh = "; ".join(c["mesh"]) or "-"
        print(f"  block {i} [{c['slot']}] {c['name']}: MeSH {mesh}  (+{len(c['freetext'])} text terms)")
    if result["dropped"]:
        print(f"  dropped (no exact MeSH match): {result['dropped']}")
    print(f"\nquery ({compiled['n_concepts']} blocks, {compiled['n_terms']} terms, "
          f"hash {compiled['hash']}):\n{compiled['query']}")
    if "count" in result:
        print(f"\nPubMed hits: {result['count']:,}")
        # esearch's warninglist / errorlist: {kind: [messages]}
        for kind, msgs in {**result["warnings"], **result["errors"]}.items():
            if msgs:
                print(f"  ! {kind}: {msgs}")
    if "fetch" in result:
        f = result["fetch"]
        print(f"fetched {f['fetched']:,} records -> {f['folder']}"
              + ("  (capped)" if f["capped"] else ""))
    if "provenance_out" in result:
        print(f"search provenance -> {result['provenance_out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
