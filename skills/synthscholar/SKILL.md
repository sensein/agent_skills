---
name: synthscholar
version: 0.1.0
description: Set up and query SynthScholar / PRISMA systematic reviews. (1) Guided protocol intake — walk a user through EVERY required and optional input (research question, PICO, inclusion/exclusion criteria, databases, risk-of-bias tool, charting questions, per-group/cohort analysis, appraisal domains, output formatting, PRISMA registration) and validate completeness before a run. (2) Provenance queries — list included publications, distinguish full-text vs abstract-only, retrieve full-text content, and audit where each full text came from, across both the RDF/SLR-ontology export (SPARQL) and the PostgreSQL article store (SQL). Use when asked to start/scope a review, gather review inputs, check a protocol is complete, list included-with-full-text vs abstract-only, retrieve full-text content, or audit full-text provenance.
license: Apache-2.0
---

# SynthScholar Review Skills

Two capabilities for working with systematic reviews produced by SynthScholar:

1. **Guided protocol intake** — before a review runs, guide the user through
   *every* input the agent supports and validate the protocol is complete.
2. **Provenance queries** — after a review runs, answer *which included
   publications have full text, which are abstract-only, what the content is,
   and where it came from*.

Pick the mode from the request: setting up / scoping a review → intake;
inspecting a finished review → queries.

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
   full PICO, inclusion/exclusion criteria, databases, and domain features
   (charting questions, `grouping_dimension`, appraisal domains) with
   domain-appropriate defaults. Use
   [references/protocol_intake.md](references/protocol_intake.md) as the field
   catalog — every field's default and validation is there.

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

**Queries (Mode 2)** — when the user asks to:

- **List included publications that have full text** (vs. everything included).
- **Distinguish full-text-included from abstract-only** inclusions.
- **Retrieve the full-text content** of included sources.
- **Audit provenance** — which resolver produced each full text (`pmc_oa`,
  `europe_pmc_oa`, `unpaywall_pdf`, `biorxiv_pdf`, `openalex_pdf`,
  `semanticscholar_pdf`, `article_store`, `cache`) and when.
- **Verify or backfill** the `article_full_text` provenance table.

Mode 2 is scoped to full-text availability/content/provenance of *included*
sources — not screening decisions or full charting extraction.

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

All live in [scripts/](scripts/). Run them from an environment where the
project's deps are importable (`rdflib` for SPARQL, `psycopg[binary]>=3.1` for
SQL; the backfill also needs the `synthscholar` package on `PYTHONPATH`).

```bash
# Intake: blank template → fill from answers → validate before running
python scripts/validate_protocol.py --print-template > protocol.json
python scripts/validate_protocol.py protocol.json

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
