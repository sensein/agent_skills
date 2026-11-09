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
        ├─ fetch_ezproxy.py      papers the user has a DOI but no PDF for
        │                        → open access (Unpaywall/OpenAlex/S2), then
        │                          institutional access → PDF → corpus entry
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

`research_questions` matters more here than anywhere: a BYO corpus usually
arrives *because* the user has a specific set of questions about it. Transcribe
their numbering and grouping verbatim into
`[{question_id, question, theme, short_title}]` — every included study is then
charted against each question and the report is organised by them (§ 5).

## 2. Corpus

```bash
python scripts/build_corpus.py --dir ./pdfs --out corpus.json
python scripts/build_corpus.py --check corpus.json        # exit 1 while incomplete
python scripts/build_corpus.py --check corpus.json --offline   # no network: syntax only
```

`--check` is the corpus gate, and `run_local_review.py` repeats it before any
LLM call (exit 1; `--allow-citation-errors` only when the user accepts the
findings, `--skip-citation-check` to skip it). It fails on:

- **missing metadata** (`_needs_metadata`);
- **duplicate studies**, matched on *content*, not filename: shared wording of
  the extracted text (word 5-grams, ≥ 50 % of the shorter document), identical
  file or text hash, same DOI or PMID, or a near-identical title. A preprint
  and its published version, a re-download and an author copy are all caught.
  Each flagged entry lists the other in `_duplicates`. Keep the version of
  record, remove the other entry and count it in `duplicates_removed`. If two
  really are different works (companion papers), add the other's `pmid` to
  `_distinct_from` on one of them;
- **identifier errors** from `verify_citations.py` over the corpus: a DOI
  registered to a different paper, a DOI or PMID that does not exist, a PMID
  whose PubMed DOI disagrees, a retraction, or one work entered twice. The
  report is `corpus.citations.md`. A wrong DOI here puts another paper's
  identity on the study, so fix it from the paper's first page or the user's
  manifest, or clear it. A `doi-candidate` is a suggestion to confirm against
  the paper, never something to copy in.

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
| `full_text` | script | Docling Markdown of the **whole** document (`--max-chars 0`, the default), or PyMuPDF text with `--no-docling` / no docling installed; cap it only to bound cost |
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

## 2b. Missing PDFs — open access first, institutional access second

When the user has the DOI but not the PDF, `fetch_ezproxy.py` retrieves it. It
tries two stages in the same order as the application's `FullTextResolver`:

| Stage | Route | Needs | Recorded as |
| --- | --- | --- | --- |
| 1 | Unpaywall → OpenAlex → Semantic Scholar | nothing (an email for Unpaywall) | `unpaywall_pdf`, `openalex_pdf`, `semanticscholar_pdf` |
| 2 | the user's own EZproxy session | a one-time browser sign-in (automatic) | `ezproxy_pdf` |

**Open access goes first, and this matters.** A DOI with a free copy costs
nothing, needs no library session, and doesn't consume one of the user's
capped proxy requests. A corpus that is largely open access can be assembled
with no institutional setup at all, and stage 2 only signs in when stage 1 has
actually left something unretrieved.

```bash
# Stage 1 alone — no institutional setup needed
export SYNTHSCHOLAR_EMAIL=you@example.com    # Unpaywall's ToS requires a contact
python scripts/fetch_ezproxy.py --corpus corpus.json --oa-only

# Both stages: the library session is checked at the start (a browser opens
# only if it is missing or expired), then reused for the whole run
python scripts/fetch_ezproxy.py --status
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs
python scripts/fetch_ezproxy.py --doi-file dois.txt --pdf-dir ./pdfs   # no corpus yet

# Sign in ahead of time, name the institution, or forget the session
python scripts/fetch_ezproxy.py --login
python scripts/fetch_ezproxy.py --login --institution "University of Michigan"   # or umich.edu
python scripts/fetch_ezproxy.py --login --library-link "https://www-nature-com.proxy.lib.umich.edu/…"
python scripts/fetch_ezproxy.py --logout
```

**How the proxy is found**, so the user is never asked for a hostname:
`--proxy-host`, then `--library-link` (any off-campus article link; the host
is read out of it), then `--institution`. After those, the saved login, then
`EZPROXY_HOST`, then the domain of the user's email (`SYNTHSCHOLAR_EMAIL`,
`NCBI_EMAIL` or `git config user.email`). The proxy works for any
institution that runs EZproxy; nothing in it is specific to one university.
- **An institution's full name** is resolved to its domain through
  [ROR](https://ror.org), the worldwide research-organisation registry. Only
  ROR's single confident ("chosen") match is used, never a runner-up, so a
  user is never signed in to someone else's library. "University of Oxford"
  becomes `ox.ac.uk`.
- **An acronym** ("mit") is tried only as `<acronym>.edu`, because ROR
  matches acronyms worldwide.
- **A domain** is used as given.
- **From a domain to the proxy:** a short built-in table, then the usual
  EZproxy hostnames probed in parallel (`ezproxy.<domain>`,
  `libproxy.<domain>`, `proxy.lib.<domain>`, `<name>.idm.oclc.org`, …). A
  candidate is accepted only if the server answers as EZproxy.

Exit 4 means none matched. Ask the user for their institution's full name
or email domain, then for any link their library gives when they open an
article off campus. The host is read straight out of that link, and this
path covers proxies with unusual hostnames. Libraries that authenticate
through OpenAthens or Shibboleth instead of EZproxy are not supported. The URL style
(`hostname-suffix` or `login-url`) is read from where the login lands.

**The saved session** lives in `~/.config/synthscholar/` (owner-only):
`ezproxy.json` (host, mode, when), `ezproxy-cookies.txt` (the proxy's own
cookies only, not the identity provider's), and `browser-profile/`, which
keeps the institution's "remember this device" so MFA isn't asked every time.
Before reuse, the session is checked against the proxy. An expired one
reopens the browser once per run.

| Setting | What it is |
| --- | --- |
| `SYNTHSCHOLAR_EMAIL` | polite-pool contact; **Unpaywall is skipped without it** (their ToS). OpenAlex and Semantic Scholar still work |
| `SEMANTIC_SCHOLAR_API_KEY` | optional; lifts Semantic Scholar's 1 req/s public limit |
| `EZPROXY_HOST` etc. | **not needed** — manual override of the browser sign-in; set all of host + cookie to use it |
| `EZPROXY_HOST` | the gateway host from your library's off-campus links |
| `EZPROXY_MODE` | `hostname-suffix` (`www-sciencedirect-com.ezproxy.uni.edu/…`) or `login-url` (`ezproxy.uni.edu/login?url=…`) |
| `EZPROXY_COOKIE_FILE` | Netscape `cookies.txt` exported from a browser already signed in to the proxy |
| `EZPROXY_COOKIE` | alternative: the raw `Cookie` header value |
| `EZPROXY_DELAY` / `EZPROXY_MAX_REQUESTS` | politeness delay and per-run ceiling |

Flags: `--oa-only` never touches the proxy; `--no-oa` skips open access entirely
(rarely what you want — it spends entitlement on free papers).

Notes that matter:

- **The user signs in; the script never sees a password.** Institutional SSO
  with MFA can't be scripted responsibly, so a real browser window is opened
  and the user completes it themselves. Only the proxy's resulting session
  cookies are kept. They expire; an expired session is detected before use
  and the browser reopens, rather than the failure looking like a paywall.
- **Reading order: the peer-reviewed version first, then fallbacks.** For
  each paper:
  1. the published version, through open access and then the proxy;
  2. its registry-linked preprint, through the same two routes;
  3. a repository-deposited copy: **Zenodo**, then **arXiv**, matched by
     exact title *and* first author, and accepted only if it really is a
     PDF.

  The two-way registry link covers both cases: a supplied preprint DOI is
  upgraded to its published version, and a published DOI falls back to its
  preprint. The paper is always **cited** by its published DOI, and what was
  actually **read** is recorded: `_read_version` (`preprint` /
  `deposited-copy`), `_read_doi` / `_read_url`, and `_preprint_doi` when the
  user supplied the preprint. In `--doi` mode the same facts go in the
  manifest, which `build_corpus.py --manifest` maps back.
- **Say what was read.** If a study was read from a preprint or a deposited
  copy, say so in the limitations: results can differ from the published
  version, so "full text of N studies was read from preprints or author
  copies" belongs there.
- **ResearchGate and Academia.edu are never queried.** Their terms forbid
  automated access. `--no-deposits` skips step 3 and `--as-given` skips
  version handling altogether.
- **An open-access miss is not a failure.** Most DOIs have no free copy; that's
  normal and says nothing about the next one. Only *proxied* failures trip the
  stop-after-3 circuit breaker, because those really do indicate a dead session.
- **A candidate is accepted only if it is really a PDF** (`%PDF-` magic bytes,
  not the content-type). Publishers routinely serve an HTML paywall at a URL
  ending in `.pdf`; those are rejected and the chain continues.
- **Credentials are environment-only.** Never pass a cookie as a CLI flag: the
  full command line is recorded verbatim in `RunConfiguration.cli_invocation`
  and would end up in every export. Provenance stores only *whether* a
  credential was present.
- **Keep it to the review you are conducting.** One article at a time, delayed,
  capped. Systematic bulk downloading breaches most publisher agreements and can
  cost an institution its access — the user is responsible for staying within
  their library's terms, and you should say so rather than raise the ceiling for
  them.
- **Each article records the route that actually got it**, which flows into the
  PRISMA retrieval table and the Turtle — so "how much of this review was open
  access?" is answerable from the export (`query_sparql.py --query
  retrieval-routes`), rather than everything collapsing into one bucket.
- In `--doi`/`--doi-file` mode the routes are written to
  `<pdf-dir>/retrieval_manifest.json`; pass it to `build_corpus.py --manifest`
  or the corpus will mislabel every one of those PDFs as `user_supplied_pdf`.
- The same two-stage cascade is what the application does: open access first,
  then `synthscholar --ezproxy-host … --ezproxy-cookie-file …` as the **last**
  step of the full-text chain.

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

Write every prose field in the formal register of
[writing_style.md](writing_style.md) from the first draft:
- study counts rather than "many studies";
- findings attributed to the included studies;
- GRADE-matched wording;
- no AI vocabulary.

It is cheaper than rewriting afterwards. Then run
`python scripts/check_writing.py review.json` before exporting.

**The paper is assembled from two files.** `review.json` holds the data the
exporters understand. `review.manuscript.json` holds what makes it a paper
([review_template.md](review_template.md), schema at the end), and must
fill:
- `discussion` (all of 4.1–4.8);
- `conclusions`;
- `gaps`, at least five, each with a number;
- `prior_reviews`, or a "none identified" statement;
- `abstract`, at least 250 words, with the reading basis;
- the per-question `results.rq` syntheses;
- each figure's interpretation.

The app's review model drops fields it doesn't know, so none of these go in
`review.json`: a re-export would delete them. Run
`export_review.py review.json --check` before `--outdir`. It reports the
completeness checks and the template gates together. Then run
`assemble_manuscript.py review.json --check` until no gate fails.

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
   A–G plus one `custom_fields` entry per `protocol.research_questions` (keyed
   by `question_id`) and per `protocol.charting_questions` (keyed by the
   question text). Use `""` for anything the paper doesn't report; never fill a
   field by inference. The export pivots these into a question-first view
   (§ 5, *Research questions*), and the ids are what group and address it.
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

### Research questions

`protocol.research_questions` — declared alongside PICO and the eligibility
criteria — is asked of every included study, and the charting stage writes each
answer into that article's `DataChartingRubric.custom_fields`, keyed by
`question_id`. Correct storage, unreadable on its own: in the Markdown all of a
study's answers sit in one narrative-table cell, in the JSON they are an
unlabelled dict, and in the RDF they aren't there at all.

So the exporters pivot them question-first
(`synthscholar.research_questions`), into all three documents at once:

| Export | What it gains |
| --- | --- |
| `review.md` | an appendix — one section per question, every study's answer under it, themed by question group; the oversized `Review Q&A` cells become a link to it |
| `review.json` | a top-level `research_questions` block: question id, text, theme, and each answer with its `source_id` / `pmid` / title / year |
| `review.ttl`, `review.jsonld` | one `slr:ResearchQuestion` per question under a stable IRI (`<review-iri>/question/RQ1.1`), `slr:research_question` from the review, and `slr:has_answer` ↔ `slr:answers_question` ↔ `slr:about_source` per answer |

This lives in the exporters, not in this mode's scripts, so it holds for every
review — a corpus you supplied and the hosted pipeline's own searches alike.

Per-study `custom_fields` are left exactly as charted — this is a second view of
the same data, not a move. Two consequences worth knowing:

- `research_questions` is **not** a `PRISMAReviewResult` field. Pydantic drops
  it on re-validation, so a re-export regenerates it from the rubrics; the
  rubrics stay the single source of truth. Don't hand-edit the block.
- The merge is idempotent — re-exporting replaces the appendix and the question
  nodes rather than stacking a second copy.

```sparql
# every study's answer to one question, without string-matching an id in a literal
PREFIX slr: <https://w3id.org/slr-ontology/>
PREFIX bibo: <http://purl.org/ontology/bibo/>
SELECT ?pmid ?answer WHERE {
  ?q slr:question_id "RQ4.5" ; slr:has_answer ?a .
  ?a slr:answer_text ?answer ; slr:about_source ?s .
  ?s bibo:pmid ?pmid .
}
```

Give each question a `question_id` and a `theme` in the protocol: the id drives
section grouping, the `slr:question_id` literal and the question IRI; the theme
names the heading its questions are reported under (falling back to the major id,
`RQ1`). Unnumbered `charting_questions` still work — keyed by a slug of their
text and collected under *Other charted questions*.

To relabel the sections of a review whose protocol didn't set themes, or to
retrofit a review exported before this existed:

```bash
python scripts/research_questions.py out/review.json --outdir out/
python scripts/research_questions.py out/review.json --outdir out/ --themes themes.json
```

`export_review.py --rq-themes` does the same for a one-off re-export. There is
no switch to turn the view off: a review that declares no research questions
gets no appendix, no block and no question nodes.

### Citation verification

`run_local_review.py` and `export_review.py` run this after every export and
exit 1 on errors (`--offline-citations` for syntax and in-text checks only,
`--skip-citation-check` to skip). Run it by hand to apply fixes, and again
after any re-export:

```bash
python scripts/verify_citations.py out/review.json                 # exit 1 on errors
python scripts/verify_citations.py out/review.json --apply out/review.verified.json
python scripts/export_review.py out/review.verified.json --outdir out/ --formats md ttl json
```

It resolves every included article's DOI (Crossref, then DataCite / other
agencies) and PMID (PubMed), compares the registered title, first author and
year with the corpus metadata, and looks up retraction notices from the paper's
side. It also checks every `PMID:` / DOI / author–year citation in the generated
text against the included set and the screening log. A BYO corpus is exposed
here in particular: its DOIs were read off PDF headers by `build_corpus.py`
and completed by hand. Finding codes, actions, and what `--apply` will and won't
change: [citation_verification.md](citation_verification.md).

### PDF

```bash
python scripts/generate_review_pdf.py --check-deps
python scripts/verify_citations.py out/review.json --style vancouver   # after the last export
python scripts/review_figures.py out/review.json
python scripts/assemble_manuscript.py out/review.json --check
python scripts/generate_review_pdf.py out/review.manuscript.md -o out/review.pdf
```

**Build the manuscript, not the raw export.** `review.manuscript.md` is the
paper. The raw `review.md` inlines every record and is what produced
1,000-page PDFs. Given `review.md`, the builder switches to the manuscript
beside it, if there is one, and warns otherwise.

**LaTeX route.** On a TeX engine (xelatex, lualatex, tectonic, pdflatex):
- pandoc writes a standalone `.tex`, kept next to the PDF so every page's
  source can be inspected or edited. Its converted images go in
  `<name>_files/` beside it, so the kept `.tex` recompiles on its own;
- the engine compiles it, running twice for xelatex, lualatex and pdflatex;
- SVG figures are converted to PDF first, and image paths are made
  absolute;
- figures stay where the text puts them, with their captions underneath:
  they are not floated away.

Typst and the HTML engines are the fallbacks.

**Page guard.** The page count is estimated before rendering (words/550 +
table rows/45 + 0.4 per image). Over `--page-limit` (default 50), the
builder stops and names the heaviest sections. After rendering, the real
count is read from the PDF and enforced again. `--allow-over-limit`
overrides both.

The script renders the Markdown through pandoc without changing its content:
- the `# title` becomes the PDF title, and the review's own section numbers are
  kept (`--number-sections` would double them);
- `--references <style>` swaps in the verified list from `verify_citations.py --style`;
- `--include` appends sidecar exports as appendices, each on a new page. One
  whose title already heads a section of `review.md` is skipped: `appraisal`
  repeats the review's own *Appendix C*.

**The PRISMA 2020 flow diagram is drawn and placed automatically.** It goes
under the review's *PRISMA Flow* heading, above the counts table, which
stays. The counts come from the review's `flow` (`<stem>.json` beside the
Markdown, or `--flow-json`), with one line per identification source and
per exclusion reason. `scripts/prisma_flow.py out/review.json --pdf` draws
it on its own as an SVG and a PDF. Typst and the HTML engines embed the
SVG; TeX engines need it converted, through rsvg-convert, cairosvg or
typst. If none of those is installed, the build warns and omits the
diagram; ask before installing one. `--no-flow-diagram` turns it off.

**No image is cut off.** Each local image's real size is read: PNG, JPEG,
GIF, SVG or PDF. Only one that would run past the A4 text block gets a cap:
the text width, or 23 cm tall. Smaller images keep their natural size, and
a width or height the author already set is never changed.

Cell line breaks (`<br>`), `---` separators and the `✓ ✗ ⚠` symbols are
rewritten for each engine, so tables keep their rows and the symbols print. On
TeX engines a font covering the review's characters (`≥ μ κ →` …) is chosen
with `fc-list` (`--mainfont` overrides), and any glyph still missing is
reported. The build stops while `review.citations.json` has errors or is older
than `review.md` (re-verify after the last re-export; `--citations` points at a
report elsewhere), and warns when there is none. Engines, in the order tried:
xelatex, lualatex, tectonic, pdflatex, typst, weasyprint, wkhtmltopdf, prince.
`--pdf-engine` forces one, `--keep-source` writes the Markdown pandoc actually
read, and `--dry-run` prints the command without running it.

### Ingestion

`review.ttl` is ready to load as-is — it parses under both rdflib and
pyoxigraph. Verify then ingest:

```bash
python -c "import rdflib; g=rdflib.Graph(); g.parse('out/review.ttl'); print(len(g), 'triples')"
```

**Keep that triple count.** It is the only baseline for checking the ingest
didn't quietly lose anything.

For BrainKB, hand the file to the **`brainkb` skill** → *"Ingest a SynthScholar
review"*, which has the full procedure. The four things that section decides,
so this one doesn't have to guess:

- The graph IRI is derived from the review, not invented:
  `https://brainkb.org/synthscholar/reviews/<review_id>/` — `<review_id>` being
  `protocol.review_id`, registered to a space with `brainkb_add_space_graph`
  before ingesting. Anything run from here carries the `review_local_` prefix,
  enforced by `run_local_review.py` even when the protocol supplies its own id,
  so a review's origin is readable from its graph IRI and a local run can never
  claim a hosted review's address.
- Ingest goes through the **upload endpoint**, never `brainkb_ingest_text` — a
  review export is megabytes, and RDF routed through a model gets mangled.
- **Once per review.** Ingest is append-only, and this file's `content_text`
  literals hang off blank nodes, whose identity is per-parse. A second ingest of
  the same file duplicates every abstract rather than de-duplicating.
- Whoever ingests is who the provenance names, permanently — that is the point
  of going through the pipeline rather than writing to the triplestore directly.

What lands in the graph for a BYO review:

| Triple | Source |
| --- | --- |
| `slr:SystematicReview` + `slr:research_question`, flow counts | protocol + flow |
| `slr:search_query` (one per user search, self-describing) | `search_queries` |
| `slr:SearchIteration` (`slr:database`, `slr:search_query`, `slr:iteration_kind`) | `search_iterations` |
| `slr:IncludedSource` per article, IRI from PMID → DOI → hash URN | `included_articles` |
| `slr:StoredArtifact` (`slr:content_text`, `slr:content_hash`, `slr:full_text_source "user_supplied_pdf"`) + a `slr:ToolInvocation` retrieval activity | the PDFs |
| `slr:ChartingRecord`, `slr:RiskOfBiasAssessment`, appraisal nodes | charting / RoB / appraisal |
| `slr:ResearchQuestion` per protocol question + `slr:ChartingQuestionAnswer` per study answer | `charting_questions` × `custom_fields` |
| `oa:Annotation` per evidence span | `evidence_spans` |
| `slr:RunConfiguration` with `corpus_provenance: user_supplied_pdfs` | run configuration |

Mode 2's SPARQL recipes work against this file unchanged — including the
full-text-vs-abstract-only queries, since every PDF-backed article carries a
`slr:full_text_artifact`.

## Caveats

- **Scanned PDFs.** Only Docling reads them, and only because its pipeline OCRs;
  every other backend needs a text layer. `build_corpus.py` uses Docling first
  when installed, and reports the failures (`NO TEXT`, 0 chars) when it is not —
  install it, or OCR the files yourself first (`ocrmypdf`). Otherwise the entry
  screens out as an irrelevant paper rather than as a failed extraction.
- **Every PDF is read in full, and that costs tokens.** Evidence extraction
  processes every chunk of every article, so a 1 M-character survey is ~85 LLM
  calls on its own. `run_local_review.py` prints the chunk count and total call
  estimate up front; bound it with `protocol.evidence_max_chars` (per article)
  or a smaller `--max-chars` at corpus-build time when a paper doesn't warrant
  the full read.
- **Deduplication is checked, not done.** `build_corpus.py` flags entries that
  look like one study by content (see § 2), and the run stops until each pair
  is resolved. It never removes an entry itself: which version to keep, and the
  `duplicates_removed` count, are the user's. Two versions whose wording was
  heavily rewritten in peer review can fall below the text threshold; the
  registry check still catches them when Crossref links the preprint to its
  published DOI.
- **Institutional access needs a live session.** EZproxy sessions expire, and no
  amount of retrying fixes that: the script reopens the browser, or run
  `fetch_ezproxy.py --login`. Papers outside the
  institution's licence simply won't resolve, and those entries stay
  abstract-only (reported as such, not hidden).
- **Open-access coverage is uneven.** Unpaywall/OpenAlex/Semantic Scholar find
  what has been deposited somewhere; a paywalled article with no repository copy
  will miss on all three. That is the case institutional access exists for, and
  the reason stage 2 is worth configuring for a real review.
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
