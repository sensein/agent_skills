#!/usr/bin/env python3
"""Run the full SynthScholar review pipeline over a user-supplied PDF corpus.

Step 3 of the bring-your-own-corpus workflow (SKILL.md → Mode 3). Discovery
and full-text retrieval already happened — the user did them — so this skips
straight to the analytical half of the pipeline and runs *exactly* the stages
the hosted application runs:

    screening (title/abstract → full-text eligibility)
      → evidence-span extraction
      → risk of bias
      → data charting (sections A–G + custom questions)
      → critical appraisal
      → PRISMA narrative rows
      → synthesis + GRADE + bias + limitations
      → per-group analysis
      → grounding validation
      → assembled PrismaReview document

The user's search strategy is carried in as provenance (``--provenance``) and
lands in ``search_queries``, ``flow`` (identification counts) and
``search_iterations`` — the same slots the app fills from its own searches, so
the Markdown and Turtle report it identically. It can also be added after the
fact with ``update_provenance.py``.

Usage:
    export OPENROUTER_API_KEY=sk-or-v1-...
    python run_local_review.py --protocol protocol.json --corpus corpus.json \
        --provenance search_provenance.json --outdir out/

    # cheap pre-flight: build everything, make no LLM calls
    python run_local_review.py --protocol protocol.json --corpus corpus.json --dry-run

    # blank provenance file to fill in
    python run_local_review.py --print-provenance-template > search_provenance.json

Requires: the ``synthscholar`` package importable and OPENROUTER_API_KEY set.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Sibling helper — the single export surface for both BYO-corpus paths.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_review import ALL_FORMATS, write_exports  # noqa: E402

# Corpus keys that are skill bookkeeping, not Article fields.
_INTERNAL_PREFIX = "_"

PROVENANCE_TEMPLATE = {
    "searches": [
        {
            "database": "PubMed",
            "query": "(depression[MeSH]) AND (voice OR speech)",
            "date_searched": "2026-07-01",
            "records_identified": 0,
            "filters": "2015-2026, English",
            "notes": "",
        }
    ],
    "duplicates_removed": 0,
    "total_identified": 0,
    "records_screened": 0,
    "records_excluded_title_abstract": 0,
    "reports_sought": 0,
    "reports_not_retrieved": 0,
    "date_range_start": "",
    "date_range_end": "",
    "grey_literature": "",
    "search_notes": "",
}

# Flow fields a user may assert directly. Identification-stage fields are
# applied before the run (the pipeline preserves them); screening-stage
# fields are re-applied after, because the pipeline overwrites them with
# what it actually did to the supplied PDFs.
_PRE_RUN_FLOW = {
    "total_identified": "total_identified",
    "duplicates_removed": "duplicates_removed",
}
_POST_RUN_FLOW = {
    "records_screened": "screened_title_abstract",
    "records_excluded_title_abstract": "excluded_title_abstract",
    "reports_sought": "sought_fulltext",
    "reports_not_retrieved": "not_retrieved",
}


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_articles(corpus: dict) -> list:
    """Corpus entries → ``Article`` objects (skill-internal keys stripped)."""
    from synthscholar.models import Article, InclusionStatus

    items = corpus.get("items", corpus if isinstance(corpus, list) else [])
    if not items:
        raise SystemExit("corpus contains no items")

    articles = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        fields = {k: v for k, v in item.items() if not k.startswith(_INTERNAL_PREFIX)}
        pmid = str(fields.get("pmid") or "").strip()
        if not pmid:
            raise SystemExit(f"corpus item {i} has no pmid — re-run build_corpus.py")
        if pmid in seen:
            raise SystemExit(f"duplicate pmid '{pmid}' in corpus — IDs must be unique")
        seen.add(pmid)
        # Screening sets this itself; never pre-decide inclusion here.
        fields["inclusion_status"] = InclusionStatus.PENDING
        articles.append(Article(**fields))
    return articles


def build_protocol(path: str, provenance: dict):
    """Load the protocol, minting a review_id and folding in search dates."""
    from synthscholar.models import ReviewProtocol

    data = _load_json(path)
    proto = ReviewProtocol(**data)

    if not proto.review_id:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        proto.review_id = f"review_local_{ts}"

    # The searched databases are the user's, not the app's provider list.
    searched = [
        str(s.get("database") or "").strip()
        for s in provenance.get("searches", [])
        if str(s.get("database") or "").strip()
    ]
    if searched:
        seen: set[str] = set()
        proto.databases = [d for d in searched if not (d in seen or seen.add(d))]
    if provenance.get("date_range_start") and not proto.date_range_start:
        proto.date_range_start = provenance["date_range_start"]
    if provenance.get("date_range_end") and not proto.date_range_end:
        proto.date_range_end = provenance["date_range_end"]

    # No discovery phase runs here, so citation-chasing is not applicable.
    proto.max_hops = 0
    # This path never touches the shared PostgreSQL review cache.
    proto.pg_dsn = ""
    proto.share_to_cache = False
    return proto


def describe_searches(provenance: dict) -> list[str]:
    """Render each user search as one self-describing ``slr:search_query`` string."""
    out: list[str] = []
    for s in provenance.get("searches", []):
        db = str(s.get("database") or "unspecified database").strip()
        query = str(s.get("query") or "").strip()
        if not query:
            continue
        bits = [f"{db}"]
        if s.get("date_searched"):
            bits.append(f"searched {s['date_searched']}")
        head = f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]
        line = f"{head}: {query}"
        if s.get("filters"):
            line += f" | filters: {s['filters']}"
        if s.get("records_identified"):
            line += f" | {s['records_identified']} records"
        if s.get("notes"):
            line += f" | {s['notes']}"
        out.append(line)
    if provenance.get("grey_literature"):
        out.append(f"Grey literature / other sources: {provenance['grey_literature']}")
    if provenance.get("search_notes"):
        out.append(f"Search notes: {provenance['search_notes']}")
    return out


def build_search_iterations(provenance: dict) -> list:
    """One ``SearchIteration`` per user search, for the RDF provenance block."""
    from synthscholar.models import SearchIteration

    iterations = []
    for idx, s in enumerate(provenance.get("searches", []), start=1):
        query = str(s.get("query") or "").strip()
        if not query:
            continue
        iterations.append(SearchIteration(
            iteration_index=idx,
            iteration_kind="initial_query",
            database=str(s.get("database") or ""),
            query=query,
            cumulative_count=int(s.get("records_identified") or 0),
            started_at=str(s.get("date_searched") or ""),
        ))
    return iterations


# PRISMA per-database flow slots that have a dedicated field.
_NAMED_DB_FIELDS = {"pubmed": "db_pubmed", "biorxiv": "db_biorxiv", "medrxiv": "db_medrxiv"}


def apply_declared_db_counts(flow, provenance: dict) -> bool:
    """Rewrite per-database identification counts from the user's searches.

    Returns False (leaving *flow* untouched) when no search declares a record
    count — the caller then falls back to tallying the corpus itself.
    """
    declared = sum(int(s.get("records_identified") or 0)
                   for s in provenance.get("searches", []))
    if not declared:
        return False

    named = {f: 0 for f in _NAMED_DB_FIELDS.values()}
    other: dict[str, int] = {}
    for s in provenance.get("searches", []):
        db = str(s.get("database") or "unspecified").strip()
        n = int(s.get("records_identified") or 0)
        if not n:
            continue
        field = _NAMED_DB_FIELDS.get(db.lower().replace(" ", ""))
        if field:
            named[field] += n
        else:
            other[db] = other.get(db, 0) + n
    for field, n in named.items():
        setattr(flow, field, n)
    flow.db_other_sources = other
    return True


def build_flow(articles: list, provenance: dict):
    """Identification-stage PRISMA counts: the user's numbers, else the corpus."""
    from synthscholar.models import PRISMAFlowCounts
    from synthscholar.pipeline import _apply_per_db_tally

    flow = PRISMAFlowCounts()
    declared = sum(int(s.get("records_identified") or 0)
                   for s in provenance.get("searches", []))
    if not apply_declared_db_counts(flow, provenance):
        # No declared counts — tally what we actually hold, by Article.source.
        _apply_per_db_tally(flow, {a.pmid: a for a in articles})

    for key, attr in _PRE_RUN_FLOW.items():
        if provenance.get(key):
            setattr(flow, attr, int(provenance[key]))
    if not flow.total_identified:
        flow.total_identified = declared or len(articles)
    flow.after_dedup = max(flow.total_identified - flow.duplicates_removed, len(articles))
    return flow


def apply_user_flow(flow, provenance: dict) -> list[str]:
    """Re-assert user-declared screening counts over the pipeline's own.

    The user screened titles/abstracts themselves before exporting PDFs, so
    when they supply those numbers they are the reportable ones. Only keys
    actually present are touched. Returns the list of overridden fields.
    """
    applied: list[str] = []
    for key, attr in _POST_RUN_FLOW.items():
        if provenance.get(key):
            setattr(flow, attr, int(provenance[key]))
            applied.append(attr)
    return applied


def reading_plan(articles: list, protocol) -> dict:
    """How much text will be read, and how many LLM calls that implies.

    Evidence extraction reads every chunk of every article, so a long corpus
    costs real money — report it before the run rather than after the bill.
    """
    from synthscholar.text_windows import chunk_text

    total_chars = sum(len(a.full_text or "") for a in articles)
    chunks = 0
    longest = ("", 0)
    for a in articles:
        n = len(chunk_text(
            a.full_text or "",
            chunk_chars=protocol.evidence_chunk_chars,
            overlap=protocol.evidence_chunk_overlap,
            max_chars=protocol.evidence_max_chars,
        )) or 1
        chunks += n
        if n > longest[1]:
            longest = (a.pmid, n)
    n = len(articles)
    return {
        "total_chars": total_chars,
        "evidence_chunks": chunks,
        "longest": longest,
        # screening (TA + FT) + RoB + charting + appraisal + narrative row
        "per_article_calls": n * 6,
        "corpus_calls": chunks + n * 6,
    }


async def run(args: argparse.Namespace) -> int:
    from synthscholar.agents import run_per_group_analysis
    from synthscholar.pipeline import PRISMAReviewPipeline
    from synthscholar.provenance import ProvenanceCollector, build_run_configuration

    provenance = _load_json(args.provenance) if args.provenance else {}
    corpus = _load_json(args.corpus)
    articles = build_articles(corpus)
    protocol = build_protocol(args.protocol, provenance)
    queries = describe_searches(provenance)
    flow = build_flow(articles, provenance)

    n_ft = sum(1 for a in articles if (a.full_text or "").strip())
    print(f"Corpus:   {len(articles)} articles ({n_ft} with extracted full text)")
    print(f"Review:   {protocol.review_id}")
    print(f"Question: {protocol.question[:90]}")
    print(f"Searches: {len(queries)} recorded" + ("" if queries else " — provenance is EMPTY"))
    print(f"Flow in:  identified={flow.total_identified} "
          f"duplicates_removed={flow.duplicates_removed} after_dedup={flow.after_dedup}")
    plan = reading_plan(articles, protocol)
    print(f"Reading:  {plan['total_chars']:,} chars of full text → "
          f"{plan['evidence_chunks']} evidence-extraction calls "
          f"(every chunk of every article is read)")
    print(f"          ~{plan['corpus_calls']} per-article LLM calls in total, "
          f"plus synthesis/GRADE/per-group")
    if plan["longest"][1] > 20:
        print(f"  NOTE  {plan['longest'][0]} alone needs {plan['longest'][1]} chunks. "
              f"Cap it with protocol.evidence_max_chars (or a smaller "
              f"--max-chars when building the corpus) if that's more reading "
              f"than the review needs.")
    if not queries:
        print("  WARN  no search strategy recorded. Supply --provenance now, or add it "
              "later with update_provenance.py, for a PRISMA-reportable review.")
    missing = [a.pmid for a in articles if not (a.full_text or a.abstract).strip()]
    if missing:
        print(f"  WARN  {len(missing)} article(s) have neither abstract nor full text "
              f"and will screen out: {', '.join(missing[:5])}")

    if args.dry_run:
        print("\n--dry-run: no LLM calls made. Everything above is what would be run.")
        return 0

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: set OPENROUTER_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    pipeline = PRISMAReviewPipeline(
        api_key=api_key,
        model_name=args.model,
        protocol=protocol,
        enable_cache=False,      # nothing to fetch; keeps the run offline
    )
    if not hasattr(pipeline, "_run_from_deduped"):
        print("ERROR: this synthscholar version has no PRISMAReviewPipeline."
              "_run_from_deduped — see references/byo_corpus_review.md "
              "(version coupling).", file=sys.stderr)
        return 2

    collector = ProvenanceCollector()
    collector.run_configuration = build_run_configuration(
        protocol=protocol,
        review_id=protocol.review_id,
        model_name=args.model,
        pipeline_kwargs={
            "entrypoint": "synthscholar-skills/run_local_review.py",
            "corpus_provenance": "user_supplied_pdfs",
            "corpus_size": len(articles),
            "corpus_full_text_count": n_ft,
            "discovery_performed_by": "user",
            "search_provenance_file": args.provenance or "",
            "data_items": args.data_item or None,
            "per_group_analysis": not args.no_per_group,
        },
    )
    collector.search_iterations = build_search_iterations(provenance)
    pipeline.deps.provenance = collector

    result = await pipeline._run_from_deduped(
        articles,
        queries,
        flow,
        progress_callback=None,   # the pipeline already prints its own log
        data_items=args.data_item or None,
        output_synthesis_style=args.synthesis_style,
    )

    # Per-group analysis — part of run() but not of _run_from_deduped, so it
    # is driven here to keep parity with the application's output.
    dim = protocol.grouping_dimension
    if (
        not args.no_per_group
        and result.data_charting_rubrics
        and dim
        and hasattr(result.data_charting_rubrics[0], dim)
    ):
        print(f"Per-group analysis: bucketing by {dim}...")
        try:
            result.per_group_analysis = await run_per_group_analysis(
                result.included_articles,
                result.data_charting_rubrics,
                pipeline.deps,
                dimension=dim,
                default_questions=protocol.default_group_questions,
                per_group_questions=protocol.per_group_questions,
                topic=protocol.question or protocol.title,
            )
            print(f"  {result.per_group_analysis.n_groups} groups "
                  f"({result.per_group_analysis.unlabeled_count} unlabeled)")
        except Exception as exc:
            print(f"  Per-group analysis skipped: {exc}")

    overridden = apply_user_flow(result.flow, provenance)
    if overridden:
        print(f"Flow: user-declared counts applied for {', '.join(overridden)}")

    # Re-stamp so per-group / post-run invocations are included.
    collector.stamp(result)

    written = write_exports(result, args.outdir, args.formats, base=args.base)
    f = result.flow
    print(f"\nIncluded {len(result.included_articles)} of {len(articles)} articles.")
    print(f"Full text:  {f.full_text_retrieved} of {f.sought_fulltext} available"
          + (f" — {', '.join(f'{k}: {v}' for k, v in f.full_text_sources.items())}"
             if f.full_text_sources else ""))
    print(f"Eligibility judged on: {f.assessed_on_full_text} full text, "
          f"{f.assessed_on_abstract_only} abstract only")
    if f.excluded_reasons_full_text:
        print("Excluded at eligibility:")
        for reason, n in f.excluded_reasons_full_text.items():
            print(f"  {n}× {reason}")
    if f.included_abstract_only:
        print(f"NOTE  {f.included_abstract_only} included stud{'y' if f.included_abstract_only == 1 else 'ies'} "
              f"had no full text. For paywalled papers, retrieve them with "
              f"fetch_ezproxy.py and re-run.")
    print("Wrote:")
    for p in written:
        print(f"  {p}")
    if not queries:
        print("\nProvenance is still incomplete — add the search strategy with:\n"
              f"  python update_provenance.py {args.outdir}/{args.base}.json "
              "--provenance search_provenance.json --outdir " + str(args.outdir))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--protocol", help="ReviewProtocol JSON (validate_protocol.py)")
    ap.add_argument("--corpus", help="corpus JSON (build_corpus.py)")
    ap.add_argument("--provenance", default="",
                    help="search-provenance JSON describing the user's own search")
    ap.add_argument("--outdir", default="review_output", help="output directory")
    ap.add_argument("--base", default="review", help="output filename stem")
    ap.add_argument("--formats", nargs="+", default=["md", "ttl", "json", "charting", "appraisal"],
                    metavar="FMT", help=f"any of: {' '.join(ALL_FORMATS)}")
    ap.add_argument("--model", default=os.environ.get("SYNTHSCHOLAR_MODEL", "anthropic/claude-sonnet-4"),
                    help="OpenRouter model id (default: $SYNTHSCHOLAR_MODEL or anthropic/claude-sonnet-4)")
    ap.add_argument("--api-key", default="", help="OpenRouter key (else $OPENROUTER_API_KEY)")
    ap.add_argument("--data-item", action="append", default=[], metavar="ITEM",
                    help="extra per-study data item to extract (repeatable)")
    ap.add_argument("--synthesis-style", default="paragraph",
                    choices=["paragraph", "bullet"], help="synthesis prose style")
    ap.add_argument("--no-per-group", action="store_true",
                    help="skip the per-group analysis pass")
    ap.add_argument("--dry-run", action="store_true",
                    help="build corpus/protocol/flow and report, without LLM calls")
    ap.add_argument("--print-provenance-template", action="store_true",
                    help="print a blank search-provenance JSON and exit")
    args = ap.parse_args()

    if args.print_provenance_template:
        print(json.dumps(PROVENANCE_TEMPLATE, indent=2))
        return 0
    if not args.protocol or not args.corpus:
        ap.error("--protocol and --corpus are required "
                 "(or use --print-provenance-template)")

    unknown = [f for f in args.formats if f not in ALL_FORMATS]
    if unknown:
        ap.error(f"unknown format(s): {', '.join(unknown)}")

    try:
        import synthscholar  # noqa: F401
    except ImportError as e:
        print(f"synthscholar is not importable ({e}). "
              "`pip install 'synthscholar[fulltext]'`", file=sys.stderr)
        return 2

    # This path drives the app's own pipeline, so it needs the whole-document
    # reading and screening-basis work — not just the released package. Checked
    # by feature rather than version, and before any LLM spend.
    from export_review import _require_synthscholar
    _require_synthscholar()
    try:
        import synthscholar.text_windows  # noqa: F401
    except ImportError:
        print("The installed synthscholar has no synthscholar/text_windows.py, so "
              "evidence extraction would read only the opening pages of each "
              "article. Install the development checkout:\n"
              "  pip install -e /path/to/prisma-review-agent", file=sys.stderr)
        return 3

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
