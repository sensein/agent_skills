#!/usr/bin/env python3
"""Organise a review's research-question answers question-first, after the fact.

``protocol.research_questions`` is asked of every included study and charted
into ``DataChartingRubric.custom_fields``. Correct storage, unreadable on its
own: the Markdown carries all of a study's answers in one narrative-table cell,
the JSON has them as an unlabelled dict per study, and the RDF not at all. So
every export also carries a question-first view — an appendix in the Markdown,
a ``research_questions`` block in the JSON, and ``slr:ResearchQuestion`` nodes
in the RDF.

**The exporters do that themselves** (``synthscholar.research_questions``, run
from ``to_markdown`` / ``to_json`` / the RDF graph builder), so it applies to
every review however the papers were found — a corpus the user supplied, or the
pipeline's own searches. Nothing here needs to be run for a fresh export.

This script is for the retrofit case: a review exported *before* that existed,
or one whose sections you want labelled with themes the protocol didn't declare.

    python research_questions.py out/review.json --outdir out/
    python research_questions.py out/review.json --outdir out/ --themes themes.json
    python research_questions.py out/review.json --print-index   # inspect, write nothing

``--themes`` is a ``{"RQ1": "Participants", …}`` map (by question id or by its
major group) overriding the section labels; without it each question's declared
``theme`` is used, falling back to its major group id.

Prefer declaring the theme on the question itself in the protocol — a themes
file lives outside the review and won't survive into anyone else's re-export.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

try:  # canonical implementation — shared with the package's own exporters
    from synthscholar.research_questions import (  # noqa: F401
        APPENDIX_TITLE, add_to_graph, build_index, merge_json, merge_markdown,
    )
    PACKAGE_MERGES_EXPORTS = True
except ImportError:  # pragma: no cover - only on a synthscholar predating it
    PACKAGE_MERGES_EXPORTS = False

_STALE = (
    "The installed synthscholar has no research_questions module, so the "
    "exports carry the charted answers only inside each study's custom_fields.\n"
    "Install the development checkout to get the question-first view:\n"
    "  pip install -e /path/to/prisma-review-agent"
)


def merge_rdf(text: str, index: dict[str, Any], fmt: str = "turtle") -> str:
    """Add the question nodes to an already-serialised graph and re-serialise it.

    The exporters add them while the graph is still in memory; this is the
    on-disk equivalent, for a ``.ttl`` that was written before that.
    """
    if not index:
        return text
    import rdflib

    g = rdflib.Graph()
    g.parse(data=text, format=fmt)
    add_to_graph(g, index)
    out = g.serialize(format=fmt)
    return out if isinstance(out, str) else out.decode("utf-8")


_HANDLERS = {
    ".md": lambda t, ix: merge_markdown(t, ix),
    ".json": lambda t, ix: merge_json(t, ix),
    ".ttl": lambda t, ix: merge_rdf(t, ix, "turtle"),
    ".jsonld": lambda t, ix: merge_rdf(t, ix, "json-ld"),
}


def merge_into_files(paths: Iterable[str | Path], index: dict[str, Any],
                     base: str = "review") -> list[str]:
    """Merge *index* into the main ``review.{md,json,ttl,jsonld}`` exports.

    Only the primary documents are touched — ``review.narrative.md``,
    ``review.charting.json`` and friends are per-article views by design.
    Every merge is idempotent, so running this over already-merged exports
    replaces the view rather than stacking a second copy.
    """
    if not index:
        return []
    if not PACKAGE_MERGES_EXPORTS:
        print(f"  WARN  {_STALE}", file=sys.stderr)
        return []
    merged: list[str] = []
    for path in paths:
        p = Path(path)
        if p.stem != base or p.suffix not in _HANDLERS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
            p.write_text(_HANDLERS[p.suffix](text, index), encoding="utf-8")
        except Exception as e:  # never lose a written export to a merge failure
            print(f"  WARN  research-question merge skipped for {p.name}: {e}",
                  file=sys.stderr)
            continue
        merged.append(str(p))
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review", help="the review JSON (a PRISMAReviewResult)")
    ap.add_argument("--outdir", help="directory holding the exports to merge into "
                                     "(default: the review JSON's own directory)")
    ap.add_argument("--base", default="review", help="output filename stem")
    ap.add_argument("--themes", help='JSON map {"RQ1": "Participants", …} overriding '
                                     "the section labels, by id or major group")
    ap.add_argument("--print-index", action="store_true",
                    help="print the question index as JSON and exit")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from export_review import _require_synthscholar, load_result  # noqa: E402

    _require_synthscholar()
    if not PACKAGE_MERGES_EXPORTS:
        print(_STALE, file=sys.stderr)
        return 3

    result = load_result(args.review)
    themes = json.loads(Path(args.themes).read_text(encoding="utf-8")) if args.themes else None
    index = build_index(result, themes)

    if not index:
        print("No charted research questions. Add protocol.research_questions "
              "and re-run the charting stage — the answers live in each "
              "rubric's custom_fields.", file=sys.stderr)
        return 1

    if args.print_index:
        print(json.dumps(index, indent=2, ensure_ascii=False))
        return 0

    outdir = Path(args.outdir or Path(args.review).parent)
    candidates = [outdir / f"{args.base}{s}" for s in _HANDLERS]
    merged = merge_into_files([p for p in candidates if p.exists()], index, base=args.base)
    if not merged:
        print(f"No {args.base}.md / .json / .ttl / .jsonld found in {outdir}",
              file=sys.stderr)
        return 1

    print(f"Organised {index['n_questions']} research questions × "
          f"{sum(q['n_studies_charted'] for q in index['questions'])} answers into:")
    for p in merged:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
