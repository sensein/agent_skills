# Bring-your-own-corpus review (user-supplied PDFs)

The user already did the searching and the full-text collection. They hand over
a folder of PDFs; this workflow produces the *same* literature review the
SynthScholar application produces — same PRISMA 2020 Markdown, same
SLR-ontology Turtle — with discovery and full-text retrieval skipped because
they already happened.

Everything else is unchanged: screening against the protocol's criteria,
evidence extraction, risk of bias, data charting (sections A–G), critical
appraisal, narrative rows, synthesis + GRADE, per-group analysis, grounding
validation, assembled report.

What *is* different is provenance: the search strategy lives with the user, so
it has to be collected from them (now, or after the run).

---

## Pipeline overview

```
protocol intake (SKILL.md Mode 1)            ← draft, ask pivots, confirm
        │
        ├─ build_corpus.py       PDFs → corpus.json  (text + hashes + guesses)
        │        └─ agent completes title/authors/year/abstract per entry
        │
        ├─ fetch_ezproxy.py      paywalled papers the user has a DOI but no PDF for
        │                        → institutional access → PDF → corpus entry
        │
        ├─ search provenance     ask the user (table below) → search_provenance.json
        │                        …or defer and patch it in later
        ▼
run_local_review.py  ── or ──  agent-authored result JSON
        │                              │
        └──────────────┬───────────────┘
                       ▼
              export_review.py → review.md + review.ttl (+ json/jsonld/bib/…)
                       │
                       ├─ update_provenance.py   (search strategy added later)
                       └─ triple-store ingestion (the .ttl, as-is)
```

## Two execution paths

Both end at the same validated `PRISMAReviewResult` and the same exporters, so
the Markdown and Turtle are indistinguishable in structure.

| | **A — pipeline** (`run_local_review.py`) | **B — agent-authored** |
| --- | --- | --- |
| Who does the analysis | the app's own multi-agent pipeline | the host model (you) |
| Needs | `synthscholar` installed + `OPENROUTER_API_KEY` | the host model only (plus `synthscholar` to export) |
| Fidelity | identical to the app by construction | identical schema; prose quality is yours |
| Cost | OpenRouter tokens | host tokens |
| Use when | the package and a key are available | no API key, or the user wants you to do the reading |

**Prefer path A** whenever a key is available — it is the application, not an
imitation of it. Fall back to B when it isn't.

---

## 1. Protocol

Unchanged from SKILL.md → Mode 1: draft the whole protocol, ask only the
pivotal decisions, confirm, then gate it:

```bash
python scripts/validate_protocol.py protocol.json
```

Two BYO-specific notes:

- `databases` should name what the **user** searched (Scopus, Web of Science,
  Embase, arXiv…). `validate_protocol.py` warns for names outside the app's own
  provider list — that warning is expected here and can be ignored.
- `max_hops` is forced to 0 (no citation chasing happens without discovery).

## 2. Corpus

```bash
python scripts/build_corpus.py --dir ./pdfs --out corpus.json
python scripts/build_corpus.py --check corpus.json        # exit 1 while incomplete
```

Each entry is an `Article`-shaped dict plus `_`-prefixed bookkeeping that is
stripped before the run.

| Field | Filled by | Notes |
| --- | --- | --- |
| `pmid` | script | `local_<sha8>` of the file bytes — stable across re-runs. A real numeric PMID (via `--manifest`) yields a `pubmed.ncbi.nlm.nih.gov` IRI instead of a hash URN. |
| `doi` | script (regex) → verify | with a DOI, the RDF IRI becomes `https://doi.org/…` — **worth getting right**, it's the article's identity in the graph |
| `title`, `year` | script (guess) → verify | from embedded PDF metadata / header text |
| `authors` | **agent** | `"Smith J; Doe A"` — semicolon-separated; the RDF export emits one `dcterms:creator` per `;` |
| `abstract` | script (guess) → verify | extracted between the Abstract heading and the next section |
| `journal` | agent / manifest | |
| `source` | `--source` / manifest | feeds the PRISMA per-database identification tally |
| `full_text` | script | PyMuPDF text of the **whole** document (`--max-chars 0`, the default); cap it only to bound cost |
| `content_sha256`, `full_text_source`, `full_text_retrieved_at` | script | `full_text_source` is `user_supplied_pdf` — this is what makes the corpus auditable |
| `_head_text` | script | first 2500 chars — **read this to complete the metadata** |
| `_needs_metadata`, `_metadata_guesses` | script | what's missing / what was guessed and needs a look |

**Completing metadata is your job, not the user's.** Read each entry's
`_head_text`, fill `authors` (and fix any wrong guess), then re-run `--check`.
Never invent a DOI or a year — leave a field empty rather than guess it.

Known metadata can be supplied instead of inferred:

```bash
python scripts/build_corpus.py --dir ./pdfs --manifest metadata.csv --out corpus.json
# CSV: file,pmid,doi,title,authors,journal,year,abstract,source
```

Manifest values always win over guesses.

Each PDF is read **in full** (`--max-chars 0`, the default). Evidence extraction
chunks each article's stored text and reads every chunk, so truncating the
corpus would hide the results and discussion sections from the analysis. Cap it
only to bound cost on very long documents — `run_local_review.py` prints the
resulting chunk count before spending anything.

## 2b. Paywalled papers — institutional access

When the user has the DOI but not the PDF, `fetch_ezproxy.py` retrieves it
through **their own institutional EZproxy session**, so subscription articles
are read in full instead of screened on an abstract:

```bash
export EZPROXY_HOST=ezproxy.myuniversity.edu
export EZPROXY_MODE=hostname-suffix          # or login-url — check your library's links
export EZPROXY_COOKIE_FILE=~/ezproxy-cookies.txt
export EZPROXY_DELAY=3                       # seconds between requests
export EZPROXY_MAX_REQUESTS=100              # per-run ceiling

python scripts/fetch_ezproxy.py --status
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs
python scripts/fetch_ezproxy.py --doi-file dois.txt --pdf-dir ./pdfs   # no corpus yet
```

| Setting | What it is |
| --- | --- |
| `EZPROXY_HOST` | the gateway host from your library's off-campus links |
| `EZPROXY_MODE` | `hostname-suffix` (`www-sciencedirect-com.ezproxy.uni.edu/…`) or `login-url` (`ezproxy.uni.edu/login?url=…`) |
| `EZPROXY_COOKIE_FILE` | Netscape `cookies.txt` exported from a browser already signed in to the proxy |
| `EZPROXY_COOKIE` | alternative: the raw `Cookie` header value |
| `EZPROXY_DELAY` / `EZPROXY_MAX_REQUESTS` | politeness delay and per-run ceiling |

Notes that matter:

- **Log in with a browser, not here.** Institutional SSO with MFA can't be
  scripted responsibly. Export the session cookies instead; they expire, and a
  login-page bounce is detected and reported rather than silently treated as a
  paywall.
- **Credentials are environment-only.** Never pass a cookie as a CLI flag: the
  full command line is recorded verbatim in `RunConfiguration.cli_invocation`
  and would end up in every export. Provenance stores only *whether* a
  credential was present.
- **Keep it to the review you are conducting.** One article at a time, delayed,
  capped. Systematic bulk downloading breaches most publisher agreements and can
  cost an institution its access — the user is responsible for staying within
  their library's terms, and you should say so rather than raise the ceiling for
  them.
- Retrieved articles are marked `full_text_source="ezproxy_pdf"`, which flows
  into the PRISMA retrieval table and the Turtle, so the review can show that a
  paper was read via subscription access rather than open access.
- The same route is available inside the application: `synthscholar --ezproxy-host
  … --ezproxy-cookie-file …`, where it runs as the **last** step of the
  full-text cascade (open access is always tried first).

## 3. Search provenance — what to ask

Ask this as **one batch of structured questions** (`AskUserQuestion` in Claude
Code), the same style as the protocol pivots. Then write
`search_provenance.json`:

```bash
python scripts/run_local_review.py --print-provenance-template > search_provenance.json
```

| Key | Question | PRISMA item |
| --- | --- | --- |
| `searches[].database` | "Which databases did you search?" | 6 |
| `searches[].query` | "The exact query string for each — copy-paste it" | 7 |
| `searches[].date_searched` | "When did you run it?" | 6 |
| `searches[].filters` | "Any limits applied (dates, language, article type)?" | 7 |
| `searches[].records_identified` | "How many records did each search return?" | 16a |
| `duplicates_removed` | "How many duplicates did you remove?" | 16a |
| `records_screened` | "How many records did you screen by title/abstract?" | 16a |
| `records_excluded_title_abstract` | "How many did you exclude at that stage?" | 16a |
| `reports_sought` / `reports_not_retrieved` | "How many full texts did you seek, and how many couldn't you get?" | 16a |
| `grey_literature` | "Any grey literature, registries, or hand-searching?" | 6 |
| `date_range_start` / `date_range_end` | "Publication window?" | 7 |

**Never block the review on this.** If the user doesn't have the numbers to
hand, run without them — the review is complete, only the reported search
strategy is thin — and patch it in afterwards:

```bash
python scripts/update_provenance.py out/review.json \
    --provenance search_provenance.json --outdir out/
```

`update_provenance.py` touches provenance slots only (search queries, search
iterations, identification counts, databases, date range, registration /
funding / competing interests) and re-exports. Screening, charting, appraisal
and synthesis are never modified, so a late provenance fix can't invalidate the
analysis.

### PRISMA flow accounting

The flow diagram legitimately has two authors, and mixing them up is the one
way to make this review dishonest:

| Flow field | Comes from | Meaning |
| --- | --- | --- |
| `db_*`, `total_identified`, `duplicates_removed`, `after_dedup` | **user** (`search_provenance.json`) | their searches; falls back to the corpus size when undeclared |
| `screened_title_abstract`, `excluded_title_abstract`, `sought_fulltext`, `not_retrieved` | **user** when declared, else the pipeline | they screened titles/abstracts before exporting PDFs — their numbers are the reportable ones |
| `assessed_eligibility`, `excluded_eligibility`, `excluded_reasons`, `included_synthesis` | **pipeline / agent** | the eligibility assessment actually performed over the supplied PDFs |

So a typical BYO review reads: *N records identified by the user → duplicates
removed → screened by the user → M full texts supplied → M assessed for
eligibility here → K included.* The screening this workflow runs is a **second
pass over already-retrieved reports**, not a re-run of the user's title/abstract
screening. Say so in any summary you write.

### What each decision was judged on

Eligibility screening reads the **retrieved full text**, not a second pass over
the abstract, and every article that passed title/abstract screening is assessed
— including those whose full text could not be obtained. Those are judged on the
abstract, marked as such, and can be excluded on that basis; they are never
silently included. The run records:

| Field | Meaning |
| --- | --- |
| `flow.full_text_retrieved` / `flow.not_retrieved` | reports obtained vs sought |
| `flow.full_text_sources` | retrieval route → count (`user_supplied_pdf`, `ezproxy_pdf`, `pmc_oa`, `unpaywall_pdf`, …) |
| `flow.assessed_on_full_text` / `assessed_on_abstract_only` | what the eligibility decisions were made on |
| `flow.included_with_full_text` / `included_abstract_only` | basis for the studies that made it into the synthesis |
| `flow.excluded_reasons_title_abstract` / `excluded_reasons_full_text` | exclusion reasons split by stage |
| `screening_log[].assessed_on` / `.full_text_source` | per-decision basis and route |

All of it is exported: a "Full-text access and screening basis" section in the
Markdown, and in the Turtle as `slr:full_text_retrieved`,
`slr:assessed_on_full_text`, `slr:assessed_on_abstract_only`,
`slr:full_text_route_record`, `slr:exclusion_reason_full_text` and one
`slr:ScreeningDecisionRecord` per decision (with `slr:assessed_on`).

When you summarise a review, report this — "23 of 25 read in full text, 2
included on abstract alone" is materially different evidence from "25 included",
and the difference belongs in the summary, not just the appendix.

## 4a. Path A — run the pipeline

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python scripts/run_local_review.py \
    --protocol protocol.json --corpus corpus.json \
    --provenance search_provenance.json \
    --outdir out/ --formats md ttl json charting appraisal per-group

# pre-flight (no LLM calls): confirms corpus, protocol, flow, provenance
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json --dry-run
```

Useful flags: `--model` (default `$SYNTHSCHOLAR_MODEL` or
`anthropic/claude-sonnet-4`), `--data-item` (extra per-study extraction items,
repeatable), `--no-per-group`, `--synthesis-style bullet`.

It runs `PRISMAReviewPipeline._run_from_deduped` — the app's own steps 7–18 —
then the per-group pass, then exports. The shared PostgreSQL review cache is
never read or written on this path (`pg_dsn=""`, `share_to_cache=False`): a
user-supplied corpus isn't a reusable search result.

**Version coupling:** `_run_from_deduped` is a private method. It is the
deliberate seam — it is exactly "everything after acquisition" — but a
refactor upstream can rename it. The script fails with a clear message if it
disappears; the fix is to re-point it at the equivalent entry point.

## 4b. Path B — author the result yourself

Use when there's no OpenRouter key. You perform the stages; the result is the
same schema, exported by the same code.

```bash
python scripts/export_review.py --print-template > review.json   # valid example
# …replace the EXAMPLE content with the real review…
python scripts/export_review.py review.json --check
python scripts/export_review.py review.json --outdir out/ --formats md ttl json
```

Do the stages in order, reading each PDF's text from the corpus:

1. **Screen** every corpus entry against `inclusion_criteria` /
   `exclusion_criteria`, in two passes. Title/abstract first
   (`stage="title_abstract"`, be inclusive), then eligibility
   (`stage="full_text"`) **reading the actual full text** of everything that
   survived — checking methods and results, not re-reading the abstract. Log
   every decision (included *and* excluded) as a `ScreeningLogEntry` with a
   specific reason, `assessed_on` set to `full_text` or `abstract_only`, and
   `full_text_source` naming the route. Entries with no full text are still
   assessed — on the abstract, with the reason starting "Abstract only — " —
   never auto-included. Then fill `flow.assessed_on_full_text`,
   `assessed_on_abstract_only`, `full_text_retrieved`, `full_text_sources`,
   `included_with_full_text`, `included_abstract_only`, and the two per-stage
   `excluded_reasons_*` maps.
2. **Evidence spans** — verbatim (or near-verbatim) quotes from the article
   text, one `EvidenceSpan` per supported claim, with `paper_pmid` matching the
   article and `section` naming where it came from. Read the **whole** article,
   not its opening pages, and extract only the paper's **own** findings:
   anything it attributes to another study (a result with a citation, a
   related-work claim, background prevalence figures) is that other paper's
   evidence and must not be recorded here. A useful test — if the sentence
   would still be true had this study never been run, it isn't this study's
   evidence.
3. **Risk of bias** per included article, using the protocol's `rob_tool`
   domains (`Article.risk_of_bias`).
4. **Data charting** — one `DataChartingRubric` per included article: sections
   A–G plus one `custom_fields` entry per `protocol.charting_questions`. Use
   `""` for anything the paper doesn't report; never fill a field by inference.
5. **Critical appraisal** — one `CriticalAppraisalRubric` (and, ideally, one
   `CriticalAppraisalResult`) per included article, over the protocol's
   `appraisal_domains`.
6. **Narrative row** per included article (`PRISMANarrativeRow`).
7. **Synthesis** (`synthesis_text`), `bias_assessment`, `limitations`,
   `grade_assessments` per outcome, `structured_abstract`,
   `introduction_text`, `conclusions_text`.
8. **Per-group analysis** — bucket by `protocol.grouping_dimension` (a rubric
   attribute), one `GroupAnalysisEntry` per bucket, answering
   `default_group_questions` / `per_group_questions`.

Linkage rules that keep the exports coherent:

- `source_id` ties `data_charting_rubrics` ↔ `critical_appraisals` ↔
  `structured_appraisal_results` ↔ `narrative_rows`. Use one stable id per
  article (`S-001`, `S-002`, …).
- `pmid` ties `included_articles` ↔ `screening_log` ↔ `evidence_spans` ↔
  `per_group_analysis.*.supporting_pmids`. Use the corpus `pmid` verbatim.
- Keep `flow.included_synthesis == len(included_articles)`.
- Preserve each article's `full_text`, `content_sha256`, `full_text_source`,
  `full_text_retrieved_at` from the corpus — dropping them silently discards
  the audit trail that makes the corpus verifiable.
- Every number in the synthesis must appear in a source article. No invented
  effect sizes, sample sizes, or p-values.

`--check` reports what's missing before you export.

## 5. Exports

| Format | File | Use |
| --- | --- | --- |
| `md` | `review.md` | the PRISMA 2020 review document |
| `ttl` | `review.ttl` | SLR-ontology RDF for triple-store ingestion |
| `jsonld` | `review.jsonld` | same graph, JSON-LD |
| `json` | `review.json` | the `PRISMAReviewResult` — the re-exportable source of truth |
| `bib` | `review.bib` | BibTeX of included studies |
| `charting` | `review.charting.{md,json}` | per-article sections A–G tables |
| `appraisal` | `review.appraisal.{md,json}` | appraisal tables |
| `per-group` | `review.per-group.md` | per-group synthesis + Q&A |
| `narrative` | `review.narrative.{md,json}` | condensed summary |

Keep `review.json`: it is what `update_provenance.py` and any re-export read.

### Ingestion

`review.ttl` is ready to load as-is — it parses under both rdflib and
pyoxigraph. Verify then ingest:

```bash
python -c "import rdflib; g=rdflib.Graph(); g.parse('out/review.ttl'); print(len(g), 'triples')"
```

For BrainKB, hand the file to the `brainkb` skill's ingest tools (log in,
choose a space, ingest, poll the job). What lands in the graph for a BYO
review:

| Triple | Source |
| --- | --- |
| `slr:SystematicReview` + `slr:research_question`, flow counts | protocol + flow |
| `slr:search_query` (one per user search, self-describing) | `search_queries` |
| `slr:SearchIteration` (`slr:database`, `slr:search_query`, `slr:iteration_kind`) | `search_iterations` |
| `slr:IncludedSource` per article, IRI from PMID → DOI → hash URN | `included_articles` |
| `slr:StoredArtifact` (`slr:content_text`, `slr:content_hash`, `slr:full_text_source "user_supplied_pdf"`) + a `slr:ToolInvocation` retrieval activity | the PDFs |
| `slr:ChartingRecord`, `slr:RiskOfBiasAssessment`, appraisal nodes | charting / RoB / appraisal |
| `oa:Annotation` per evidence span | `evidence_spans` |
| `slr:RunConfiguration` with `corpus_provenance: user_supplied_pdfs` | run configuration |

Mode 2's SPARQL recipes work against this file unchanged — including the
full-text-vs-abstract-only queries, since every PDF-backed article carries a
`slr:full_text_artifact`.

## Caveats

- **Scanned PDFs.** No text layer → no extraction. `build_corpus.py` reports
  these; OCR them first (`ocrmypdf`) or the entry screens out.
- **Every PDF is read in full, and that costs tokens.** Evidence extraction
  processes every chunk of every article, so a 1 M-character survey is ~85 LLM
  calls on its own. `run_local_review.py` prints the chunk count and total call
  estimate up front; bound it with `protocol.evidence_max_chars` (per article)
  or a smaller `--max-chars` at corpus-build time when a paper doesn't warrant
  the full read.
- **Deduplication is the user's.** Nothing here detects that two PDFs are the
  same paper; `run_local_review.py` only rejects duplicate `pmid`s.
- **Institutional access needs a live session.** EZproxy cookies expire, and no
  amount of retrying fixes that — re-export them. Papers outside the
  institution's licence simply won't resolve, and those entries stay
  abstract-only (reported as such, not hidden).
- **The charting rubric is biomedically shaped** (sections C/D are
  disorder/control cohorts). For non-clinical corpora, set
  `grouping_dimension` to something meaningful (`study_design`, `task_type`)
  and expect several rubric fields to stay empty — that's honest, not broken.
- **Abstract-only entries are allowed.** An entry with an abstract but no
  extractable full text still screens and charts; it just carries no
  `slr:full_text_artifact`, and Mode 2's queries will correctly report it as
  abstract-only.
- **`prisma_review` assembly** (the fully structured document object) runs on
  path A only; on path B leave it `null` unless you author it.
