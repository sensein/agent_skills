#!/usr/bin/env python3
"""Validate a SynthScholar ReviewProtocol for completeness before running.

Checks that all REQUIRED fields are present, warns on missing RECOMMENDED
fields, and validates enum-like values and caps. Mirrors the ReviewProtocol
model in synthscholar/models.py so it can run standalone (no import needed).

Usage:
    python validate_protocol.py protocol.json
    python validate_protocol.py protocol.yaml
    python validate_protocol.py --print-template > protocol.json

Exit code: 0 if all required fields present (warnings allowed), 1 otherwise.
Accepts JSON always; YAML if PyYAML is installed.
"""
from __future__ import annotations

import argparse
import json
import sys

REQUIRED = [
    ("objective", "research question / objective"),
    ("pico_population", "PICO population"),
    ("pico_intervention", "PICO intervention"),
    ("pico_outcome", "PICO outcome"),
    ("inclusion_criteria", "inclusion criteria"),
    ("exclusion_criteria", "exclusion criteria"),
]
RECOMMENDED = [
    "title", "pico_comparison", "databases", "date_range_start",
    "date_range_end", "rob_tool", "target_audience", "funding_sources",
    "competing_interests", "registration_number",
]
ROB_TOOLS = {
    "RoB 2", "Jadad Scale", "ROBINS-I", "ROBINS-E", "Newcastle-Ottawa Scale",
    "QUADAS-2", "CASP Qualitative Checklist", "JBI Critical Appraisal",
    "Murad Tool", "SYRCLE", "MINORS", "ROBIS",
}
CITATION_STYLES = {"APA 7", "Vancouver", "Harvard", "IEEE", "Chicago"}
SECTION_FORMATS = {"descriptive", "yes_no", "table", "bullet_list", "numeric"}
KNOWN_PROVIDERS = {
    "PubMed", "bioRxiv", "medRxiv", "europe_pmc", "openalex", "crossref",
    "doaj", "semantic_scholar", "arxiv", "core",
}

TEMPLATE = {
    "title": "", "objective": "",
    "pico_population": "", "pico_intervention": "",
    "pico_comparison": "", "pico_outcome": "",
    "inclusion_criteria": "", "exclusion_criteria": "",
    # The review's own questions, asked of every included study and reported
    # question-first in every export. Number and theme them.
    "research_questions": [
        {"question_id": "RQ1.1", "question": "", "theme": "", "short_title": ""},
    ],
    "databases": sorted(KNOWN_PROVIDERS),
    "date_range_start": "", "date_range_end": "",
    "max_hops": 10, "rob_tool": "RoB 2",
    "charting_questions": [],
    "grouping_dimension": "disorder_cohort",
    "default_group_questions": [], "per_group_questions": {},
    "appraisal_domains": [],
    "target_audience": "", "word_count_target": 8000, "citation_style": "APA 7",
    "section_output_formats": {},
    "registration_number": "", "protocol_url": "",
    "funding_sources": "", "competing_interests": "",
    # Reading budgets. Evidence extraction reads EVERY chunk of every article,
    # so these are the knobs that bound token cost on long papers.
    "evidence_chunk_chars": 12000,
    "evidence_chunk_overlap": 400,
    "evidence_max_chars": 0,            # 0 = read the whole article
    "evidence_spans_per_article": 8,
    "article_text_budget": 16000,       # RoB / extraction / charting window
    "article_concurrency": 5,
    "max_articles": None,               # null = no cap
}

# Positive-integer processing fields: (key, minimum, maximum or None).
_INT_RANGES = [
    ("evidence_chunk_chars", 1000, None),
    ("evidence_chunk_overlap", 0, None),
    ("evidence_max_chars", 0, None),
    ("evidence_spans_per_article", 1, None),
    ("article_text_budget", 1000, None),
    ("article_concurrency", 1, 20),
    ("max_articles", 1, None),
    ("word_count_target", 1, None),
]


def _load(path: str) -> dict:
    text = open(path).read()
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            print("PyYAML not installed; convert to JSON or `pip install pyyaml`",
                  file=sys.stderr)
            raise SystemExit(2)
        return yaml.safe_load(text)
    return json.loads(text)


def validate(p: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    def empty(v) -> bool:
        return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0)

    for key, label in REQUIRED:
        if empty(p.get(key)):
            errors.append(f"REQUIRED '{key}' ({label}) is missing/empty")

    for key in RECOMMENDED:
        if empty(p.get(key)):
            warnings.append(f"recommended '{key}' is not set")

    rob = p.get("rob_tool")
    if rob and rob not in ROB_TOOLS:
        errors.append(f"rob_tool '{rob}' invalid; one of {sorted(ROB_TOOLS)}")

    cs = p.get("citation_style")
    if cs and cs not in CITATION_STYLES:
        warnings.append(f"citation_style '{cs}' not in {sorted(CITATION_STYLES)}")

    hops = p.get("max_hops")
    if hops is not None and not (isinstance(hops, int) and 0 <= hops <= 10):
        errors.append(f"max_hops must be an integer 0–10 (got {hops!r})")

    dbs = p.get("databases") or []
    for d in dbs:
        if d not in KNOWN_PROVIDERS:
            # Expected for a user-supplied corpus (Mode 3), where `databases`
            # names whatever the user actually searched (Scopus, Embase, …).
            warnings.append(f"database '{d}' is not one of the app's discovery "
                            f"providers {sorted(KNOWN_PROVIDERS)} — fine for a "
                            f"user-supplied corpus, otherwise it won't be searched")

    # Research questions — the review's own questions, charted per article and
    # reported question-first. Malformed entries would be silently dropped by
    # Pydantic, so they're errors here rather than surprises at export.
    rqs = p.get("research_questions") or []
    if not isinstance(rqs, list):
        errors.append("research_questions must be a list of "
                      "{question_id, question, theme, short_title} objects")
        rqs = []
    seen_ids: set[str] = set()
    for i, rq in enumerate(rqs):
        where = f"research_questions[{i}]"
        if not isinstance(rq, dict):
            errors.append(f"{where} must be an object, not {type(rq).__name__}")
            continue
        if empty(rq.get("question")):
            errors.append(f"{where}.question is missing/empty")
        qid = (rq.get("question_id") or "").strip()
        if not qid:
            warnings.append(f"{where} has no question_id — number them (RQ1.1, "
                            "RQ1.2 …) so the report groups and addresses them")
        elif qid in seen_ids:
            errors.append(f"{where}.question_id '{qid}' is used twice — ids key "
                          "each article's charted answers and must be unique")
        else:
            seen_ids.add(qid)
        if empty(rq.get("theme")):
            warnings.append(f"{where} has no theme — questions sharing a theme "
                            "are reported together")
    if not rqs:
        warnings.append("no research_questions — the review will have no "
                        "question-first reporting; add them alongside PICO")

    dom = p.get("appraisal_domains") or []
    if dom and not (1 <= len(dom) <= 4):
        errors.append(f"appraisal_domains must be 1–4 names (got {len(dom)})")

    dgq = p.get("default_group_questions") or []
    if len(dgq) > 10:
        errors.append(f"default_group_questions max 10 (got {len(dgq)})")
    for label, qs in (p.get("per_group_questions") or {}).items():
        if len(qs) > 10:
            errors.append(f"per_group_questions[{label!r}] max 10 (got {len(qs)})")

    for sec, fmt in (p.get("section_output_formats") or {}).items():
        if fmt not in SECTION_FORMATS:
            errors.append(f"section_output_formats[{sec!r}]='{fmt}' invalid; "
                          f"one of {sorted(SECTION_FORMATS)}")

    for key, low, high in _INT_RANGES:
        v = p.get(key)
        if v is None:
            continue
        if not isinstance(v, int) or isinstance(v, bool):
            errors.append(f"{key} must be an integer (got {v!r})")
            continue
        if v < low or (high is not None and v > high):
            bound = f"{low}–{high}" if high is not None else f"≥ {low}"
            errors.append(f"{key} must be {bound} (got {v})")

    # A truncated read is legal but weakens the review — say so rather than
    # letting a quiet cap look like a full reading of every paper.
    cap = p.get("evidence_max_chars")
    if isinstance(cap, int) and 0 < cap < 20000:
        warnings.append(
            f"evidence_max_chars={cap} means only the first ~{cap // 1000}k characters "
            "of each article are read for evidence — results/discussion may be cut. "
            "Use 0 (whole article) unless you are deliberately bounding cost."
        )

    # Soft nudge: comparison legitimately optional, but flag if PICO looks thin.
    if empty(p.get("pico_comparison")):
        warnings.append("pico_comparison empty — fine for single-arm/scoping "
                        "reviews, otherwise consider adding one")

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("protocol", nargs="?", help="protocol .json / .yaml file")
    ap.add_argument("--print-template", action="store_true",
                    help="print a blank protocol template as JSON and exit")
    args = ap.parse_args()

    if args.print_template:
        print(json.dumps(TEMPLATE, indent=2))
        return 0
    if not args.protocol:
        ap.error("protocol file required (or use --print-template)")

    data = _load(args.protocol)
    if not isinstance(data, dict):
        print("Protocol must be a JSON/YAML object", file=sys.stderr)
        return 2

    errors, warnings = validate(data)
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  FAIL  {e}")
    if errors:
        print(f"\n✗ {len(errors)} required problem(s), {len(warnings)} warning(s). "
              "Not ready to run.")
        return 1
    print(f"\n✓ All required fields present ({len(warnings)} warning(s)). "
          "Ready to run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
