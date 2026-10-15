---
name: neuroimaging-kg
description: >-
  Use when building, extending, validating or querying a knowledge graph for a neuroimaging study
  or dataset — participants, phenotypes and assessments, scans, derived measures (brain volumes,
  connectivity), analysis provenance, cohorts, published claims, or links to other labs' graphs —
  including converting BIDS or phenotype tables to NIDM, joining a lab's data to public NIDM files
  (ReproNim, OpenNeuro, ABIDE), answering competency questions with SPARQL, or deciding what may
  be shared (ReproPond/ReproLake, consent scope).
---

# Neuroimaging knowledge graphs

A knowledge graph is a set of statements (subject → relation → object) that share identifiers.
For a neuroimaging study that means: who was scanned, what was measured and how, what analysis
produced which number from which inputs, which cohort a result used, and which published claim
rests on which data. This skill builds such a graph from standard tools, joins it to other graphs,
checks it, and answers questions from it. It **orchestrates existing tools**; it does not
re-implement them.

Plain-language terms used below: **NIDM** — Neuroimaging Data Model, a vocabulary for participants,
acquisitions, measurements and derivatives. **PROV** — W3C provenance standard (which activity
produced what, from what, by whom). **SHACL** — machine-checkable rules a graph must satisfy.
**SPARQL** — the query language for graphs. **Oxigraph** — a database that answers SPARQL.
**Competency question** — a question the graph must be able to answer, with a known answer.

## Before you start

- **Find the skill's files, don't search the disk.** Everything below lives next to this file:
  `SKILL_DIR=.claude/skills/neuroimaging-kg` (project install) or `~/.claude/skills/neuroimaging-kg`
  (user install). Use `$SKILL_DIR/scripts/…`, `$SKILL_DIR/assets/core_shapes.ttl`,
  `$SKILL_DIR/examples/abide_one_site/…`.
- **Run Python through uv.** `uv run $SKILL_DIR/scripts/<name>.py …`; for your own code
  `uv run --with rdflib --with pyoxigraph python x.py`. The system `python3` lacks these packages.
- **Pinned ABIDE sites:** `bash $SKILL_DIR/examples/abide_one_site/run_demo.sh <Caltech|SBL|OHSU|Stanford>`
  builds, validates and queries a site in seconds; start there and query its `graph/` and `data/`.
- **Identify things by persistent identifiers** in the graph and in answers: DOI IRIs for papers, ORCID
  for people, repository URL or RRID for tools, landing page or accession for datasets.
- **Consult and extend the wiki** (`$SKILL_DIR/wiki/patterns/`; index in `wiki/evolution_log.md`) when
  the source repository is available: it records failures and fixes from earlier runs. After a run
  that hit a new problem, add a short note to `wiki/experience/` (tried / happened / fix).

## Workflow — each step names its tool and the check that gates it

1. **Scope first.** Write 5–10 competency questions and say where each gold answer comes from
   (computed from the source files, not from the graph). Pin every input: commit or version plus
   SHA-256. Templates: [references/competency_questions.md](references/competency_questions.md).
   *Gate:* every input has a hash; every question has a gold source.
2. **Dataset layer (NIDM).** BIDS imaging → PyNIDM `bidsmri2nidm`. Phenotype/assessment tables →
   PyNIDM `csv2nidm`, run non-interactively with a JSON data dictionary that maps each column to a
   shared concept (`nidm:isAbout` → InterLex / nidm-terms / Cognitive Atlas / UBERON):
   `uvx --from pynidm==4.5.5 csv2nidm -csv table.csv -json_map terms.json -no_concepts -out phenotype.ttl -log .`
   Participants carry `ndar:src_subject_id`. *Gate:* participant count equals the table's row count.
3. **Derived measures.** Prefer tools that already write NIDM: ReproNim `freesurfer-nidm_bidsapp`,
   `ants-nidm_bidsapp`, `mriqc-nidm_bidsapp`, `segstats_jsonld`. Record each of your own analysis
   steps (cohort selection, connectivity, models) as PROV with
   `uv run scripts/record_activity.py --id … --label … --used IN --generated OUT --software name==ver --param k=v --agent "Name" --out provenance.ttl`
   (files are identified by SHA-256; outputs are linked to every input). For an AI agent use
   `--agent-kind ai --model <id>`. To read brain volumes out of ReproNim NIDM files (values in the site
   file, meanings in the tool CDE files), use
   `uv run $SKILL_DIR/scripts/nidm_volumes.py --nidm site_nidm.ttl --cde freesurfer_cde.ttl --cde fsl_cde.ttl --cde ants_cde.ttl --region <UBERON IRI> --out vols.csv`
   (volumes are selected by unit, mm^3). *Gate:* every output names its inputs, software version and agent.
4. **Publications and claims.** Extract claims with evidence spans with the `structsense` skill; run
   systematic literature searches with the `synthscholar` skill (exports SLR-ontology Turtle). Record
   each claim with its **scope** — population/cohort, measure, contrast, evaluation (association is
   not prediction) — and a verbatim quote plus source. Link dataset ↔ publication with a status
   (`mentions` | `uses-data` | `candidate`). *Gate:* every claim has scope and a checked quote.
5. **Join across graphs.** Identity is a reviewable link, never `owl:sameAs` by default:
   `uv run scripts/join_identity.py --local table.csv --id-col SUB_ID --graph their_nidm.ttl --normalize int --check AGE=about:<concept IRI> --reviewer "Name" --out links.ttl --report join.json`.
   Report matched / only-local / only-external / duplicates / conflicts. *Gate:* conflicts reviewed;
   `checks_compared` > 0 for every check (0 comparisons means the check found nothing).
   Before comparing values from two tools, confirm they are the same measure (unit, definition).
6. **Scope and sharing.** Mark each record's place: a lab's **ReproPond** (local store) or the shared
   **ReproLake**; federation through Neurobagel sends the query to the pond and returns only permitted
   results. Record `nkg:consentScope` as `broad-sharing`, `study-only` or `named-uses` (data with no
   research consent does not belong in the graph). *Gate:* nothing promoted to the lake unless its
   consent covers broad sharing (enforced in the core shapes).
7. **Validate.** Your own layers: `uv run scripts/validate_graph.py --data provenance.ttl --data links.ttl … --mutation-test`
   (core shapes in `assets/core_shapes.ttl`; the mutation test deletes derivation edges and must
   fail). Third-party NIDM: SHACL generated from PyNIDM's LinkML schema,
   `uvx --from linkml gen-shacl --non-closed nidm_schema.yaml > nidm_shapes.ttl`, run with
   `--warn-only` and record the findings. *Gate:* authored layers conform and the mutation is caught.
8. **Load and query.** One named graph per source in Oxigraph; run the competency questions and
   compare with gold: `uv run scripts/query_graph.py --graph urn:g:x=x.ttl … --queries q/ --gold gold.json --union`.
   *Gate:* every question with a gold answer passes.
9. **Optional.** Resources with dated activity snapshots (commits, issues, citing papers, as-of
   date) so searches can say how fresh the evidence is; publish to BrainKB with the `brainkb` skill.

Modeling snippets for each step: [references/modeling_patterns.md](references/modeling_patterns.md).
Known traps: [references/pitfalls.md](references/pitfalls.md).

## Worked example

`examples/abide_one_site/run_demo.sh Caltech` builds, validates and queries a graph for one ABIDE I
site (Autism Brain Imaging Data Exchange) from pinned public files in about 15 seconds after the
first download. See its README.

## Quick reference

| Need | Tool |
|---|---|
| Phenotype table → NIDM | `uvx --from pynidm==4.5.5 csv2nidm … -json_map … -no_concepts` |
| BIDS dataset → NIDM | `uvx --from pynidm==4.5.5 bidsmri2nidm -d <bids> -o nidm.ttl` |
| Record an analysis step | `scripts/record_activity.py` |
| Brain volumes from NIDM → table | `scripts/nidm_volumes.py` |
| Link participants across graphs | `scripts/join_identity.py` |
| Check rules + mutation test | `scripts/validate_graph.py` |
| Named graphs + competency questions | `scripts/query_graph.py` |
| Claims from papers | `structsense` skill |
| Literature review | `synthscholar` skill |
| Publish | `brainkb` skill |
