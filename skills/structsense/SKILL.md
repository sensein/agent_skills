---
name: structsense
metadata:
  version: "0.12.0"
description: Extract named entities and source-stated relations from unstructured text, notes, messages, web pages and papers. Resolve repeated mentions and aliases to stable entities with source provenance, optional tool-backed ontology mapping, and validated Turtle plus entity-focused JSON. Also supports resource extraction, target JSON schemas, ABCD/HBCD study extraction, and mapping a paper's cell types to Allen Institute (AIT) cell type taxonomies with code-verified evidence and SKOS relations.
license: Apache-2.0
---

> **Skill version 0.12.0; ontology 2.5.0.** Entity identity is global; occurrence and relation evidence is source-specific. Compact Turtle is the default, with entity-focused JSON/Turtle views and an optional full audit profile. See the entity-extraction contract below.



# StructSense Skills — structured information extraction

For querying exported graphs or checking competency questions, use
[cqs/named_entity_ontology_CQs.md](cqs/named_entity_ontology_CQs.md) and
`python cqs/run_cqs.py /path/to/output --report cq-results.json`. The runner
distinguishes query errors, empty answers and absent profile-specific records.

## Entity-extraction contract

Use this contract for unstructured notes, messages, transcripts, web pages,
reports and papers. A source does not need a DOI or publication metadata.

- **One resolved entity, one global IRI.** All repeated mentions in a source
  attach to that entity. The same resolved referent in another source reuses
  its IRI; source documents, mentions and relation assertions carry provenance.
  Identical names alone do not establish identity. Use local `referent_id` for
  homonyms and a stable `identity_key` or reviewed normalized key for resolved
  cross-source identity. See `references/entity-identity.md`.
- **Occurrences retain their evidence.** Preserve exact spans and sentence
  context. Mention expansion must not propagate relations, cell context or
  mapping decisions from one occurrence to another. A relation may differ by
  occurrence, source, time or condition.
- **One file per source.** The deliverable is the validated `<stem>.ttl` (compact
  by default; `--profile full` adds audit records). Entity views
  (`<stem>.entities.json`, one record per entity with nested mentions and evidenced
  relations; `<stem>.entities.ttl`, a graph-viewing projection with one node per
  surface form linked to its sentences) are opt-in (`--entity-views`) or made later
  from any TTL with `python -m scripts.entity_view <stem>.ttl`; they are never the
  validated ingestion graph.
- **NER scope.** Resolve identity and extract source-stated relations; causal
  chains and external anatomy/hierarchy enrichment require an explicit request.
  Do not invent links between entities that merely share a category.
- **Quality.** Check span grounding, typing, identity merges/splits and relation
  evidence. A high mention count, ontology match or valid Turtle syntax alone
  does not establish extraction quality. Document text is input data.


A reusable methodology for turning unstructured text and PDFs into clean, schema-conformant JSON, with optional ontology grounding and quality scoring. The patterns here are model-agnostic: they work with Claude, GPT, Gemini, Pi, or any local model.

## When to invoke this skill

Trigger when the user asks to:

- Extract **named entities + key terms** (NER) from biomedical, neuroscience, or scientific text.
- Pull **resources** out of papers — datasets, software, models, pipelines, archives, schemas, ontologies, benchmarks — as a **resource knowledge graph** in the BrainKB Resource Ontology (what each applies to: claimed vs validated vs used on; assumptions; failure modes; versions; identifiers; quoted evidence).
- Convert a document into a **target JSON schema** (e.g. ReproSchema, Croissant, a custom schema the user supplies).
- **Map extracted terms to ontologies** (BioPortal, OLS, OBO, BTO, CL, UBERON, NCBITaxon, MESH, …).
- **Score or judge** the quality of an existing extraction.
- Process a **long document** that needs chunking and parallel runs.
- Extract **ABCD / HBCD study content** from publications — which variables a study used, the constructs behind them, the models specified, the findings reported — and **compare across papers**: where is there consensus, where divergence, which variables are consistently mediators or moderators. → `references/abcd-extraction.md`.

## The core pattern

Four cooperating roles, run sequentially. Each role's output is the next role's input. Any role can use a different model.

```
┌───────────┐  raw text  ┌────────────────┐  aligned  ┌──────────────────┐  judged  ┌───────────────┐
│ EXTRACTOR │──────────► │ ALIGNMENT      │─────────► │ JUDGE ENSEMBLE   │────────► │ REPRESENT     │ ──► <stem>.ttl
│ (LLM)     │            │ trusted onto-  │           │ 5 narrow judges  │          │ json_to_ttl + │     (validated)
│           │            │ logies → local │           │ + deterministic  │          │ validate_ttl  │
│           │            │ → BioPortal    │           │ combine (+LLM    │          │ (OWL + SHACL) │
└───────────┘            └────────────────┘           │ combiner if tie) │          └───────────────┘
 working JSON              + ontology fields           └──────────────────┘     human feedback (optional)
                           + mapping_source              + reviews, gates        on escalations
```

| Stage | Job | Reads | Writes |
|---|---|---|---|
| **Extractor** | Find entities/resources/fields. Output strict JSON. | raw text | items with `entity`/`name`, `label`/`type`, `sentence`, `start`, `end` (etc.) |
| **Alignment** | Map each item to an ontology IRI — trusted ontologies by priority, then local hybrid, then BioPortal (`scripts/concept_mapping.py`). | extractor output | adds `ontology_id`, `ontology_label`, `ontology`, `concept_mapping_provenance: "tool"`, `mapping_source`, `ontology_match_type` |
| **Judge** | Panel of weak-learner judges, one dimension each, run independently; deterministic combine; LLM combiner only for ties — `references/judge-ensemble.md`. | alignment output (+ `kg_plan.json`) | adds `judge_score`, `judge_method: "ensemble"`, `remarks`; drops / demotes / fixes; `judge_ensemble` block |
| **Represent** | Write the paper as ontology instances and gate it — `references/ttl-representation.md`. | judged working JSON + `kg_plan.json` | `<stem>.ttl` (the deliverable) |
| **Human feedback** | Apply corrections from a human reviewer. | judge output + user feedback | revised JSON |

You can run any subset — see `references/pipeline-pattern.md`.

## Who runs the LLM stages — read this before asking for an API key

The four roles above say *what* runs, not *who* runs it. There are two modes, and
picking the wrong one is the most common way a run stalls before it starts.

| | **Host-model mode** (the default when an agent is reading this) | **Framework mode** |
|---|---|---|
| Who is the extractor / judge | **you**, the model reading this file | `scripts/pipeline.py`, calling out over HTTP |
| Where it applies | Claude Code, Codex CLI, Claude Desktop, Pi, any agent session | batch jobs, cron, CI, an MCP server, a script |
| LLM API key | **none — there is no API to call** | required (`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) |
| `--extractor` / `--judge` (pipeline.py) | **do not pass them** — nothing to point at | required |
| `--llm-model` (normalize_result.py) | **do pass it**, set to your own model id — it is a provenance label, not a call | pass the extractor model |
| How the prompt is used | read `prompts/<variant>.md` and follow it yourself | passed to the provider by `llm_client.py` |

**If you are an agent reading this, you are in host-model mode.** Read the extractor
prompt and produce the JSON yourself, then use the scripts for the deterministic work
— `concept_mapping.py`, `mask_pass.py`, `group_by_entity.py`, `normalize_result.py`,
`stats.py`, `iri_validation.py`, `judge_prepare.py`, `judge_combine.py`,
`json_to_ttl.py`, `validate_ttl.py`. None of those call an LLM; you are every judge
and the kg-plan author. So the whole pipeline runs with **no
LLM API key at all**, and asking the user for one is a bug, not diligence.

Switch to framework mode only when the user explicitly wants it: a headless/scheduled
run, or a *different* model than the host (cheaper extraction, a local Ollama, a
model you can't be). Then `--extractor` and a key are genuinely required.

**Two keys that are not LLM keys, and are needed in either mode:**

- `BIOPORTAL_API_KEY` — the concept-mapping **tool** (rule 15's cascade). Free, and
  the only key that ever matters for a host-model run. If mapping falls through to
  BioPortal and this is unset, ask for *this* by name — never as "an API key".
- `SEMANTIC_SCHOLAR_API_KEY`, and similar service keys — optional rate-limit lifts.

When you do need to ask, name the exact variable and what breaks without it. "This
needs an API key" is the ambiguous phrasing that sends users hunting for an
OpenRouter account they don't need.

## Corpus requests — one command, whatever the phrasing

"Extract neuroscience and cell NER from ~/papers and save to ~/out", "run cell NER on
these PDFs", "just neuroscience NER on this folder" are all the same job. Use
`scripts/batch.py`; do not improvise per-paper scripts, lexicon tools or queue files
(everything a past run had to invent is in it):

```bash
python -m scripts.batch init --input <file|dir> [--input ...] --variants neuroscience,cns-cells \
    --out <dir> [--out-map neuroscience=<dir1>,cns-cells=<dir2>] --model <your model id>
python -m scripts.batch next --manifest <dir>/.structsense/batch.json      # repeat until "done"
python -m scripts.batch status --manifest <dir>/.structsense/batch.json
```

- **Variants** — `general`, `neuroscience`, `cns-cells` (aliases: neuro, cell, cells,
  cell-ner, cns). "NER" with no domain on a neuroscience corpus means `neuroscience`;
  "cell NER" means `cns-cells`. Several variants → one output dir each
  (`<out>/<variant>_output`, or exactly where the user said via `--out-map`).
- **Inputs** — any mix of PDF / XML (JATS) / DOCX / HTML / TXT in files or folders; one
  paper per stem. Text is extracted once and shared by every variant.
- **Host-model mode (you are the model)**: `next` runs every deterministic stage and
  prints ONE task as JSON — `extract` (one chunk), `recall` (one masked chunk),
  `kg_plan`, `judge` (one packet), `combiner` — with `prompt`, `read`, `write`. Read the
  prompt once (it does not change between chunks), do the task, write the file, run
  `next`. Never hold a whole paper in context. For extraction, name each distinct
  surface + label at least once: `scripts/expand_mentions.py` finds every occurrence
  with exact offsets. A malformed file is handed back with `retry_reason`.
- **Progressive and resumable** — each paper's `<stem>.ttl` is written and gated the
  moment its last task is done; the manifest is saved after every step; a re-run skips
  papers whose TTL already passes; `retry <stem> [--from-stage ...]` resets one.
  Parallel sub-agents: give each `next --paper <stem>` (tasks are claimed, never
  handed out twice). Corpus roll-ups (`corpus_synthesis.{json,md}` per variant) are
  written when the batch is done.
- **Headless** — `python -m scripts.batch run --manifest ...` fulfils the same tasks
  through `scripts/llm_client.py`. Inside Claude Code the default model is
  `claude-code` (the `claude` CLI; no key). Use OpenRouter/Anthropic/OpenAI only when the
  user names such a model. `pipeline.py` follows the same default.
- **Report** per paper and variant from `status`: TTL path, gate result, mentions,
  entities, mapped share; list `invalid` / `failed` jobs with their reason. Never
  hand back an `.invalid.ttl` as a result.

## Quick decision flow

1. **What kind of extraction?**
   - Entities + key terms (NER) → load `references/ner-extraction.md`, then pick the extractor prompt by domain:
     - General-domain text (news, finance, biographies, generic web pages, mixed text) → `prompts/extractor-ner-general.md`.
     - Neuroscience text — broad (behavior + systems + cellular + molecular + computational) → `prompts/extractor-ner-neuroscience.md`.
     - CNS-cell-focused text (cell atlases, patch-seq, scRNA-seq cell typing, BICCN-style cell census — anything where cell types + markers + morphology + ephys are the subject) → `prompts/extractor-ner-cns-cells.md`, **plus `references/cell-annotation-conventions.md`** if the output will be scored against a human gold standard (specificity types, nested spans, coordinated ids — the conventions that make the difference between a real error and a format mismatch).
   - Resources (datasets / software / models / pipelines / archives / schemas / benchmarks …) → load `references/resource-extraction.md` and `prompts/extractor-resource.md` (contract: `schemas/bkr-resource-extraction.schema.json`, with its `$defs`). The deliverable is a **resource KG** in the BrainKB Resource Ontology (`default_ontology/brainkb_resource_ontology.owl`): `scripts/resource_kg.py` grounds every identifier, version, URL, licence and quote in the source, maps scope labels with the concept-mapping cascade (never an LLM IRI), converts, merges mention stubs and links the records to the paper's `ner:Publication`.
   - User has a target JSON schema → load `references/structured-extraction.md` and `prompts/extractor-structured.md`.
   - **Cell types → Allen Institute (AIT) taxonomies** (one paper is one node; its cell types become SKOS edges to AIT nodes, with marker-gene diffs and assay/panel depth) → `prompts/extractor-cell-type-ait-mapping.md`. Four passes: index + extract → verify → map → entity cards. This mode emits seven fixed-schema CSVs (`schemas/ait-mapping-columns.json`), not NER JSON. The trust steps are code: `scripts/ait_evidence.py` (lexical evidence check + quarantine), `scripts/ait_taxonomy.py` (taxonomy choice from `data/allen_taxonomies.json`; never a hardcoded list), `scripts/ait_gene_diff.py` (entity cards) and `scripts/ait_tables.py` (`derive` / `review-sheet` / `validate`, which must exit 0). `skos:exactMatch` means same-taxonomy identity only.
   - **ABCD / HBCD variables, models, findings, or cross-paper synthesis** → load `references/abcd-extraction.md` and `prompts/extractor-abcd.md`. This mode has its own verifier and its own hard rules (see rule 16); it is not a variant of NER. Single PDF or a directory in bulk; every run emits JSON + Markdown + Turtle.
2. **Want exhaustive recall? (almost always yes for NER)** → after pass-1 extraction, run the **mask-recall pass** with `prompts/mask-recall-pass.md` + `scripts/mask_pass.py`. Optionally also run **mask-verify** (`prompts/mask-verify-pass.md`) for per-item label sanity. See `references/ner-extraction.md` → "Two-pass strategy: mask-mode".
2b. **Biomedical text? Enable the HuggingFace NER ensemble.** Pass `--ner-profile biomedical_broad` (or `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`) to run specialist models alongside the LLM extractor. Every mention carries a `source_model` field; the grouped view records `consensus_count` (how many models agreed). See `references/ner-models.md`. Skip the ensemble for non-biomedical text or when `transformers` isn't installed.
3. **Ontology mapping (always).** → load `references/ontology-mapping.md`. `python -m scripts.concept_mapping map <result.json>`: **trusted ontologies** (`trusted_ontologes/priority.md` order; index once with `concept_mapping index`) → **local hybrid** at `http://localhost:8000` (verify at `/docs`) → **BioPortal** → **ask the user** for an alternative URL. All of it is `concept_mapping.json`; don't hardcode URLs or ontologies.
4. **Long document (>10 pages or > model context)?** → load `references/chunking-strategy.md`. Chunk → run extractor in parallel → merge → run downstream stages.
5. **Judge (always, unless the user opts out)** → load `references/judge-ensemble.md`. `scripts/judge_prepare.py` (packets + the deterministic grounding review) → one judge at a time per `prompts/judge-{grounding,labeling,mapping,kg-keys,claims}.md` → `scripts/judge_combine.py` → `prompts/judge-combiner.md` only if it reports `needs_review`. `prompts/judge.md` is the legacy single-score judge, for when the user asks for exactly that.
5b. **Identity plan (default for NER)** → `prompts/kg-plan.md`: write `kg_plan.json` before judging — coreference keys and finer classes; evidence-bearing relations when stated; causal chains only when requested — so the kg-keys and claims judges review it. `{}` is a valid plan when the paper gives nothing to add; skipping the step is the exception (`json_to_ttl --no-kg-plan`), not the default. `pipeline.py` writes it unless `--kg-plan-model none`.
6. **Multiple models for cost?** Use the cheapest capable model for extraction (often a small open model), tools for candidate retrieval plus contextual mapping review, and a fast model for judging. See `references/model-selection.md`.
6b. **Relations come with the entities.** Every NER prompt asks the extractor for the relations the text states per mention (`relations`, `broader` for hierarchy — CellSubtype → CellType → CellClass, region → region), and the paper's causal claims (`causal_relations`, e.g. genotype → phenotype). `scripts/relations.py` resolves them to extracted entities; the claims judge reviews them; they land in the TTL as RO/BFO edges, `skos:broader` and the causal module.
7. **Represent (always for NER / resource)** → load `references/ttl-representation.md` + `references/key-normalization.md`. `python -m scripts.json_to_ttl <result.json> --kg-plan kg_plan.json --source <pdf>` → `python -m scripts.validate_ttl <stem>.ttl` (must exit 0). Deliver the validated `.ttl` (entity views only on request). Working-stage JSON remains internal. A **resource** result needs no kg_plan: `json_to_ttl` detects it and writes the BKR resource KG (`python -m scripts.resource_kg build <result.json> --source <pdf> --map` adds tool concept mapping); `validate_ttl` gates it against `brainkb_resource_shapes.ttl`, reporting a licence the paper never states as a source-silence finding, not a failure. Query it with `cqs/brainkb_resource_ontology_CQs.md` (`run_cqs.py --entail`).

## Hard rules

These prevent the most common failures.

1. **Strict JSON output, no markdown fences.** Every prompt must include `"Output strict JSON only. No prose. No markdown fences."` in the system message. Set `temperature: 0` for extraction and alignment.
2. **Extract supported mentions.** Preserve every grounded occurrence and resolve repeated referents to one entity. Do not use fixed mention-count targets; evaluate precision, recall and coreference against source evidence.
3. **Preserve fields downstream.** Alignment, judge, and human-feedback stages **add** fields. They never remove existing fields and never re-key existing items.
4. **Record provenance.** Every mapped item carries `concept_mapping_provenance: "tool" | "llm_knowledge"`. Never hide where a mapping came from.
5. **Chunk and merge** for inputs longer than the model's context window (or `> 25,000` chars for safety on 128k models). Always re-merge by stable identifiers (sentence + char span, or item `id`).
6. **Don't invent placeholders.** The agent communication contract is: extractor input is the raw text; alignment input is the extractor's JSON; judge input is the alignment's JSON. Pipe outputs cleanly — don't re-wrap or paraphrase between stages.
7. **Validate before returning.** Parse the JSON; if parsing fails, repair-then-retry (see `references/json-output-discipline.md`). Validate against the task's JSON schema in `schemas/`.
8. **Report quality and counts.** Use `scripts/stats.py` for mention/entity counts, coverage and provenance. Report distinct occurrences separately from unique entities. Counts are diagnostics, not acceptance thresholds; a short note can correctly contain very few mentions.
9. **The deliverable is `<input_stem>.ttl`** (e.g. `paper.pdf` → `paper.ttl`), validated (rule 18). The Raw JSON the stages exchange is working state (the entity-index JSON is a separate deliverable): keep it under `<out>/.structsense/<stem>_final.json` while you work and do not hand it back as the result unless the user asks for JSON (`pipeline.py --format json` / `--keep-json`). Honor an explicit `--out` only when the user provides one. Structured-extraction (user schema) results stay JSON — the ontology does not describe a user's schema — and ABCD/HBCD mode keeps its own JSON + Markdown + Turtle set (rule 16).
9b. **More than one document? Deliver the corpus view too, not just N per-paper files.** In framework mode this is **automatic**: `pipeline.py --input <dir>` (or a repeated `--input`) runs each paper, writes each `<stem>_final.json`, and then merges them into `corpus_synthesis.{json,md}` — auto-detected from the input count, exactly as `abcd_extract` decides on its synthesis, with `--no-synthesize` / `--synthesize` to override. In **host-model mode you are the loop**, so nothing runs it for you: after the last paper, run `python -m scripts.merge_corpus <out-dir> --out <out-dir>/corpus_synthesis` yourself — a directory works, no glob needed, and it skips anything that looks like a previous roll-up so a re-run cannot fold its own output back in. Per-paper `<stem>_final.json` stays the authoritative record of raw mentions; the roll-up adds one canonical row per entity across every paper, which documents it appears in, and where papers disagree about its ontology id. Handing back a directory of per-paper JSON and leaving the user to reconcile it is an unfinished deliverable: the questions a corpus is *for* ("which cell types does this collection talk about", "which mappings conflict") cannot be answered from any single file. The index is grouped, not concatenated — pass `--include-mentions` only if the raw union is genuinely wanted.
10. **Concept-mapping cascade — trusted ontologies first, and you MUST probe before declaring a remote mapper unavailable.**
    First source is the **trusted ontologies** (`python -m scripts.concept_mapping map`; `index` once — it needs no network). Their order is `trusted_ontologes/priority.md` and nothing else: edit it to reorder or enable one. Only what they leave unmapped goes on. Next is the local hybrid service at **`http://localhost:8000`**. Before saying "no mapper available" you MUST run at least one probe in your current runtime:
    ```bash
    curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/docs
    ```
    If the probe returns 200, USE the mapper. The real API schema is **`{ "max_results": N, "text": [{"text": "...", "context": "..."}] }`** — NOT `terms:[...]`. See `prompts/alignment-via-http.md` for a turnkey curl + jq pipeline.
    - On connection refused: try BioPortal (`BIOPORTAL_API_KEY`).
    - On further failure: **ask the user** for an alternative URL (ports 8001 / 8080 / 9000 / reverse-proxied paths are common) — do not give up silently.
    - Only after the user declines should you skip alignment (`concept_mapping_provenance: "skipped"`).
    - **If your runtime can't reach the user's `localhost`** (claude.ai web app, Anthropic Skills hosted, ChatGPT cloud), say so explicitly and direct the user to the MCP bridge or tunnel options in `connecting/mcp-server.md`. Don't pretend the service is unreachable when the user has it running — be explicit that the runtime is the constraint.
11. **Always tag `source_model` provenance.** Every entity item carries `source_model` (HF model id like `d4data/biomedical-ner-all`, or `llm_ner:<model>` for LLM-extracted items). The grouped view (`entities_grouped[]`) lists all contributing models per entity and the `consensus_count`. Never strip or merge these fields. See `references/ner-models.md`.
12. **Document metadata lives at the top, not on every entity.** `paper_title` / `doi` / `source_path` go into a top-level **`source_metadata`** block — ONCE per run. `paper_location` (section / page / paragraph) stays per-entity because it varies. Never repeat document-level metadata on hundreds of items.
13. **Always emit both `entities[]` (raw, one per occurrence) and `entities_grouped[]` (canonical, with merged sentences from every location).** The raw list is the authoritative record for exhaustive extraction; the grouped list is what downstream consumers navigate by. Use `scripts/group_by_entity.py:attach_grouped_views` — it's automatic in `pipeline.run()`.
14. **Canonical-shape guarantee via normalizer.** `scripts/normalize_result.py` runs automatically before every save and produces the canonical shape regardless of what the LLM emitted: top-level `source_metadata`, stripped per-entity `paper_title`/`doi`, tagged `source_model` on every item, `entities_grouped` attached, `stats` embedded. **Idempotent** — safe to run on already-canonical results. It's also exposed as a CLI to fix legacy result files in place. If you ever see legacy output, do not panic and do not edit by hand — run `python -m scripts.normalize_result <file>` and the file is brought up to spec.
15. **Concept mapping is MANDATORY and TOOL-ONLY. Zero hallucination.**
    - The pipeline **never silently skips** alignment. Default cascade (`concept_mapping.json`): **trusted ontologies** (`trusted_ontologes/priority.md`) → **local hybrid** (`http://localhost:8000`, verify at `/docs`) → **BioPortal** (`BIOPORTAL_API_KEY`) → **ask the user** for an alternate URL → **hard-stop with a clear error**. A trusted-ontology match is a tool mapping — a lookup in a curated artifact, never model knowledge. OLS is no longer in the auto-cascade (it has no gene coverage); the user must opt in explicitly via `--allow-ols-fallback`.
    - `concept_mapping_provenance: "llm_knowledge"` is **strictly forbidden** in canonical output. Any item carrying it is automatically demoted to `unmapped` by `scripts/iri_validation.py` and marked `alignment_method: "validation_failed"`. The item itself is preserved (exhaustive extraction is not compromised); only the fabricated mapping is dropped.
    - Every `ontology_id` is **structurally validated** against known IRI patterns (OBO PURLs, identifiers.org, BioPortal PURLs, EBI OLS, semanticweb.org, generic `<NS>_<NUM>` OWL IRIs). Malformed strings get demoted too.
    - The output's `stats.validation` block reports `passed` / `demoted` counts and the breakdown by failure reason — so you can tell at a glance whether the LLM tried to hallucinate.
    - If you cannot reach a real mapping tool, **say so to the user** and let them decide. Never invent IRIs to make the run "look complete."

16. **ABCD/HBCD mode: verification is the feature, not a formality.**
    - **Who runs the model depends on where you are.** In Claude Code / Codex **you** are the model: run `--prepare`, do the extraction yourself against `prompts/extractor-abcd.md`, write `<stem>.payload.json`, then `--payload <dir>`. Do **not** pass `--llm-model` there — there is no API to call. Only pass it when a framework (Pi, batch, cron) should call an API. Verification and every output are identical on both paths, and `provenance.extraction_path` records which was used.
    - **The paper is the only source of what a study used.** Never add a variable because ABCD papers usually include it, and never enumerate the data dictionary into the output. The dictionary and the Cognitive Atlas exist only to *verify and join* what the paper says.
    - **Every item needs a verbatim `evidence.quote` (>= 25 chars) that is findable in that paper.** `scripts/abcd_verify.py` deletes items whose quote is not there and records why in `rejected[]`. The requirement is per-section: a **variable** name must appear literally in its quote; a **construct** need not (it is a reading of the prose, and `label_in_quote` records which case it was); a **finding/model** must name at least one variable that appears in its quote.
    - **Results never land among the papers.** Everything goes to `<input>/abcd_results/` (override with `--out-dir`), including `--prepare`'s extracted text (`text/`) and the agent's payloads (`payloads/`). That directory is excluded from input scanning, which is load-bearing: it holds a `.txt` of every paper, and a rerun would otherwise extract each study twice — once from the PDF, once from its own extracted text — and checksum dedupe cannot catch that, since a PDF and its text layer are genuinely different files.
    - **One paper per distinct file.** Inputs are deduplicated by content checksum *before* any PDF is parsed, so `paper.pdf` and `paper(1).pdf` are one paper. This is a correctness rule, not tidiness: the synthesis counts by paper, so a duplicate doubles one study's weight in every consensus and turns "two papers agree" into a fact about one. The clean filename wins over the `(1)` copy, and the drop is reported in `input_duplicates_dropped`.
    - **One entry per variable per timepoint.** A paper writing "internalizing behaviors" in its Methods and "internalizing behavior" in its Results gets one merged entry (`also_written_as`, `merged_from` keep every wording and quote). Left separate, one resolves to a table and the other does not. Timepoint stays part of the key — conflict at year 1 and year 2 *are* different quantities.
    - **Cover the release the paper used.** All seven releases ship with the skill in `data/dictionaries/` (ABCD `nda-legacy`/4.x-5.x, `6.0`, `6.1`, `7.0`; HBCD `1.0`, `1.1`, `2.0`) — no workbook or network needed. Rebuild for a newer release with `--from-xlsx --all-sheets --minimal --gzip`; a local snapshot beats a bundled one. They matter together because ABCD 6.x renamed variables wholesale and the alternate namings (`name_nda`, `name_deap`, …) are what let a 2021 paper's `nihtbx_flanker_uncorrected` resolve to 6.1's `nc_y_nihtb__flnkr__uncor_score`.
    - **A role belongs to an analysis, not to a variable.** A paper that runs brain metrics as outcomes of preterm birth and then as mediators of gestational age has assigned two roles, and `variables[].role` can hold one. `scripts/abcd_roles.py` derives roles from `models[]` — which already record what each analysis did — and reports `roles[]`, `role_assignments[]` (which model, in its own words), `role_varies_by_analysis` and a **Roles by analysis** matrix in the Markdown. Correlation/descriptive analyses assign no roles: everything in a correlation matrix is on both axes, and reading that literally made one paper's income a predictor of itself. The one inference is `prior_wave_control` — an earlier wave of an outcome the paper *already* declares a covariate at some other wave — and it records why.
    - **One measure, several waves, one identity.** "internalizing behaviors", "Internalizing Time 2" and "Internalizing problems year 1" are one instrument at three waves. `timepoint_order` parses the wave to a comparable integer (baseline cue wins, then an explicit follow-up year, then `Time N`/`wave N` counting from one, then a bare `year N`; irreconcilable cues leave it null rather than guessing), and `measure`/`measure_key` group the wordings so duplicates merge on `(measure, wave)` instead of on the string. Growth intercepts, slopes, change scores and composites stay separate from the measure they derive from.
    - **Only what THIS study did.** A paper's introduction and discussion are largely other people's work. A variable counts only if this paper measured it; a finding counts only if this paper's own analysis produced it. `gate_scope` in `scripts/abcd_verify.py` enforces it independently of the extractor and rejects the rest as `finding_attributed_to_cited_work` / `measure_only_mentioned_in_cited_work`. This is not tidiness: without it, paper A's summary of paper B arrives in the synthesis as independent evidence and the literature gets double-counted.
    - **A string is only called an ABCD variable when a real dictionary release contains it.** Two routes get there. `dictionary_status: "verified"` means the paper printed a real dictionary name. `context_variable` means the paper's *wording* resolved to one variable through `scripts/abcd_context.py`, using what the paper itself stated — instrument, respondent, metric, release. Everything else stays visible as `unverified_variable` / `not_a_variable_name`.
    - **Most papers name no variable at all, so context mapping is the difference between a filled and an empty `nda_or_nbdc_table`.** On a three-paper sample it went from 1 of 57 variables carrying a table to 29. Four signals do the work, all of them the paper's own words: the **instrument** scopes candidates to one table (`externalizing` exists in the CBCL, ABCL, YSR and BPM — the paper naming the CBCL settles it); the **respondent** filters rather than merely penalises (`fes_y_ss_fc` vs `fes_p_ss_fc` are different measures, not near misses); the **metric** picks `_t` over `_r`; the **release** decides which snapshot is even eligible. That last one matters most — a 5.0 paper matched against 6.1 turns one clear measure into rival candidates in two tables.
    - **Never name a variable the paper did not name.** When several variables in one table fit equally well — 68 Desikan-Killiany thickness ROIs for "cortical thickness" — the result is `context_family`: table and domain reported, variable `null`, family prefix and candidate list attached. When tables disagree but the domain does not, `context_domain`. When the paper named an instrument rather than a variable, `instrument_table`. Each of those is more useful than a guess and more honest than a blank.
    - **Every mapping carries its own audit.** `context_mapping` records the cues that fired, the ranked candidates with scores, the thresholds applied, and why one variable was or was not named. A reader disagreeing with a mapping can see exactly which step to reject.
    - **The NDA element API confirms names; it does not map wordings.** `scripts/abcd_nda_api.py` looks up a printed name (giving `verified_via_nda_api` for a release we do not have) and can full-text search element descriptions — but a search hit is recorded as `nda_api_suggestions`, never as a mapping. Every table NDA can return is already in the snapshots, so a search hit is something the offline matcher already rejected, and NDA ranks over the whole archive: it offered an *Adult* Behavior Checklist score for "internalizing behaviors" and an SST series timestamp for "age at time of scan". Suggestions for a human, not evidence.
    - **Release membership is checked where it can be.** The `nda-legacy` snapshot is the union of NDA releases 2.0, 3.0 and 4.0 (116,353 variables, of which 87,682 existed in 3.0), and each row carries the releases its structure shipped in. A paper naming 2.0/3.0/4.0 is matched only against that release's rows; a literal name that resolves outside it gets `nda_release_conflict` — a warning, never a rejection, because the check is structure-level and papers do misstate their release. NDA labels nothing as 5.x, so 5.0/5.1 papers search the union.
    - **A local snapshot shadows a bundled one, and now says so.** Local wins by design, but a stale local build wins *silently*: an 85,984-variable `dd-abcd-nda-legacy.json` built before structure discovery was fixed shadowed the corrected 116,353-row bundle, so the fix appeared to do nothing. Shadowing now prints both counts.
    - **Build snapshots first if you need a newer release**: `python -m scripts.abcd_dictionary build --study abcd --release latest`. Load two or more releases so renames surface as `dd_release_gap`.
    - **Report the mention AND the mapping.** Keep `mention_as_written` (how the paper wrote it — prose label or id) alongside the resolved `dictionary_match.variable`, `nda_or_nbdc_table` and `nbdc_domain`. A reader must be able to see both sides of the join.
    - **Construct ids are tool-only.** A `trm_`/`tsk_` id is attached only when `scripts/cognitive_atlas.py` returned it; a model-supplied id is demoted into `demoted_claim`. Unmapped is an acceptable answer — run `cognitive_atlas search` and offer candidates to the user rather than auto-picking.
    - **Provenance includes where in the paper**: `section`, `page`, char offsets into the original text, and `used_context` (the surrounding sentences). Per run, record every dictionary snapshot consulted (release, source, sha256, retrieval time) and the Atlas vocabulary versions.
    - **Extract every variable the study used, not just the ones in the Measures section.** Table 1 rows, the covariate list, per-wave instances and self-computed composites are all variables. The verifier reports every string that a model or finding names but `variables[]` never declared (`coverage.referenced_but_not_declared`); those reach the synthesis with no quote, no table and no domain, which is a hole in the extraction rather than a detail.
    - **Synthesis counts by PAPER, never by finding**, so one verbose paper cannot outvote several others. `divergent` means opposing signs, not differing magnitudes. A contested mediator/moderator role is reported as contested, never resolved by majority vote — which is why the extractor must emit `unspecified` when a paper is ambiguous.
    - **Agreement is measured over paper-direction claims, and role consistency needs exclusivity.** Papers as the agreement denominator let a row print `1.00` agreement and a `divergent` verdict at once. And a variable that is a mediator in every paper *and* an outcome in every paper is contested: `role_exclusivity` is the share of papers where the dominant role is the only one, and both it and the share must clear the threshold.
    - **Two papers are the same variable only when they resolve to the same dictionary variable, share a paper-declared alias, or share a normalised mention.** Never on similarity: parent-report and youth-report versions of a scale stay separate rows, and when two wordings do resolve differently the row carries `mapping_disagreement` rather than silently picking one.
    - **The synthesis must say where every number came from.** Each variable row carries `paper_evidence` (per paper: wording used, instrument, respondent, metric, roles, timepoints, resolved variable, table, dd release, quotes); each construct row carries the variables that measured it — declared measures kept separate from variables that merely appear in its findings; each paper row carries its dataset (release, sample, analytic sample, waves, cohort, source). `claims[]` states what the corpus supports, the evidence paper by paper with a strength rating derived only from reported facts, and — separately — the contradictions and caveats, including "these papers report the same sample size, so their agreement is not independent".
    - **Bulk is first-class**: `--bulk` over a directory keeps going when one paper fails, writes one output set per paper, and `--synthesize` adds the cross-paper pass. Per-paper evidence stays inspectable; the synthesis never becomes the only record.
17. **Never ask for an LLM API key in host-model mode.** If you are an agent reading this file, you *are* the extractor and the judge — there is no API to call, so `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are irrelevant and `pipeline.py`'s `--extractor` / `--judge` have nothing to point at. (`--llm-model` on `normalize_result.py` is the exception that proves the rule: it is a provenance *label*, makes no call, and you SHOULD pass your own model id or every item lands as `llm_ner:unknown`.) Read the prompt, produce the JSON, and use the scripts for the deterministic stages (`mask_pass.py`, `group_by_entity.py`, `normalize_result.py`, `stats.py`, `iri_validation.py` — none of them call an LLM). A key is required only when the *user* asks for a headless run or a different model than you. The one key a host-model run can legitimately need is `BIOPORTAL_API_KEY`, which is a concept-mapping **tool** credential, not an LLM one — ask for it by name, and only after the local mapper has actually failed (rule 15). Blocking a run on "please provide an API key" when none is needed is a defect. See "Who runs the LLM stages".
18. **Turtle is the deliverable, and it passes the gate.** `scripts/json_to_ttl.py` writes it (never hand-written Turtle; a paper has hundreds to thousands of mentions); `scripts/validate_ttl.py` must report 0 violations — OWL vocabulary and domain/range under subclass closure, the SHACL shapes in `default_ontology/named_entity_shapes.ttl` (every entity has a mention, a key and a primary source; every external `skos:*Match` is backed by a tool-verified `ner:OntologyConcept`; mapping provenance is `"tool"`), the policy in `default_ontology/ttl_config.json` (generic keys, interventional evidence for non-hypothetical claims), prefix consistency, and one connected component. A file that fails is not handed back as a result. It also carries the full provenance: when (recorded times only — pass `json_to_ttl --started-at/--ended-at` in host-model mode if you timed the run), which agent version, which configuration (sha256), which mapping source and method decided each IRI, and every judge step, prompt hash, verdict and change (references/ttl-representation.md).
19. **One prefix means one namespace, everywhere.** Prefixes come from `scripts/prefixes.py` only — OBO prefixes, `ttl_config.json` `curie_expansions`, and each trusted file's own namespace under its priority.md CURIE prefix. A term keeps the prefix of its namespace whichever file it came from (cl.owl's UBERON terms are UBERON); a namespace nobody registered is never given a guessed prefix — it stays unmapped and is reported. `python -m scripts.prefixes check` must show 0 conflicts after adding an ontology.
20. **Identity is deterministic.** Every instance IRI is `kb:<uuid5>`: entities from `entity|<normalizedEntityKey>`, concepts from `concept|<IRI>`, ontology hubs from `ontology|<ACRONYM>` — shared across papers — and everything else from `<kind>|<DOI>|<local>`. The same entity in two papers is the same node; so the key must be right: from `kg_plan.json`, else the trusted ontologies' preferred label for the one class the text denotes, else the algorithm (`references/key-normalization.md`).
21. **Judges never fix; critical judges are gates.** One `fail` from grounding (script or LLM) drops an item; from claims, the claim. A mapping `fail` demotes the IRI (the item stays). Uncontested suggestions are applied by `judge_combine.py`; the combiner may only choose among judges' suggestions (unlicensed fixes are rejected) and escalates what the text does not settle. In host-model mode run each judge as its own pass over its own packet and say so (`"mode": "host_sequential"`).

22. **A node label is a name, never an explanation.** `rdfs:label` is what a graph
    viewer shows on the node: at most 60 characters (`ttl_config.json` `labels`), and
    the gate rejects longer ones. Structural nodes are named by kind ("annotation v1",
    "grounding review", "mapping decision CL:0000617", "sentence 12"). Prose —
    evidence, rationale, summaries, glosses — is a string: `rdfs:comment`,
    `ner:reviewComment`, `ner:sentenceText`. In kg_plan, `normalized_label` is the
    canonical term (optionally "(ABBR)"); an explanation goes in `note`.
23. **Every node is a UUID IRI, scoped to its extraction.** No blank nodes and no slug
    IRIs (the gate rejects both). Entities and concepts are shared across papers;
    mentions, classifications, reviews and runs are per paper AND per variant
    (`json_to_ttl --variant`, default `task_type:ner_domain`), so the neuroscience and
    cns-cells TTLs of one paper can sit in the same store without merging two readings
    into one node. The publication, its text, sentences and sections are shared.
24. **Scope and keys, learned on a real corpus.**
    - Extract from the paper's own content — title, abstract, body, methods, figure and
      table captions. Never from references, acknowledgements, funding / grant numbers,
      author lists or affiliations (`expand_mentions` skips those sections).
    - A bare generic noun ("cells", "neurons", "brain", "gene", "human", "disease") is
      an entity only when the paper uses it for a specific referent; its key is then
      that referent (`homo_sapiens`, `pyramidal_neuron`). Do not write a generic key
      in kg_plan, and do not invent a paper prefix yourself: leave the key out. The
      converter then derives it (trusted ontology's preferred label, else the
      algorithm), and if it is still generic, deliberately qualifies it with the paper
      id (`fnsyn_2023_1274383_neuron`) so it stays a paper-local entity — a missed
      cross-paper merge is recoverable, a wrong one is not. The batch runner drops
      generic kg_plan keys for this reason.
    - Keys are singular (`calcium_dye`, not `calcium_dyes`); mass nouns and fields stay
      as they are (`transcriptomics`).
    - Use the extractor prompt's own label set. An invented label becomes
      `ScientificNamedEntity` unless `label_class_map.json` maps it to a declared class.
    - A cns-cells run keeps nested spans, sets `specificity` on every cell mention, and
      states coordination (`coordinated_elements` with one `ontology_id` slot per
      element) — the TTL writes a `ner:CoordinatedEntityMention` with component
      mentions.
    - Relations and causal claims are part of extraction, not an optional extra:
      state them per mention (`relations`, `broader`, `cell_context`) and per chunk
      (`causal_relations`); the kg_plan adds cross-sentence ones.

## Install

```bash
pip install -r requirements.txt          # core: HTTP, schema validation, PDF, xlsx, rdflib + pyshacl (TTL)
python -m scripts.concept_mapping index  # once: index the trusted ontologies (~2 min, no network)
pip install -r requirements-llm.txt      # ONLY if a framework calls an API (--llm-model)
pip install -r requirements-ner.txt      # ONLY for the HuggingFace NER ensemble (heavy: torch)
```

`requirements.txt` is enough to run every mode with the calling agent as the model,
including ABCD/HBCD — **the ABCD dictionaries ship inside the skill**
(`data/dictionaries/`, all seven releases, 8.6 MB gzipped), so nothing external is
needed to verify a variable. A missing PDF backend is the most common first-run failure
("all PDF extractors failed"), so install it before blaming a paper.

## File map (load on demand)

The files below are intentionally separated so you only load what the current task needs.

### Configuration (edit these, not code)
- `concept_mapping.json` — mapping sources and their order, trusted-ontology match properties, `label_routing`, abbreviation guard, `match_type_tiers`, remote URLs.
- `trusted_ontologes/priority.md` — **the** list of trusted ontology files and their priority (Priority · Name · File · CURIE prefix · Namespace · Labels · Notes); `trusted_ontologes/README.md` lists them (regenerate with `concept_mapping readme`).
- `judges_config.json` — the judge panel: dimensions, weights, which are critical, prompts, batch size.
- `default_ontology/named_entity_ontology.owl` — the target ontology (Named Entity Ontology 2.4.0, `https://brainkb.org/ner/`): entities, mentions, alignment decisions, causal claims, and the v2.4 extraction-event record (timestamps, agent versions, configuration hashes).
- `default_ontology/named_entity_shapes.ttl` — SHACL shapes for instance data (structure).
- `default_ontology/ttl_config.json` — representation policy: IRI scheme (UUIDv5), generic keys, interventional evidence bases, relation predicates, OBO prefixes and `curie_expansions` (the prefix registry's static part), tiers, statuses.
- `default_ontology/label_class_map.json` — extractor label → ontology class.
- `default_ontology/key_synonyms.json` — user overrides for keys only (keys come from the trusted ontologies).
- `default_ontology/brainkb_resource_ontology.owl` — the BrainKB Resource Ontology (BKR 0.5.6 standalone: core, vocabularies, life-science scope axes, BrainKB/SEPIO and schema.org/SOSA/DataCite bridges, no imports) — the ontology of the resource KG, and the vocabulary `extracted_type → class` is read from.
- `default_ontology/brainkb_resource_shapes.ttl` — BKR policy shapes (SHACL) for the resource KG.
- `default_ontology/resource_kg_config.json` — resource KG settings: instance base, concept routing per scope dimension, grounding, source-silence shapes, extraction chunk size.

### `references/`
- `pipeline-pattern.md` — multi-stage agent pattern, how to chain stages, when to skip, resume from a saved stage.
- `ner-extraction.md` — NER methodology: entity types, output keys, edge cases, exhaustive extraction, mask-recall pass, grouped view.
- `ner-models.md` — HuggingFace + LLM ensemble: model roster, profiles by domain, `source_model` provenance, consensus count, when to skip.
- `resource-extraction.md` — resource extraction → BKR resource KG: two-tier extraction, grounding rules, tool concept mapping, provenance and identity (shared publication node, one resource node across papers), mention stubs, the gate and its source-silence findings, the competency questions, and the legacy-input crosswalk.
- `structured-extraction.md` — generic schema-driven extraction (PDF → user-supplied JSON schema).
- `ontology-mapping.md` — BioPortal REST API, OLS REST API, embedding-based hybrid retrieval, LLM-only fallback. Picking and combining backends.
- `chunking-strategy.md` — sentence-aligned chunking, parallel extraction, merge by stable key, context window math.
- `json-output-discipline.md` — schema-locked prompting, JSON repair, validation.
- `cell-annotation-conventions.md` — how a human annotator marks up cell mentions: the `cell_phenotype` / `cell_vague` / `cell_hetero` specificity axis, nested hedge-plus-head spans, one ontology id per coordinated element (`;` positional, `-` for a gap), `skos:exact` vs `skos:related`, BioC `(offset, length)` conversion, and a validation checklist. Load this whenever cell extraction will be **scored**, and note it overrides the older non-CNS exclusion in the cns-cells prompt.
- `model-selection.md` — picking models per stage; OpenRouter / Ollama / vLLM / Claude / GPT / Gemini configuration.
- `human-feedback.md` — designing the human-in-the-loop review step.
- `judge-ensemble.md` — the weak-learner judge panel: dimensions, independence, review format, aggregation semantics, combiner, host vs framework mode.
- `ttl-representation.md` — the Turtle deliverable: who writes what, UUIDv5 IRIs, prefix registry, entity/mention/concept/decision/review/causal structure, non-negotiables, the gate.
- `key-normalization.md` — `normalizedEntityKey`, the merge handle: ontology-derived canonical keys, the algorithm, guardrails.
- `abcd-extraction.md` — **ABCD/HBCD mode**: extracting variables/constructs/models/findings from publications, the three hard rules (strict verification, complete provenance, single-or-bulk), building dictionary snapshots from NBDCtools, Cognitive Atlas construct mapping, and how to read the cross-paper synthesis verdicts.

### `prompts/`
- `extractor-ner-general.md` — general-domain NER (Person, Org, Location, Product, Event, Date, …).
- `extractor-ner-neuroscience.md` — broad neuroscience NER (BrainRegion, CellType, Gene, Protein, Drug, Behavior, Disease, Method, Stimulus, Measurement, …).
- `extractor-ner-cns-cells.md` — CNS-cell-focused NER (CellClass / CellType / CellSubtype with lineage markers, morphology, ephys, layer, projection, atlas references, profiling method).
- `mask-recall-pass.md` — **pass-2** that surfaces mentions pass-1 missed. Run on any of the three NER prompts above. Big recall booster (typical +30–80%).
- `mask-verify-pass.md` — per-item label sanity check via cloze (mask one entity, predict label from context). Optional precision booster.
- `extractor-resource.md` — research resource extractor, BKR profile: deep records for what the document describes, catalogue records with an observed scope for what it uses, `mentions` for the rest; claimed / validated / observed / out-of-scope applicability, assumptions, failure modes, IO, versions, quotes, explicit `not_found_fields`.
- `extractor-structured.md` — schema-driven extractor (PDF → user-supplied JSON Schema).
- `alignment.md` — ontology alignment (LLM + concept-mapping tool).
- `alignment-via-http.md` — turnkey curl + jq pipeline for calling the local hybrid `/map/batch` endpoint directly. Use when you have Bash + network access but no Python client (Claude Code is the common case).
- `judge-grounding.md`, `judge-labeling.md`, `judge-mapping.md`, `judge-kg-keys.md`, `judge-claims.md` — the ensemble's judges, one dimension each.
- `judge-combiner.md` — resolves only `needs_review`, choosing among judges' suggestions.
- `kg-plan.md` — the judgment layer of the TTL: keys, finer classes, paper-stated relations, causal claims.
- `judge.md` — legacy single-score judge (only on request).
- `humanfeedback.md` — apply human reviewer edits.
- `extractor-abcd.md` — ABCD/HBCD extractor: variables (as mentioned), constructs, models, findings with roles/directions, each with a verbatim quote + section/page.
- `extractor-cell-type-ait-mapping.md` — four-pass workflow (index + extract → lexical verification → SKOS mapping to Allen Institute (AIT) cell type taxonomies → entity cards with marker-gene diff), emitting seven fixed-schema CSV tables plus `run_report.md`. Drives the `ait_*.py` scripts.

### `schemas/`
- `ner-output.schema.json` — JSON Schema for NER output. **Task-agnostic — keep it that way**; cell-specific constraints live in the two files below.
- `cell-ner-output.schema.json` — per-paper CNS cell NER output. Superset of the generic NER schema: the closed cns-cells label taxonomy (enforced for LLM-extracted items only, since the HF ensemble legitimately emits `Anatomy`/`Gene`/`CellLine`), the `cell_context` block, `specificity`, `coordinated_elements`, and a rule that a `cell_vague` item **must** carry a null `ontology_id`.
- `cell-ner-corpus.schema.json` — the corpus roll-up written by `scripts/merge_corpus.py`.
- `bkr-resource-extraction.schema.json` — the resource extraction contract (BKR profile, 30 fields per record).
- `resource-output.schema.json` — the legacy resource shape; still accepted as input and upgraded to BKR records by the ontology's own crosswalk.
- `aligned-item.schema.json` — fragment schema for any aligned item (adds ontology + provenance fields).
- `judged-item.schema.json` — fragment schema for any judged item (adds judge_score + remarks + judge_method).
- `judge-review.schema.json` — one judge's review of one packet.
- `kg-plan.schema.json` — kg_plan.json.
- `abcd-paper.schema.json` — ABCD/HBCD per-paper result: variables (with `mention_as_written`, `dictionary_status`, `nda_or_nbdc_table`, `nbdc_domain`), constructs, models, findings, `rejected[]`, `verification`, and provenance including every dictionary snapshot consulted.
- `ait-mapping-columns.json` — the AIT mapping mode's column contract: all seven tables (147 columns) in order, with type, multi-valued/aligned flags, controlled vocabularies and required flags. Not a JSON Schema; `scripts/ait_tables.py validate` reads it, and a test keeps it in sync with the prompt's column lists.
- `abcd-synthesis.schema.json` — cross-paper synthesis: per-construct consensus/divergence verdicts, per-variable role consistency, variable↔construct links, and the `method` block recording the thresholds a verdict was reached under.

### `scripts/` (runnable helpers)
- `concept_mapping.py` — **concept mapping from configuration**: trusted-ontology lexicon (`index` → TSV per ontology + SQLite), priority-ordered exact lookup with routing and the abbreviation guard, then local hybrid → BioPortal. CLI: `index` / `show` / `lookup` / `map` / `export-synonyms` / `init-priorities` / `readme`.
- `prefixes.py` — **the prefix registry** (rule 19). CLI: `show` / `check` / `compact` / `expand` / `canonical`.
- `json_to_ttl.py` — **result → Turtle** (rule 18): entities, every mention, sentences, sections, annotation versions, tool-verified concepts + mapping decisions, judge reviews, kg_plan edges and the causal module; UUIDv5 IRIs; keys from reviewed identity / source-defined aliases / algorithm. Compact is the default; `--profile full` adds audit records.
- `validate_ttl.py` — **the gate**: OWL vocabulary + domain/range, SHACL shapes, config policy, prefix consistency, labels, one connected component; `--check-ols` optional. A resource KG is recognised and gated against the BKR ontology + shapes instead.
- `resource_kg.py` — **resource result → resource KG** (BrainKB Resource Ontology): legacy/BKR records → grounding against the source text → tool concept mapping of scope labels → run/paper provenance → `bkr_convert` → `bkr_stubs` → shared `ner:Publication`. `build` and `validate`; `json_to_ttl` and `pipeline --task resource` call it.
- `bkr_convert.py`, `bkr_stubs.py` — the BKR converter and mention-stub resolver, vendored from bkr-0.5.6 (vocabulary from the bundled OWL; optional global resource keys and publication aliasing).
- `judge_prepare.py` — deterministic grounding review + one packet per judge.
- `judge_combine.py` — deterministic aggregation (gates, demotions, fixes, scores) on the raw mentions; `--apply-fixes` for the combiner.
- `judge_ensemble.py` — framework-mode runner for the panel and the kg_plan (one call per packet).
- `chunking.py` — sentence-aligned chunking, span re-anchoring, deduplication.
- `json_repair.py` — four-tier JSON repair (strict → fences → json-repair → truncate-to-balanced) + schema-driven LLM repair.
- `span_validator.py` — validate `text[start:end] == entity`, repair from sentence context.
- `mask_pass.py` — build masked text for the mask-recall pass (offset-preserving placeholders), translate offsets back, and mask single items for the mask-verify pass.
- `stats.py` — compute the `stats` block (totals, label histogram, by-source-model histogram, alignment provenance, judge score buckets, per-stage timings) and a human-readable summary.
- `group_by_entity.py` — collapse raw mentions into `entities_grouped[]` keyed by canonical `(entity, label)`: collects all `mentions`, deduplicates `sentences` across locations, lists every contributing `source_model`, and reports `consensus_count`. Also exposes `unify_ontology_across_entities()` — when the same surface form gets different ontology IDs from different chunks/models, pick the best one (tool-mapped > LLM > unmapped) and apply it to every occurrence. Both run automatically in `attach_grouped_views()`.
- `ner_models.py` — HuggingFace NER ensemble (biomedical + clinical specialists). Default roster: d4data, BC5CDR-chem, NCBI-disease, BioBERT-genetic, plus the BENT-PubMedBERT family (Gene / Chemical / Disease / Anatomical / Cell-Type / Cell-Line / Organism / Bioprocess). Picked by profile: `biomedical_broad` / `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`. Every emitted item carries a `source_model` provenance field.
- `normalize_result.py` — **idempotent post-processor**. Lifts per-entity `paper_title`/`doi` into top-level `source_metadata`, strips the per-entity dupes, tags missing `source_model` + `alignment_method`, infers `task_type`, runs **strict IRI validation** (demotes `llm_knowledge` and malformed IRIs), attaches `entities_grouped[]`, and computes `stats` (with prominent `totals` block at the top). **Runs automatically** in `pipeline.py` before saving. Also exposed as a CLI: `python -m scripts.normalize_result legacy.json --input paper.txt --llm-model …`. This is the safety net that guarantees the canonical shape even when the LLM ignores the prompt.
- `iri_validation.py` — **strict IRI validator**. Per-ontology regex patterns + permissive structural fallback. Rejects `concept_mapping_provenance: "llm_knowledge"` outright (zero hallucination policy), demotes malformed IRIs to `unmapped`, accepts legitimate cross-ontology mappings (e.g. CIDO results that reuse HP IRIs). Adds `result["validation"]` with `passed` / `demoted` counts and `demoted_by_reason` breakdown.
- `input_loader.py` — **document ingestion, any format**: PDF, DOCX, PPTX, XLSX, HTML, AsciiDoc, MD, CSV, images (PNG/JPEG/TIFF/BMP/WEBP), TXT. [Docling](https://github.com/docling-project/docling) is stage 1 for **all** of them — it dispatches on the format itself, and for DOCX/PPTX/XLSX/HTML/images it is the only backend here, so without it those are unreadable. PDFs then fall back GROBID (if reachable) → pymupdf4llm → PyMuPDF (`fitz`) → pdfminer.six, so a machine with no Docling still reads papers. Also writes a sibling `<stem>.txt` so subsequent stages have stable character offsets. CLI: `python -m scripts.input_loader paper.docx [--no-docling] [--no-grobid] [--grobid-url …]`.
  Captions and tables are the thing to watch: the GROBID path used to drop them entirely, and `caption_coverage()` / `warn_if_captions_missing()` now report it on every backend. Cascade is Docling → GROBID → pymupdf4llm → PyMuPDF → pdfminer.
  **Scanned PDFs and images**: Docling is the only backend that reads one, because it converts through layout and table-structure models and its default pipeline OCRs; every other backend needs a text layer and returns nothing, which looks identical to a corrupt file. It costs a model download on first use and seconds per page, so `--no-docling` (and `abcd_extract.py --no-docling`) skips it for a large corpus of clean PDFs — at the price of losing the non-PDF formats entirely.
- `task_detection.py` — **auto-detect task type** from a free-text task description: heuristic regex first (fast, no LLM), then LLM fallback via `llm_client`. Returns a `TaskDetection` with `task_type` (`ner`/`resource`/`structured_extraction`/`relation_extraction`/`keyphrase_extraction`/…), `confidence`, `labels`, `rationale`.
- `model_context.py` — **model context-window registry** (~50 model families, longest-match wins) + token-aware `compute_downstream_chunk_size(...)` for sizing alignment/judge/humanfeedback chunks. CLI: `python -m scripts.model_context openrouter/anthropic/claude-sonnet-4-6 --items 2000 --workers 8`.
- `bioportal_map.py` — throttled + LRU-cached BioPortal client.
- `merge_corpus.py` — **corpus roll-up (rule 9b)**. Merges per-paper `*_final.json` into `<stem>.json` + `<stem>.md` (`--out`, default `corpus_synthesis`, mirroring `abcd_synthesize.py`): one canonical row per entity across all papers, per-document counts, cross-paper ontology conflicts, and specificity totals. Groups with the same `_canonical_key` as per-paper `entities_grouped`, so corpus counts reconcile against per-paper counts. Recomputes totals from the items rather than summing each file's `stats`, so one stale block can't corrupt the total. No LLM call. `--include-mentions` embeds the raw union, `--no-index` gives roll-up only.
- `fetch_fulltext.py` — **structured full text without a PDF or GROBID**. PMCID/PMID → publisher XML (NCBI BioC → Europe PMC JATS → NCBI efetch), keeping figure captions and table cells as first-class content. Open-access only, and it says so rather than falling back to a caption-less parse. Via the BioC source the passage segmentation matches a BioC gold standard exactly. No key, no server; `NCBI_API_KEY` only raises a rate limit.
- `ols_map.py` — EBI OLS client (no API key).
- `local_hybrid_map.py` — client for a self-hosted BM25+dense mapping service (one POST, many terms).
- `llm_client.py` — provider-agnostic LLM call (OpenAI / OpenRouter / Anthropic / Ollama / Gemini).
- `batch.py` — **the corpus runner** (papers × variants): `init` / `next` (host mode: one task at a time) / `run` (headless; `claude-code` inside Claude Code) / `status` / `retry`. Resumable manifest, per-paper TTL written and gated as it finishes, claims for parallel workers, corpus roll-up per variant. See "Corpus requests".
- `expand_mentions.py` — every occurrence of each extracted (surface, label) with exact offsets, whole-span sentences and sections; tolerant of PDF line wraps / hyphenation / NBSP, skips URLs and out-of-scope sections (references, acknowledgements, funding). Used by `batch.py`; CLI for a hand-written lexicon.
- `pipeline.py` — reference end-to-end pipeline (extract → align → judge) wiring the helpers together. `--input` takes a file **or a directory** and is repeatable; several inputs run in turn, one failure does not abort the batch (exit 2 = partial, 1 = none succeeded), and the corpus roll-up runs at the end (rule 9b).
- `abcd_context.py` — **context-aware mapping from a paper's wording to a dictionary variable**. `Dictionary.resolve()` answers "is this string a variable name?", which most papers never satisfy; this answers "which variable did this sentence mean?" by matching against dictionary *labels* with the instrument, respondent, metric and release the paper stated. Returns one variable, a family, a domain or an instrument table — never a guess — with the candidate list and thresholds attached. CLI: `match` / `instrument` / `stats`.

- `abcd_nda_api.py` — **NDA data-element API**: confirm a printed element name (with its structures and aliases), or full-text search element descriptions. Hits are intersected with the loaded dictionary's tables, and search results are suggestions rather than mappings. Cached under `~/.cache/structsense/nda_api`. CLI: `element` / `search`.

- `abcd_dictionary.py` — **ABCD/HBCD variable dictionary**. Builds release snapshots from NBDCtools (`nbdctools` on PyPI reads `lst_dds` without R) or from your own CSV export, with provenance (source, sha256, retrieval time). `Dictionary.resolve()` decides whether a string from a paper IS a real variable — exact name → normalised name → full label/description, never fuzzy — and `releases_for()` surfaces renames across releases. CLI: `build` / `info` / `lookup` / `search`.
- `cognitive_atlas.py` — Cognitive Atlas client (~918 concepts, ~856 tasks), cached to disk after one fetch. Exact/singular/alias matching only; an unmatched construct stays unmapped rather than being guessed. CLI: `refresh` / `map` / `search`.
- `abcd_verify.py` — **the ABCD verifier**. Anchors every `evidence.quote` in the paper's own text (whitespace/ligature-normalised, re-anchoring offsets but never inventing quotes), applies the per-section surface rules, gates variables against the dictionary and constructs against the Atlas, and returns `rejected[]` + a `verification` summary.
- `abcd_extract.py` — driver taking **one argument, auto-detected**: a PDF, a directory, a CSV/TSV/XLSX of DOIs, a DOI list, or a bare DOI. load → chunk → LLM extract → merge → verify → write `<stem>_abcd.{json,md,ttl}` (`--formats` also takes `codebook`). More than one paper implies a synthesis (`--no-synthesize` to skip); `--reverify` re-checks an existing extraction with no LLM calls.
- `abcd_inputs.py` — the input resolver. Detects single vs bulk vs DOI table, and fetches **open-access** PDFs for DOIs (Unpaywall → OpenAlex → Semantic Scholar). Downloads are magic-byte verified, so a paywall answering `200 text/html` is reported as unresolved rather than saved as a broken PDF. Records service, URL, license and sha256 per fetch.
- `abcd_synthesize.py` — cross-paper synthesis: `claims[]` with per-paper evidence, strength and contradictions; consensus/divergence per construct with the papers behind each direction and the variables that measured it; role consistency per variable with per-paper provenance and the dd release each mapping holds in; and a dataset row per paper. **Counts by paper, not by finding.**
- `abcd_roles.py` — **per-analysis roles and wave identity**, run after verification. Parses each `timepoint` into a comparable order, groups per-wave entries under one `measure`, and cross-references `models[]` to give every variable the role each analysis assigned it (`roles[]`, `role_assignments[]`, `role_summary`, `role_varies_by_analysis`, `bidirectional_in[]`). `python -m scripts.abcd_roles` runs its self-checks.
- `abcd_export.py` — JSON + Markdown tables + Turtle writers shared by both drivers. The Turtle uses PROV-O plus a small `abcd:` vocabulary, carrying quote, `usedContext`, char offsets, section/page, `mentionAsWritten`, `ndaOrNbdcTable` and `nbdcDomain` into triples, plus an `abcd:RoleAssignment` per variable-in-analysis. `--formats codebook` additionally writes a TSV in the ABCD annotators' own coding scheme (Text Content · Source · Codes), so a run can be diffed against hand-coded gold data.

- `ait_taxonomy.py` — **AIT taxonomy catalog**: `list` / `rank --species … --region …` / `show <AIT id|CCN>` over `data/allen_taxonomies.json` (a dated snapshot of the eight supported brain-map.org taxonomies, each with its page URL, with per-species AIT numbers for the multi-species ones).
- `ait_evidence.py` — **AIT Pass 2a lexical verifier**. Normalises quote and source identically (NFKC, quotes/dashes, soft hyphens, line-break hyphenation, whitespace), then tests at the recorded offsets → whole document → fuzzy (≥0.95). Writes `exact | exact_offset_corrected | fuzzy | not_found` per aligned sentence, corrects offsets, quarantines entities with no found evidence, and writes `evidence_report.json`.
- `ait_tables.py` — **AIT column contract**: `init` (header-only files), `derive` (`match_confidence` from `skos_relation`, crosswalk copies), `review-sheet` (the deterministic join, strictly verified evidence only), `validate` (headers, types, vocabularies, crosswalk, quarantine, same-taxonomy `exactMatch` rule, review-sheet equality).
- `ait_gene_diff.py` — **AIT Pass 4 entity cards**: shared / paper-only / taxonomy-only marker sets, counts, Jaccard, and `jaccard_panel_restricted` for targeted assays, from `{ait_node_id: [genes]}` marker sets. Exact symbol comparison; resolve orthologs first.

### `examples/`
- `ttl/` — **end to end on a real open-access paper** (Hu et al. 2026, CC BY 4.0): text, a human-curated reference TTL, the working JSON with 1,039 grounded mentions, kg_plan, `run.sh`, and the validated result `hu2026.ttl` whose entity and concept IRIs equal the curated graph's.
- `ner-example.md` — end-to-end NER worked example.
- `resource-example.md` — end-to-end resource extraction → BKR resource KG worked example (with `resource-superanimal.{txt,json}`); `resource-bkr-example.json` is the BKR bundle's own extraction record.
- `reproschema-example.md` — end-to-end PDF → ReproSchema worked example.

### `connecting/` (how to wire the skill into different LLM platforms)

All of these are **host-model mode** (see "Who runs the LLM stages") except the MCP
server and a deliberately headless `pipeline.py` run: the agent is the extractor, so
no LLM API key is involved. Codex CLI needs no guide of its own — it reads `SKILL.md`
and behaves like Claude Code here.

- `claude-code.md` — install as a Claude Code skill (`~/.claude/skills/` or `.claude/skills/`). Auto-discovery via the `SKILL.md` frontmatter. Also the reference for "why no API key is needed".
- `pi-dev.md` — install as a [Pi](https://pi.dev) skill (`~/.pi/agent/skills/`, `~/.agents/skills/`, or `.pi/skills/`). Pi is a CLI coding agent with native Agent Skills support and a built-in `bash` tool, so it runs the pipeline directly — same story as Claude Code.
- `claude-desktop.md` — **Claude Desktop has a split execution model**: chat UI on your machine, code interpreter in Anthropic's cloud sandbox (so it cannot reach your `localhost:8000` directly). Use the MCP server config in this guide to bridge.
- `claude-skills.md` — upload as a hosted Anthropic Skill on claude.ai or use with the Claude Agent SDK.
- `custom-gpt.md` — wire as an OpenAI Custom GPT (Instructions + Knowledge files + optional server-side Action).
- `mcp-server.md` — expose the pipeline as an MCP server so any MCP-aware client (Claude Code, Cursor, ChatGPT desktop, custom agents) can call it.

## Minimal mental model

If you remember nothing else, remember this:

> Extract with a short strict-JSON prompt; map every term with tools — the trusted ontologies first, in the priority you set; judge with narrow independent judges whose hallucination checks are gates; then write the paper as Turtle instances of the ontology, with keys that make the same thing the same node in every paper, and do not hand it back until the gate says 0 violations.

That's the entire skill. The references and prompts here are the careful version of that one sentence.
