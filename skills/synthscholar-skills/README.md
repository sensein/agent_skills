# synthscholar-skills

An [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills)
for setting up, running, and querying SynthScholar systematic reviews. Three
modes:

1. **Guided protocol intake** — draft a complete `ReviewProtocol` from minimal
   input, interactively lock only the pivotal decisions (as structured question
   cards), confirm, and validate before a run.
2. **Provenance queries** — after a review runs, answer *which included
   publications have full text vs. are abstract-only, what the content is, and
   where it came from* — across both the RDF export (SPARQL over the SLR
   ontology) and the PostgreSQL article store (SQL).
3. **Bring-your-own-corpus review** — the user supplies the PDFs they already
   collected; run the same PRISMA review over them (screening → charting →
   appraisal → synthesis → per-group analysis) and export Markdown plus
   SLR-ontology Turtle for triple-store ingestion. Their own search strategy is
   captured as provenance, up front or added later. Papers they have only a DOI
   for are retrieved open access first (Unpaywall → OpenAlex → Semantic
   Scholar), then through their institutional EZproxy session for whatever is
   still paywalled — so the review reads the paper instead of its abstract, and
   records which route got each one.

## Layout

```
synthscholar-skills/
├── SKILL.md                     # skill entry point (all modes, interaction pattern)
├── references/
│   ├── protocol_intake.md       # complete ReviewProtocol field catalog (intake)
│   ├── byo_corpus_review.md     # user-supplied-PDF workflow, schemas, PRISMA flow rules
│   ├── sparql_queries.md        # SPARQL recipe catalog
│   ├── sql_queries.md           # SQL recipe catalog
│   └── data_model.md            # SLR ontology + DB schema + provenance values
└── scripts/
    ├── validate_protocol.py     # scaffold + completeness-gate a protocol file
    ├── build_corpus.py          # PDFs → corpus.json (full text, hashes, provenance)
    ├── fetch_ezproxy.py         # DOIs → PDFs: open access first, institutional second
    ├── oa_client.py             #   └─ vendored Unpaywall → OpenAlex → Semantic Scholar
    ├── ezproxy_client.py        #   └─ vendored EZproxy client
    ├── run_local_review.py      # run the full pipeline over a supplied corpus
    ├── export_review.py         # validate + export a review to md/ttl/jsonld/bib/…
    ├── update_provenance.py     # add the search strategy later, then re-export
    ├── query_sparql.py          # run recipes against a .ttl / .jsonld export
    ├── query_postgres.py        # run recipes against the article store DB
    └── backfill_full_text.py    # populate article_full_text from legacy rows
```

## Quick start

```bash
# Intake: scaffold a protocol, fill it, gate it before running
python scripts/validate_protocol.py --print-template > protocol.json
python scripts/validate_protocol.py protocol.json

# BYO corpus: PDFs → review.md + review.ttl
python scripts/build_corpus.py --dir ./pdfs --out corpus.json
python scripts/build_corpus.py --check corpus.json            # completes? exit 0

# Missing PDFs: open access first (free, no setup) …
export SYNTHSCHOLAR_EMAIL=you@example.com                     # Unpaywall's ToS requires it
python scripts/fetch_ezproxy.py --status
python scripts/fetch_ezproxy.py --corpus corpus.json --oa-only

# … then your own institutional access for whatever is still paywalled
export EZPROXY_HOST=ezproxy.myuniversity.edu
export EZPROXY_COOKIE_FILE=~/ezproxy-cookies.txt              # exported from a logged-in browser
python scripts/fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs

python scripts/run_local_review.py --print-provenance-template > search_provenance.json
export OPENROUTER_API_KEY=sk-or-v1-...
python scripts/run_local_review.py --protocol protocol.json --corpus corpus.json \
    --provenance search_provenance.json --outdir out/ \
    --formats md ttl json charting appraisal per-group

# Re-export, or add the search strategy after the fact
python scripts/export_review.py out/review.json --outdir out/ --formats md ttl
python scripts/update_provenance.py out/review.json \
    --provenance search_provenance.json --registration CRD42026123456

# List available named queries
python scripts/query_sparql.py --list
python scripts/query_postgres.py --list

# RDF export (SPARQL)
python scripts/query_sparql.py review.ttl --query included-full-text
python scripts/query_sparql.py review.ttl --query status-all --format csv

# PostgreSQL (SQL) — DSN via --dsn or $SYNTHSCHOLAR_PG_DSN
python scripts/query_postgres.py source-breakdown
python scripts/query_postgres.py get-content --pmid 39012345

# Backfill provenance table (after applying migration 006)
python scripts/backfill_full_text.py
```

## Which SynthScholar build you need

Modes 1 and 3's corpus/EZproxy steps run anywhere. **Everything that touches the
review pipeline or the provenance fields needs the development checkout, not the
released package** — PyPI's latest (0.0.11) predates the whole-document reading,
full-text screening and retrieval-provenance work:

```bash
pip install -e /path/to/prisma-review-agent    # the checkout, not `pip install synthscholar`
```

| Script | Runs on released 0.0.11? |
| --- | --- |
| `validate_protocol.py`, `build_corpus.py`, `update_provenance.py` | yes — no app import, or models only |
| `fetch_ezproxy.py` | yes — the skill vendors `oa_client.py` and `ezproxy_client.py`, so it needs only `httpx` |
| `export_review.py`, `run_local_review.py` | **no** — they refuse to run and say what's missing |

That refusal is deliberate. Pydantic drops unknown fields silently, so an older
package would accept a review and quietly discard `assessed_on`,
`full_text_retrieved`, `full_text_sources` and the rest — producing an export
that looks complete but records nothing about what was actually read. The check
is by feature, not version string (the package's `__version__` and its
`pyproject.toml` version have drifted apart).

## Requirements

- SPARQL scripts: `rdflib`.
- SQL scripts: `psycopg[binary]>=3.1`.
- Backfill: the `synthscholar` package importable, and migration 006 applied.
- BYO corpus: `pip install 'synthscholar[fulltext]'` (adds `pymupdf` for PDF
  text). `run_local_review.py` also needs `OPENROUTER_API_KEY`; without a key,
  the agent authors the result JSON itself and `export_review.py` serialises it
  through the same exporters.
- Retrieving missing PDFs: `httpx`. **Open access needs no credentials** —
  `SYNTHSCHOLAR_EMAIL` only to include Unpaywall (their ToS), optionally
  `SEMANTIC_SCHOLAR_API_KEY` to lift a rate limit. Run `--oa-only` first; a
  mostly-OA corpus needs nothing else.
- Institutional access, for what open access can't reach: `EZPROXY_HOST` plus a
  session cookie (`EZPROXY_COOKIE_FILE` or `EZPROXY_COOKIE`) exported from a
  browser already signed in to your library's proxy. Credentials are read from
  the environment only — never a CLI flag, since the command line is stored
  verbatim in provenance. Requests are serialised, delayed (`EZPROXY_DELAY`) and
  capped (`EZPROXY_MAX_REQUESTS`); using this within your library's terms is
  your responsibility. Open-access hits never consume that budget.

## Integration requirements (structured questions)

Mode 1's intake works best by surfacing the pivotal decisions as **structured
question cards** with a recommended option — the one-click experience described
in SKILL.md. That rendering is provided by the **host**, not by this skill: the
skill can *instruct* the agent to ask structured questions, but it cannot force
a given environment to draw the cards.

- **Claude Code / Claude apps** — the `AskUserQuestion` tool is built in, so the
  cards render automatically. No extra work.
- **Custom web UI / your own agent host** — you must expose an equivalent
  tool (e.g. an `AskUserQuestion`-style function that returns the user's choice)
  for the cards to appear. Without it, the agent **degrades gracefully** to
  asking the same pivotal decisions as plain text — the intake still works, it
  just isn't one-click.

The intake never *depends* on the cards for correctness; they are a UX
enhancement. Everything downstream (draft → confirm → `validate_protocol.py` →
run) is host-agnostic.

## Installation as a skill

Copy or symlink this directory into a skills path Claude Code discovers, e.g.:

```bash
ln -s "$PWD/synthscholar-skills" ~/.claude/skills/synthscholar-skills
```

Then invoke it by asking to set up / scope a review, or about included
publications and their full-text status.
