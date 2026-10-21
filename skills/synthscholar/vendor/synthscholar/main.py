#!/usr/bin/env python3
"""
SynthScholar — PRISMA 2020 Systematic Review CLI

Usage:
    # Set your API key
    export OPENROUTER_API_KEY="sk-or-v1-..."

    # Quick review with defaults
    python main.py --title "CRISPR gene therapy efficacy" \
                   --inclusion "Clinical trials, human subjects" \
                   --exclusion "Animal-only studies, reviews"

    # Full PICO specification
    python main.py --title "GLP-1 agonists for type 2 diabetes" \
                   --objective "Evaluate efficacy of GLP-1 RAs vs placebo" \
                   --population "Adults with T2DM" \
                   --intervention "GLP-1 receptor agonists" \
                   --comparison "Placebo or standard care" \
                   --outcome "HbA1c reduction, weight change" \
                   --inclusion "RCTs, English, 2019-2024" \
                   --exclusion "Case reports, editorials" \
                   --model "anthropic/claude-sonnet-4" \
                   --max-results 30 \
                   --hops 2 \
                   --export md json bib

    # Interactive mode (prompts for input)
    python main.py --interactive
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path
from datetime import datetime

from synthscholar.models import ReviewProtocol, RoBTool, ReviewPlan, PlanRejectedError, MaxIterationsReachedError
from synthscholar.pipeline import PRISMAReviewPipeline
from synthscholar.export import (
    to_markdown, to_bibtex, to_json, to_turtle, to_jsonld, to_oxigraph_store,
    to_compare_markdown, to_compare_json,
)

ROB_TOOL_CHOICES = [t.value for t in RoBTool]


OUTPUT_DIR = Path("synthscholar_results")


def get_api_key(cli_value: str = "") -> str:
    """Get OpenRouter API key — CLI arg takes precedence over env var."""
    # api_key is called at line 1 of run_review(), before PRISMAReviewPipeline construction
    key = cli_value or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("ERROR: Set OPENROUTER_API_KEY environment variable or pass --api-key.")
        print("  export OPENROUTER_API_KEY='sk-or-v1-...'")
        print("  synthscholar --title '...' --api-key 'sk-or-v1-...'")
        sys.exit(1)
    return key


def build_ezproxy_config(args: argparse.Namespace):
    """Build an EZProxyConfig from ``EZPROXY_*`` env vars plus CLI overrides.

    Session cookies are read from the environment only — never from a flag,
    because the full command line is stored verbatim in the run's
    ``RunConfiguration.cli_invocation`` and would leak the credential into
    every export.
    """
    from synthscholar.ezproxy import EZProxyConfig

    cfg = EZProxyConfig.from_env()
    if args.ezproxy_host:
        cfg.host = args.ezproxy_host
    if args.ezproxy_mode:
        cfg.mode = args.ezproxy_mode
    if args.ezproxy_cookie_file:
        cfg.cookie_file = args.ezproxy_cookie_file
    if args.ezproxy_delay is not None:
        cfg.delay_seconds = args.ezproxy_delay
    if args.ezproxy_max_requests is not None:
        cfg.max_requests = args.ezproxy_max_requests
    return cfg


def describe_ezproxy(cfg) -> str:
    """Human-readable readiness line for the configured proxy."""
    from synthscholar.ezproxy import EZProxyFetcher

    return EZProxyFetcher(config=cfg).status()


def build_protocol_from_args(args: argparse.Namespace) -> ReviewProtocol:
    """Build ReviewProtocol from CLI arguments."""
    # Postgres caching: CLI flag wins, otherwise env var, otherwise empty (disabled).
    pg_dsn = args.pg_dsn or os.environ.get("SYNTHSCHOLAR_PG_DSN", "")

    # Per-group analysis: collapse repeated --group-question LABEL QUESTION
    # pairs into a single dict[label, list[questions]]. Same label can appear
    # multiple times to stack questions for that group.
    per_group_questions: dict[str, list[str]] = {}
    for label, question in getattr(args, "group_question", []) or []:
        per_group_questions.setdefault(label, []).append(question)

    return ReviewProtocol(
        title=args.title,
        objective=args.objective or args.title,
        pico_population=args.population or "",
        pico_intervention=args.intervention or "",
        pico_comparison=args.comparison or "",
        pico_outcome=args.outcome or "",
        inclusion_criteria=args.inclusion or "",
        exclusion_criteria=args.exclusion or "",
        databases=args.databases,
        date_range_start=args.date_start or "",
        date_range_end=args.date_end or "",
        max_hops=args.hops,
        registration_number=args.registration or "",
        rob_tool=RoBTool(args.rob_tool),
        article_concurrency=args.concurrency,
        evidence_chunk_chars=args.evidence_chunk_chars,
        evidence_max_chars=args.evidence_max_chars,
        evidence_spans_per_article=args.evidence_spans_per_article,
        article_text_budget=args.article_text_budget,
        max_articles=args.max_articles if args.max_articles and args.max_articles > 0 else None,
        pg_dsn=pg_dsn,
        force_refresh=args.force_refresh,
        cache_threshold=args.cache_threshold,
        cache_ttl_days=args.cache_ttl_days,
        grouping_dimension=getattr(args, "group_by", "disorder_cohort"),
        default_group_questions=getattr(args, "default_group_question", []) or [],
        per_group_questions=per_group_questions,
    )


def build_protocol_interactive() -> ReviewProtocol:
    """Build protocol interactively via prompts."""
    print("\n" + "=" * 60)
    print("  PRISMA 2020 Systematic Review — Protocol Setup")
    print("=" * 60 + "\n")

    title = input("Review title: ").strip()
    objective = input("Objective/research question: ").strip() or title

    print("\n--- PICO Framework ---")
    population = input("Population: ").strip()
    intervention = input("Intervention: ").strip()
    comparison = input("Comparison: ").strip()
    outcome = input("Outcome: ").strip()

    print("\n--- Criteria ---")
    inclusion = input("Inclusion criteria: ").strip()
    exclusion = input("Exclusion criteria: ").strip()

    print("\n--- Search settings ---")
    hops = input("Citation hops (0-10) [10]: ").strip()
    hops = int(hops) if hops.isdigit() else 1
    print("  Available RoB tools:")
    for i, tool in enumerate(RoBTool, 1):
        print(f"    {i}. {tool.value}")
    rob_choice = input(f"  Select RoB tool (1-{len(RoBTool)}) [1]: ").strip()
    rob_idx = int(rob_choice) - 1 if rob_choice.isdigit() else 0
    rob_tools_list = list(RoBTool)
    rob = rob_tools_list[min(rob_idx, len(rob_tools_list) - 1)]

    return ReviewProtocol(
        title=title,
        objective=objective,
        pico_population=population,
        pico_intervention=intervention,
        pico_comparison=comparison,
        pico_outcome=outcome,
        inclusion_criteria=inclusion,
        exclusion_criteria=exclusion,
        max_hops=hops,
        rob_tool=rob if isinstance(rob, RoBTool) else RoBTool(rob),
    )


def _cli_confirm(plan: ReviewPlan) -> "bool | str":
    """Interactive CLI callback for plan confirmation."""
    width = 50
    border = "═" * width
    print(f"\n{border}")
    print(f"  Generated Search Plan (Iteration {plan.iteration})")
    print(border)
    print(f"Research question: {plan.research_question}\n")
    print(f"PubMed queries ({len(plan.pubmed_queries)}):")
    for i, q in enumerate(plan.pubmed_queries, 1):
        print(f"  {i}. {q}")
    if plan.mesh_terms:
        print(f"\nMeSH terms: {', '.join(plan.mesh_terms)}")
    print(f"\nRationale: {plan.rationale}")
    print(border)
    answer = input("Confirm plan? [yes / no / <feedback>]: ").strip()
    if answer.lower() in ("", "y", "yes"):
        return True
    if answer.lower() in ("no", "abort"):
        return False
    return answer


def save_exports(result, formats: list[str]):
    """Save review results in requested formats."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = result.research_question[:40].replace(" ", "_").lower()
    base = f"{slug}_{ts}"

    saved = []
    if "md" in formats or "markdown" in formats:
        path = OUTPUT_DIR / f"{base}.md"
        path.write_text(to_markdown(result), encoding="utf-8")
        saved.append(str(path))

    if "json" in formats:
        path = OUTPUT_DIR / f"{base}.json"
        path.write_text(to_json(result), encoding="utf-8")
        saved.append(str(path))

    if "bib" in formats or "bibtex" in formats:
        path = OUTPUT_DIR / f"{base}.bib"
        path.write_text(to_bibtex(result), encoding="utf-8")
        saved.append(str(path))

    if "ttl" in formats or "turtle" in formats:
        path = OUTPUT_DIR / f"{base}.ttl"
        path.write_text(to_turtle(result), encoding="utf-8")
        saved.append(str(path))

    if "jsonld" in formats or "json-ld" in formats:
        path = OUTPUT_DIR / f"{base}.jsonld"
        path.write_text(to_jsonld(result), encoding="utf-8")
        saved.append(str(path))

    return saved


async def run_review(args: argparse.Namespace):
    """Execute the PRISMA review pipeline."""
    # get_api_key() is intentionally first — fails fast before any pipeline construction
    api_key = get_api_key(args.api_key)

    # Build protocol
    if args.interactive:
        protocol = build_protocol_interactive()
        # Apply cache flags after the interactive build so users can still
        # combine `--interactive` with `--pg-dsn` etc.
        protocol.pg_dsn = args.pg_dsn or os.environ.get("SYNTHSCHOLAR_PG_DSN", "")
        protocol.force_refresh = args.force_refresh
        protocol.cache_threshold = args.cache_threshold
        protocol.cache_ttl_days = args.cache_ttl_days
    else:
        if not args.title:
            print("ERROR: --title is required (or use --interactive)")
            sys.exit(1)
        protocol = build_protocol_from_args(args)

    # Data extraction items
    data_items = None
    if args.extract_data:
        data_items = [
            "Study design", "Sample size", "Population",
            "Intervention", "Comparator", "Primary outcomes",
            "Effect sizes", "Follow-up duration",
        ]

    print("\n" + "=" * 60)
    print(f"  Starting PRISMA Review: {protocol.title}")
    print(f"  Model: {args.model}")
    print(f"  Databases: {', '.join(protocol.databases)}")
    print(f"  Max results/query: {args.max_results}")
    print(f"  Citation hops: {protocol.max_hops}")
    print(f"  RoB tool: {protocol.rob_tool.value}")
    print(f"  Concurrency: {protocol.article_concurrency} parallel LLM calls")
    print("=" * 60 + "\n")

    # Auth resolution: CLI flag wins, otherwise fall back to env var. The
    # pipeline's own _resolve_*() helpers apply the same precedence, but we
    # do it here too so the env-var lookup happens once at the entry point.
    ncbi_key = args.ncbi_api_key or os.environ.get("NCBI_API_KEY", "")
    email = args.email or os.environ.get("SYNTHSCHOLAR_EMAIL", "")
    cli_oa_keys: dict[str, str] = {}
    if args.semantic_scholar_key:
        cli_oa_keys["semantic_scholar"] = args.semantic_scholar_key
    if args.core_key:
        cli_oa_keys["core"] = args.core_key

    # Institutional access for paywalled articles. Credentials are env-only
    # (EZPROXY_COOKIE / EZPROXY_COOKIE_FILE) — a session cookie passed on the
    # command line would be captured verbatim in the run's provenance.
    ezproxy_cfg = build_ezproxy_config(args)
    print(f"  Institutional access: {describe_ezproxy(ezproxy_cfg)}")

    pipeline = PRISMAReviewPipeline(
        api_key=api_key,
        model_name=args.model,
        ncbi_api_key=ncbi_key,
        email=email,
        api_keys=cli_oa_keys or None,  # None → resolver reads env vars itself
        protocol=protocol,
        enable_cache=not args.no_cache,
        max_per_query=args.max_results,
        related_depth=args.related_depth,
        biorxiv_days=args.biorxiv_days,
        ezproxy=ezproxy_cfg,
    )

    cb = None if args.auto else _cli_confirm

    # ── Compare mode ──
    if getattr(args, "compare_models", None):
        if len(args.compare_models) < 2:
            print("ERROR: Compare mode requires at least 2 models.")
            sys.exit(1)
        print(f"  Compare mode: {', '.join(args.compare_models)}")
        try:
            compare_result = await pipeline.run_compare(
                args.compare_models,
                data_items=data_items,
                auto_confirm=args.auto,
                confirm_callback=cb,
                max_plan_iterations=args.max_plan_iterations,
            )
        except Exception as e:
            print(f"\nCompare run failed: {e}")
            sys.exit(1)

        OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = (compare_result.protocol.title or "review")[:40].replace(" ", "_").lower()
        base = f"{slug}_{ts}"

        compare_md_path = OUTPUT_DIR / f"{base}_compare.md"
        compare_md_path.write_text(to_compare_markdown(compare_result), encoding="utf-8")
        print(f"\nCompare report: {compare_md_path}")

        compare_json_path = OUTPUT_DIR / f"{base}_compare.json"
        compare_json_path.write_text(to_compare_json(compare_result), encoding="utf-8")
        print(f"Compare JSON:   {compare_json_path}")

        for run in compare_result.model_results:
            if run.succeeded and run.result:
                model_short = run.model_name.rsplit("/", 1)[-1]
                model_path = OUTPUT_DIR / f"{base}_{model_short}.md"
                model_path.write_text(to_markdown(run.result), encoding="utf-8")
                print(f"Model report ({run.model_name}): {model_path}")

        print("\nCompare run complete.")
        return compare_result

    # ── Single-model mode ──
    try:
        result = await pipeline.run(
            data_items=data_items,
            auto_confirm=args.auto,
            confirm_callback=cb,
            max_plan_iterations=args.max_plan_iterations,
        )
    except PlanRejectedError as e:
        print(f"\nPlan rejected after {e.iterations} iteration(s).")
        sys.exit(1)
    except MaxIterationsReachedError as e:
        print(f"\nMaximum re-generation limit ({e.max_allowed}) reached. Aborting.")
        sys.exit(1)

    # Print summary
    f = result.flow
    print("\n" + "=" * 60)
    print("  PRISMA Flow Summary")
    print("=" * 60)
    print(f"  Identified:      {f.total_identified}")
    print(f"  After dedup:     {f.after_dedup}")
    print(f"  Screened:        {f.screened_title_abstract}")
    print(f"  Excluded (TA):   {f.excluded_title_abstract}")
    print(f"  FT assessed:     {f.assessed_eligibility}")
    print(f"  Excluded (FT):   {f.excluded_eligibility}")
    print(f"  INCLUDED:        {f.included_synthesis}")
    print("=" * 60)

    if result.included_articles:
        print(f"\nIncluded studies ({len(result.included_articles)}):")
        for a in result.included_articles:
            rob = a.risk_of_bias.overall.value if a.risk_of_bias else "?"
            print(f"  [{a.pmid}] {a.short_author} ({a.year}) — RoB: {rob}")

    if result.evidence_spans:
        print(f"\nTop evidence spans ({len(result.evidence_spans)}):")
        for e in result.evidence_spans[:5]:
            print(f"  [{e.paper_pmid}, {e.relevance_score:.2f}] {e.text[:100]}...")

    # Print synthesis preview
    if result.synthesis_text:
        print("\n" + "-" * 60)
        print("SYNTHESIS (first 500 chars):")
        print("-" * 60)
        print(result.synthesis_text[:500] + "...")

    # Export
    formats = args.export or ["md"]
    saved = save_exports(result, formats)
    if saved:
        print(f"\nExported to:")
        for p in saved:
            print(f"  {p}")

    # Pyoxigraph RDF store + optional push to external graph DB.
    # CLI args override env-var defaults loaded into GraphDBConfig.
    from synthscholar.ontology.rdf_push import GraphDBConfig

    cfg = GraphDBConfig.from_env()
    if args.graph_db_host:        cfg.host = args.graph_db_host
    if args.graph_db_path:        cfg.path = args.graph_db_path
    if args.graph_db_endpoint:    cfg.endpoint = args.graph_db_endpoint
    if args.graph_db_user:        cfg.user = args.graph_db_user
    if args.graph_db_password is not None:
        cfg.password = args.graph_db_password
    if args.graph_db_auth:        cfg.auth = args.graph_db_auth
    if args.graph_db_named_graph: cfg.named_graph = args.graph_db_named_graph
    if args.graph_db_protocol:    cfg.protocol = args.graph_db_protocol
    if args.graph_db_replace:     cfg.replace = True
    # Merge per-flag --graph-db-header KEY:VALUE on top of env-loaded headers.
    for spec in args.graph_db_header or []:
        if ":" not in spec:
            print(f"\nWARNING: ignoring malformed --graph-db-header {spec!r} "
                  "(expected KEY:VALUE)", file=sys.stderr)
            continue
        key, _, value = spec.partition(":")
        cfg.headers[key.strip()] = value.strip()

    needs_store = bool(args.rdf_store_path or cfg.is_configured())
    if needs_store:
        store = to_oxigraph_store(result)

        if args.rdf_store_path:
            store.save(args.rdf_store_path)
            print(f"\nRDF store saved to: {args.rdf_store_path}")

        if cfg.is_configured():
            try:
                status = store.push_with_config(cfg)
                target = cfg.resolved_endpoint
                if cfg.named_graph:
                    target += f" (graph <{cfg.named_graph}>)"
                print(f"\nPushed RDF to {target} — HTTP {status}")
            except Exception as exc:
                print(f"\nWARNING: graph DB push failed: {exc}", file=sys.stderr)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="SynthScholar — PRISMA 2020 systematic review agent (Pydantic AI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python main.py --title "CRISPR gene therapy" --inclusion "Clinical trials" --exclusion "Reviews"
  python main.py --interactive
  python main.py --title "ML drug discovery" --model google/gemini-2.5-pro --export md json bib
        """,
    )

    # Protocol
    parser.add_argument("--title", "-t", type=str, default="",
                        help="Review title / research question")
    parser.add_argument("--objective", type=str, default="",
                        help="Detailed objective (defaults to title)")
    parser.add_argument("--population", type=str, default="")
    parser.add_argument("--intervention", type=str, default="")
    parser.add_argument("--comparison", type=str, default="")
    parser.add_argument("--outcome", type=str, default="")
    parser.add_argument("--inclusion", type=str, default="",
                        help="Inclusion criteria")
    parser.add_argument("--exclusion", type=str, default="",
                        help="Exclusion criteria")
    parser.add_argument("--registration", type=str, default="",
                        help="Registration number (e.g., PROSPERO)")

    # Search settings
    parser.add_argument("--model", "-m", type=str, default="anthropic/claude-sonnet-4",
                        help="OpenRouter model name")
    parser.add_argument("--databases", nargs="+", default=["PubMed", "bioRxiv"],
                        help="Databases to search")
    parser.add_argument("--max-results", type=int, default=20,
                        help="Max results per query")
    parser.add_argument("--related-depth", type=int, default=1,
                        help="Related article search depth")
    parser.add_argument("--hops", type=int, default=10,
                        help="Multi-hop citation depth (0-4)")
    parser.add_argument("--biorxiv-days", type=int, default=180,
                        help="bioRxiv lookback days")
    parser.add_argument("--date-start", type=str, default="",
                        help="Date range start (YYYY-MM-DD)")
    parser.add_argument("--date-end", type=str, default="",
                        help="Date range end (YYYY-MM-DD)")
    parser.add_argument("--rob-tool", type=str, default="RoB 2",
                        choices=ROB_TOOL_CHOICES,
                        help="Risk of bias assessment tool")

    # Per-group analysis (synthesis + Q&A per bucket)
    parser.add_argument("--group-by", type=str, default="disorder_cohort",
                        metavar="ATTR",
                        help=("DataChartingRubric attribute used to bucket the "
                              "included corpus (default: disorder_cohort). "
                              "Use 'study_design', 'country_region', "
                              "'task_type', etc. for non-disorder splits."))
    parser.add_argument("--default-group-question", action="append", default=[],
                        metavar="QUESTION",
                        help=("Question asked of every group that has no "
                              "--group-question override. Repeatable. "
                              "Example: --default-group-question 'What sample "
                              "sizes are reported?'. Hard-capped at 10."))
    parser.add_argument("--group-question", action="append", default=[],
                        nargs=2, metavar=("LABEL", "QUESTION"),
                        help=("Per-group question override. "
                              "LABEL matches a bucket label (case-insensitive). "
                              "Repeatable; multiple uses for the same LABEL "
                              "stack as that group's question list. "
                              "Example: --group-question Depression 'What "
                              "severity scales are used?'. Cap: 10 per group."))

    # Pipeline options
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable the SQLite article-fetch cache (per-run, in-process)")

    # PostgreSQL result-cache + checkpointing (separate from the SQLite cache above).
    parser.add_argument("--pg-dsn", type=str, default="",
                        help="PostgreSQL DSN that activates the protocol-similarity result cache, "
                             "the article store, and per-stage resumable checkpoints. "
                             "Falls back to the SYNTHSCHOLAR_PG_DSN env var. Empty disables Postgres caching.")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Bypass any matching cache entry and overwrite it on completion. "
                             "Only meaningful with --pg-dsn (or SYNTHSCHOLAR_PG_DSN) set.")
    parser.add_argument("--cache-threshold", type=float, default=0.95,
                        help="Min protocol-similarity score (0.0–1.0) for a Postgres cache hit. "
                             "Default 0.95.")
    parser.add_argument("--cache-ttl-days", type=int, default=30,
                        help="Postgres cache TTL in days; 0 = never expire. Default 30.")
    parser.add_argument("--extract-data", action="store_true",
                        help="Enable per-study data extraction")
    parser.add_argument("--concurrency", type=int, default=5, metavar="N",
                        help="Max concurrent LLM calls for per-article steps (screening, extraction, RoB, charting, appraisal, narrative). Default: 5. Max: 20.")
    parser.add_argument("--max-articles", type=int, default=None, metavar="N",
                        help="After deduplication, rerank articles by relevance and keep only the top N. Useful for quick/test runs.")

    # Full-text reading budgets — every included article is read end to end for
    # evidence extraction; these bound how that reading is chunked and capped.
    parser.add_argument("--evidence-chunk-chars", type=int, default=12000, metavar="N",
                        help="Characters of full text per evidence-extraction call. "
                             "Every chunk of every article is processed. Default: 12000.")
    parser.add_argument("--evidence-max-chars", type=int, default=0, metavar="N",
                        help="Cap on how much of each article's full text evidence "
                             "extraction reads; 0 = the whole document. Default: 0.")
    parser.add_argument("--evidence-spans-per-article", type=int, default=8, metavar="N",
                        help="Max grounded evidence spans kept per included article. Default: 8.")
    parser.add_argument("--article-text-budget", type=int, default=16000, metavar="N",
                        help="Characters of full text given to risk-of-bias, data "
                             "extraction and charting (head+tail window). Default: 16000.")

    # Institutional access (EZproxy) for paywalled articles. The session cookie
    # is env-only (EZPROXY_COOKIE / EZPROXY_COOKIE_FILE) so it never lands in
    # the recorded command line.
    parser.add_argument("--ezproxy-host", type=str, default="", metavar="HOST",
                        help="Institutional EZproxy host (e.g. ezproxy.myuni.edu). "
                             "Overrides $EZPROXY_HOST. Needs a session cookie in "
                             "$EZPROXY_COOKIE_FILE or $EZPROXY_COOKIE.")
    parser.add_argument("--ezproxy-mode", type=str, default="",
                        choices=["hostname-suffix", "login-url"],
                        help="URL rewrite convention your library uses. "
                             "Default: hostname-suffix.")
    parser.add_argument("--ezproxy-cookie-file", type=str, default="", metavar="PATH",
                        help="Netscape cookies.txt exported from a browser session "
                             "logged into the proxy. Overrides $EZPROXY_COOKIE_FILE.")
    parser.add_argument("--ezproxy-delay", type=float, default=None, metavar="SECONDS",
                        help="Delay between proxied requests. Default: 3.")
    parser.add_argument("--ezproxy-max-requests", type=int, default=None, metavar="N",
                        help="Per-run ceiling on proxied requests — keep it to the "
                             "size of the review you are actually conducting. Default: 100.")
    parser.add_argument("--ezproxy-status", action="store_true",
                        help="Print whether institutional access is usable, then exit.")

    # Output
    parser.add_argument("--export", "-e", nargs="+", default=["md"],
                        choices=["md", "markdown", "json", "bib", "bibtex",
                                 "ttl", "turtle", "jsonld", "json-ld"],
                        help="Export formats (ttl/turtle = Turtle RDF, jsonld/json-ld = JSON-LD)")

    parser.add_argument("--rdf-store-path", type=str, default=None,
                        help="Persist pyoxigraph RDF store to this Turtle file path after export")

    # Push to an external SPARQL-compatible graph database.
    # Endpoint can be specified either as a full URL (--graph-db-endpoint) or
    # composed from --graph-db-host + --graph-db-path. Likewise, auth can be
    # given as a single --graph-db-auth string OR --graph-db-user/password.
    # Each flag has a matching PRISMA_GRAPH_DB_* env var.
    parser.add_argument("--graph-db-host", type=str, default=None,
                        help="Graph DB base URL, e.g. 'http://localhost:7878'. "
                             "Combined with --graph-db-path to form the endpoint. "
                             "Falls back to PRISMA_GRAPH_DB_HOST.")
    parser.add_argument("--graph-db-path", type=str, default=None,
                        help="Path appended to --graph-db-host (default '/store' for "
                             "Oxigraph GSP; use '/update' for SPARQL Update endpoints, "
                             "or a Fuseki/GraphDB-specific path). "
                             "Falls back to PRISMA_GRAPH_DB_PATH.")
    parser.add_argument("--graph-db-endpoint", type=str, default=None,
                        help="Full graph DB endpoint URL — overrides --graph-db-host+path. "
                             "Falls back to PRISMA_GRAPH_DB_ENDPOINT.")
    parser.add_argument("--graph-db-protocol", choices=["gsp", "update"], default=None,
                        help="Wire protocol. 'gsp' = SPARQL 1.1 Graph Store HTTP Protocol "
                             "(default); 'update' = SPARQL 1.1 Update INSERT DATA. "
                             "Falls back to PRISMA_GRAPH_DB_PROTOCOL.")
    parser.add_argument("--graph-db-named-graph", type=str, default=None,
                        help="Optional named graph IRI to load the triples into. "
                             "Falls back to PRISMA_GRAPH_DB_NAMED_GRAPH.")
    parser.add_argument("--graph-db-user", type=str, default=None,
                        help="Basic-auth username. Combined with --graph-db-password. "
                             "Falls back to PRISMA_GRAPH_DB_USER.")
    parser.add_argument("--graph-db-password", type=str, default=None,
                        help="Basic-auth password. Falls back to PRISMA_GRAPH_DB_PASSWORD.")
    parser.add_argument("--graph-db-auth", type=str, default=None,
                        help="Full auth string — overrides --graph-db-user/password. "
                             "Forms: 'user:password' (Basic), 'Bearer <token>'. "
                             "Falls back to PRISMA_GRAPH_DB_AUTH.")
    parser.add_argument("--graph-db-header", action="append", default=[], metavar="KEY:VALUE",
                        help="Extra HTTP header for the graph DB push, e.g. "
                             "'X-API-Key:abc123' or 'X-GraphDB-Repository-Token:xyz'. "
                             "Repeat for multiple headers. "
                             "Falls back to PRISMA_GRAPH_DB_HEADERS (semicolon-separated).")
    parser.add_argument("--graph-db-replace", action="store_true",
                        help="Use HTTP PUT (replace graph) instead of POST (append). "
                             "Only meaningful with --graph-db-protocol=gsp.")

    # Mode
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="Interactive protocol setup")
    parser.add_argument("--api-key", type=str, default="",
                        help="OpenRouter API key (overrides OPENROUTER_API_KEY env var)")
    parser.add_argument("--ncbi-api-key", type=str, default="",
                        help="NCBI E-utilities API key (overrides NCBI_API_KEY env var) — "
                             "lifts PubMed rate limit 3 → 10 req/s")
    parser.add_argument("--email", type=str, default="",
                        help="Polite-pool contact email (overrides SYNTHSCHOLAR_EMAIL env var) — "
                             "used in User-Agent and as Unpaywall's required email parameter")
    parser.add_argument("--semantic-scholar-key", type=str, default="",
                        help="Semantic Scholar API key (overrides SEMANTIC_SCHOLAR_API_KEY env var) — "
                             "raises rate limits in the DOI resolver chain")
    parser.add_argument("--core-key", type=str, default="",
                        help="CORE API key (overrides CORE_API_KEY env var) — "
                             "enables the CORE OA aggregator (silent without a key)")
    parser.add_argument("--auto", action="store_true", default=False,
                        help="Skip plan confirmation and run pipeline end-to-end without prompts")
    parser.add_argument("--max-plan-iterations", type=int, default=3,
                        help="Maximum plan re-generation attempts before aborting (default 3)")
    parser.add_argument("--compare-models", nargs="+", metavar="MODEL", default=None,
                        help="Run compare mode with 2+ models, e.g. anthropic/claude-sonnet-4 openai/gpt-4o")

    args = parser.parse_args()

    # Diagnostic: report institutional-access readiness without running a review.
    if args.ezproxy_status:
        print(describe_ezproxy(build_ezproxy_config(args)))
        return

    asyncio.run(run_review(args))


if __name__ == "__main__":
    main()
