#!/usr/bin/env python3
"""Validate a review result and export it to Markdown / Turtle / JSON-LD / BibTeX.

The single export surface for both bring-your-own-corpus paths (SKILL.md →
Mode 3): the pipeline path (``run_local_review.py`` calls this module's
``write_exports``) and the agent-authored path (the agent writes a
``PRISMAReviewResult`` JSON by hand and runs this script over it).

Everything goes through ``synthscholar.export``, so a hand-authored review
serialises to exactly the same Markdown and the same SLR-ontology Turtle as
one produced by the full application — which is what makes the ``.ttl``
safe to ingest into a triple store.

Usage:
    # validate + report what the result contains
    python export_review.py review.json --check

    # export (default: md + ttl + json)
    python export_review.py review.json --outdir out/
    python export_review.py review.json --formats md ttl jsonld bib \
        charting appraisal per-group narrative --outdir out/

    # a filled-in, schema-valid skeleton to author a review against
    python export_review.py --print-template > review.json

Formats:
    md         PRISMA 2020 review document (to_markdown)
    ttl        SLR-ontology RDF, ready for triple-store ingestion (to_turtle)
    jsonld     same graph as JSON-LD
    json       the validated PRISMAReviewResult itself
    bib        BibTeX of included studies
    charting   per-article data-charting tables (sections A–G)
    appraisal  critical-appraisal tables
    per-group  per-group synthesis + Q&A
    narrative  condensed narrative summary (md + json)

Requires the ``synthscholar`` package importable (``pip install synthscholar``
or run from a checkout).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ALL_FORMATS = [
    "md", "ttl", "jsonld", "json", "bib",
    "charting", "appraisal", "per-group", "narrative",
]
DEFAULT_FORMATS = ["md", "ttl", "json"]


# Fields the retrieval / screening-basis provenance depends on. Pydantic drops
# unknown fields silently, so an older synthscholar would accept the data and
# discard exactly these — producing a review that looks complete but records
# nothing about what was actually read. Detected by feature, not by version
# string, because the package's __version__ and pyproject version have drifted.
REQUIRED_FLOW_FIELDS = ("full_text_retrieved", "full_text_sources",
                        "assessed_on_full_text", "assessed_on_abstract_only",
                        "included_with_full_text", "excluded_reasons_full_text")
REQUIRED_LOG_FIELDS = ("assessed_on", "full_text_source")


def _require_synthscholar():
    try:
        import synthscholar.export  # noqa: F401
        import synthscholar.models  # noqa: F401
    except ImportError as e:
        print(
            f"synthscholar is not importable ({e}).\n"
            "Install it with `pip install synthscholar` (add the [fulltext] extra "
            "for PDF parsing) or run this script from a checkout of the repo.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from synthscholar.models import PRISMAFlowCounts, ScreeningLogEntry

    missing = [f"PRISMAFlowCounts.{f}" for f in REQUIRED_FLOW_FIELDS
               if f not in PRISMAFlowCounts.model_fields]
    missing += [f"ScreeningLogEntry.{f}" for f in REQUIRED_LOG_FIELDS
                if f not in ScreeningLogEntry.model_fields]
    if missing:
        import synthscholar
        print(
            f"The installed synthscholar ({getattr(synthscholar, '__version__', 'unknown')}) "
            "predates this skill's provenance fields:\n  "
            + "\n  ".join(missing)
            + "\n\nPydantic discards unknown fields silently, so exports would look "
              "complete while recording nothing about which reports were retrieved "
              "or whether each decision was made on the full text. Install the "
              "development checkout instead:\n"
              "  pip install -e /path/to/prisma-review-agent",
            file=sys.stderr,
        )
        raise SystemExit(3)


def load_result(path: str):
    """Parse + validate a review JSON into a ``PRISMAReviewResult``."""
    from pydantic import ValidationError
    from synthscholar.models import PRISMAReviewResult

    raw = Path(path).read_text(encoding="utf-8")
    try:
        return PRISMAReviewResult.model_validate_json(raw)
    except ValidationError as e:
        print(f"✗ {path} is not a valid PRISMAReviewResult:\n", file=sys.stderr)
        for err in e.errors()[:25]:
            loc = ".".join(str(p) for p in err["loc"])
            print(f"  {loc}: {err['msg']}", file=sys.stderr)
        n = len(e.errors())
        if n > 25:
            print(f"  … and {n - 25} more", file=sys.stderr)
        raise SystemExit(1)


def write_exports(result, outdir: str | Path, formats: list[str], base: str = "review") -> list[str]:
    """Write *result* in every requested format under *outdir*; return paths."""
    import synthscholar.export as ex

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def _w(suffix: str, text: str) -> None:
        p = out / f"{base}{suffix}"
        p.write_text(text, encoding="utf-8")
        written.append(str(p))

    if "md" in formats:
        _w(".md", ex.to_markdown(result))
    if "ttl" in formats:
        _w(".ttl", ex.to_turtle(result))
    if "jsonld" in formats:
        _w(".jsonld", ex.to_jsonld(result))
    if "json" in formats:
        _w(".json", ex.to_json(result))
    if "bib" in formats:
        _w(".bib", ex.to_bibtex(result))
    if "charting" in formats:
        _w(".charting.md", ex.to_charting_markdown(result))
        _w(".charting.json", ex.to_charting_json(result))
    if "appraisal" in formats:
        _w(".appraisal.md", ex.to_appraisal_markdown(result))
        _w(".appraisal.json", ex.to_appraisal_json(result))
    if "per-group" in formats:
        _w(".per-group.md", ex.to_per_group_markdown(result))
    if "narrative" in formats:
        _w(".narrative.md", ex.to_narrative_summary_markdown(result))
        _w(".narrative.json", ex.to_narrative_summary_json(result))
    return written


def check(result) -> int:
    """Print a completeness report. Exit 1 only when the result is unusable."""
    flow = result.flow
    n_inc = len(result.included_articles)
    ft = sum(1 for a in result.included_articles if (a.full_text or "").strip())

    print("✓ Valid PRISMAReviewResult\n")
    print(f"  research_question   {result.research_question[:70] or '(empty)'}")
    print(f"  review_id           {result.protocol.review_id or '(unset — a URN is minted at export)'}")
    print(f"  included articles   {n_inc} ({ft} with full text, {n_inc - ft} abstract-only)")
    print(f"  screening log       {len(result.screening_log)} decisions")
    print(f"  evidence spans      {len(result.evidence_spans)}")
    print(f"  charting rubrics    {len(result.data_charting_rubrics)}")
    print(f"  appraisals          {len(result.critical_appraisals)} rubric / "
          f"{len(result.structured_appraisal_results)} structured")
    print(f"  narrative rows      {len(result.narrative_rows)}")
    print(f"  GRADE outcomes      {len(result.grade_assessments)}")
    print(f"  per-group analysis  "
          f"{result.per_group_analysis.n_groups if result.per_group_analysis else 0} groups")
    print(f"  search queries      {len(result.search_queries)}")
    print(f"  flow                identified={flow.total_identified} "
          f"dedup={flow.after_dedup} screened={flow.screened_title_abstract} "
          f"included={flow.included_synthesis}")
    print(f"  full text           {flow.full_text_retrieved} retrieved, "
          f"{flow.not_retrieved} not retrieved"
          + (f" — {', '.join(f'{k}: {v}' for k, v in flow.full_text_sources.items())}"
             if flow.full_text_sources else ""))
    print(f"  eligibility basis   {flow.assessed_on_full_text} on full text, "
          f"{flow.assessed_on_abstract_only} on abstract only")
    print(f"  included basis      {flow.included_with_full_text} full text, "
          f"{flow.included_abstract_only} abstract only")
    print(f"  exclusion reasons   {len(flow.excluded_reasons_title_abstract)} at screening, "
          f"{len(flow.excluded_reasons_full_text)} at eligibility")
    print(f"  assembled report    {'yes' if result.prisma_review else 'no'}")

    problems: list[str] = []
    warnings: list[str] = []

    if n_inc == 0:
        problems.append("no included_articles — nothing to export")
    if not result.research_question.strip():
        problems.append("research_question is empty")

    if not result.search_queries:
        warnings.append("search_queries is empty — the search strategy is unrecorded. "
                        "Add the user's keywords (update_provenance.py) for a "
                        "PRISMA-reportable review.")
    if flow.total_identified == 0:
        warnings.append("flow.total_identified is 0 — identification counts unrecorded")
    if flow.included_synthesis != n_inc:
        warnings.append(f"flow.included_synthesis ({flow.included_synthesis}) != "
                        f"len(included_articles) ({n_inc})")
    if not result.synthesis_text.strip():
        warnings.append("synthesis_text is empty — the Markdown will have no synthesis")
    if not result.data_charting_rubrics:
        warnings.append("no data_charting_rubrics — no per-article charting tables")
    if flow.assessed_on_abstract_only and not flow.assessed_on_full_text:
        warnings.append("every eligibility decision was made on an abstract — retrieve "
                        "full texts (fetch_ezproxy.py for paywalled papers) before "
                        "treating this as a full review")
    ft_entries = sum(1 for e in result.screening_log if e.stage.value == "full_text")
    if result.screening_log and not ft_entries:
        warnings.append("screening_log has no eligibility-stage decisions — record one "
                        "per report sought, with assessed_on set")
    if not result.critical_appraisals and not result.structured_appraisal_results:
        warnings.append("no critical appraisal — required for a PRISMA-compliant review")
    if not result.protocol.registration_number:
        warnings.append("protocol.registration_number unset (PRISMA item 24a)")

    for w in warnings:
        print(f"\n  WARN  {w}")
    for p in problems:
        print(f"\n  FAIL  {p}")
    if problems:
        print(f"\n✗ {len(problems)} blocking problem(s).")
        return 1
    print(f"\n✓ Exportable ({len(warnings)} warning(s)).")
    return 0


def build_template() -> str:
    """A schema-valid, fully-populated example result to author against.

    Built from the real models rather than hand-written JSON, so it can never
    drift out of sync with the schema this script validates against.
    """
    from synthscholar.models import (
        Article, CriticalAppraisalDomain, CriticalAppraisalItem,
        CriticalAppraisalRubric, DataChartingRubric, DomainAppraisal,
        CriticalAppraisalResult, EvidenceSpan, GRADEAssessment,
        GRADEDomainRating, GroupAnalysisEntry, GroupQuestionAnswer,
        GroupSummary, InclusionStatus, ItemRating, PerGroupAnalysis,
        PRISMAFlowCounts, PRISMANarrativeRow, PRISMAReviewResult,
        ReviewProtocol, RiskOfBiasResult, RoBDomainAssessment, RoBJudgment,
        ScreeningDecisionType, ScreeningLogEntry, ScreeningStage,
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    appraisal_rubric = CriticalAppraisalRubric(
        source_id="S-001",
        domain_1_participant_quality=CriticalAppraisalDomain(
            domain_name="Participant and Sample Quality",
            items=[CriticalAppraisalItem(
                item_text="Is the sample adequately described?",
                rating="Yes",
                notes="Full demographics reported in Table 1.",
            )],
            overall_concern="Low",
        ),
    )
    article = Article(
        pmid="local_1a2b3c4d",
        title="EXAMPLE — replace with the study title",
        abstract="Study abstract as printed in the paper.",
        authors="Smith J; Doe A",
        journal="Journal of Examples",
        year="2024",
        doi="10.1234/example.2024.001",
        keywords=["example"],
        source="user_supplied",
        full_text="Extracted PDF text (from build_corpus.py).",
        content_sha256="0" * 64,
        full_text_source="user_supplied_pdf",
        full_text_retrieved_at=now,
        inclusion_status=InclusionStatus.INCLUDED,
        risk_of_bias=RiskOfBiasResult(
            assessments=[RoBDomainAssessment(
                domain="Randomisation process",
                judgment=RoBJudgment.LOW,
                support="Computer-generated sequence, allocation concealed.",
            )],
            overall=RoBJudgment.LOW,
            summary="Low risk across all domains.",
        ),
        critical_appraisal=appraisal_rubric,
        quality_score=0.85,
    )

    return PRISMAReviewResult(
        research_question="EXAMPLE — the review's objective / research question",
        protocol=ReviewProtocol(
            title="EXAMPLE review title",
            objective="EXAMPLE — the review's objective",
            pico_population="…",
            pico_intervention="…",
            pico_comparison="…",
            pico_outcome="…",
            inclusion_criteria="…",
            exclusion_criteria="…",
            databases=["PubMed"],
            review_id="review_local_00000000_000000",
            registration_number="",
        ),
        # The user's own search — see references/byo_corpus_review.md.
        search_queries=['PubMed: ("example"[MeSH]) AND (2015:2024[dp])'],
        # Counts describe a 3-record example: 1 included after reading the full
        # text, 1 excluded at eligibility on the full text, 1 excluded at
        # title/abstract — so every retrieval / basis field is demonstrated
        # rather than left at zero for you to overlook.
        flow=PRISMAFlowCounts(
            db_other_sources={"user_supplied": 3},
            total_identified=3,
            duplicates_removed=0,
            after_dedup=3,
            screened_title_abstract=3,
            excluded_title_abstract=1,
            sought_fulltext=2,
            full_text_retrieved=2,
            not_retrieved=0,
            full_text_sources={"user_supplied_pdf": 1, "ezproxy_pdf": 1},
            assessed_eligibility=2,
            excluded_eligibility=1,
            assessed_on_full_text=2,
            assessed_on_abstract_only=0,
            included_with_full_text=1,
            included_abstract_only=0,
            excluded_reasons={"Sample below the 20-participant minimum": 1,
                              "Not human participants": 1},
            excluded_reasons_title_abstract={"Not human participants": 1},
            excluded_reasons_full_text={"Sample below the 20-participant minimum": 1},
            included_synthesis=1,
        ),
        included_articles=[article],
        # One entry per decision, at every stage. `assessed_on` must match the
        # stage: 'title_abstract' at screening; at eligibility 'full_text' when
        # the report was read, 'abstract_only' when it could not be retrieved.
        screening_log=[
            ScreeningLogEntry(
                pmid="local_1a2b3c4d",
                title="EXAMPLE — replace with the study title",
                decision=ScreeningDecisionType.INCLUDE,
                reason="Population, comparator and primary outcome all reported.",
                stage=ScreeningStage.TITLE_ABSTRACT,
                assessed_on="title_abstract",
            ),
            ScreeningLogEntry(
                pmid="local_1a2b3c4d",
                title="EXAMPLE — replace with the study title",
                decision=ScreeningDecisionType.INCLUDE,
                reason="Full text confirms a randomised design and reports sensitivity/specificity.",
                stage=ScreeningStage.FULL_TEXT,
                assessed_on="full_text",
                full_text_source="user_supplied_pdf",
            ),
            ScreeningLogEntry(
                pmid="local_5e6f7a8b",
                title="EXAMPLE — a study excluded at eligibility",
                decision=ScreeningDecisionType.EXCLUDE,
                reason="Sample below the 20-participant minimum (n=12 case series).",
                stage=ScreeningStage.FULL_TEXT,
                assessed_on="full_text",
                full_text_source="ezproxy_pdf",
            ),
            ScreeningLogEntry(
                pmid="local_9c0d1e2f",
                title="EXAMPLE — a study excluded at screening",
                decision=ScreeningDecisionType.EXCLUDE,
                reason="Not human participants (rodent model).",
                stage=ScreeningStage.TITLE_ABSTRACT,
                assessed_on="title_abstract",
            ),
        ],
        evidence_spans=[EvidenceSpan(
            text="Sensitivity was 0.91 (95% CI 0.87–0.94).",
            paper_pmid="local_1a2b3c4d",
            paper_title="EXAMPLE — replace with the study title",
            section="results",
            relevance_score=0.9,
            claim="The method achieves high sensitivity.",
            doi="10.1234/example.2024.001",
            grounding_score=1.0,
            grounded=True,
        )],
        synthesis_text="Narrative synthesis across included studies, with citations.",
        bias_assessment="Cross-study risk-of-bias summary.",
        limitations="Review-level limitations.",
        grade_assessments={"Primary outcome": GRADEAssessment(
            outcome="Primary outcome",
            domains={"Risk of bias": GRADEDomainRating(
                rating="No downgrade", explanation="Most studies low risk.",
            )},
            summary="Moderate certainty overall.",
        )},
        timestamp=now,
        data_charting_rubrics=[DataChartingRubric(
            source_id="S-001",
            title="EXAMPLE — replace with the study title",
            authors="Smith J; Doe A",
            year="2024",
            journal_conference="Journal of Examples",
            doi="10.1234/example.2024.001",
            database_retrieved="user_supplied",
            disorder_cohort="Example cohort",
            primary_focus="Diagnosis",
            study_design="Cross-sectional",
            n_disordered="42",
            summary_key_findings="Key findings for this study.",
            custom_fields={"Which instrument was used?": "Example scale"},
        )],
        narrative_rows=[PRISMANarrativeRow(
            source_id="S-001",
            study_design_sample_dataset="Cross-sectional, n=42",
            methods="…",
            outcomes="…",
            key_limitations="…",
            relevance_notes="…",
        )],
        critical_appraisals=[appraisal_rubric],
        structured_appraisal_results=[CriticalAppraisalResult(
            source_id="S-001",
            domains=[DomainAppraisal(
                domain_name="Participant and Sample Quality",
                item_ratings=[ItemRating(
                    item_text="Is the sample adequately described?", rating="Yes",
                )],
                domain_concern="Low",
            )],
        )],
        structured_abstract="Background: … Objective: … Methods: … Results: … Conclusion: …",
        introduction_text="Introduction section text.",
        conclusions_text="Conclusions section text.",
        per_group_analysis=PerGroupAnalysis(
            dimension="disorder_cohort",
            topic="EXAMPLE — the review's objective",
            n_articles_synthesized=1,
            n_groups=1,
            unlabeled_count=0,
            groups=[GroupAnalysisEntry(
                label="Example cohort",
                n_studies=1,
                summary=GroupSummary(
                    label="Example cohort",
                    n_studies=1,
                    aggregate_finding="Aggregate quantitative finding for this group.",
                    representative_pmids=["local_1a2b3c4d"],
                ),
                answers=[GroupQuestionAnswer(
                    question="What is the dominant study design?",
                    answer="Cross-sectional in 1/1 studies.",
                    supporting_pmids=["local_1a2b3c4d"],
                )],
            )],
        ),
    ).model_dump_json(indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("review", nargs="?", help="review result JSON")
    ap.add_argument("--formats", nargs="+", default=DEFAULT_FORMATS,
                    metavar="FMT", help=f"any of: {' '.join(ALL_FORMATS)} (default: "
                                        f"{' '.join(DEFAULT_FORMATS)})")
    ap.add_argument("--outdir", default="review_output", help="output directory")
    ap.add_argument("--base", default="review", help="output filename stem")
    ap.add_argument("--check", action="store_true",
                    help="validate + report completeness, write nothing")
    ap.add_argument("--print-template", action="store_true",
                    help="print a schema-valid example review JSON and exit")
    args = ap.parse_args()

    _require_synthscholar()

    if args.print_template:
        print(build_template())
        return 0
    if not args.review:
        ap.error("review JSON required (or use --print-template)")

    unknown = [f for f in args.formats if f not in ALL_FORMATS]
    if unknown:
        ap.error(f"unknown format(s): {', '.join(unknown)}; choose from {' '.join(ALL_FORMATS)}")

    result = load_result(args.review)
    if args.check:
        return check(result)

    written = write_exports(result, args.outdir, args.formats, base=args.base)
    print("Wrote:")
    for p in written:
        print(f"  {p}")
    if "ttl" in args.formats:
        print("\nThe .ttl is SLR-ontology RDF — ingest it into a triple store "
              "(e.g. the brainkb skill's ingest tools) as-is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
