# Validation

What a SynthScholar review is checked for, at which step, by which script, and
what happens when a check fails. The same checks apply to every review:
discovered by the online database search (the hosted app or the `synthscholar`
CLI) or built over PDFs the user supplied. A review reaches the user through
`export_review.py`, `assemble_manuscript.py` and `generate_review_pdf.py`, and
those are where the gates sit.

The rule throughout: **everything delivered is source-grounded, and what is not
grounded is not delivered.** Every statement about a paper rests on verbatim
text of that paper. Every number is either quoted or computed from the review's
own data by an explicit derivation. That covers prose, tables, captions,
charting answers, appraisal ratings, risk-of-bias judgements, narrative cells
and GRADE ratings. Ungrounded values are removed before delivery. Ungrounded
sentences block delivery until they are corrected or removed.

Two questions are kept apart:

- **Are the references real?** Each DOI and PMID exists, belongs to the paper
  named, and is not retracted, and every cited study is one the review
  included. This is `verify_citations.py`.
- **Is the content grounded?** Every quote is text of its paper, at the stated
  place. Every judgement rests on such quotes. Every number is quoted or
  computed. This is `extract_evidence.py` and `audit_grounding.py`.

A real citation is not a supported claim. Both reports must say PASS, for
exactly the delivered text, before anything is released.

## Dependencies

Every validation dependency is required. `scripts/check_deps.py` lists what is
missing with its install command. The agent asks the user, installs on
confirmation, and proceeds only once the check passes. A check whose library is
missing reports INCOMPLETE or refuses to run; it never reports a pass. The
pipeline's own span filter also refuses to run without `rapidfuzz`.

## Verdicts

| Verdict | Meaning | Exit |
| --- | --- | --- |
| **PASS** | everything was checked and nothing failed | 0 |
| **FAIL** | at least one error | 1 |
| **INCOMPLETE** | no error, but something was not checked: offline, a registry did not answer, a claim is unjudged, a location cannot be verified, an overlap is unresolved, a report is stale | 3 |

INCOMPLETE is not a pass. Every report records the SHA-256 of each file it
vouches for (`inputs`). Edit any of them and the report is stale, so the
release step re-runs the check.

## Deterministic evidence checks

All of the following is mechanical: no model is involved, and the same inputs
give the same result. The rules live in one module, `scripts/grounding_core.py`,
which the extraction script and the audit share.

| Check | Rule |
| --- | --- |
| **Quote is source text** | The quote occurs in the paper's stored text (title, abstract or full text), exactly or after folding case, curly quotes, whitespace, PDF hyphenation, thousands separators and Unicode minus signs. Signs, decimal points, comparators (`<` `>` `≤` `≥` `±`) and `%` are kept: "−5 points" ≠ "+5 points", "p < 0.05" ≠ "p = 0.05". |
| **Source id** | The quote's study is an included study; for a value about one study, it is that study. Spans name an included paper. Cited PMIDs, DOIs and resource ids resolve (§7). |
| **Section** | If the evidence states a section, the quote lies in that section of the stored text, split at its detected standard headings (Abstract, Introduction, Methods, Results, Discussion, Conclusion, …). A paper with no detectable heading for that section makes the label *unverifiable* (unresolved), never assumed right. `extract_evidence.py locate` replaces labels with where the text actually is. |
| **Page** | If the evidence states a page, the quote starts on that page of the page map (`<stem>.pages.json`, one text per PDF page, written by `build_corpus.py`). With no page map the page is unverifiable. |
| **Numbers** | Each quantity in a claim or value, with comparator, sign, decimals and unit, appears in one of its quotes, or is produced by one of its derivations (§8). |
| **Units** | "5 mg" needs "5 mg" in the evidence; "5 g" does not match. A claim that states no unit accepts the number with any unit. |
| **Denominators** | "12 of 40" / "12/40" needs 12 and 40 together in one quote (or both derived). "N of M (P%)" and "N/M (P%)" must compute. |
| **Offsets and hashes** | Every located quote records its character offset in the field that holds it and the SHA-256 of what was read, so a reader can open it at that point. |

## Overview

| # | Stage | What is validated | Script | On failure |
| --- | --- | --- | --- | --- |
| 1 | Protocol | required PICO, eligibility, databases, appraisal tool | `validate_protocol.py` | exit 1 before the run |
| 2 | Corpus (supplied PDFs) | metadata; duplicate studies by content; DOIs/PMIDs verified; page map written | `build_corpus.py --check`, start of `run_local_review.py` | run refused before any LLM call |
| 3 | Retrieval, reading basis | how each text was obtained, its hash, full text vs abstract only | pipeline | recorded; enforced in 5 |
| 4 | Screening | log against the PRISMA flow | `audit_grounding.py` | FAIL |
| 5 | Evidence spans | verbatim, right section/page, read basis, version, every study has evidence | pipeline (lenient) → `audit_grounding.py` (strict) | FAIL; non-verbatim spans dropped on `apply` |
| 6 | Structured values | every charting answer, appraisal item, RoB judgement/field, narrative cell, GRADE domain has verbatim evidence | `extract_evidence.py` → `audit_grounding.py` | removed (`apply`), else FAIL |
| 7 | Citations | registries, fallbacks, retractions, in-text, background, resource catalog | `verify_citations.py` | FAIL / INCOMPLETE |
| 8 | Claims | every sentence, table row and caption judged; quotes verified; numbers quoted or derived | `audit_grounding.py` + `<stem>.claims.json` | FAIL / INCOMPLETE |
| 9 | Numbers | flow arithmetic, log vs flow, percentages, totals | `audit_grounding.py` | FAIL |
| 10 | Version changes | new text retrieved and re-extracted after a preprint → published switch | `verify_citations.py --apply` → `extract_evidence.py refresh` → audit | FAIL until cleared |
| 11 | Studies vs reports | reports sharing participants grouped or declared distinct | `<stem>.studies.json` | INCOMPLETE |
| 12 | Appendices | each appended sidecar equals a fresh render from `review.json` | `audit_grounding.py` | FAIL |
| 13 | Manuscript structure, prose | template gates; AI-sounding writing | `assemble_manuscript.py --check`, `check_writing.py` | listed / errors |
| 14 | Release | all of the above, for exactly the delivered text; numbered version saved | `generate_review_pdf.py` | nothing released |

## 1–4. Protocol, corpus, retrieval, screening

- **Protocol**: `validate_protocol.py` checks required fields and values.
  Exclusion criteria come from the protocol, never from code.
- **Corpus** (`build_corpus.py --check`, repeated by `run_local_review.py`):
  - metadata complete;
  - duplicate studies matched on *content* (≥ 50% shared word 5-grams,
    identical bytes or text, the same DOI or PMID, a near-identical title),
    each resolved by removing one entry or by `_distinct_from`;
  - every identifier verified as in §7.

  `build_corpus.py` also writes the page map `<corpus>.pages.json`, which
  `run_local_review.py` copies beside the review.
- **Retrieval**: each article records `full_text_source`,
  `full_text_retrieved_at` and `content_sha256`; each screening decision records
  `assessed_on`.
- **Screening against the flow**:
  - included = `len(included_articles)`;
  - assessed − excluded = included;
  - identified − duplicates = after deduplication;
  - the log's full-text exclusions = `flow.excluded_eligibility`.

## 5. Evidence spans

**In the pipeline** (`synthscholar/evidence.py`, `validation.py`):

- every chunk of every full text is read;
- only first-party findings are kept (what the paper found, not what it cites);
- each span must fuzzy-match its paper at 65/100 or more.

That filter is lenient: it accepts paraphrases. It removes gross fabrications,
but it does not prove a span is a quote.

**In the skill**, run by `extract_evidence.py spans` (the app's extraction,
with each span stamped with the DOI of the version read) and checked by
`audit_grounding.py`:

| Finding | Severity | Meaning |
| --- | --- | --- |
| `span-not-in-source` | error | not text of the paper |
| `span-paraphrased` | warning | partly the paper's wording; not evidence, and removed by `apply` |
| `span-section-mismatch` / `-unverifiable` | error / unresolved | not in its stated section / the paper has no detectable heading for it |
| `span-page-mismatch` / `-unverifiable` | error / unresolved | not on its stated page / no page map |
| `span-reading-basis` | error | the paper was read as abstract only, but the span claims a later section |
| `span-other-version` | error | extracted from a DOI other than the article's |
| `study-without-evidence` | error | no span of this study is verbatim text of it |

## 6. Structured values — `extract_evidence.py`

Every value the review states about a paper is an item in
`<stem>.evidence-ledger.json`:

- each charting answer (rubric fields, custom questions, section outputs);
- each critical-appraisal item rating;
- each risk-of-bias domain judgement and extracted field (design, sample size,
  population, outcomes, key findings, effect measures);
- each narrative-table cell;
- each GRADE domain rating with its explanation.

```bash
python scripts/extract_evidence.py items  out/review.json   # list them, with candidate spans
python scripts/extract_evidence.py quote  out/review.json   # model proposes verbatim quotes
python scripts/extract_evidence.py locate out/review.json   # labels ← where the text is
python scripts/extract_evidence.py verify out/review.json   # exit 1 while anything is ungrounded
python scripts/extract_evidence.py apply  out/review.json --out out/review.grounded.json
```

Without an API key, the agent writes the quotes into the ledger itself; they
are verified the same way.

**Statuses:**
- `grounded`: at least one quote is verbatim text of that study, at its stated
  section or page, and every number of the value is in a quote, with every
  "N of M" together in one quote.
- `absence`: the value states that nothing was reported ("NR", "Not reported",
  "No", "Unclear", "None (…)"). There is nothing to quote for those.
- `ungrounded`: anything else.

**What `apply` removes:**
- a text field becomes "NR (not source-grounded)";
- list entries, appraisal items and RoB domains are dropped;
- a GRADE outcome with a dropped domain is set to "Not assessed", with the
  reason in its summary;
- non-verbatim evidence spans are dropped.

Everything removed is listed in `<out>.ungrounded.json`. Export and deliver
the grounded copy. The audit fails while any ungrounded value remains
(`structured-ungrounded`).

## 7. Citations — `verify_citations.py`

Full detail: [citation_verification.md](citation_verification.md).

- **Registries**: DOI → Crossref, then the DOI handle server and content
  negotiation (DataCite, mEDRA, …); PMID → PubMed. Title, first author and
  year are compared with the review's.
- **Fallback indexes**: OpenAlex, then Semantic Scholar, when a registry cannot
  answer. They confirm what a paper is, never that a DOI does not exist. A
  record confirmed only by Semantic Scholar is `retraction-unchecked`. Google
  Scholar has no API and forbids automated access. `habanero` wraps the same
  Crossref API already used here.
- **Retractions and notices**, from the paper's side. A failed notice query
  makes the reference unverified, not clean.
- **In-text and manuscript mode**: every PMID, DOI and author–year citation in
  the delivered text is an included study or a verified background reference.
  Resource ids resolve through `resource_catalog`, and catalog links must name
  included studies.

## 8. Claims — `<stem>.claims.json`

`audit_grounding.py --init-claims` makes every unit of the delivered text a
claim: each prose sentence, each table row ("Header: cell; …") and each figure
or table caption. Headings, code, the reference list and appended sidecars
(§12) are not claims. Resource-catalog rows require the same source evidence
and judgement as other table rows; copying a record does not establish support.

Each claim must be judged:

| `support` | Needs | Checked |
| --- | --- | --- |
| `supported` / `partially_supported` | `evidence: [{study, quote, section?, page?}]` | quotes verbatim at their stated place; numbers in quotes; denominators paired |
| `derived` | `derivations: [{value, metric, population, expr}]`, one per number | each `expr` is evaluated against the data and must equal `value`; the noun the number counts must fit the source |
| `not_empirical` | `explanation` (aim, definition, structure) | only allowed when the claim has no number and no citation; listed for review |
| `contradicted` / `insufficient_evidence` | — | fail: correct or remove the sentence |
| none | — | unresolved |

Derivations compare signed results: a positive result cannot establish a negative
claim. Measurement units must come from the source expression, not the ledger's
metric label. Numeric fields can declare a sibling `<field>_unit` or `unit`;
recorded text supplies its printed unit, and aggregations require consistent
source units. Counts cannot establish doses or other measurements. Unknown or
mismatched units block the claim. Quotes preserve dates and numeric-token
boundaries as well as signs, so `5 mg` is not a quote of `15 mg`.

A claim may combine quotes and derivations, but every number must be covered by
one or the other. **Derivations** are explicit and evaluated:

```json
{"value": "103", "metric": "resources using RDF or OWL", "population": "resource catalog",
 "expr": {"count": {"from": "resource_catalog", "where": {"representation": {"regex": "RDF|OWL"}}}}}
{"value": "32%", "metric": "share using RDF or OWL", "population": "resource catalog",
 "expr": {"percent": [{"count": {"from": "resource_catalog", "where": {"representation": {"regex": "RDF|OWL"}}}},
                      {"count": {"from": "resource_catalog"}}]}}
```

**The derivation language:**
- **Sources**: `included_articles`, `charting` (each rubric with its `study`),
  `resource_catalog`, `screening_log`, `evidence_spans`, `studies` (reports
  grouped per §11).
- **Operators**: `count`, `distinct`, `sum`, `mean`, `median`, `min` and `max`
  over a source (`from`, `field`, `where`); `percent` and `ratio` of two
  expressions; `value` and `len` of a path (`flow.total_identified`,
  `protocol.research_questions`); `recorded` (the number appears verbatim in a
  provenance field, e.g. `search_queries`).
- **Conditions in `where`**: equality, a list of values, `contains`, `regex`,
  `not`, `empty`, `gt`/`gte`/`lt`/`lte`.

**The unit check.** A count over a source can only count what that source
holds. "1 patient" derived from a count of `included_articles` fails, because
articles are studies, publications or reports, not patients.

The ledger is bound to the delivered text's hash. Re-running `--init-claims`
keeps the judgement of every unchanged claim. `suggested_derivations` and
`candidate_evidence` are proposals; nothing is accepted without a judgement.

## 9. Numbers

The audit checks:
- PRISMA flow arithmetic, and the screening log against the flow (§4);
- "N of M (P%)" and "N/M (P%)": P must equal N/M within rounding, and N ≤ M;
- stated totals of included studies or reports, with "studies" counted after
  the grouping in §11.

## 10. Version changes

`verify_citations.py --apply` can replace a preprint with its published
version. It records each switch in `<stem>.revalidation.json`, with the DOIs,
the time and the SHA-256 of the text read before. The study fails
(`version-changed`) until **all** of these hold:

1. the article carries the new DOI;
2. a full text is stored, it **differs** from the text read before (hash), and
   it was retrieved after the switch;
3. `extract_evidence.py refresh --only <id>` ran on **that** text after the
   switch, re-running spans, risk of bias, charting, appraisal and the
   narrative row. It is logged in `<stem>.extraction.json` with the text hash;
4. every span of the study carries the new DOI and is verbatim in the new text;
5. its charting rubric carries the new DOI (when the review is charted).

Relabelling the old text's spans with the new DOI clears nothing (condition 2).

## 11. Studies vs reports

Reports may share participants when they have a shared trial registration
(NCT, ISRCTN, ACTRN, ChiCTR, DRKS, EudraCT), a shared dataset accession (GEO,
SRA/ENA, EGA, dbGaP, OpenNeuro, PRIDE), or the same first author and sample
size. Each such group is resolved in `<stem>.studies.json`, as `studies` (one
`study_id`, several reports) or `distinct` (with a reason). Until then the
audit is INCOMPLETE. RRIDs name software and are not used for this.

## 12. Appendices

`generate_review_pdf.py` builds the delivered text: the manuscript, plus the
styled reference list (`--references`) and the sidecar appendices
(`--include`). Each appendix is marked with its file's hash. The audit
re-renders every charting, appraisal, per-group and narrative sidecar from the
current `review.json` with the exporter, and fails on any difference
(`sidecar-not-from-review`), or if a file changed after assembly
(`sidecar-changed`).

## 13. Manuscript structure and prose

`assemble_manuscript.py --check` covers the template's sections, numbering,
page limit, figures, sibling files, Python repr and bare PMIDs.
`check_writing.py` covers chatbot artifacts and AI vocabulary. Neither touches
quotes, charted answers or numbers.

## 14. Release

```bash
python scripts/generate_review_pdf.py out/review.manuscript.md --include charting appraisal --prepare
#   → out/review.delivered.md: the exact text that will be delivered
python scripts/audit_grounding.py out/review.json --init-claims   # claims of the delivered text
#   judge them; ground or remove structured values; resolve overlaps; re-extract changed versions
python scripts/audit_grounding.py out/review.json                 # until PASS
python scripts/generate_review_pdf.py out/review.manuscript.md --include charting appraisal
#   or --no-pdf: deliver the manuscript only (no pandoc/TeX needed)
```

The release step re-assembles the delivered text, then:

1. verifies its citations (manuscript mode), requiring a **PASS** whose hashes
   match the delivered text, `review.json` and `review.manuscript.json`;
2. audits its grounding, requiring a **PASS** whose hashes match `review.json`,
   the delivered text, every sidecar and ledger, and every appended file;
3. builds the PDF (unless `--no-pdf`) from that same text, and refuses if the
   re-assembled text differs from what was checked;
4. saves a **numbered release**: `releases/<stem>-vN/`.

Each release holds:
- the delivered manuscript (`<stem>.manuscript.md`), and the PDF if one was
  built;
- both reports, the ledgers, and the `review.json` it was checked against;
- `manifest.json`: every file's SHA-256, both verdicts, any override, and the
  previous version's manifest hash, so versions form a verifiable chain.

Earlier versions are never overwritten.

`--allow-citation-errors` and `--allow-ungrounded` still build. The PDF then
says "Citations not verified" / "Grounding not verified" on its first page,
and the release is saved as `<stem>-vN-UNVERIFIED` with `verified: false`. Use
them only when the user accepts an unverified document, and say so.

## Not validated

- **Whether a judgement is right.** Support judgements, and the model's
  proposed quotes, are recorded with who made them. The audit verifies their
  quotes, places, numbers and derivations, not their reasoning. A model that
  agrees with the model that wrote the claim is not independent evidence; the
  reports list every judgement for review.
- **`not_empirical` claims** are listed for a person to confirm they really
  are aims, definitions or structure.
- **Section labels** come from detected standard headings. A paper that uses
  non-standard headings makes a label unverifiable, not wrong.
- **Pages** are verifiable only for PDFs with a page map. Online full texts
  (PMC XML, HTML) have no pages.
- **Output that bypasses the skill** (written directly by the hosted app or
  the CLI) passes these gates only when it goes through the skill's release
  step.
- **Correction notices** are reported; whether a correction changes a result
  is not assessed automatically.
- **Participant overlap without a shared identifier** is not detected; declare
  it in `studies.json` when known.
