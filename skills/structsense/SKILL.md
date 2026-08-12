---
name: structsense
version: 0.5.0
description: Extract structured information (named entities, key terms, resources like tools/datasets/models/benchmarks, or any target JSON schema) from unstructured text and PDFs using a model-agnostic multi-stage pipeline (extract → align to ontologies → judge → optional human feedback). Use this skill when the user asks to do NER, pull resources out of papers, convert documents to a target JSON schema (e.g. ReproSchema), or map terms to ontologies (BioPortal, OLS, OBO, BTO, CL, UBERON, NCBITaxon, etc.). Also extracts ABCD/HBCD study content from publications — which variables a study used (mapped to the NBDC data dictionary: nda_or_nbdc_table, nbdc_domain), the constructs behind them (Cognitive Atlas), the models specified, and the findings reported — with strict quote-level verification and full provenance, for single or bulk PDFs, plus cross-paper synthesis of consensus, divergence and whether variables are consistently mediators/moderators. Works with any LLM (Claude, GPT, Gemini, Pi, local Ollama/vLLM) — no library dependency.
license: Apache-2.0
---

> **Skill version 0.4.0.** Concept mapping is now **mandatory and tool-only**. The pipeline refuses to silently skip alignment; it cascades local hybrid → BioPortal → ask the user for an alternate URL → hard-stop. Items with `concept_mapping_provenance: "llm_knowledge"` are automatically demoted to `unmapped` because hallucinated IRIs are not acceptable output. The pipeline now also tracks `validation` counts (passed / demoted / by_reason) and surfaces a prominent `totals` block in `stats`. Legacy outputs can be brought up to spec via `python -m scripts.normalize_result <file> --input <text> --llm-model <model>` (idempotent). See [CHANGELOG.md](CHANGELOG.md).



# StructSense Skills — structured information extraction

A reusable methodology for turning unstructured text and PDFs into clean, schema-conformant JSON, with optional ontology grounding and quality scoring. The patterns here are model-agnostic: they work with Claude, GPT, Gemini, Pi, or any local model.

## When to invoke this skill

Trigger when the user asks to:

- Extract **named entities + key terms** (NER) from biomedical, neuroscience, or scientific text.
- Pull **resources** out of papers — tools, datasets, models, benchmarks, leaderboards.
- Convert a document into a **target JSON schema** (e.g. ReproSchema, Croissant, a custom schema the user supplies).
- **Map extracted terms to ontologies** (BioPortal, OLS, OBO, BTO, CL, UBERON, NCBITaxon, MESH, …).
- **Score or judge** the quality of an existing extraction.
- Process a **long document** that needs chunking and parallel runs.
- Extract **ABCD / HBCD study content** from publications — which variables a study used, the constructs behind them, the models specified, the findings reported — and **compare across papers**: where is there consensus, where divergence, which variables are consistently mediators or moderators. → `references/abcd-extraction.md`.

## The core pattern

Four cooperating roles, run sequentially. Each role's output is the next role's input. Any role can use a different model.

```
┌───────────┐   raw text     ┌────────────┐   extracted   ┌───────┐   aligned   ┌──────────────┐
│ EXTRACTOR │ ──────────────►│ ALIGNMENT  │──────────────►│ JUDGE │────────────►│ HUMAN FB     │
│ (LLM)     │                │ (LLM+tool) │               │ (LLM) │             │ (optional)   │
└───────────┘                └────────────┘               └───────┘             └──────────────┘
   strict JSON                 + ontology fields           + judge_score          + corrections
                               + provenance                + remarks              + revised JSON
```

| Stage | Job | Reads | Writes |
|---|---|---|---|
| **Extractor** | Find entities/resources/fields. Output strict JSON. | raw text | items with `entity`/`name`, `label`/`type`, `sentence`, `start`, `end` (etc.) |
| **Alignment** | Map each item to an ontology IRI. | extractor output | adds `ontology_id`, `ontology_label`, `ontology`, `concept_mapping_provenance` (`tool` or `llm_knowledge`) |
| **Judge** | Score quality of each item (0–1). | alignment output | adds `judge_score`, `remarks` |
| **Human feedback** | Apply corrections from a human reviewer. | judge output + user feedback | revised JSON |

You can run any subset — see `references/pipeline-pattern.md`.

## Quick decision flow

1. **What kind of extraction?**
   - Entities + key terms (NER) → load `references/ner-extraction.md`, then pick the extractor prompt by domain:
     - General-domain text (news, finance, biographies, generic web pages, mixed text) → `prompts/extractor-ner-general.md`.
     - Neuroscience text — broad (behavior + systems + cellular + molecular + computational) → `prompts/extractor-ner-neuroscience.md`.
     - CNS-cell-focused text (cell atlases, patch-seq, scRNA-seq cell typing, BICCN-style cell census — anything where cell types + markers + morphology + ephys are the subject) → `prompts/extractor-ner-cns-cells.md`.
   - Tools / datasets / models / benchmarks → load `references/resource-extraction.md` and `prompts/extractor-resource.md`.
   - User has a target JSON schema → load `references/structured-extraction.md` and `prompts/extractor-structured.md`.
   - **ABCD / HBCD variables, models, findings, or cross-paper synthesis** → load `references/abcd-extraction.md` and `prompts/extractor-abcd.md`. This mode has its own verifier and its own hard rules (see rule 16); it is not a variant of NER. Single PDF or a directory in bulk; every run emits JSON + Markdown + Turtle.
2. **Want exhaustive recall? (almost always yes for NER)** → after pass-1 extraction, run the **mask-recall pass** with `prompts/mask-recall-pass.md` + `scripts/mask_pass.py`. Optionally also run **mask-verify** (`prompts/mask-verify-pass.md`) for per-item label sanity. See `references/ner-extraction.md` → "Two-pass strategy: mask-mode".
2b. **Biomedical text? Enable the HuggingFace NER ensemble.** Pass `--ner-profile biomedical_broad` (or `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`) to run specialist models alongside the LLM extractor. Every mention carries a `source_model` field; the grouped view records `consensus_count` (how many models agreed). See `references/ner-models.md`. Skip the ensemble for non-biomedical text or when `transformers` isn't installed.
3. **Need ontology mapping?** → load `references/ontology-mapping.md`. Default cascade: **local hybrid** at `http://localhost:8000` (verify at `/docs`) → **BioPortal** → **ask the user** for an alternative URL → skip alignment only if declined. Don't hardcode the URL; the port and host vary across deployments.
4. **Long document (>10 pages or > model context)?** → load `references/chunking-strategy.md`. Chunk → run extractor in parallel → merge → run downstream stages.
5. **Need quality scoring?** → load `prompts/judge.md`.
6. **Multiple models for cost?** Use the cheapest capable model for extraction (often a small open model), a stronger model for alignment if you don't have a mapping tool, and a fast model for judging. See `references/model-selection.md`.

## Hard rules

These prevent the most common failures.

1. **Strict JSON output, no markdown fences.** Every prompt must include `"Output strict JSON only. No prose. No markdown fences."` in the system message. Set `temperature: 0` for extraction and alignment.
2. **Extract EXHAUSTIVELY.** For NER, emit every occurrence of every mention as a distinct item with its own `start`/`end`. Never deduplicate by surface form. A multi-page neuroscience paper should yield hundreds to thousands of entity items, not a few hundred. If yield feels low, run the mask-recall pass (`prompts/mask-recall-pass.md` + `scripts/mask_pass.py`) — typical recovery is +30–80%.
3. **Preserve fields downstream.** Alignment, judge, and human-feedback stages **add** fields. They never remove existing fields and never re-key existing items.
4. **Record provenance.** Every mapped item carries `concept_mapping_provenance: "tool" | "llm_knowledge"`. Never hide where a mapping came from.
5. **Chunk and merge** for inputs longer than the model's context window (or `> 25,000` chars for safety on 128k models). Always re-merge by stable identifiers (sentence + char span, or item `id`).
6. **Don't invent placeholders.** The agent communication contract is: extractor input is the raw text; alignment input is the extractor's JSON; judge input is the alignment's JSON. Pipe outputs cleanly — don't re-wrap or paraphrase between stages.
7. **Validate before returning.** Parse the JSON; if parsing fails, repair-then-retry (see `references/json-output-discipline.md`). Validate against the task's JSON schema in `schemas/`.
8. **Always emit a `stats` block.** Every final result must embed a `stats` block at the top level (totals, label histogram, alignment provenance, judge score buckets, per-stage elapsed times) and print a human-readable summary to stderr. Use `scripts/stats.py`. This is the answer to "did the run do what it was supposed to?" — a healthy NER run on a paper has hundreds-to-thousands of entity mentions and `mentions_per_unique > 1`. A summary with 230 mentions and `mentions_per_unique ≈ 1` is the symptom of surface-form deduplication; re-run with the mask-recall pass and double-check no upstream step is collapsing duplicates.
9. **Final-result filename convention.** When writing the result to disk, name it **`<input_stem>_final.json`** (e.g. `paper.pdf` → `paper_final.json`, `note.txt` → `note_final.json`). Honor an explicit `--out` only when the user provides one. The reference helper is `scripts/pipeline.py::default_output_path`.
10. **Concept-mapping cascade — and you MUST probe before declaring unavailable.**
    Default mapper is the local hybrid service at **`http://localhost:8000`**. Before saying "no mapper available" you MUST run at least one probe in your current runtime:
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
    - The pipeline **never silently skips** alignment. Default cascade: **local hybrid** (`http://localhost:8000`, verify at `/docs`) → **BioPortal** (`BIOPORTAL_API_KEY`) → **ask the user** for an alternate URL → **hard-stop with a clear error**. OLS is no longer in the auto-cascade (it has no gene coverage); the user must opt in explicitly via `--allow-ols-fallback`.
    - `concept_mapping_provenance: "llm_knowledge"` is **strictly forbidden** in canonical output. Any item carrying it is automatically demoted to `unmapped` by `scripts/iri_validation.py` and marked `alignment_method: "validation_failed"`. The item itself is preserved (exhaustive extraction is not compromised); only the fabricated mapping is dropped.
    - Every `ontology_id` is **structurally validated** against known IRI patterns (OBO PURLs, identifiers.org, BioPortal PURLs, EBI OLS, semanticweb.org, generic `<NS>_<NUM>` OWL IRIs). Malformed strings get demoted too.
    - The output's `stats.validation` block reports `passed` / `demoted` counts and the breakdown by failure reason — so you can tell at a glance whether the LLM tried to hallucinate.
    - If you cannot reach a real mapping tool, **say so to the user** and let them decide. Never invent IRIs to make the run "look complete."

16. **ABCD/HBCD mode: verification is the feature, not a formality.**
    - **The paper is the only source of what a study used.** Never add a variable because ABCD papers usually include it, and never enumerate the data dictionary into the output. The dictionary and the Cognitive Atlas exist only to *verify and join* what the paper says.
    - **Every item needs a verbatim `evidence.quote` (>= 25 chars) that is findable in that paper.** `scripts/abcd_verify.py` deletes items whose quote is not there and records why in `rejected[]`. The requirement is per-section: a **variable** name must appear literally in its quote; a **construct** need not (it is a reading of the prose, and `label_in_quote` records which case it was); a **finding/model** must name at least one variable that appears in its quote.
    - **A string is only called an ABCD variable when a real dictionary release contains it** (`dictionary_status: "verified"`). Otherwise it stays visible as `unverified_variable` / `not_a_variable_name`. Build snapshots first: `python -m scripts.abcd_dictionary build --study abcd --release latest`. Load two or more releases so renames surface as `dd_release_gap`.
    - **Report the mention AND the mapping.** Keep `mention_as_written` (how the paper wrote it — prose label or id) alongside the resolved `dictionary_match.variable`, `nda_or_nbdc_table` and `nbdc_domain`. A reader must be able to see both sides of the join.
    - **Construct ids are tool-only.** A `trm_`/`tsk_` id is attached only when `scripts/cognitive_atlas.py` returned it; a model-supplied id is demoted into `demoted_claim`. Unmapped is an acceptable answer — run `cognitive_atlas search` and offer candidates to the user rather than auto-picking.
    - **Provenance includes where in the paper**: `section`, `page`, char offsets into the original text, and `used_context` (the surrounding sentences). Per run, record every dictionary snapshot consulted (release, source, sha256, retrieval time) and the Atlas vocabulary versions.
    - **Synthesis counts by PAPER, never by finding**, so one verbose paper cannot outvote several others. `divergent` means opposing signs, not differing magnitudes. A contested mediator/moderator role is reported as contested, never resolved by majority vote — which is why the extractor must emit `unspecified` when a paper is ambiguous.
    - **Bulk is first-class**: `--bulk` over a directory keeps going when one paper fails, writes one output set per paper, and `--synthesize` adds the cross-paper pass. Per-paper evidence stays inspectable; the synthesis never becomes the only record.

## File map (load on demand)

The files below are intentionally separated so you only load what the current task needs.

### `references/`
- `pipeline-pattern.md` — multi-stage agent pattern, how to chain stages, when to skip, resume from a saved stage.
- `ner-extraction.md` — NER methodology: entity types, output keys, edge cases, exhaustive extraction, mask-recall pass, grouped view.
- `ner-models.md` — HuggingFace + LLM ensemble: model roster, profiles by domain, `source_model` provenance, consensus count, when to skip.
- `resource-extraction.md` — resource extraction methodology (tools/datasets/models/benchmarks/leaderboards).
- `structured-extraction.md` — generic schema-driven extraction (PDF → user-supplied JSON schema).
- `ontology-mapping.md` — BioPortal REST API, OLS REST API, embedding-based hybrid retrieval, LLM-only fallback. Picking and combining backends.
- `chunking-strategy.md` — sentence-aligned chunking, parallel extraction, merge by stable key, context window math.
- `json-output-discipline.md` — schema-locked prompting, JSON repair, validation.
- `model-selection.md` — picking models per stage; OpenRouter / Ollama / vLLM / Claude / GPT / Gemini configuration.
- `human-feedback.md` — designing the human-in-the-loop review step.
- `abcd-extraction.md` — **ABCD/HBCD mode**: extracting variables/constructs/models/findings from publications, the three hard rules (strict verification, complete provenance, single-or-bulk), building dictionary snapshots from NBDCtools, Cognitive Atlas construct mapping, and how to read the cross-paper synthesis verdicts.

### `prompts/`
- `extractor-ner-general.md` — general-domain NER (Person, Org, Location, Product, Event, Date, …).
- `extractor-ner-neuroscience.md` — broad neuroscience NER (BrainRegion, CellType, Gene, Protein, Drug, Behavior, Disease, Method, Stimulus, Measurement, …).
- `extractor-ner-cns-cells.md` — CNS-cell-focused NER (CellClass / CellType / CellSubtype with lineage markers, morphology, ephys, layer, projection, atlas references, profiling method).
- `mask-recall-pass.md` — **pass-2** that surfaces mentions pass-1 missed. Run on any of the three NER prompts above. Big recall booster (typical +30–80%).
- `mask-verify-pass.md` — per-item label sanity check via cloze (mask one entity, predict label from context). Optional precision booster.
- `extractor-resource.md` — research resource extractor (one primary Model/Dataset/Tool/Benchmark per source, with `mentions` for secondaries).
- `extractor-structured.md` — schema-driven extractor (PDF → user-supplied JSON Schema).
- `alignment.md` — ontology alignment (LLM + concept-mapping tool).
- `alignment-via-http.md` — turnkey curl + jq pipeline for calling the local hybrid `/map/batch` endpoint directly. Use when you have Bash + network access but no Python client (Claude Code is the common case).
- `judge.md` — per-item quality judge.
- `humanfeedback.md` — apply human reviewer edits.
- `extractor-abcd.md` — ABCD/HBCD extractor: variables (as mentioned), constructs, models, findings with roles/directions, each with a verbatim quote + section/page.

### `schemas/`
- `ner-output.schema.json` — JSON Schema for NER output.
- `resource-output.schema.json` — JSON Schema for resource output.
- `aligned-item.schema.json` — fragment schema for any aligned item (adds ontology + provenance fields).
- `judged-item.schema.json` — fragment schema for any judged item (adds judge_score + remarks).
- `abcd-paper.schema.json` — ABCD/HBCD per-paper result: variables (with `mention_as_written`, `dictionary_status`, `nda_or_nbdc_table`, `nbdc_domain`), constructs, models, findings, `rejected[]`, `verification`, and provenance including every dictionary snapshot consulted.
- `abcd-synthesis.schema.json` — cross-paper synthesis: per-construct consensus/divergence verdicts, per-variable role consistency, variable↔construct links, and the `method` block recording the thresholds a verdict was reached under.

### `scripts/` (runnable helpers)
- `chunking.py` — sentence-aligned chunking, span re-anchoring, deduplication.
- `json_repair.py` — four-tier JSON repair (strict → fences → json-repair → truncate-to-balanced) + schema-driven LLM repair.
- `span_validator.py` — validate `text[start:end] == entity`, repair from sentence context.
- `mask_pass.py` — build masked text for the mask-recall pass (offset-preserving placeholders), translate offsets back, and mask single items for the mask-verify pass.
- `stats.py` — compute the `stats` block (totals, label histogram, by-source-model histogram, alignment provenance, judge score buckets, per-stage timings) and a human-readable summary.
- `group_by_entity.py` — collapse raw mentions into `entities_grouped[]` keyed by canonical `(entity, label)`: collects all `mentions`, deduplicates `sentences` across locations, lists every contributing `source_model`, and reports `consensus_count`. Also exposes `unify_ontology_across_entities()` — when the same surface form gets different ontology IDs from different chunks/models, pick the best one (tool-mapped > LLM > unmapped) and apply it to every occurrence. Both run automatically in `attach_grouped_views()`.
- `ner_models.py` — HuggingFace NER ensemble (biomedical + clinical specialists). Default roster: d4data, BC5CDR-chem, NCBI-disease, BioBERT-genetic, plus the BENT-PubMedBERT family (Gene / Chemical / Disease / Anatomical / Cell-Type / Cell-Line / Organism / Bioprocess). Picked by profile: `biomedical_broad` / `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`. Every emitted item carries a `source_model` provenance field.
- `normalize_result.py` — **idempotent post-processor**. Lifts per-entity `paper_title`/`doi` into top-level `source_metadata`, strips the per-entity dupes, tags missing `source_model` + `alignment_method`, infers `task_type`, runs **strict IRI validation** (demotes `llm_knowledge` and malformed IRIs), attaches `entities_grouped[]`, and computes `stats` (with prominent `totals` block at the top). **Runs automatically** in `pipeline.py` before saving. Also exposed as a CLI: `python -m scripts.normalize_result legacy.json --input paper.txt --llm-model …`. This is the safety net that guarantees the canonical shape even when the LLM ignores the prompt.
- `iri_validation.py` — **strict IRI validator**. Per-ontology regex patterns + permissive structural fallback. Rejects `concept_mapping_provenance: "llm_knowledge"` outright (zero hallucination policy), demotes malformed IRIs to `unmapped`, accepts legitimate cross-ontology mappings (e.g. CIDO results that reuse HP IRIs). Adds `result["validation"]` with `passed` / `demoted` counts and `demoted_by_reason` breakdown.
- `input_loader.py` — **PDF / CSV / TXT / MD** ingestion. PDF backends in order: GROBID (if reachable) → PyMuPDF (`fitz`) → pdfminer.six. Graceful fallback so it works with whichever library is installed. Also writes a sibling `<stem>.txt` so subsequent NER stages have stable character offsets. CLI: `python -m scripts.input_loader paper.pdf [--no-grobid] [--grobid-url …]`.
- `task_detection.py` — **auto-detect task type** from a free-text task description: heuristic regex first (fast, no LLM), then LLM fallback via `llm_client`. Returns a `TaskDetection` with `task_type` (`ner`/`resource`/`structured_extraction`/`relation_extraction`/`keyphrase_extraction`/…), `confidence`, `labels`, `rationale`.
- `model_context.py` — **model context-window registry** (~50 model families, longest-match wins) + token-aware `compute_downstream_chunk_size(...)` for sizing alignment/judge/humanfeedback chunks. CLI: `python -m scripts.model_context openrouter/anthropic/claude-sonnet-4-6 --items 2000 --workers 8`.
- `bioportal_map.py` — throttled + LRU-cached BioPortal client.
- `ols_map.py` — EBI OLS client (no API key).
- `local_hybrid_map.py` — client for a self-hosted BM25+dense mapping service (one POST, many terms).
- `llm_client.py` — provider-agnostic LLM call (OpenAI / OpenRouter / Anthropic / Ollama / Gemini).
- `pipeline.py` — reference end-to-end pipeline (extract → align → judge) wiring the helpers together.
- `abcd_dictionary.py` — **ABCD/HBCD variable dictionary**. Builds release snapshots from NBDCtools (`nbdctools` on PyPI reads `lst_dds` without R) or from your own CSV export, with provenance (source, sha256, retrieval time). `Dictionary.resolve()` decides whether a string from a paper IS a real variable — exact name → normalised name → full label/description, never fuzzy — and `releases_for()` surfaces renames across releases. CLI: `build` / `info` / `lookup` / `search`.
- `cognitive_atlas.py` — Cognitive Atlas client (~918 concepts, ~856 tasks), cached to disk after one fetch. Exact/singular/alias matching only; an unmatched construct stays unmapped rather than being guessed. CLI: `refresh` / `map` / `search`.
- `abcd_verify.py` — **the ABCD verifier**. Anchors every `evidence.quote` in the paper's own text (whitespace/ligature-normalised, re-anchoring offsets but never inventing quotes), applies the per-section surface rules, gates variables against the dictionary and constructs against the Atlas, and returns `rejected[]` + a `verification` summary.
- `abcd_extract.py` — driver for **one PDF or a whole directory** (`--bulk`). load → chunk → LLM extract → merge → verify → write `<stem>_abcd.{json,md,ttl}`. `--synthesize` chains the cross-paper pass; `--reverify` re-checks an existing extraction with no LLM calls.
- `abcd_synthesize.py` — cross-paper synthesis: consensus/divergence per construct and role consistency per variable, **counting by paper, not by finding**. Emits evidence pointers (paper id, section, verified quote) for every aggregate.
- `abcd_export.py` — JSON + Markdown tables + Turtle writers shared by both drivers. The Turtle uses PROV-O plus a small `abcd:` vocabulary, carrying quote, `usedContext`, char offsets, section/page, `mentionAsWritten`, `ndaOrNbdcTable` and `nbdcDomain` into triples.

### `examples/`
- `ner-example.md` — end-to-end NER worked example.
- `resource-example.md` — end-to-end resource extraction worked example.
- `reproschema-example.md` — end-to-end PDF → ReproSchema worked example.

### `connecting/` (how to wire the skill into different LLM platforms)
- `claude-code.md` — install as a Claude Code skill (`~/.claude/skills/` or `.claude/skills/`). Auto-discovery via the `SKILL.md` frontmatter.
- `pi-dev.md` — install as a [Pi](https://pi.dev) skill (`~/.pi/agent/skills/`, `~/.agents/skills/`, or `.pi/skills/`). Pi is a CLI coding agent with native Agent Skills support and a built-in `bash` tool, so it runs the pipeline directly — same story as Claude Code.
- `claude-desktop.md` — **Claude Desktop has a split execution model**: chat UI on your machine, code interpreter in Anthropic's cloud sandbox (so it cannot reach your `localhost:8000` directly). Use the MCP server config in this guide to bridge.
- `claude-skills.md` — upload as a hosted Anthropic Skill on claude.ai or use with the Claude Agent SDK.
- `custom-gpt.md` — wire as an OpenAI Custom GPT (Instructions + Knowledge files + optional server-side Action).
- `mcp-server.md` — expose the pipeline as an MCP server so any MCP-aware client (Claude Code, Cursor, ChatGPT desktop, custom agents) can call it.

## Minimal mental model

If you remember nothing else, remember this:

> Three short, focused prompts in sequence (extract, align, judge), each emitting strict JSON that the next prompt parses. Add a concept-mapping tool call inside alignment when you need real ontology IRIs. Chunk long inputs at sentence boundaries and merge by stable identifiers. Validate against a JSON schema.

That's the entire skill. The references and prompts here are the careful version of that one sentence.
