# Changelog

## 0.6.1 — Dictionary coverage, citation detection, duplicate handling

Three of these are other people's fixes, verified before applying and credited as
such; the rest are the regressions they exposed.

### NDA structure discovery (fix from a parallel audit)

- `_rows_from_nda` matched structures by `shortName.startswith("abcd_")`, which finds
  292 of ABCD's 420 NDA structures. The missing 128 include `acspsw03` (which holds
  `race_ethnicity` and `rel_family_id`, variables a large share of ABCD papers use),
  the whole KSADS diagnostic set, `fhxp102`, the nBack beta-weight tables and Barkley
  EF — 27,504 variables that resolved as unverified. Matching on the short name *or*
  the study word in NDA's own title fixes it: **85,984 → 116,353 variables**,
  142,913 → 189,493 resolvable names, none lost.
- Sneaky because it degraded `abcd_nda_api.known_tables()` too, so the live API
  second opinion could not rescue what the offline snapshot never knew — offline and
  online failed together.
- Verified independently before applying: the NDA index has 6,433 structures, 292
  match by prefix, 420 by prefix-or-title, and all 128 additions are unambiguously
  ABCD ("ABCD Cash Choice Task", "ABCD Family History Assessment"). Immediate effect:
  "Child race/ethnicity" resolves to `race_ethnicity` in `acspsw03` instead of a
  discrimination item in `abcd_ydmes01`.

### Citation detection (fix from the same audit)

- `_CITATION_RE` put the opening `\(` outside the alternation, so the narrative
  branch (`Telzer and Fuligni (2013) found`) and the numeric branch (`[12]`) were
  dead code — they could only match a sentence that literally opens a paren. Gate 2
  was passing cited work whenever prior-work phrasing happened to be absent.
- The branch now also accepts `&` (as common as "and" in APA prose) and the
  single-author form (`Steinberg (2001) warned`). Verified both directions on 12
  strings: all citation forms fire; the paper's own parentheticals — `(35 items;
  e.g., …)`, `(n = 11,868)`, `(CFI = 0.98)`, `(FES-Conflict; Moos & Moos, 1976)` —
  stay silent.
- Gap statements are exempted: "no prior work has considered … problem behaviors" is
  the paper motivating its own study, and it was rejecting the construct in this
  paper's own title on the words "prior work".

### Fields NDA returns that the code discarded

- `nda_releases` (from `sources`) and `level_range` (from `valueRange`) are now
  populated. A field must be in **both** `KEEP_COLUMNS` and `MINIMAL_COLUMNS` to
  survive a minimal build — `_keep` filters on the first, the projection re-filters
  on the second — and `level_range` was already in `KEEP_COLUMNS`, so an earlier
  version of this entry duplicated it.
- `nda_releases` is now used, not just stored: a paper naming release 2.0/3.0/4.0 is
  matched against that release's rows only, and a literal name resolving outside it
  is flagged `nda_release_conflict`.

### Snapshot shadowing

- A local snapshot beats a bundled one by design; it also did so silently. The stale
  85,984-variable local build shadowed the corrected bundle, so applying the fix
  changed nothing until the local file was moved aside. Shadowing now warns with both
  counts and the exact `mv` command.

### Duplicate inputs and duplicate variables

- Inputs are deduplicated by SHA-256 **before** any PDF is parsed, so `paper.pdf` and
  `paper(1).pdf` are one paper — a duplicate otherwise doubles one study's weight in
  every paper-counted consensus. The clean filename is kept over the copy.
- Variable entries are merged per (variable, timepoint) within a paper, keeping the
  strongest mapping and every wording and quote (`also_written_as`, `merged_from`).

### Regressions this exposed, and their fixes

- **The instrument index accepted any label head.** "Median family income" was
  indexed as an instrument and claimed the mention "family income". A head is now
  only an instrument if it names one (scale, checklist, inventory, questionnaire,
  interview, test, task, toolbox, …): 18,397 "instruments" → 603 real ones.
- **A long-label penalty demoted correct answers.** Introduced to stop "scan site"
  matching a 30-word COVID question, it also demoted `fes_p_ss_fc`, whose label
  spells out its item formula, below its sibling subscales. The penalty is now
  confined to labels that are questions.
- **An instrument guessed from context could not be overridden.** Now, when the paper
  names an instrument and the resolved variable sits in a *different* instrument's
  table, the instrument wins ("Youth Self-Report (YSR)" was landing on a
  prosocial-behaviour scale that shares only "youth" and "report").
- **Non-response codes outscored measures.** `devhx_2_p_dk` ("Birth weight, pounds.
  Don't know") beat `birth_weight_lbs` on every content word; "don't know" /
  "refused" labels and `_dk`-suffixed names are now treated as administrative.
- **`ambiguous` was reported as `not_a_variable_name`.** The matcher's refusal to
  choose between candidates is different information from finding nothing, and the
  status now says which.
- Study-design wording is substituted before matching from a short documented table
  ("scan site" → "Site ID at each event", "child sex" → "Sex of subject at birth"):
  "scan site" scored 0.78 against a COVID item containing both "scan" and "on-site"
  while `site_id_l` scored 0.57 and lost.
- Respondent cues now record what said parent/youth, where it came from, and whether
  that was a table note — this corpus contains a paper whose Table 1 note contradicts
  its own Methods about which FES version was used, and the two are different
  measures.

## 0.6.0 — Context-aware variable mapping, own-study scope, provenance-carrying synthesis

Driven by a real three-paper run whose output was mostly empty columns: 1 of 57
variables carried a dictionary table, and the synthesis said which construct diverged
without saying which paper measured what, with which instrument, in which release.

### Mapping the paper's wording (`scripts/abcd_context.py`, new)

- Matches a paper's phrasing against dictionary **labels**, not just names. Most
  papers never print `nihtbx_cryst_fc`; they write "Crystallized Cognition Composite
  Score", which resolved to nothing before.
- Disambiguates with the paper's own statements: **instrument** scopes candidates to
  one table, **respondent** filters (`fes_y_ss_fc` vs `fes_p_ss_fc` are different
  measures), **metric** picks `_t` over `_r`, and the stated **release** decides which
  snapshot is eligible — matching a 5.0 paper against 6.1 manufactured rival
  candidates in two tables.
- Refuses to name a variable the paper did not: `context_family` (68 thickness ROIs
  fit "cortical thickness" equally well), `context_domain`, `instrument_table`. Table
  and domain are still reported; the variable stays null.
- Every result carries `context_mapping`: cues fired, ranked candidates with scores,
  thresholds applied, and the reason a single variable was or was not named.
- Bridges inflection ("externalizing behaviors" vs the CBCL's "External ... Scale")
  and the handful of documented naming differences where a paper and the dictionary
  share no content word at all (axial/longitudinal diffusivity,
  connectivity/correlation, surface/cortical area).
- The instrument scope is dropped and the match rescored when scoping excludes every
  candidate — an instrument read out of the surrounding sentence is a guess, and one
  wrong guess ("functional MRI", from a paragraph that also described cortical
  thickness) discarded all 136 correct candidates in the structural table.
- Result on the sample corpus: variables carrying `nda_or_nbdc_table` went from 1/57
  to 32/57 (29 of 49 distinct variables in the synthesis).

### NDA element API (`scripts/abcd_nda_api.py`, new)

- `element()` confirms a printed name with its structures and aliases —
  `verified_via_nda_api` for a release we do not bundle.
- Full-text element search is available but its hits are recorded as
  `nda_api_suggestions`, **not** as mappings. Every table NDA can return is already in
  the snapshots, and NDA ranks over the whole archive: asked about "internalizing
  behaviors" it offered an *Adult* Behavior Checklist score, and "age at time of scan"
  returned an SST series timestamp. An earlier build of this release did claim those
  as mappings; it was wrong.
- Cached under `~/.cache/structsense/nda_api`; `--nda-api auto|on|off`, and `auto`
  skips live search above 25 papers rather than making thousands of requests.

### Own-study scope (gate 2 in `scripts/abcd_verify.py`)

- Variables and findings attributed to cited work are rejected
  (`finding_attributed_to_cited_work`, `measure_only_mentioned_in_cited_work`), with
  the signals that decided it recorded per item. Findings are held to the stricter
  bar: a results-bearing section or first-person framing.
- Without this, one paper's summary of another arrives in the synthesis as
  independent evidence.

### Coverage audit

- `coverage.referenced_but_not_declared` lists every string a model or finding names
  that `variables[]` never declared. Those rows previously reached the synthesis with
  no quote, no table and no domain, looking like ordinary variables.

### Synthesis rewritten (`scripts/abcd_synthesize.py`)

- **`claims[]`**: a statement per construct, the evidence paper by paper with a
  strength rating derived only from reported facts (sample band, design, effect size
  present, subgroup-only), contradictions listed separately, and caveats — including
  "these papers report the same sample size, so their agreement is not independent".
- **Provenance on every row**: per-paper wording, instrument, respondent, metric,
  roles, timepoints, resolved variable, table, domain, **dd release**, quotes.
- **Constructs carry their measures**, with paper-declared measures kept apart from
  variables that merely appear in a construct's findings.
- **Datasets per paper**: release, sample, analytic sample, design, waves, cohort,
  sites, source — so "three papers agree" can be read as "three papers agree, all
  analysing the same 11,868 children".
- **Maths fixed**: agreement is now over paper-direction claims (papers as the
  denominator printed `1.00` agreement beside a `divergent` verdict); role
  consistency requires `role_exclusivity` as well as share, so a variable that is a
  mediator in every paper *and* an outcome in every paper reads as contested.
- **Identity fixed**: rows merge on the resolved dictionary variable, then declared
  aliases, then the normalised mention — "family income" and "Family income" were two
  rows with one paper each. Never on similarity, and a `mapping_disagreement` is
  reported rather than resolved.

### Extractor prompt and schemas

- Per-variable `instrument`, `respondent`, `metric`, `aliases`; per-construct
  `measured_by`; per-finding `analytic_n`; document-level `analytic_sample`,
  `timepoints`, `cohort`, `site_count`, `data_source`.
- An exhaustiveness checklist naming the five places variables hide (Table 1 rows, the
  covariate list, the Measures section, per-wave instances, self-computed composites).
- Both schemas updated and validated against real output.

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

Releases 4.x and 5.x come from NDA's public data dictionary (`--from-nda`): 292
`abcd_*` structures, ~86k elements, cached per structure so a rebuild is free and an
interrupted run resumes. NBDC releases start at 6.0, so without this every pre-6.0
paper verified against the wrong era. Loading both eras together resolves a paper
from either and shows the bridge between them.

The skill now ships its own dictionaries in `data/dictionaries/`: ABCD nda-legacy
(4.x/5.x), 6.0, 6.1, 7.0 and HBCD 1.0, 1.1, 2.0 — 539,781 variables in 8.6 MB.
Minimal columns plus gzip does that (the same data is 250 MB+ raw), and snapshots
load transparently from either .json or .json.gz. Extraction therefore works with no
workbook in ~/Downloads, no network and no R; a locally built snapshot still wins
over a bundled one for the same study+release, and `--from-xlsx` with no path
auto-discovers the catalog workbook instead of hard-coding somebody's download
directory.

Requirements files, so an agent can install without guessing: `requirements.txt`
(core — requests, jsonschema, pymupdf + pdfminer.six, openpyxl) runs every mode with
the agent as the model, verified in a clean environment. `requirements-llm.txt`
(provider SDKs) is only for the API path, `requirements-ner.txt` (transformers,
torch) only for the NER ensemble, `requirements-dev.txt` (rdflib, pandas) only for
validating the skill's own output. Splitting them keeps a plain ABCD run from
dragging in torch.

`--llm-model` is optional, because in Claude Code / Codex the calling agent IS the
model and there is no API to call. `--prepare` extracts text and prints a plan;
`--payload` (a .json, a directory of <stem>.payload.json, or a .jsonl) verifies and
exports an agent-produced payload with no LLM call. Verification and all outputs are
identical on both paths, and provenance records which one ran. Passing neither is an
error rather than a guess, since one path spends API credits.

Input handling is now one argument with no mode flags: a PDF, a directory, a
CSV/TSV/XLSX of DOIs, a DOI list, or a bare DOI. DOIs are fetched open access only
(Unpaywall -> OpenAlex -> Semantic Scholar), magic-byte verified so a paywall
answering 200 text/html is reported unresolved instead of saved as a broken PDF.
More than one paper implies a synthesis.

The preferred dictionary source is the NBDC variable catalog workbook
(`--from-xlsx`, `--all-sheets`): one sheet per study+release, ~83-96k variables
each, read with openpyxl. It carries the alternate namings, which is what makes the
mode work on real papers — ABCD 6.x renamed variables wholesale, so
`nihtbx_flanker_uncorrected` from a 2022 paper appears nowhere in 6.1's `name`
column but resolves through `name_nda` to `nc_y_nihtb__flnkr__uncor_score`. NDA,
DEAP, REDCap, short and Stata names are all indexed, and the match method records
which naming the paper used.

Dictionary imports also accept a DEAP variable export or an NDA data-dictionary
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
