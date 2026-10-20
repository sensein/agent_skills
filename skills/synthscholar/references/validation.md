# Validation

What a SynthScholar review is checked for, at which step, by which script, and
what happens when a check fails. The same checks apply to every review:
discovered by the online database search (the hosted app or the `synthscholar`
CLI) or built over PDFs the user supplied. A review reaches the user through
`export_review.py`, `assemble_manuscript.py` and `generate_review_pdf.py`, and
those are where the gates sit.

Two questions are kept apart throughout:

- **Are the references real?** Each DOI and PMID exists, belongs to the paper
  named, is not retracted, and every cited study is one the review included.
  This is `verify_citations.py`.
- **Is the content grounded?** Each piece of evidence is text of its paper,
  each claim is supported by quotes from the studies it cites, and each number
  traces to a source or to the structured data. This is `audit_grounding.py`.

A real citation is not a supported claim. Both reports must say PASS before a
PDF is built.

## Dependencies

Every validation dependency is required. `scripts/check_deps.py` lists what is
missing with its install command; the agent asks the user, installs on
confirmation, and proceeds only once it passes. A check whose library is
missing reports INCOMPLETE or refuses to run: it never reports a pass.

## Verdicts

Every check that can block delivery ends in one of three verdicts:

| Verdict | Meaning | Exit |
| --- | --- | --- |
| **PASS** | everything was checked and nothing failed | 0 |
| **FAIL** | at least one error | 1 |
| **INCOMPLETE** | no error, but something was not checked: offline, a registry did not answer, a claim is unjudged, an overlap is unresolved, a report is stale | 3 |

INCOMPLETE is not a pass. A registry outage, an offline run and an unfilled
claim ledger all used to look clean; now each of them blocks.

Every report records the SHA-256 of each file it vouches for (`inputs`). Edit
any of them and the report is stale: the PDF build re-runs the check rather
than trusting it.

## Overview

| # | Stage | What is validated | Script | On failure |
| --- | --- | --- | --- | --- |
| 1 | Protocol | required PICO, eligibility, databases, appraisal tool; values and caps | `validate_protocol.py` | exit 1 before the run |
| 2 | Corpus (supplied PDFs) | metadata complete; duplicate studies by content; every DOI/PMID verified | `build_corpus.py --check`, start of `run_local_review.py` | run refused before any LLM call |
| 3 | Retrieval and reading basis | how each text was obtained, its hash, whether each decision used full text or only the abstract | pipeline (`full_text_source`, `content_sha256`, `assessed_on`) | recorded; checked in 5 and 9 |
| 4 | Screening | two stages, a reason for every exclusion, log against PRISMA flow | pipeline; reconciled by `audit_grounding.py` | FAIL on mismatch |
| 5 | Evidence extraction | each span is text of its paper, first-party, from a section actually read, from the version cited | pipeline (lenient), then `audit_grounding.py` (strict) | FAIL on invented span |
| 6 | Charting, appraisal | schema-valid against the models | pipeline (pydantic), `export_review.py --check` | export refused / warned |
| 7 | Citations | identifiers against Crossref, DataCite, PubMed, with OpenAlex and Semantic Scholar as fallbacks; retractions; in-text citations; background references; resource catalog | `verify_citations.py` (run by the exporters and the PDF build) | FAIL / INCOMPLETE blocks |
| 8 | Claims | every substantive sentence judged against quotes from the studies it cites | `audit_grounding.py` + `<stem>.claims.json` | FAIL / INCOMPLETE blocks |
| 9 | Numbers | flow arithmetic, log vs flow, percentages, stated totals, aggregate counts | `audit_grounding.py` | FAIL blocks |
| 10 | Version changes | evidence re-extracted after a preprint → published switch | `verify_citations.py --apply` → `audit_grounding.py` | FAIL until re-extracted |
| 11 | Studies vs reports | reports describing the same participants grouped or declared distinct | `audit_grounding.py` + `<stem>.studies.json` | INCOMPLETE until resolved |
| 12 | Manuscript structure | template sections, numbering, page limit, figures, no Python repr | `assemble_manuscript.py --check` | gate failures listed |
| 13 | Prose | AI-sounding and informal writing; never touches quotes or numbers | `check_writing.py` | errors must be rewritten |
| 14 | Release | all of the above current for exactly the delivered files | `generate_review_pdf.py` | no PDF |

## 1. Protocol

`validate_protocol.py protocol.json` checks the protocol drafted in the intake
(Mode 1) before anything runs: required fields present (objective, PICO,
inclusion and exclusion criteria, databases, risk-of-bias tool), enum values
valid, caps within range. Exclusion criteria come from the protocol, never from
code. A "human participants only" rule, for example, exists only if the
protocol says so, and §2.1 of the report prints the criteria verbatim.

## 2. Corpus (supplied PDFs)

`build_corpus.py --check corpus.json`, repeated at the start of
`run_local_review.py`, before any LLM spend:

- **Metadata complete.** Title, authors, year and abstract are present
  (`_needs_metadata`). Guessed fields are listed for checking.
- **Duplicate studies, by content.** Two entries are flagged as one study when
  they share at least 50% of the shorter text's word 5-grams, have identical
  bytes or text, share a DOI or PMID, or have near-identical titles. This
  catches a preprint and its published version, a re-download and an author
  copy, whatever the filenames. Each pair is resolved by removing one entry
  (and counting it in `duplicates_removed`), or by listing the other's `pmid`
  in `_distinct_from`. Nothing is removed automatically.
- **Identifiers verified.** Every DOI and PMID goes through the same registry
  check as §7, before the review is written around it. A DOI guessed from a
  PDF header that belongs to a different paper fails here.

## 3. Retrieval and reading basis

Each article records `full_text_source` (`user_supplied_pdf`, `unpaywall_pdf`,
`openalex_pdf`, `semanticscholar_pdf`, `ezproxy_pdf`, …),
`full_text_retrieved_at` and `content_sha256`. Each screening decision records
`assessed_on` (full text or abstract only). These make "what was actually read"
answerable (Mode 2 queries) and feed the reading-basis checks in §5 and §8.

## 4. Screening

The pipeline screens titles and abstracts, then assesses full texts for
eligibility. Every decision has a stage and every exclusion a reason in
`screening_log`. `audit_grounding.py` reconciles the log with the PRISMA flow:

- included = `len(included_articles)`;
- assessed − excluded at eligibility = included;
- identified − duplicates = after deduplication;
- full-text exclusions in the log = `flow.excluded_eligibility`.

## 5. Source-grounded evidence extraction

**In the pipeline** (`synthscholar/evidence.py`, `validation.py`):

- **Whole-document coverage.** Each full text is chunked and every chunk is
  read, so results and discussion sections are mined, not only the first pages.
- **First-party only.** Spans the paper attributes to other studies ("Smith et
  al. reported …", related work) are discarded: a review synthesises what each
  paper found, not what it cites.
- **Grounding filter.** A span must match the article's abstract or full text
  with a fuzzy score of at least 65/100 (the higher of rapidfuzz `partial_ratio`
  and `token_set_ratio`), and be at least 4 tokens long. Spans below that are
  dropped and logged.

This filter is deliberately lenient: it accepts close paraphrases. It removes
gross fabrications; it does not prove a span is a quote. It needs `rapidfuzz`
and refuses to run without it (it used to treat every span as grounded).

**In the skill** (`audit_grounding.py`, strict, independent of the above):

| Finding | Severity | Meaning |
| --- | --- | --- |
| exact / normalized | — | the span is verbatim text of the paper (normalized: case, quotes, whitespace, PDF hyphenation folded) |
| `span-paraphrased` | warning | at least 50% of its word 6-grams are in the paper, but it is not a quote and cannot be cited as one |
| `span-not-in-source` | error | not text of the paper: invented or from another paper |
| `span-reading-basis` | error | the paper was read as abstract only, but the span claims its methods or results |
| `span-other-version` | error | the span records a DOI other than the article's: extracted from a different version |
| `span-unknown-paper` | error | the span names a paper that is not included |

Every located span is reported with its character offset and the SHA-256 of
the text it was found in (`span_locations` in `<stem>.grounding.json`).

## 6. Charting and appraisal

Charting rubrics and appraisal results are validated against the pydantic
models when the review is loaded (`export_review.py --check` reports what is
missing). The rubric's own publication fields (title, authors, DOI) are an LLM
extraction. The authoritative bibliographic record is `included_articles`, which
is what §7 verifies.

## 7. Citations — `verify_citations.py`

Run automatically by `export_review.py`, `run_local_review.py` and
`generate_review_pdf.py`. Full detail and the action for each finding code:
[citation_verification.md](citation_verification.md).

- **Registries.** DOI → Crossref, then the DOI handle server and content
  negotiation (DataCite, mEDRA, …). PMID → PubMed E-utilities. Title, first
  author and year are compared with the review's metadata. A DOI registered to
  a different paper is an error.
- **Fallback indexes.** When Crossref, the handle server or PubMed cannot
  answer, the id is looked up in OpenAlex, then Semantic Scholar, and compared
  the same way. They can confirm what a paper is, but never that a DOI does not
  exist. A record confirmed only by Semantic Scholar has no retraction data
  and stays unverified (`retraction-unchecked`). DOI-less references are also
  title-searched there; candidates are suggestions and are never applied.
  Google Scholar has no API and forbids automated access, so it is not used.
  `habanero` wraps the same Crossref API already used here, so it adds no
  source.
- **Retractions and notices**, read from the paper's side (`updated-by`,
  `updates:` filter, PubMed "Retracted Publication", OpenAlex `is_retracted`).
  A failed notice query makes the reference unverified, not clean.
- **In-text citations.** Every PMID, DOI and author–year citation in the
  generated prose must resolve to an included study. Citing an excluded or
  unknown study is an error.
- **Manuscript mode** (`verify_citations.py review.manuscript.md`). The
  delivered manuscript is checked against the review it was built from:
  - included studies (`role: included`);
  - every other reference-list entry verified as background literature
    (`role: background`);
  - in the prose, an id must be one of the two;
  - resource ids (`NKG-029`) are resolved through `resource_catalog`. An
    unknown resource id, or a catalog link that matches no included study, is
    an error.
- **Duplicates**: two included entries resolving to one work (`duplicate-study`).

## 8. Claims — the claim ledger

`audit_grounding.py --init-claims` extracts every substantive sentence from the
delivered Markdown (`<stem>.manuscript.md`, else `<stem>.md`): one with a
number or a citation. Tables, code, headings, the reference list and
appendices are skipped. Each sentence becomes a claim in `<stem>.claims.json`
with a stable id (a hash of its normalized text), its section, the studies it
cites (by PMID, DOI, author–year or resource id), and its numbers.

**Study claims** (citing at least one included study) are judged by the agent
or a person:

```json
{"id": "C1a2b3c4d5e", "support": "supported",
 "evidence": [{"study": "31234567", "quote": "accuracy of 91.2% on 120 participants"}],
 "explanation": "the quote states both figures", "judged_by": "claude-opus-5-5"}
```

`support` is one of `supported`, `partially_supported`, `contradicted` or
`insufficient_evidence`. *Insufficient* (the cited evidence does not establish
the claim) and *contradicted* (it says otherwise) are different findings, and
both fail until the sentence is corrected or qualified. The audit then checks
the judgement mechanically:

- every quote must be verbatim (or normalized) text of the cited study
  (`quote-not-in-source`), so an abstract-only study can only be quoted from
  its abstract;
- every number in the claim must appear in one of its quotes
  (`number-unsourced`);
- the quoted study must be included (`evidence-unknown-study`), and should be
  one the sentence cites (`evidence-not-cited`, warning);
- `partially_supported` is listed as a warning with its explanation;
- an empty `judged_by` is a warning.

**Aggregate claims** (citing no study) pass when every number in them is
recomputed from the structured data: flow counts, the included and full-text
counts, publication years, value counts of each charting field and catalog
column, search record counts, and percentages that equal one such count over
another or over a review total. A number that cannot be recomputed needs a
`derived_from` naming its source; it is then listed (`aggregate-asserted`) for
a person to check, and without one it is unresolved.

The ledger is bound to the manuscript's hash. Editing the manuscript makes it
stale. Re-running `--init-claims` keeps the judgement of every unchanged
sentence and adds the new ones unjudged.

What this does not do: decide on its own whether a quote supports a claim.
That judgement is the agent's or a person's, and a model agreeing with the
model that wrote the claim is not independent evidence. What it guarantees is
that every judgement exists, rests on real text of the cited paper, and
accounts for every number. The report lists every judgement for review.

## 9. Numbers

`audit_grounding.py`:

- **PRISMA flow arithmetic** and the **screening log against the flow** (§4);
- **"N of M (P%)"**: P must equal N/M within rounding, and N ≤ M
  (`percentage-mismatch`, `percentage-impossible`);
- **stated totals**: "N studies/publications were included" must equal the
  data. "Studies" counts studies after grouping reports (§11); "publications"
  and "reports" count reports (`count-mismatch`);
- **aggregate numbers** (§8) and **study-level numbers in their quotes** (§8).

## 10. Version changes

`verify_citations.py --apply` can replace a preprint with its peer-reviewed
version when the registry links them. That changes what the review reports,
but the charting, appraisal and evidence were extracted from the version read
before. Each switch is therefore recorded in `<stem>.revalidation.json`, and
`audit_grounding.py` fails that study (`version-changed`) until its evidence
spans carry the new DOI and are found in the current text. Then re-run the
extraction for it, re-chart and re-appraise, and re-audit.

## 11. Studies vs reports

A study (one set of participants) can be reported in several papers. Counting
reports as studies double-counts participants. `audit_grounding.py` groups
reports that may share participants:

- a shared trial registration (NCT, ISRCTN, ACTRN, ChiCTR, DRKS, EudraCT);
- a shared dataset accession (GEO, SRA/ENA BioProject, EGA, dbGaP, OpenNeuro,
  PRIDE);
- the same first author and the same sample size.

Research resource identifiers (RRIDs such as `SCR_…`) name software, not
participants, and are not used. Each group must be resolved in
`<stem>.studies.json`:

```json
{"studies": [{"study_id": "ST-001", "reports": ["31234567", "35555555"],
              "basis": "NCT01234567, same cohort"}],
 "distinct": [{"reports": ["31234567", "36666666"], "reason": "different sites, no shared participants"}]}
```

Until then the audit is INCOMPLETE (`study-overlap-unresolved`). The number of
studies, used for "N studies" in §9, is the number of groups plus the ungrouped
reports.

## 12–13. Manuscript structure and prose

- `assemble_manuscript.py --check`: template sections present, conclusions
  non-empty, numbering continuous, page limit, figures placed and interpreted,
  sibling files present, no structured data printed as Python repr, no bare
  PMIDs in the body.
- `check_writing.py`: chatbot artifacts, reader address and emojis are errors;
  AI vocabulary, stacked hedges, "significant" without a statistic and causal
  overclaim are warnings. It checks only the review's own prose, never quotes,
  charted answers or numbers.

## 14. Release

`generate_review_pdf.py` builds a PDF only when, for exactly the files being
delivered:

1. the manuscript's citation report (`<md stem>.citations.json`) is **PASS**,
   and its hashes match the Markdown, `review.json` and `review.manuscript.json`.
   If it is missing or stale, it is re-run.
2. the grounding report (`<stem>.grounding.json`) is **PASS**, and its hashes
   match `review.json`, the Markdown and every sidecar (`manuscript`, `claims`,
   `studies`, `revalidation`). If it is missing or stale, it is re-run.

`--allow-citation-errors` and `--allow-ungrounded` build anyway and print
"Citations not verified" / "Grounding not verified" on the first page of the
body. Use them only when the user accepts an unverified document, and say so
when handing it over. The exporters also exit non-zero on a citation verdict
other than PASS: their output is a draft until it passes.

## Workflow

```bash
python scripts/validate_protocol.py protocol.json
python scripts/build_corpus.py --check corpus.json                 # supplied PDFs only
python scripts/run_local_review.py … / export_review.py …         # citations checked on export
python scripts/assemble_manuscript.py out/review.json --check
python scripts/audit_grounding.py out/review.json --init-claims    # write the ledger
#   judge each study claim in out/review.claims.json; source each unrecomputable aggregate;
#   resolve overlaps in out/review.studies.json; re-extract after version changes
python scripts/audit_grounding.py out/review.json                  # until PASS
python scripts/verify_citations.py out/review.manuscript.md        # until PASS
python scripts/generate_review_pdf.py out/review.manuscript.md     # re-checks both
```

## Not validated

- **Whether a judgement is right.** Support judgements are made by a model or a
  person and recorded; the audit verifies their quotes and numbers, not their
  reasoning (§8).
- **Paraphrased spans** are flagged, not rejected. They may be faithful, but
  they cannot serve as quotes.
- **Asserted aggregates** (`derived_from`) are listed for a person to check.
- **Output that bypasses the skill.** A review written directly by the hosted
  app or the `synthscholar` CLI passes these gates only when it goes through
  `export_review.py` / `generate_review_pdf.py`.
- **Correction notices** are reported. Whether a correction changes an
  extracted result is not assessed automatically; read the notice for each
  corrected study you rely on.
- **Participant overlap without a shared identifier** (same cohort, no registry
  id, different first author) is not detected; declare it in `studies.json`
  when known.
