#!/usr/bin/env python3
"""Add or correct the search provenance on a finished review, then re-export.

For the bring-your-own-corpus workflow (SKILL.md → Mode 3): the user supplies
the PDFs, so *they* hold the search strategy. If they didn't have their
keywords, databases, dates and record counts to hand when the review ran, the
review is still complete — the provenance is just missing. This script fills
it in afterwards and regenerates the Markdown and Turtle, so the reported
PRISMA search strategy ends up identical to a run that had it from the start.

It writes only provenance slots — ``search_queries``, ``search_iterations``,
identification-stage ``flow`` counts, ``protocol.databases`` / date range, and
the PRISMA registration/funding items. Screening decisions, charting,
appraisal and synthesis are never touched.

Usage:
    # fill in from a search-provenance file, re-exporting md/ttl/json
    python update_provenance.py out/review.json \
        --provenance search_provenance.json --outdir out/

    # quick edits without a file
    python update_provenance.py out/review.json \
        --query 'PubMed (searched 2026-07-01): depression AND voice | 412 records' \
        --database PubMed --database Scopus \
        --total-identified 480 --duplicates-removed 57 \
        --registration CRD42026123456 --outdir out/

    # blank provenance file to fill in
    python run_local_review.py --print-provenance-template > search_provenance.json

By default supplied queries are appended to whatever is already recorded;
``--replace-queries`` clears the existing list first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_review import ALL_FORMATS, load_result, write_exports  # noqa: E402
from run_local_review import (  # noqa: E402
    _PRE_RUN_FLOW, apply_declared_db_counts, apply_user_flow,
    build_search_iterations, describe_searches,
)


def apply_provenance(result, provenance: dict, args: argparse.Namespace) -> list[str]:
    """Mutate *result* in place; return a human-readable list of changes."""
    changes: list[str] = []

    # ── Search strategy ──
    queries = describe_searches(provenance) + list(args.query or [])
    if queries:
        if args.replace_queries:
            result.search_queries = []
        existing = set(result.search_queries)
        added = [q for q in queries if q not in existing]
        result.search_queries.extend(added)
        changes.append(f"search_queries: +{len(added)} "
                       f"({len(result.search_queries)} total)")

    iterations = build_search_iterations(provenance)
    if iterations:
        # Re-index so appended searches keep a monotonic iteration_index.
        offset = 0 if args.replace_queries else len(result.search_iterations)
        if args.replace_queries:
            result.search_iterations = []
        for it in iterations:
            it.iteration_index += offset
        result.search_iterations.extend(iterations)
        changes.append(f"search_iterations: +{len(iterations)}")

    # ── Identification-stage flow counts ──
    if apply_declared_db_counts(result.flow, provenance):
        per_db = ", ".join(f"{k}={v}" for k, v in result.flow.db_other_sources.items())
        changes.append(f"flow per-database counts from the user's searches ({per_db})")
    for key, attr in _PRE_RUN_FLOW.items():
        cli = getattr(args, key, None)
        value = cli if cli is not None else provenance.get(key)
        if value:
            setattr(result.flow, attr, int(value))
            changes.append(f"flow.{attr} = {int(value)}")
    applied = apply_user_flow(result.flow, provenance)
    changes.extend(f"flow.{a} (user-declared)" for a in applied)

    # after_dedup must stay consistent with the numbers above.
    if result.flow.total_identified:
        after = result.flow.total_identified - result.flow.duplicates_removed
        result.flow.after_dedup = max(after, len(result.included_articles))
        changes.append(f"flow.after_dedup = {result.flow.after_dedup}")

    # ── Protocol-level reporting items ──
    databases = list(args.database or []) or [
        str(s.get("database") or "").strip()
        for s in provenance.get("searches", [])
        if str(s.get("database") or "").strip()
    ]
    if databases:
        seen: set[str] = set()
        result.protocol.databases = [d for d in databases if not (d in seen or seen.add(d))]
        changes.append(f"protocol.databases = {', '.join(result.protocol.databases)}")

    for attr, value in (
        ("date_range_start", args.date_start or provenance.get("date_range_start")),
        ("date_range_end", args.date_end or provenance.get("date_range_end")),
        ("registration_number", args.registration),
        ("protocol_url", args.protocol_url),
        ("funding_sources", args.funding),
        ("competing_interests", args.competing_interests),
        ("amendments", args.amendments),
    ):
        if value:
            setattr(result.protocol, attr, value)
            changes.append(f"protocol.{attr} = {str(value)[:60]}")

    # Keep the run-configuration snapshot honest about the late edit.
    if changes and result.run_configuration is not None:
        result.run_configuration.pipeline_kwargs["provenance_updated_by"] = (
            "synthscholar-skills/update_provenance.py"
        )

    return changes


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("review", help="review result JSON to update")
    ap.add_argument("--provenance", default="", help="search-provenance JSON")
    ap.add_argument("--query", action="append", default=[], metavar="TEXT",
                    help="one search-strategy line (repeatable)")
    ap.add_argument("--replace-queries", action="store_true",
                    help="clear existing search_queries / search_iterations first")
    ap.add_argument("--database", action="append", default=[], metavar="NAME",
                    help="database searched (repeatable; any name, e.g. Scopus)")
    ap.add_argument("--total-identified", dest="total_identified", type=int, default=None,
                    help="records identified across all searches")
    ap.add_argument("--duplicates-removed", dest="duplicates_removed", type=int, default=None,
                    help="duplicate records removed before screening")
    ap.add_argument("--date-start", default="", help="publication date range start")
    ap.add_argument("--date-end", default="", help="publication date range end")
    ap.add_argument("--registration", default="", help="PROSPERO / registry ID")
    ap.add_argument("--protocol-url", default="", help="protocol URL")
    ap.add_argument("--funding", default="", help="funding sources")
    ap.add_argument("--competing-interests", default="", help="competing interests")
    ap.add_argument("--amendments", default="", help="protocol amendments")
    ap.add_argument("--outdir", default="", help="re-export here (default: alongside the review)")
    ap.add_argument("--base", default="", help="output filename stem (default: the review's)")
    ap.add_argument("--formats", nargs="+", default=["md", "ttl", "json"], metavar="FMT",
                    help=f"any of: {' '.join(ALL_FORMATS)}")
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    args = ap.parse_args()

    unknown = [f for f in args.formats if f not in ALL_FORMATS]
    if unknown:
        ap.error(f"unknown format(s): {', '.join(unknown)}")

    provenance = (
        json.loads(Path(args.provenance).read_text(encoding="utf-8"))
        if args.provenance else {}
    )
    result = load_result(args.review)
    changes = apply_provenance(result, provenance, args)

    if not changes:
        print("Nothing to update — no provenance values supplied.")
        return 0
    print("Applied:")
    for c in changes:
        print(f"  {c}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    review_path = Path(args.review)
    outdir = args.outdir or str(review_path.parent)
    base = args.base or review_path.stem
    written = write_exports(result, outdir, args.formats, base=base)
    print("\nRe-exported:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
