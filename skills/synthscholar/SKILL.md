---
name: synthscholar
version: 0.6.0
description: Set up, run, and query SynthScholar / PRISMA systematic reviews. (1) Protocol intake — draft a complete protocol (research question, PICO, eligibility, databases, risk-of-bias tool, charting, per-group analysis, registration) and validate it before a run. (2) Provenance queries over a finished review — full-text vs abstract-only inclusions, retrieval routes, and the screening audit trail (decisions on full text vs abstract, exclusion reasons per stage), via SPARQL over the RDF export or SQL over the article store. (3) Bring-your-own-corpus review — the user supplies PDFs, or DOIs whose full text is fetched open access (Unpaywall, OpenAlex, Semantic Scholar) then via their own institutional EZproxy access; produces the full PRISMA review (screening, charting, appraisal, synthesis) as Markdown and SLR-ontology Turtle. Use when asked to start or scope a review, run a review over supplied PDFs, get full text for a DOI, export a review to md/ttl, or audit full-text and screening provenance.
license: Apache-2.0
---

# SynthScholar Review Skills

Three capabilities for working with systematic reviews produced by SynthScholar:

1. **Guided protocol intake** — before a review runs, guide the user through
   *every* input the agent supports and validate the protocol is complete.
2. **Provenance queries** — after a review runs, answer *which included
   publications have full text, which are abstract-only, what the content is,
   and where it came from*.
3. **Bring-your-own-corpus review** — the user hands over PDFs they collected
   themselves; produce the full review over them and export Markdown + Turtle.

Pick the mode from the request: setting up / scoping a review → intake; the
user has PDFs that meet the eligibility criteria for the review → BYO
corpus; inspecting a finished review → queries. Modes compose — a BYO review
starts with the Mode 1 intake and its output is queryable with Mode 2.

---

## Mode 1 — Guided protocol intake

The `ReviewProtocol` has ~40 fields. **Do not interrogate the user field by
field** — that is the wrong interaction. A user often gives only a title
("a systematic review of vocal biomarkers for depression"). From that, you can
draft a *complete, sensible* protocol yourself. The intake's job is to **draft,
lock the few decisions that genuinely change the outcome, and confirm** — not
to collect 40 answers.

### The interaction pattern (do this)

1. **Draft the whole protocol from whatever the user gave you.** Infer objective,
   full PICO, inclusion/exclusion criteria, **the research questions**,
   databases, and domain features (`grouping_dimension`, appraisal domains)
   with domain-appropriate defaults. Use
   [references/protocol_intake.md](references/protocol_intake.md) as the field
   catalog — every field's default and validation is there.

   **`research_questions` sits with PICO and eligibility, not with the
   extraction settings** — it is what the review answers. When the user gives
   you a numbered set, transcribe it verbatim into
   `[{question_id, question, theme, short_title}]`, keeping their numbering and
   grouping; when they don't, draft 4–8 from the objective and show them back.
   Each one is asked of every included study and reported question-first in the
   Markdown, JSON and RDF, so a vague question produces 170 vague answers —
   make each one answerable from a single paper.

2. **Identify the 2–4 *pivotal* decisions** — the ones where a wrong default is
   costly to run on, and ask **only those**, as structured questions with a
   **recommended** option pre-selected. Everything else stays a silent default.
   The usual pivots (not fixed — judge per topic):
   - **Scope of the key concept** — how broadly to read the intervention/exposure
     for inclusion (narrow vs. include adjacent methods). Drives recall.
   - **Appraisal instrument** (`rob_tool`) — the default `RoB 2` assumes RCTs;
     most observational/diagnostic corpora need `ROBINS-I`, `QUADAS-2`, etc.
   - **Publication date window** — unbounded vs. a cutoff.
   - Occasionally: primary **outcome** framing, or the **cohort/grouping**
     dimension for per-group analysis.

   Surface these through the host's structured-question UI — **`AskUserQuestion`
   in Claude Code**, or the decision-card UI in the web app — one question each,
   each with a recommended answer and 2–4 concrete options. Ask the pivots
   together in one batch, not one message at a time.

3. **Show the full drafted protocol back** (all fields, not just the answered
   ones) and get a single confirm before running. Make clear which values were
   the user's explicit answers vs. your defaults.

4. **Record provenance** — each *asked* decision becomes a `slr:UserInput`
   (`question_asked`, `input_value`, `options_presented`) inside a
   `slr:PreWorkflowSession`; defaults you filled are attributed to the agent.
   See [references/data_model.md](references/data_model.md).

5. **Validate before running:**

   ```bash
   python scripts/validate_protocol.py --print-template > protocol.json  # scaffold
   # …write the drafted + confirmed values in…
   python scripts/validate_protocol.py protocol.json                     # gate
   ```

   Exits non-zero if a required field is missing; warns on missing recommended
   fields / invalid enums. Only run the review once it passes.

### Anti-patterns (don't do these)

- ❌ Asking the user to fill every field, or pasting the whole checklist at them.
- ❌ Asking about advanced features (batch size, cache TTL, section formats)
  unless the user raised them — default silently.
- ❌ Running without showing the drafted protocol and getting a confirm.
- ❌ Free-text-prompting the pivotal decisions when the host has a structured
  question UI — use the cards so the recommended option is one click.

### Worked example

User: *"a systematic review of vocal biomarkers for depression."*

You draft the full protocol (objective, PICO, inclusion/exclusion, databases,
charting questions, `grouping_dimension`, etc.) from that one line, then ask
**only** the pivots as structured cards, each with a recommended answer:

- **Scope of "vocal biomarkers" for inclusion?** → *Any speech-derived signal
  (incl. ASR & deep embeddings)* / Acoustic-prosodic features only / Clinician-rated only
- **Appraisal instrument?** (RoB 2 assumes RCTs; this corpus is
  diagnostic/observational) → *ROBINS-I* / QUADAS-2 / Newcastle-Ottawa
- **Publication date window?** → *Unbounded* / 2015+ / last 10 years

Then show the complete drafted protocol and get one confirm before running.
The three answers are recorded as `slr:UserInput`; every defaulted field is
attributed to the agent.

---

## Mode 3 — Bring-your-own-corpus review (user-supplied PDFs)

> *"Here are 40 PDFs I collected — write the review."*

Same review as the hosted application: screening → evidence extraction → risk
of bias → data charting (sections A–G) → critical appraisal → narrative rows →
synthesis + GRADE → per-group analysis → grounding validation → assembled
report, exported to **Markdown** and **SLR-ontology Turtle**. The only
difference is that discovery and full-text retrieval already happened — the
user did them — so the search strategy has to be *collected* rather than
generated.

Full workflow, schemas, and the PRISMA-flow accounting rules:
**[references/byo_corpus_review.md](references/byo_corpus_review.md)** — read
it before running this mode.

### The steps

Steps 6–9 (citations, source grounding, manuscript, delivery) apply to every review, including one from the hosted app's online search: export it to `out/review.json` and start at step 6.

```bash
# 1. Protocol — the Mode 1 intake, unchanged, then gate it
python scripts/validate_protocol.py protocol.json

# 2. Corpus — PDFs in, full text + hashes + provenance out
#    (Docling first: best tables, and the only backend that reads a scan)
python scripts/build_corpus.py --dir ./pdfs --out corpus.json
#    large corpus of clean text-layer PDFs, or need the app's exact parser:
python scripts/build_corpus.py --dir ./pdfs --out corpus.json --no-docling
#    …then YOU complete each entry's metadata by reading its `_head_text`…
python scripts/build_corpus.py --check corpus.json          # exit 1 until complete
#    --check is the corpus gate: metadata complete, no duplicate studies
#    (matched on content, not filename — a preprint and its published version
#    are one study), and every DOI/PMID verified against Crossref/PubMed. A
#    guessed DOI that belongs to another paper is an error: fix it from the
#    paper or clear it. run_local_review.py repeats the gate before any LLM call.

# 2b. Papers the user has a DOI but no PDF for — open access, then institutional
python scripts/fetch_ezproxy.py --status
python scripts/fetch_ezproxy.py --corpus corpus.json --oa-only        # free, no setup
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs # + paywalled

# 3. Search provenance — ask the user (one batch of structured questions)
python scripts/run_local_review.py --print-provenance-template > search_provenance.json

# 4. Run
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json \
    --provenance search_provenance.json --outdir out/ \
    --formats md ttl json charting appraisal per-group

# 5. Provenance added late (if the user didn't have it in step 3)
python scripts/update_provenance.py out/review.json \
    --provenance search_provenance.json --outdir out/

# 6a. Writing — the prose must read as a formal review, not generated text.
#     Rewrite every flagged sentence in out/review.json (prose only; see
#     references/writing_style.md), repeat until clean, then re-export.
python scripts/check_writing.py out/review.json
python scripts/export_review.py out/review.json --outdir out/ --formats md ttl json

# 6b. Citations — gate before handing the review over (exit 1 on errors).
#    run_local_review.py and export_review.py already run this check after
#    every export (out/review.citations.md); the commands below fix what it found.
#    --apply writes registry-confirmed fixes to a copy; re-export it over the
#    review, then verify again so the report describes what will ship.
python scripts/verify_citations.py out/review.json
python scripts/verify_citations.py out/review.json --apply out/review.verified.json
python scripts/export_review.py out/review.verified.json --outdir out/ --formats md ttl json
python scripts/verify_citations.py out/review.json --style vancouver

# 7. Source grounding — everything delivered rests on verbatim text of a paper
#    or on an explicit computation over the review's data; what is not grounded
#    is not delivered (references/validation.md).
#    a. Evidence spans from the current text of every study (the app's extraction):
python scripts/extract_evidence.py spans  out/review.json
#    b. Every structured value — charting answers, appraisal ratings, RoB
#       judgements, narrative cells, GRADE domains — needs verbatim evidence.
#       `quote` asks the model; without a key, write the quotes into
#       out/review.evidence-ledger.json yourself. `locate` sets section/page
#       labels from where the text is. `apply` removes whatever stays ungrounded.
python scripts/extract_evidence.py items  out/review.json
python scripts/extract_evidence.py quote  out/review.json
python scripts/extract_evidence.py locate out/review.json
python scripts/extract_evidence.py apply  out/review.json --out out/review.grounded.json
python scripts/export_review.py out/review.grounded.json --outdir out/ --formats md ttl json charting appraisal per-group
#    c. After `verify_citations --apply` switched a preprint to its published
#       version: fetch the published text into the corpus, then
python scripts/extract_evidence.py refresh out/review.json --only <id>

# 8. Manuscript — the paper, in the template's order (references/review_template.md).
#    Draw the figures, write out/review.manuscript.json (Discussion 4.1–4.8,
#    prior reviews, gaps, confidence, figure interpretations, declarations),
#    assemble, and clear the gates. Re-run check_writing on the manuscript.
python scripts/review_figures.py out/review.json                 # out/figures/*.svg + figures.json
python scripts/assemble_manuscript.py out/review.json --check    # out/review.manuscript.md
python scripts/check_writing.py out/review.manuscript.md

# 9. Delivery — the manuscript and the PDF, as a numbered release.
#    a. Assemble exactly what will be delivered (styled references and
#       appendices included) → out/review.delivered.md
python scripts/generate_review_pdf.py out/review.manuscript.md --include charting appraisal --prepare
#    b. Every sentence, table row and caption of it is a claim: judge each in
#       out/review.claims.json (quotes verbatim; every number quoted or derived
#       by an evaluated derivation), resolve overlapping reports in
#       out/review.studies.json; repeat until PASS.
python scripts/audit_grounding.py out/review.json --init-claims
python scripts/audit_grounding.py out/review.json
python scripts/verify_citations.py out/review.delivered.md
#    c. Release: re-assembles, re-verifies citations and grounding for exactly
#       that text (hash-bound), builds the PDF (ACM layout; page limit) and
#       saves releases/review-vN/ — the manuscript, the PDF, both reports, the
#       ledgers and a manifest chained to the previous version. --no-pdf
#       releases the manuscript alone. Overrides (--allow-citation-errors,
#       --allow-ungrounded) print a notice in the PDF and save -UNVERIFIED.
python scripts/generate_review_pdf.py out/review.manuscript.md --include charting appraisal
# Existing reviews with no PDF yet (or an outdated one): find and build them all
python scripts/generate_review_pdf.py --batch out/ ~/reviews/
```

Step 4 needs `OPENROUTER_API_KEY` and the `synthscholar` package — it drives
the app's real pipeline. **Without a key**, author the review yourself and
serialise it through the same exporters:

```bash
python scripts/export_review.py --print-template > review.json   # valid example
# …perform the stages, fill it in (see references/byo_corpus_review.md § 4b)…
python scripts/export_review.py review.json --check
python scripts/export_review.py review.json --outdir out/ --formats md ttl json
```

The exporters organise `protocol.research_questions` into every export on the
way out — Markdown appendix, JSON `research_questions`, RDF
`slr:ResearchQuestion` — from the `custom_fields` the charting stage filled in.
This is in the exporters themselves, so it holds for **any** review, including
ones the hosted pipeline ran over papers it found itself; nothing extra to run.
To retrofit a review exported before that existed, or to relabel its sections:

```bash
python scripts/research_questions.py out/review.json --outdir out/
python scripts/research_questions.py out/review.json --outdir out/ --themes themes.json
```

### Report structure and length

The final review is the **assembled manuscript**, `review.manuscript.md`.
It follows
[references/review_template.md](references/review_template.md): the
section order, per-section source slots, word and page budgets, the figure
plan and the export gates. It comes in two variants, set by `review_type`:
`effect` for systematic reviews (GRADE, optional meta-analysis) and
`mapping` for scoping reviews, systematic maps and resource surveys. The
data stays in `review.json`. The prose that makes it a paper goes in
`review.manuscript.json`: Discussion §4.1–4.8, prior reviews, gaps, the
confidence statement, figure interpretations and declarations.
`assemble_manuscript.py` renders the two. The pipeline's raw `review.md`
is an export, not the paper.

Hard rules:

- **50 pages maximum** (`page_limit`). Per-record material is exported as
  sibling files and linked, never inlined: full charting tables, evidence
  spans, URL checks, the excluded list, the resource ↔ publication map. The
  PDF builder refuses a document over the limit before rendering, so a
  1,300-page PDF cannot happen. Fix an overrun by moving appendices out,
  never by cutting the Discussion.
- **No empty declared section.** Every heading is emitted, numbered without
  gaps. A section with no data says "Not assessed: <reason>". The
  Discussion's principal findings, interpretation, comparison, implications
  and research agenda, and the Conclusions, are mandatory.
- **Figures where a human author would put them.** `review_figures.py`
  draws the standard set, and only when the data supports each figure. Its
  `facts` give the numbers: write each figure's interpretation from them,
  and refer to it in the section's prose as `{fig:<id>}`. The assembler
  numbers the figures and places each under the paragraph that introduces
  it. No decorative charts, and at most 10 to 12.
- **For resource and landscape surveys (`mapping`)**, the primary unit is
  the *system*, not the publication. §3.3 renders the deduplicated
  inventory, and every count in the body says whether it counts resources
  or publications.

### Rules for this mode

- **Write the Discussion, not just the limitations.** A review whose
  Discussion is a limitations list has not been synthesised. Principal
  findings per research question, interpretation, comparison with prior
  reviews, implications and a research agenda are required (template §4).
- **Keep the manuscript a manuscript.** Check the page estimate
  (`assemble_manuscript.py --check`) before building the PDF. Move
  appendices to sibling files rather than cutting synthesis.

- **Complete the corpus metadata yourself** from each entry's `_head_text` —
  don't hand the user a list of fields to fill. Never invent a DOI or year;
  leave it empty instead.
- **Identify every paper by its published version, with its PMID.** These
  rules apply to the reference list, in-text citations and fetching alike:
  - **Cite the peer-reviewed version.** When a study exists as both a
    preprint and a peer-reviewed article, cite, fetch and read the
    peer-reviewed article. Its venue DOI is the article's DOI.
  - **Add the PubMed ID wherever PubMed has the article.**
  - **Read the published version where possible.** If neither open access
    nor the proxy can reach it, read its preprint, then a copy deposited on
    Zenodo or arXiv, but still cite the published DOI. Say in the
    limitations which studies were read from a preprint or a deposited
    copy. ResearchGate and Academia.edu are never used.
  - **Cite a preprint only when no peer-reviewed version exists.** Label it
    as a preprint, and say in the limitations that it is not peer-reviewed.

  The scripts do this:
  - `fetch_ezproxy.py` tries the published version first.
  - `verify_citations.py` reports a preprint with a published version,
    adds missing PMIDs, and `--apply` makes both changes, rewriting the
    article's id everywhere it appears in the review.
  - The styled reference list ends each entry with its PMID.

  A switched version may report different numbers: re-check the extracted
  results against the published text. A preprint and its published version
  are one study, and including both is an error: exclude one and correct
  the PRISMA counts.
- **Ask for the search strategy, but never block on it.** Databases, exact
  query strings, dates searched, filters, records identified, duplicates
  removed — ask as one batch of structured questions. If the user doesn't have
  the numbers, run anyway and patch them in with `update_provenance.py`.
- **Keep the flow honest.** Identification counts are the user's; the screening
  this mode performs is a *second pass over already-retrieved reports*, not a
  re-run of their title/abstract screening. Say so when summarising.
- **Never drop full-text provenance** — `full_text_source`,
  `content_sha256`, `full_text_retrieved_at` are what make the corpus
  auditable, and they carry into the Turtle.
- **Screen on the full text, and record the basis.** Eligibility decisions are
  made on the retrieved report (methods and results), not a re-read of the
  abstract. Articles whose full text couldn't be obtained are still assessed —
  on the abstract, marked `assessed_on="abstract_only"` — never auto-included.
- **Extract only each paper's own evidence.** A number a paper cites from
  another study is not that paper's finding; excluding those is what makes the
  synthesis attributable.
- **Try open access before asking for a library session.** `fetch_ezproxy.py`
  resolves DOIs through Unpaywall → OpenAlex → Semantic Scholar first, which
  needs no setup; only what's left behind a paywall needs the user's library
  sign-in. **Never ask the user to set `EZPROXY_*` variables or export
  cookies.** Run `fetch_ezproxy.py` and it signs them in itself, **at the
  start of the run**. The saved session is checked first; if there is none,
  or it has expired, a browser window opens at their library's login before
  any fetching. So a long run never stalls halfway, waiting on a window
  nobody saw. A live session passes without a browser. For a long session
  (fetching, then the review), sign in once up front with
  `fetch_ezproxy.py --login`. `--lazy-login` restores on-demand sign-in.
  Tell them it's coming ("a browser window will open; sign in with
  your library account"), and run the command with a long timeout (it waits
  up to 5 minutes for them). The proxy is found from their email domain,
  for any university that runs EZproxy. If the script exits 4, ask one
  question, "Which university or library do you get journal access
  through?", and pass the answer as `--institution`. It takes the full
  name (looked up in ROR) or the email domain. Never assume a particular
  university.
  If it still exits 4, ask for any link their library gives when they open
  an article, and pass it as `--library-link`. Exit 3 means Playwright is
  missing: ask before installing it (dependency table below). Keep within
  their licence: one paper at a time, delayed, capped, and never raise the
  ceiling on their behalf.
- **Report the reading basis when you summarise.** "23 of 25 read in full text,
  2 included on abstract alone" is different evidence from "25 included".
- **The research questions organise themselves — declare them in
  `protocol.research_questions` and answer them per article.** Every export
  then carries a question-first view alongside the per-study one: an appendix
  in the Markdown, a `research_questions` block in the JSON, and
  `slr:ResearchQuestion` nodes in the RDF. Key each rubric's `custom_fields` by
  the question's `question_id`; the ids group the appendix and become the
  question IRIs. Never hand-assemble that view or paste the answers into the
  synthesis: it is generated, and a hand-made copy goes stale on the next
  export.
- **The review's prose is formal scientific writing and must not read as
  generated.** Write it that way from the start when you author the review
  (Path B). Either path ends with a revision pass:
  `check_writing.py out/review.json` flags each sentence with an AI pattern
  or an informal register, and you rewrite those sentences **in
  `review.json`**, following
  [references/writing_style.md](references/writing_style.md):
  - count studies ("seven of 12"), not "many studies";
  - attribute findings to studies, not to "research";
  - "significant" only with its test;
  - "associated with" for observational evidence;
  - GRADE wording that matches the certainty ("probably" at moderate, "may"
    at low);
  - no contractions, reader address, stacked hedges or "paves the way".

  Change only the review's own prose: never quoted evidence, charted
  answers, numbers, citations, ratings or research questions. A rewrite
  never adds a claim or a number the review's data doesn't hold. After
  rewriting, re-run the checker until it reports no errors, then re-export
  and re-verify citations. The checker catches patterns, not argument, so
  read the prose through once as well.
- **Verify citations before handing the review over** — for every review,
  online search or supplied PDFs alike. Only a **PASS** verdict is releasable:
  FAIL means errors, INCOMPLETE means something was not checked (offline, a
  failed registry lookup, a reference with no identifier), and neither is
  "verified". The exporters run the check after every export; verify the
  assembled manuscript too (`verify_citations.py review.manuscript.md`, which
  the PDF build does itself), since the Discussion and other authored prose
  exist only there. It resolves each
  included article's DOI and PMID against Crossref / DataCite / PubMed, compares
  the registered title, author and year with the review's, flags retractions,
  and checks that every PMID, DOI and author–year citation in the generated
  prose belongs to an included study. Clear the errors before ingesting. Fix
  references from the registry (`--apply` does the registry-confirmed ones),
  remove or re-attribute claims that cite excluded or unknown studies, and tell
  the user about retracted studies rather than dropping them silently. Never
  make a citation pass by inventing a DOI or by adopting a `doi-candidate`
  the user hasn't confirmed. Finding codes and the response to each:
  [references/citation_verification.md](references/citation_verification.md).
- **Always offer both exports.** `.md` to read, `.ttl` to ingest. Offer a PDF
  (`generate_review_pdf.py`) when the review is going to people: it renders
  `review.md` as is, so fix content in `review.json` and re-export rather than
  editing the PDF's source. It stops while `review.citations.json` reports
  errors. Pass `--allow-citation-errors` only when the user explicitly accepts
  a PDF with unresolved citations, and say so when you hand it over.
  Needs `pandoc` and a TeX engine for the paper layout (`--check-deps`).
  The manuscript is typeset in the **ACM journals layout** (`acmart`,
  `acmsmall`: the class behind Overleaf's "ACM Journals Primary Article
  Template"), with the abstract and keywords in the title block, figures as
  numbered floats with their captions, and tables a size smaller than the text.
  The class is bundled in `assets/acmart/` (LPPL, unmodified), so nothing is
  fetched per build; `--layout plain` gives pandoc's article layout instead.
  Without TeX the script falls back to typst/HTML in the plain layout and says
  so; ask before installing TeX. When the user has earlier reviews without a
  PDF, offer `--batch <dir>`: it finds every exported review under the folder,
  re-assembles a stale manuscript and builds each missing or outdated PDF
  (`--force` rebuilds all).
- **Ingesting into BrainKB is the `brainkb` skill's job — hand off, don't
  improvise.** Its *"Ingest a SynthScholar review"* section owns the graph IRI
  (`https://brainkb.org/synthscholar/<space>/reviews/<review_id>/`, registered to a
  space), the upload path, and the once-per-review rule. Print the export's
  triple count when you hand the file over; that number is what the ingest is
  reconciled against.
- **Hand over the output directory, not just the `.ttl`.** BrainKB ingests a run's
  whole output folder into a space (`brainkb_synthscholar_ingest`, or in the UI
  *Ingest Knowledge Graph → SynthScholar review*): the RDF and an import record go
  into the review's graph, and the manuscript, PDF, figures, tables, search log and
  verification reports are kept for its page. `work/` is left out (and recorded as
  left out). `review.json` and `review.ttl` are required. So finish the run first —
  export with `--formats md ttl`, manuscript, PDF, checks — then hand over the
  folder that holds them. Keep `protocol.review_id` as the run minted it: it names
  the review's graph, and a review is ingested once per space.

## Mode 2 — Provenance queries

> *Which included publications have full text, which are abstract-only, what is
> the full-text content, and where did it come from?*

The same review is materialised in **two places**, and this mode covers both:

| Representation | Where | Query language | Full-text signal |
| --- | --- | --- | --- |
| RDF graph (SLR ontology) | exported `.ttl` / `.jsonld` | **SPARQL** | `slr:full_text_artifact` node |
| PostgreSQL article store | `article_store` + `article_full_text` | **SQL** | row in `article_full_text` |

Pick the surface that matches what the user has in hand. If they exported RDF,
use SPARQL. If they have the live database, use SQL. The two are kept in
parity: `slr:content_hash` in RDF equals `article_full_text.content_sha256`.

## When to invoke this skill

**Intake (Mode 1)** — when the user asks to:

- **Start / scope a new review** or "set up a protocol".
- **Gather the review inputs** — research question, PICO, inclusion/exclusion.
- **Check a protocol is complete** before running.
- Configure **domain features**: charting questions, per-group / cohort
  analysis, custom appraisal domains, output formatting, PRISMA registration.

**BYO corpus (Mode 3)** — when the user:

- Provides **PDFs / a folder of papers** and wants a review, synthesis, or
  evidence table built from them.
- Has **DOIs for paywalled papers** and institutional access, and wants the full
  texts retrieved so the review reads the papers rather than their abstracts.
- Says they **already did the search** (or exported records from Scopus / Web of
  Science / Embase / a reference manager) and wants the review run on that set.
- Wants an existing review **exported to Markdown or Turtle**, or the **TTL
  ingested** into a triple store / BrainKB.
- Wants to **add or correct the search strategy** on a review that already ran.
- Wants the **citations of a review (or any Markdown manuscript) verified**:
  DOIs/PMIDs real and pointing at the cited paper, retractions, in-text
  citations matching the included studies, or a reference list in APA /
  Nature / Vancouver / IEEE.
- Wants the review **as a PDF**, optionally with the charting / appraisal /
  per-group / citation report appended.

**Queries (Mode 2)** — when the user asks to:

- **List included publications that have full text** (vs. everything included).
- **Distinguish full-text-included from abstract-only** inclusions.
- **Retrieve the full-text content** of included sources.
- **Audit provenance** — which resolver produced each full text (`pmc_oa`,
  `europe_pmc_oa`, `unpaywall_pdf`, `biorxiv_pdf`, `openalex_pdf`,
  `semanticscholar_pdf`, `ezproxy_pdf` — institutional subscription access —
  `zenodo_pdf`, `arxiv_pdf` — deposited copies, the last resort —
  `user_supplied_pdf`, `article_store`, `cache`) and when.
- **Ask what the review actually read** — how many reports were retrieved vs
  not, how many eligibility decisions were made on full text vs abstract only,
  which studies were included without their full text, which papers came via
  institutional access, and why reports were excluded at each stage. Recipes:
  `reading-basis`, `retrieval-routes`, `ezproxy-articles`, `exclusion-reasons`,
  `screening-decisions`, `abstract-only-inclusions`.
- **Verify or backfill** the `article_full_text` provenance table.

Mode 2 covers full-text availability, content and provenance of *included*
sources, plus the screening-decision audit trail (stage, basis, reason) — not
full charting extraction.

## The core model

An **included** publication is a `slr:IncludedSource` (SPARQL) / a row in
`article_store` that survived screening (SQL). Among included publications:

- **full-text-included** → full text was retrieved. In RDF it carries a
  `slr:full_text_artifact` → `slr:StoredArtifact` (kind `source_text_extract`)
  with `slr:content_text`, `slr:content_hash`, `slr:content_size_bytes`. In SQL
  it has a matching row in `article_full_text`.
- **abstract-only** → included but no full text. It has `dcterms:abstract`
  (RDF) / a non-empty `abstract` in `article_store`, but **no**
  `slr:full_text_artifact` / **no** `article_full_text` row.

Presence of the artifact / row **is** the distinguishing signal — never infer
full-text availability from the length of an abstract.

## How to answer a request

1. **Determine the surface.** Ask (or infer from the artifacts present)
   whether the user has an RDF export (`.ttl`/`.jsonld`) or DB access (a DSN).
2. **Pick the query.** Match the request to a named recipe in
   [references/sparql_queries.md](references/sparql_queries.md) or
   [references/sql_queries.md](references/sql_queries.md).
3. **Run it** with the matching helper script, or hand the user the query.
4. **Report** the distinction plainly: N included, of which M have full text
   and (N−M) are abstract-only; include provenance/source breakdown if asked.

## Helper scripts

### Check dependencies before running — ask, install, then proceed

Before the first run in a session, run `python scripts/check_deps.py` (or
`--for corpus|review|verify|pdf|query|tests` for one stage). It lists every
missing dependency with its install command. **All required dependencies
must be installed before proceeding**: the validation steps are not optional,
and a missing library would turn a check into "not checked". For each one
missing, ask the user with `AskUserQuestion` (the install command as the
recommended option), install it when they confirm, and re-run `check_deps.py`
until it passes. Never install without that confirmation. Pick the command for
their environment: an active conda env → `conda`/`pip` there, a `uv` project →
`uv add`, otherwise `pip`. If the user declines a required dependency, stop
and say which steps cannot run and that the review cannot be delivered as
verified without them; do not continue as if it had passed. Optional
dependencies (`docling`, `playwright`, `psycopg`) may be skipped.

| `verify_citations.py`, `audit_grounding.py`, `fetch_ezproxy.py`, and the gates in `build_corpus.py --check`, `export_review.py`, `run_local_review.py`, `generate_review_pdf.py` | `httpx` (**required**) | `python -c "import httpx"` | `pip install httpx` |
| `run_local_review.py` (the pipeline's evidence grounding) | `rapidfuzz>=3.0` (**required**; extraction refuses without it) | `python -c "import rapidfuzz"` | `pip install 'rapidfuzz>=3.0'` |
| `tests/` (regression tests) | `pytest` | `python -c "import pytest"` | `pip install pytest` |
| `fetch_ezproxy.py` library sign-in (paywalled papers only) | `playwright` + Chrome, Edge or Playwright's Chromium | `python -c "import playwright"` | `pip install playwright` (+ `playwright install chromium` only if neither Chrome nor Edge is installed) |
| `query_sparql.py` | `rdflib` | `python -c "import rdflib"` | `pip install rdflib` |
| `query_postgres.py`, `backfill_full_text.py` | `psycopg` | `python -c "import psycopg"` | `pip install "psycopg[binary]>=3.1"` |
| `build_corpus.py` | `pymupdf`; `docling` optional | `python -c "import fitz"` | `pip install pymupdf` (+ `pip install docling` for scans/tables) |
| `export_review.py`, `run_local_review.py`, `update_provenance.py` | `synthscholar` dev checkout | the script refuses with a message | `pip install -e /path/to/prisma-review-agent` (ask for the path) |
| `generate_review_pdf.py` | `pandoc` + a **TeX engine** (xelatex, lualatex, pdflatex or tectonic) for the ACM layout; `rsvg-convert` (or `cairosvg`/`typst`) to convert SVG figures for TeX; `fc-list` optional. Without TeX: `typst` or an HTML engine, plain layout only | `python scripts/generate_review_pdf.py --check-deps` (also finds a TeX Live/MacTeX install that is not on `PATH`) | TeX: `brew install --cask mactex-no-gui` (macOS), `apt-get install texlive-xetex texlive-fonts-extra texlive-latex-extra` (Debian/Ubuntu), or `conda install -c conda-forge tectonic` (smallest; fetches packages once into its cache). Then `conda install -c conda-forge pandoc librsvg` (or `brew install pandoc librsvg`). The ACM class itself is bundled in `assets/acmart/` |
| `validate_protocol.py`, `research_questions.py`, `check_writing.py`, `review_figures.py`, `assemble_manuscript.py`, `check_deps.py` | stdlib | — | — |

Network services (Crossref, PubMed, Unpaywall) and `OPENROUTER_API_KEY` are
not installs: mention them if a run will need them, and ask the user for any
key. Never invent one.

All live in [scripts/](scripts/). Run them from an environment where the
project's deps are importable (`rdflib` for SPARQL, `psycopg[binary]>=3.1` for
SQL; the backfill also needs the `synthscholar` package on `PYTHONPATH`). The
Mode 3 scripts need `synthscholar` importable too — plus `pymupdf` for PDF text
and `OPENROUTER_API_KEY` for `run_local_review.py`. `docling` is the first
extraction backend `build_corpus.py` reaches for; without it the script says so
and uses the PyMuPDF chain, and `--no-docling` skips it deliberately.

**`export_review.py` and `run_local_review.py` need the development checkout,
not the released package** — `pip install -e /path/to/prisma-review-agent`. The
PyPI build predates the whole-document reading and screening-basis provenance,
and they refuse to run on it rather than silently dropping those fields.
`fetch_ezproxy.py` and `verify_citations.py` work anywhere — they need only `httpx`, since the skill
vendors both `scripts/oa_client.py` (the open-access DOI chain) and
`scripts/ezproxy_client.py` (`synthscholar.ezproxy` ships only in the checkout).

```bash
# Intake: blank template → fill from answers → validate before running
python scripts/validate_protocol.py --print-template > protocol.json
python scripts/validate_protocol.py protocol.json

# BYO corpus: PDFs → corpus → review → md/ttl (see Mode 3)
python scripts/build_corpus.py --dir ./pdfs --out corpus.json
python scripts/build_corpus.py --check corpus.json
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs   # OA, then paywalled
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json \
    --provenance search_provenance.json --outdir out/
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json --dry-run
python scripts/export_review.py out/review.json --outdir out/ --formats md ttl
python scripts/research_questions.py out/review.json --outdir out/   # retrofit only
python scripts/update_provenance.py out/review.json --provenance search_provenance.json
python scripts/check_writing.py out/review.json                        # AI-voice / register pass
python scripts/verify_citations.py out/review.json                     # citation gate
python scripts/verify_citations.py out/review.json --style apa --apply out/review.verified.json
python scripts/assemble_manuscript.py out/review.json --check
python scripts/generate_review_pdf.py out/review.manuscript.md
python scripts/generate_review_pdf.py --batch out/                    # PDFs for reviews that lack one
python scripts/generate_review_pdf.py --check-deps                     # pandoc + TeX + acmart?

# SPARQL over an RDF export — named recipe or your own query
python scripts/query_sparql.py review.ttl --query included-full-text
python scripts/query_sparql.py review.ttl --query status-all --format csv
python scripts/query_sparql.py review.ttl --sparql ./my_query.rq

# SQL over the Postgres article store (DSN via --dsn or $PRISMA_PG_DSN)
python scripts/query_postgres.py included-full-text
python scripts/query_postgres.py source-breakdown
python scripts/query_postgres.py get-content --pmid 39012345

# Backfill article_full_text from legacy article_store.full_text rows
python scripts/backfill_full_text.py            # needs migration 006 applied
```

Run any script with `-h` for its full option list, and `--list` (query
scripts) to see every named recipe.

## References

- [references/protocol_intake.md](references/protocol_intake.md) — complete
  guided-intake checklist: every `ReviewProtocol` input with question, example,
  validation, default, and tier.
- [references/byo_corpus_review.md](references/byo_corpus_review.md) —
  bring-your-own-corpus workflow: corpus schema, search-provenance intake,
  PRISMA-flow accounting, the two execution paths, exports and ingestion.
- [references/validation.md](references/validation.md) — every validation the skill
  performs (protocol, corpus, source-grounded evidence extraction, citations, claim
  ledger, numbers, version changes, studies vs reports, release gates), with its
  verdicts and what is not validated. Read it before delivering a review.
- [references/citation_verification.md](references/citation_verification.md) —
  what `verify_citations.py` checks, every finding code with the action it
  calls for, `--apply` semantics, and the reference styles.
- [references/review_template.md](references/review_template.md) — the
  manuscript section plan, for both the `effect` and `mapping` variants: the
  source slot, required content, word and page budget and export gate for
  every section, the figure plan, the 50-page appendix policy, and the
  `review.manuscript.json` schema.
- [references/writing_style.md](references/writing_style.md) — the formal
  register the review's prose must meet: review-specific rules (study counts,
  attribution, GRADE wording), the AI-writing patterns `check_writing.py`
  flags, what a rewrite may never touch, and the revision pass.
- [references/sparql_queries.md](references/sparql_queries.md) — SPARQL recipe catalog.
- [references/sql_queries.md](references/sql_queries.md) — SQL recipe catalog.
- [references/data_model.md](references/data_model.md) — the SLR-ontology terms
  and the `article_store` / `article_full_text` schema this skill relies on,
  plus how provenance is captured in the pipeline.

## Caveats

- Both signals are written **going forward**. Reviews exported/run before the
  full-text-provenance change won't have `slr:full_text_artifact` triples or
  `article_full_text` rows until re-exported or backfilled
  (`scripts/backfill_full_text.py`).
- `full_text_source` is empty for backfilled historical rows — their provenance
  was never recorded.
- Full-text bodies can be large; the SQL `get-content` recipe streams a single
  PMID. Avoid `SELECT content` across the whole table in interactive use.
- **Scanned PDFs** need [Docling](https://github.com/docling-project/docling),
  which `build_corpus.py` uses first when it is installed — its pipeline OCRs.
  Without it (or with `--no-docling`) every backend reads the text layer only, so
  a scan yields an empty `full_text`. Either way check `_extractor` and the char
  count in the build output before running the review — an empty body screens out
  as an irrelevant paper rather than as a failed extraction. Each paper is
  read **in full** (evidence extraction processes every chunk), so a long corpus
  costs real tokens; `run_local_review.py` prints the call estimate before
  spending, and `protocol.evidence_max_chars` bounds it. It never removes an
  entry itself: likely duplicates (matched on content) stop the run until the
  user says which version to keep.
- Open-access retrieval reaches only what has been deposited somewhere; a
  paywalled paper with no repository copy misses on all three providers.
  Unpaywall additionally needs `SYNTHSCHOLAR_EMAIL` (their ToS) and is skipped
  without it.
- Institutional access (EZproxy) reaches only what the institution licenses.
  The browser sign-in saves the proxy's session cookies under
  `~/.config/synthscholar/` (owner-only permissions), never the password, and
  `fetch_ezproxy.py --logout` deletes them. A session lasts hours, and the
  browser reopens by itself when it expires. Never put a cookie on a command
  line: the CLI invocation is stored verbatim in provenance.
