# synthscholar

An [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills)
for setting up and querying SynthScholar systematic reviews. Two modes:

1. **Guided protocol intake** — draft a complete `ReviewProtocol` from minimal
   input, interactively lock only the pivotal decisions (as structured question
   cards), confirm, and validate before a run.
2. **Provenance queries** — after a review runs, answer *which included
   publications have full text vs. are abstract-only, what the content is, and
   where it came from* — across both the RDF export (SPARQL over the SLR
   ontology) and the PostgreSQL article store (SQL).

## Layout

```
synthscholar/
├── SKILL.md                     # skill entry point (both modes, interaction pattern)
├── references/
│   ├── protocol_intake.md       # complete ReviewProtocol field catalog (intake)
│   ├── sparql_queries.md        # SPARQL recipe catalog
│   ├── sql_queries.md           # SQL recipe catalog
│   └── data_model.md            # SLR ontology + DB schema + provenance values
└── scripts/
    ├── validate_protocol.py     # scaffold + completeness-gate a protocol file
    ├── query_sparql.py          # run recipes against a .ttl / .jsonld export
    ├── query_postgres.py        # run recipes against the article store DB
    └── backfill_full_text.py    # populate article_full_text from legacy rows
```

## Quick start

```bash
# Intake: scaffold a protocol, fill it, gate it before running
python scripts/validate_protocol.py --print-template > protocol.json
python scripts/validate_protocol.py protocol.json

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

## Requirements

- SPARQL scripts: `rdflib`.
- SQL scripts: `psycopg[binary]>=3.1`.
- Backfill: the `synthscholar` package importable, and migration 006 applied.

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
ln -s "$PWD/synthscholar" ~/.claude/skills/synthscholar
```

Then invoke it by asking to set up / scope a review, or about included
publications and their full-text status.
