# Changelog

## 0.5.0 — ABCD / HBCD extraction and cross-paper synthesis

New mode for publications that analyse ABCD or HBCD data. Extracts what a study
actually **used** (variables, constructs, models) and **found** (findings with
direction and role), then compares across papers.

**The paper is the only source of what a study used.** The NBDC data dictionary and
the Cognitive Atlas are used to verify and join, never to enumerate.

- `scripts/abcd_dictionary.py` — release snapshots from NBDCtools (`nbdctools` reads
  `lst_dds` without R) or your own CSV export, with source/sha256/timestamp
  provenance. Resolution is exact name → normalised name → full label, never fuzzy.
  Load several releases and renames surface as `dd_release_gap`.
- `scripts/cognitive_atlas.py` — construct vocabulary (918 concepts, 856 tasks),
  cached after one fetch. Exact/singular/alias only; unmapped stays unmapped.
- `scripts/abcd_verify.py` — strict verification. Every item's quote must be
  findable verbatim in that paper (≥25 chars, PDF artefacts normalised); failures
  land in `rejected[]` with a reason instead of vanishing. Per-section rules: a
  variable name must appear literally, a construct need not (it is a reading of the
  prose — `label_in_quote` records which), a finding/model must name a variable
  present in its quote.
- `scripts/abcd_extract.py` — one PDF or a directory (`--bulk`, keeps going past
  failures), `--synthesize` for the cross-paper pass, `--reverify` to re-check an
  existing result with no LLM calls.
- `scripts/abcd_synthesize.py` — consensus/divergence per construct and role
  consistency per variable, **counted by paper, not by finding**, so a verbose paper
  cannot outvote several others. `divergent` means opposing signs, not differing
  magnitudes; a contested mediator/moderator role is reported as contested rather
  than resolved by majority.
- `scripts/abcd_export.py` — every run emits **JSON + Markdown + Turtle**. The
  Turtle carries quote, `usedContext`, char offsets, section/page,
  `mentionAsWritten`, `ndaOrNbdcTable`, `nbdcDomain` and PROV-O provenance, so
  "where in the paper did this come from?" is a SPARQL query.
- `prompts/extractor-abcd.md`, `schemas/abcd-paper.schema.json`,
  `schemas/abcd-synthesis.schema.json`, `references/abcd-extraction.md`, and
  SKILL.md hard rule 16.

Dictionary imports accept a DEAP variable export or an NDA data-dictionary
download as-is (CSV or TSV, BOM tolerated, headers matched across the spellings
those sources use), recording the header translation in provenance. DEAP's own API
is behind NDA login and is not scraped. ABCD release ids are cross-checked against
the public release notes at docs.abcdstudy.org, which supplies a citation URL per
release — advisory, never blocking.

Variables report both sides of the join: `mention_as_written` (how the paper wrote
it — prose label or id) alongside the resolved variable, `nda_or_nbdc_table` and
`nbdc_domain`.


All notable changes to `structsense`. Versions follow semantic versioning.
The `version:` field in `SKILL.md` frontmatter is the source of truth; this file
records what changed between versions so you can tell which features your local
copy has.

## [0.4.0] — 2026-06-05 (zero-hallucination policy)

### Concept mapping policy (breaking)
- **Mapping is mandatory and tool-only.** The cascade no longer silently
  skips alignment. Default cascade: local hybrid (`http://localhost:8000`,
  verified at `/docs`) → BioPortal → ask user for an alternate URL →
  **hard-stop** with an actionable error. The skill no longer falls back
  to OLS by default (OLS lacks gene coverage); pass `--allow-ols-fallback`
  to opt in.
- **`concept_mapping_provenance: "llm_knowledge"` is forbidden in canonical
  output.** Any item carrying it is automatically demoted to `unmapped`,
  with `alignment_method: "validation_failed"`. The item itself is
  preserved (exhaustive extraction is not compromised); only the
  fabricated mapping is dropped.

### New: `scripts/iri_validation.py`
- Per-ontology regex patterns for UBERON, CL, NCBITaxon, MONDO, DOID, HP,
  CHEBI, DRON, BTO, OBI, MP, HGNC, NCBIGene, MGI, UniProt, PR, GO, EFO,
  NIFSTD, CIDO, plus a permissive structural fallback for unknown
  ontologies (`<NS>_<NUM>` OWL IRIs, identifiers.org, semanticweb.org).
- Accepts **legitimate cross-ontology mappings** (e.g. CIDO results that
  reuse HP IRIs as synonyms) — the declared `ontology` field does NOT
  have to match the IRI prefix. We only reject `llm_knowledge` provenance
  and structurally-malformed IRIs.
- Adds `result["validation"]` with `passed` / `demoted` counts and
  `demoted_by_reason` breakdown (`llm_knowledge_rejected`,
  `malformed_iri`, `wrong_ontology_pattern`, `missing_ontology_id`).

### Stats clarity
- **`stats.totals` block at the top** of every result. Top-level
  `total_items`, `total_entity_mentions`, `total_key_term_mentions`,
  `total_resources`, `unique_entities`, `unique_key_terms`. The human
  summary shows `TOTAL ITEMS: …` as the second line after `task_type`.
- **`stats.task_type` auto-inferred** when missing — entities present →
  `ner`; resources → `resource`; activity/items → `structured_extraction`.
- **`alignment.by_method`** no longer shows `{missing: N}` for items that
  have `concept_mapping_provenance` set. The normalizer fills
  `alignment_method` from the provenance (`tool` → `direct_tool_call`,
  `skipped` → `skipped`).

### Connecting / runtime
- **`connecting/claude-desktop.md`** added. Explains Claude Desktop's
  **split execution model** (chat UI on your machine, code interpreter in
  Anthropic's cloud sandbox), why your `localhost:8000` is unreachable
  from a sandboxed code-interpreter call (`/home/claude/work/...` paths
  are the giveaway), and how to bridge via an MCP server configured in
  `claude_desktop_config.json`.

### Stats fix verified against real numbers
Smoke-tested against the user's actual run (2331 entity mentions across
15 labels, 210 key terms, 922 tool-mapped + 1619 skipped). The new
output:
- `totals.total_items: 2541` (prominent at top)
- `task_type: "ner"` (was `null`)
- `alignment.by_method: {direct_tool_call: 922, skipped: 1409, missing: 210}` (was `{missing: 2541}`)

## [0.3.1] — 2026-06-05 (later same day)

### Concept-mapping (fixed)
- **API schema bug fixed.** `scripts/local_hybrid_map.py` was POSTing
  `{terms: [...]}` but the real `/map/batch` endpoint takes
  `{text: [{text, context?}], max_results}`. Verified against a live
  deployment. The client now supports either a flat list of strings or
  shaped dicts with optional per-term `context`. Also handles the real
  response shape (`results` is a dict keyed by input text, score is
  `final_score`).
- **`prompts/alignment-via-http.md`** added: a turnkey curl + jq pipeline
  for hitting `/map/batch` directly. Use this when in Claude Code (Bash
  available) but the Python client isn't loaded.
- **`SKILL.md` rule #10 strengthened**: "you MUST probe `curl
  ${MAPPER_URL}/docs` before declaring the mapper unavailable." Plus a
  runtime-reachability table (Claude Code → yes; claude.ai web →
  no, needs MCP bridge; ChatGPT cloud → no, needs Action).

### New scripts ported from structsense
- **`scripts/input_loader.py`** — PDF / CSV / TXT / MD ingestion. PDF chain:
  GROBID → PyMuPDF (`fitz`) → pdfminer.six. CLI writes a sibling `<stem>.txt`
  so subsequent NER stages have stable character offsets.
- **`scripts/task_detection.py`** — auto-detect task type (ner / resource /
  structured_extraction / relation_extraction / keyphrase_extraction / …)
  from a free-text description. Heuristic regex first; LLM fallback via
  `llm_client`.
- **`scripts/model_context.py`** — model context-window registry (~50 model
  families, longest-match wins) + `estimate_payload_tokens` +
  `compute_downstream_chunk_size` for sizing alignment/judge chunks
  programmatically.
- **`group_by_entity.unify_ontology_across_entities`** — when the same
  surface form gets different ontology IDs from different chunks/models
  (parallel extraction is the usual cause), unify on the best one
  (tool-mapped > llm_knowledge > unmapped). Runs automatically inside
  `attach_grouped_views`. Mentions are preserved; only the ontology
  fields are normalized.

## [0.3.0] — 2026-06-05

### Schema (breaking)
- **`paper_title` / `doi` removed from per-entity items.** They now live ONCE at
  the top level under `source_metadata: { paper_title, doi, source_path }`.
  `paper_location` (section / page / paragraph) stays per-entity because it
  varies per mention.
- **`source_model` provenance** added to every entity item (HF model id, or
  `llm_ner:<llm_model>` for the LLM extractor).
- **`entities_grouped[]`** (and `key_terms_grouped[]`) added: per-entity index
  collapsing raw mentions by canonical `(entity.lower(), label)`. Each group has
  `source_models[]`, `source_model_counts{}`, `consensus_count`,
  `judge_score_max/avg/min`, deduplicated `sentences[]` with their
  `paper_locations[]`, and slim per-occurrence `mentions[]`.
- **`stats` block** always embedded: totals, label histogram,
  **by_source_model** histogram, alignment provenance, judge score buckets,
  per-stage timings.

### Pipeline
- **Post-processing normalizer** (`scripts/normalize_result.py`) added. Runs
  automatically before every save, and is also exposed as a CLI so legacy
  result files can be normalized in place. It lifts per-entity
  `paper_title`/`doi` into `source_metadata`, tags missing `source_model`,
  attaches `entities_grouped`, and computes `stats`. **Idempotent.**
- **Mapper cascade**: local hybrid (`http://localhost:8000`, verify via
  `/docs`) → BioPortal → ask the user for an alternative URL → skip alignment.
- **HuggingFace NER ensemble** (`scripts/ner_models.py`): d4data,
  BC5CDR-chem, NCBI-disease, BioBERT-genetic, BENT-PubMedBERT family
  (Gene/Chemical/Disease/Anatomical/Cell-Type/Cell-Line/Organism/Bioprocess),
  Clinical-AI-Apollo, blaze999. Selectable via `--ner-profile`:
  `biomedical_broad` / `cns_cells` / `pharmacology` / `genetic` / `clinical` /
  `minimal` / `all`.
- **Final filename** is `<input_stem>_final.json` by default.

### NER prompts
- Three variants: **general**, **neuroscience**, **CNS-cells**.
- Each ships with explicit **EXHAUSTIVENESS** rules ("extract every occurrence,
  do not deduplicate by surface form") and explicit **WRONG / RIGHT** example
  blocks for the `source_metadata` shape.
- **Mask-recall pass** (`prompts/mask-recall-pass.md`) and **mask-verify pass**
  (`prompts/mask-verify-pass.md`) for boosting recall and detecting label
  errors.

### Connecting (new)
- `connecting/claude-code.md` — install as a Claude Code skill (`~/.claude/skills/`
  or `.claude/skills/`).
- `connecting/claude-skills.md` — publish as a hosted Anthropic Skill.
- `connecting/custom-gpt.md` — wire as an OpenAI Custom GPT (Instructions +
  Knowledge files + Actions).
- `connecting/mcp-server.md` — expose as an MCP server.

## [0.2.0] — 2026-06-04

### Added
- Multi-stage pipeline pattern (extractor → alignment → judge → human feedback).
- Three NER variants and resource / structured-extraction prompts.
- Ontology mapping clients: BioPortal, OLS, local hybrid.
- JSON repair, span validator, chunking helpers.
- Reference pipeline driver (`scripts/pipeline.py`).

## [0.1.0] — 2026-06-03

- Initial skill scaffolding from the structsense pipeline patterns.
