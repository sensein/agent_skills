# StructSense Skills

**Current version: 0.3.1** (see [CHANGELOG.md](CHANGELOG.md)).

A model-agnostic skill for **structured information extraction**: NER, research-resource extraction, and schema-driven JSON extraction, with optional ontology mapping (BioPortal / OLS / local hybrid), quality scoring, and human-in-the-loop review.

Works with any LLM — Claude, GPT, Gemini, Pi, local Ollama / vLLM. The skill ships system prompts, JSON Schemas, and pure-Python helper scripts, and does not depend on the `structsense` library.

> **Why this exists.** It distills the proven multi-stage pattern that the [`structsense`](https://docs.brainkb.org/structsense_overview.html) pipeline uses (extract → align → judge → human feedback) into a portable, drop-in skill. You can use the prompts directly with any chat/agent tool, or wire them into your own pipeline with the included helper scripts.

---

## Directory map

```
structsense-skills/
├── SKILL.md                 ← entry point with name + description + version frontmatter (load this first)
├── README.md                ← you are here
├── CHANGELOG.md             ← what changed in each version
├── connecting/              ← integration guides per platform
│   ├── claude-code.md
│   ├── claude-skills.md
│   ├── custom-gpt.md
│   └── mcp-server.md
├── references/              ← progressive-disclosure documentation
│   ├── pipeline-pattern.md
│   ├── ner-extraction.md
│   ├── resource-extraction.md
│   ├── structured-extraction.md
│   ├── ontology-mapping.md
│   ├── chunking-strategy.md
│   ├── json-output-discipline.md
│   ├── model-selection.md
│   └── human-feedback.md
├── prompts/                 ← copy-paste-ready system + user prompts
│   ├── extractor-ner-general.md           ← Person / Org / Location / Product / Event / …
│   ├── extractor-ner-neuroscience.md      ← BrainRegion / Gene / Protein / Drug / Method / …
│   ├── extractor-ner-cns-cells.md         ← CellType / CellSubtype / LineageMarker / Ephys / …
│   ├── mask-recall-pass.md                ← pass-2: catch mentions pass-1 missed
│   ├── mask-verify-pass.md                ← per-item cloze label check
│   ├── extractor-resource.md              ← Model / Dataset / Tool / Benchmark / …
│   ├── extractor-structured.md            ← user-supplied JSON Schema
│   ├── alignment.md                       ← ontology mapping
│   ├── judge.md                           ← per-item quality scoring
│   └── humanfeedback.md                   ← apply reviewer edits
├── schemas/                 ← JSON Schemas for outputs (drop into structured-outputs APIs)
│   ├── ner-output.schema.json
│   ├── resource-output.schema.json
│   ├── aligned-item.schema.json
│   └── judged-item.schema.json
├── scripts/                 ← pure-Python runnable helpers
│   ├── chunking.py
│   ├── json_repair.py
│   ├── span_validator.py
│   ├── mask_pass.py
│   ├── bioportal_map.py
│   ├── ols_map.py
│   ├── local_hybrid_map.py
│   ├── llm_client.py        ← OpenAI / OpenRouter / Anthropic / Ollama / Gemini
│   ├── ner_models.py        ← HF biomedical NER ensemble (d4data, BC5CDR, NCBI-disease,
│   │                          BioBERT-genetic, BENT-PubMedBERT family, …) + provenance
│   ├── group_by_entity.py   ← merge mentions into entities_grouped[] (per-entity index)
│   ├── stats.py             ← totals, label histogram, by-source-model breakdown,
│   │                          alignment provenance, judge score buckets, timings
│   ├── normalize_result.py  ← idempotent post-processor. Lifts paper_title/doi to
│   │                          source_metadata, tags source_model, attaches grouped
│   │                          + stats. Runs automatically in pipeline.py; also CLI.
│   ├── input_loader.py      ← PDF/CSV/TXT ingestion with GROBID → PyMuPDF →
│   │                          pdfminer fallback chain. CLI writes <stem>.txt.
│   ├── task_detection.py    ← auto-detect task type (ner/resource/structured/…)
│   │                          from a free-text description. Heuristic + LLM.
│   ├── model_context.py     ← model context-window registry + downstream
│   │                          chunk-size math.
│   └── pipeline.py          ← reference driver: ensemble + LLM extract → mask-recall →
│                              align (cascade) → judge → normalize → group → stats
└── examples/                ← worked end-to-end examples
    ├── ner-example.md
    ├── resource-example.md
    └── reproschema-example.md
```

The entry point an LLM should load is **`SKILL.md`**. Everything else loads on demand from there.

---

## Using this skill (platform-specific setup)

Different ways to wire this skill into the tool you actually use day-to-day. Pick the one that matches your workflow:

| You want to use it with… | Read |
|---|---|
| **Claude Code** (CLI) | [connecting/claude-code.md](connecting/claude-code.md) — drop into `~/.claude/skills/` (user-global) or `.claude/skills/` (per-project). Auto-discovered. |
| **Hosted Claude Skills** on claude.ai / Claude Agent SDK | [connecting/claude-skills.md](connecting/claude-skills.md) |
| **ChatGPT Custom GPT** | [connecting/custom-gpt.md](connecting/custom-gpt.md) — Instructions + Knowledge files + (optional) server-side Action calling the pipeline. |
| **MCP-aware clients** (Claude Code, Cursor, ChatGPT desktop, custom agents) | [connecting/mcp-server.md](connecting/mcp-server.md) — expose `pipeline.py` as an MCP server. |
| Python script / direct API calls | The "Usage with the Anthropic SDK" / "Usage with OpenAI" sections below. |

**Got legacy output that has `paper_title`/`doi` on every entity, missing `source_metadata`, or no `entities_grouped`?** Don't edit by hand. Run the normalizer:

```bash
python -m scripts.normalize_result paper_final.json \
    --input paper.txt \
    --llm-model openrouter/anthropic/claude-sonnet-4-6
```

It's **idempotent** — safe to run on already-canonical files. It also runs automatically as the last step in every `scripts/pipeline.py` invocation, so any new run produces the canonical shape regardless of what the LLM emitted.

## Quick start

### 1. Decide what you want to extract

| User intent | Prompt |
|---|---|
| Entities + key terms from general text | `prompts/extractor-ner-general.md` |
| Entities + key terms from neuroscience text | `prompts/extractor-ner-neuroscience.md` |
| CNS cell-typing extraction (atlases, patch-seq, scRNA-seq) | `prompts/extractor-ner-cns-cells.md` |
| Pull tools / datasets / models / benchmarks from a paper | `prompts/extractor-resource.md` |
| Convert a PDF to a target JSON schema (e.g. ReproSchema) | `prompts/extractor-structured.md` |

### 2. Run pass-1, then pass-2 (mask-recall) for exhaustive yield

For NER, **always run mask-recall on top of pass-1** unless cost is critical. Typical recovery on neuroscience text: **+30–80% mentions**. See `references/ner-extraction.md` → "Two-pass strategy: mask-mode".

### 3. (Optional) align, judge, review

| Stage | Prompt |
|---|---|
| Ontology alignment | `prompts/alignment.md` (or skip the LLM and call the mapping helper directly — see `scripts/bioportal_map.py`, `ols_map.py`, `local_hybrid_map.py`) |
| Quality scoring | `prompts/judge.md` |
| Human review | `prompts/humanfeedback.md` |

---

## Usage with Claude Code

### As a project skill

```bash
cp -r structsense-skills/ /path/to/your/project/.claude/skills/structsense-skills/
```

Claude Code will discover it automatically from the `name` + `description` in `SKILL.md`'s frontmatter. Invoke implicitly by mentioning the task ("extract entities from this paper") or explicitly ("use the structsense-skills skill").

### As a user-global skill (auto-loaded on every project)

```bash
cp -r structsense-skills/ ~/.claude/skills/structsense-skills/
```

### Verify it loaded

In Claude Code:

```
/skills
```

You should see `structsense-skills` in the list with its one-line description.

---

## Usage with the Anthropic SDK

The skill files are plain Markdown / JSON / Python — there's no SDK-specific format. Load `SKILL.md` (or a specific prompt file) as a system prompt:

```python
from pathlib import Path
from anthropic import Anthropic

SKILL_DIR = Path("structsense-skills")
client = Anthropic()

# Load the NER extractor prompt (neuroscience variant)
system_prompt = (SKILL_DIR / "prompts" / "extractor-ner-neuroscience.md").read_text()

# Or compose: SKILL.md + the specific prompt(s) the task needs
system_prompt = "\n\n---\n\n".join([
    (SKILL_DIR / "SKILL.md").read_text(),
    (SKILL_DIR / "prompts" / "extractor-ner-neuroscience.md").read_text(),
])

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    system=system_prompt,
    messages=[{"role": "user", "content": f"INPUT TEXT:\n<<<\n{paper_text}\n>>>"}],
)
print(response.content[0].text)
```

For the **full pipeline** (extract → mask-recall → align → judge), use `scripts/pipeline.py` as a reference implementation.

For **structured outputs**, pass `schemas/ner-output.schema.json` (or any other schema) via tool-use with a JSON-typed input or via prompt-side schema embedding.

---

## Usage with OpenAI / GPT

### ChatGPT custom GPTs

1. Create a new GPT.
2. Paste the contents of `SKILL.md` + the relevant `prompts/extractor-*.md` into the "Instructions" field.
3. Upload `schemas/ner-output.schema.json` (and any other schemas you need) as a Knowledge file.
4. Save and invoke by asking it to extract from text.

### OpenAI Assistants API or `chat.completions`

Same pattern as the Anthropic example:

```python
from pathlib import Path
from openai import OpenAI

SKILL_DIR = Path("structsense-skills")
client = OpenAI()

system_prompt = (SKILL_DIR / "prompts" / "extractor-ner-neuroscience.md").read_text()
schema = (SKILL_DIR / "schemas" / "ner-output.schema.json").read_text()

resp = client.chat.completions.create(
    model="gpt-4.1",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"INPUT TEXT:\n<<<\n{paper_text}\n>>>"},
    ],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "ner", "schema": eval(schema)["properties"], "strict": True}},
    temperature=0,
)
```

For best results with GPT, use **OpenAI structured outputs** with the JSON Schemas in `schemas/` — it eliminates the need for `json_repair.py`.

### Through OpenRouter

Same as OpenAI, but the model string is `openrouter/<provider>/<model>` (e.g. `openrouter/anthropic/claude-sonnet-4-6`) and `base_url="https://openrouter.ai/api/v1"`. `scripts/llm_client.py` handles this transparently.

---

## Usage with Pi, Gemini, Mistral, etc.

The prompts work with any model that takes a system message. For providers without JSON-mode:

1. Paste the prompt as the system instruction.
2. Add to the system prompt: `"Output JSON only. Do not wrap in markdown fences."`
3. Run the response through `scripts/json_repair.py:parse_or_repair()` — it strips fences, repairs trailing commas, and handles truncations.

```python
from scripts.json_repair import parse_or_repair
raw_text = some_other_model.complete(system=prompt, user=text)
parsed = parse_or_repair(raw_text)
```

For Pi specifically: paste the prompt into the chat, then send the text in the next message.

---

## Usage with local models (Ollama / vLLM)

`scripts/llm_client.py` supports Ollama out of the box:

```python
from scripts.llm_client import call

raw = call(
    model="ollama/qwen2.5:14b",
    system=Path("structsense-skills/prompts/extractor-ner-neuroscience.md").read_text(),
    user=f"INPUT TEXT:\n<<<\n{paper_text}\n>>>",
    base_url="http://localhost:11434",
    json_mode=True,
    temperature=0,
)
```

For vLLM (OpenAI-compatible API), use `model="openai/<your-model>"` and set `base_url` to your vLLM server.

Small local models often produce malformed JSON; pair them with `scripts/json_repair.py` and consider constrained decoding (Outlines, llguidance, vLLM's `guided_json`).

---

## Running the reference pipeline end-to-end

`scripts/pipeline.py` wires together: (optional) HF NER ensemble + LLM extraction → mask-recall → ontology-mapping cascade → judging → grouping → stats. Standalone, no framework:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...

python -m scripts.pipeline \
    --task ner \
    --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper local \
    --mapper-url http://localhost:8000 \
    --ner-profile biomedical_broad \
    --chunk-size 2000 --max-workers 8
# writes paper_final.json (the input stem + _final.json) and prints stats to stderr.
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--task` | `ner` | `ner` / `resource` / `structured`. Picks the matching `extractor-*.md` prompt. |
| `--input` | (required) | Path to a `.txt` input. |
| `--extractor` | (required) | Model string for LLM extraction (e.g. `openrouter/anthropic/claude-sonnet-4-6`). |
| `--judge` | none = auto-approve | Model string for the judge stage. Omit to auto-approve (saves cost). |
| `--mapper` | `local` | Preferred concept-mapping backend: `local` / `bioportal` / `ols` / `none`. With `local`, the cascade is **local → BioPortal → ask user for URL → skip**. |
| `--mapper-url` | `http://localhost:8000` | Local hybrid service URL. Verify it's up at `/docs`. |
| `--non-interactive` | off | Disable the user prompt when the mapper cascade fails. Fail fast instead. |
| `--ner-profile` | none | Enable the HF ensemble: `biomedical_broad` / `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`. Requires `pip install transformers torch`. |
| `--ner-models` | none | Comma-separated explicit list of HF model IDs (overrides `--ner-profile`). |
| `--ner-device` | `-1` (CPU) | CUDA device index for HF models. |
| `--chunk-size` | `2000` | Characters per chunk for parallel extraction. |
| `--max-workers` | `8` | Parallel workers. |
| `--out` | `<input>_final.json` | Output JSON path. Defaults to `<input_stem>_final.json` (e.g. `paper.pdf` → `paper_final.json`). |

### What you get in the output

The pipeline always writes a `<input_stem>_final.json` containing:

```jsonc
{
  "source_metadata": {
    "paper_title": "…",   // ONCE per run, not per entity
    "doi":         "…",
    "source_path": "paper.txt"
  },

  "entities":         [ … ],   // raw mentions — one per occurrence (exhaustive)
                                // each item carries source_model, judge_score, ontology_*
  "key_terms":        [ … ],

  "entities_grouped": [ … ],   // canonical entities — mentions collapsed by (entity, label)
                                // each group lists source_models, sentences merged from all
                                // locations, mention_count, consensus_count, judge_score_max/avg/min
  "key_terms_grouped": [ … ],

  "ensemble_models": [          // per-HF-model summary (skipped/loaded)
    {"source_model": "d4data/biomedical-ner-all", "count": 215, "skipped_reason": null},
    {"source_model": "pruas/BENT-PubMedBERT-NER-Gene", "count": 124, "skipped_reason": null},
    "..."
  ],

  "stats": {                    // run summary — see scripts/stats.py
    "task_type": "ner",
    "elapsed_seconds": {"total": 42.3, "extraction": 18.4, "ensemble_ner": 12.1, "alignment": 5.2, "judge": 6.6},
    "input": {"char_count": 28430, "chunk_count": 14, "chunk_size_chars": 2000, "input_path": "paper.txt"},
    "entities": {
      "total_mentions": 1132,
      "unique_surface_forms": 287,
      "mentions_per_unique": 3.94,
      "by_label":        {"Gene": 312, "Protein": 198, "BrainRegion": 87, "…": "…"},
      "by_source_model": {"alvaroalon2/biobert_genetic_ner": 412,
                          "llm_ner:openrouter/anthropic/claude-sonnet-4-6": 287,
                          "d4data/biomedical-ner-all": 215, "…": "…"}
    },
    "alignment": {"mapper_used": "local_hybrid", "mapper_url": "http://localhost:8000",
                  "fallback_triggered": false, "cascade_history": ["local_hybrid@http://localhost:8000"]},
    "judge":     {"method": "llm", "score_buckets": {"1.00": 412, "0.85-0.99": 487, "…": "…"}}
  }
}
```

A human-readable summary of the stats block is also printed to stderr after the run.

See `examples/ner-example.md`, `examples/resource-example.md`, and `examples/reproschema-example.md` for worked walkthroughs.

---

## Helper-script dependencies

Pure-Python deps, install only what you use:

```bash
# Always useful
pip install requests

# For json_repair.py Tier-3 fallback (highly recommended)
pip install json-repair

# For repair_to_schema()
pip install jsonschema

# For llm_client.py — install whichever providers you actually use
pip install openai                # OpenAI / OpenRouter / vLLM
pip install anthropic             # Anthropic direct
pip install google-generativeai   # Gemini

# For the HuggingFace NER ensemble (ner_models.py) — required only if you
# use --ner-profile or pass ner_ensemble_profile=... to run()
pip install transformers torch

# For sentence-aligned chunking with spaCy (optional; chunking.py falls back to regex)
pip install "spacy>=3.7" && python -m spacy download en_core_web_sm
```

None of the scripts depend on `structsense` or `crewai` — they're standalone.

---

## Important rules (full list in `SKILL.md`)

1. **Strict JSON only**, no markdown fences. `temperature: 0` for extraction and alignment.
2. **Extract EXHAUSTIVELY.** Emit every occurrence of every mention as its own item with its own `start`/`end`. Never deduplicate by surface form. If yield feels low, run the mask-recall pass.
3. **Preserve fields downstream.** Alignment, judge, and human-feedback stages ADD fields. They never remove existing ones.
4. **Record provenance** — every mapped item carries `concept_mapping_provenance` (`tool` / `llm_knowledge` / `unmapped` / `skipped`), `alignment_method`, and `source_model` (which NER model — HF or LLM — surfaced this mention).
5. **Chunk and merge** for inputs > model context (or > 25k chars for safety on 128k models). Re-merge by stable identifiers, not order.
6. **Don't invent placeholders.** Pipe stage outputs verbatim — extractor JSON → alignment, alignment JSON → judge.
7. **Validate before returning.** Parse JSON; repair-then-retry on failure; validate against the schema in `schemas/`.
8. **Always emit a `stats` block** at the top level (totals, label/source_model histograms, alignment provenance, judge score buckets, timings).
9. **Final-result filename:** `<input_stem>_final.json` (e.g. `paper.pdf` → `paper_final.json`). Honor `--out` only when the user provides one.
10. **Mapping cascade:** local hybrid (`http://localhost:8000`, verify at `/docs`) → BioPortal → **ask the user** for an alternative URL → skip alignment only if they decline. Don't hardcode the URL.
11. **Document metadata at top, not per entity.** `paper_title` / `doi` / `source_path` go ONCE in `source_metadata`. `paper_location` stays per-entity because it varies.
12. **Always emit both `entities[]` (raw) and `entities_grouped[]` (per-entity index).** The raw list is one-per-occurrence; the grouped list collapses by canonical (entity, label) with merged sentences + every contributing `source_model`.

---

## Licence

Apache 2.0 (matches `structsense`'s licence).
