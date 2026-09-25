# StructSense Skills

**Current version: 0.9.0** (see [CHANGELOG.md](CHANGELOG.md)).

A model-agnostic skill for **structured information extraction**: NER, research-resource extraction, and schema-driven JSON extraction, with ontology mapping, a judge ensemble, and human-in-the-loop review.

Since 0.9.0 the result of a NER or resource run is **Turtle — one validated `<stem>.ttl` per paper**, instances of the bundled Named Entity Ontology (v2.4) with deterministic UUIDv5 IRIs, so the same entity in two papers is the same node. Concepts are mapped against the bundled **trusted ontologies first**, in the order you set in [`trusted_ontologes/priority.md`](trusted_ontologes/priority.md), then a local hybrid service, then BioPortal. Quality comes from a **judge ensemble** — independent grounding / labeling / mapping / key / claim judges plus a deterministic combiner — and every judge step, verdict and change is recorded as provenance in the Turtle. See [Turtle output](#turtle-output-09) and [Trusted ontologies](#trusted-ontologies-09).

Since 0.5.0 it also does **ABCD / HBCD extraction and cross-paper synthesis** — which variables a study used (resolved against the NBDC data dictionary, with `nda_or_nbdc_table` and `nbdc_domain`), the constructs behind them (Cognitive Atlas), the models specified, and the findings reported — under strict quote-level verification, for one PDF or a whole corpus. See [ABCD / HBCD extraction](#abcd--hbcd-extraction).

Works with any LLM — Claude, GPT, Gemini, Pi, local Ollama / vLLM. The skill ships system prompts, JSON Schemas, and pure-Python helper scripts, and does not depend on the `structsense` library.

> **Why this exists.** It distills the proven multi-stage pattern that the [`structsense`](https://docs.brainkb.org/structsense_overview.html) pipeline uses (extract → align → judge → human feedback) into a portable, drop-in skill. You can use the prompts directly with any chat/agent tool, or wire them into your own pipeline with the included helper scripts.

---

## Directory map

```
structsense/
├── SKILL.md                 ← entry point with name + description + version frontmatter (load this first)
├── README.md                ← you are here
├── CHANGELOG.md             ← what changed in each version
├── concept_mapping.json     ← mapping sources + order, match properties, label routing, tiers
├── judges_config.json       ← the judge panel: dimensions, weights, critical judges, prompts
├── default_ontology/        ← the target ontology and its representation policy
│   ├── named_entity_ontology.owl   ← Named Entity Ontology 2.4.0 (https://brainkb.org/ner/)
│   ├── named_entity_shapes.ttl     ← SHACL shapes for instance data
│   ├── ttl_config.json             ← IRI scheme (UUIDv5), guardrails, predicates, prefix registry (static part)
│   ├── label_class_map.json        ← extractor label → ontology class
│   └── key_synonyms.json           ← user overrides for keys (keys come from the trusted ontologies)
├── trusted_ontologes/       ← the trusted ontology files (OWL/TTL)
│   ├── priority.md                 ← THE priority table (edit to reorder / enable / add)
│   ├── README.md                   ← generated list of the ontologies
│   ├── sources.json                ← acquisition log
│   └── lexicon/                    ← generated index (TSV per ontology + SQLite; gitignored)
├── connecting/              ← integration guides per platform
│   ├── claude-code.md
│   ├── claude-desktop.md
│   ├── claude-skills.md
│   ├── custom-gpt.md
│   ├── mcp-server.md
│   └── pi-dev.md
├── references/              ← progressive-disclosure documentation
│   ├── pipeline-pattern.md
│   ├── ner-extraction.md
│   ├── resource-extraction.md
│   ├── structured-extraction.md
│   ├── ontology-mapping.md
│   ├── chunking-strategy.md
│   ├── json-output-discipline.md
│   ├── model-selection.md
│   ├── human-feedback.md
│   ├── judge-ensemble.md                  ← the judge panel and its aggregation semantics
│   ├── ttl-representation.md              ← the Turtle deliverable: IRIs, prefixes, provenance, the gate
│   ├── key-normalization.md               ← normalizedEntityKey, the cross-paper merge handle
│   └── abcd-extraction.md                 ← ABCD/HBCD mode: verification, provenance, synthesis
├── prompts/                 ← copy-paste-ready system + user prompts
│   ├── extractor-ner-general.md           ← Person / Org / Location / Product / Event / …
│   ├── extractor-ner-neuroscience.md      ← BrainRegion / Gene / Protein / Drug / Method / phenotypes by level …
│   ├── extractor-ner-cns-cells.md         ← CellType / CellSubtype / LineageMarker / Ephys / …
│   │                                        (all three also emit relations, hierarchy, causal claims)
│   ├── extractor-abcd.md                  ← ABCD/HBCD variables / constructs / models / findings
│   ├── mask-recall-pass.md                ← pass-2: catch mentions pass-1 missed
│   ├── mask-verify-pass.md                ← per-item cloze label check
│   ├── extractor-resource.md              ← Model / Dataset / Tool / Benchmark / …
│   ├── extractor-structured.md            ← user-supplied JSON Schema
│   ├── alignment.md                       ← ontology mapping
│   ├── judge-grounding.md / judge-labeling.md / judge-mapping.md /
│   │   judge-kg-keys.md / judge-claims.md ← the ensemble's judges, one dimension each
│   ├── judge-combiner.md                  ← settles judge disagreements only
│   ├── kg-plan.md                         ← keys, classes, paper-stated relations, causal claims
│   ├── judge.md                           ← legacy single-score judge (on request)
│   └── humanfeedback.md                   ← apply reviewer edits
├── schemas/                 ← JSON Schemas for outputs (drop into structured-outputs APIs)
│   ├── ner-output.schema.json
│   ├── resource-output.schema.json
│   ├── aligned-item.schema.json
│   ├── judged-item.schema.json
│   ├── judge-review.schema.json           ← one judge's review of one packet
│   ├── kg-plan.schema.json                ← kg_plan.json
│   ├── abcd-paper.schema.json             ← ABCD per-paper result (+ rejected[], verification)
│   └── abcd-synthesis.schema.json         ← cross-paper consensus / divergence / roles
├── scripts/                 ← pure-Python runnable helpers
│   ├── concept_mapping.py   ← trusted-ontology lexicon (index / lookup / map), priority,
│   │                          routing, then local hybrid → BioPortal
│   ├── prefixes.py          ← the one prefix registry (show / check / compact / expand)
│   ├── relations.py         ← resolves the extractor's relations / hierarchy / causal claims
│   │                          (and cns-cells cell_context) to extracted entities
│   ├── json_to_ttl.py       ← judged result + kg_plan → Turtle (UUIDv5 IRIs, full provenance)
│   ├── validate_ttl.py      ← the gate: OWL vocabulary, SHACL, policy, prefixes, connectivity
│   ├── judge_prepare.py     ← deterministic grounding review + one packet per judge
│   ├── judge_combine.py     ← deterministic aggregation (gates, demotions, fixes, scores)
│   ├── judge_ensemble.py    ← headless runner for the panel and the kg_plan
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
│   ├── input_loader.py      ← document ingestion for every format Docling
│   │                          reads (PDF, DOCX, PPTX, XLSX, HTML, images, …).
│   │                          Docling first, then GROBID → pymupdf4llm →
│   │                          PyMuPDF → pdfminer for PDFs. Writes <stem>.txt.
│   ├── task_detection.py    ← auto-detect task type (ner/resource/structured/…)
│   │                          from a free-text description. Heuristic + LLM.
│   ├── model_context.py     ← model context-window registry + downstream
│   │                          chunk-size math.
│   ├── pipeline.py          ← reference driver: ensemble + LLM extract → mask-recall →
│   │                          align (cascade) → judge → normalize → group → stats
│   ├── abcd_dictionary.py   ← ABCD/HBCD dictionary: build release snapshots (bundled
│   │                          workbook / NDA API / CSV), resolve a mention across
│   │                          releases and namings. CLI: build/info/lookup/releases
│   ├── cognitive_atlas.py   ← Cognitive Atlas concepts + tasks, cached; tool-only
│   ├── abcd_verify.py       ← the verifier: anchor every quote in the paper, gate
│   │                          variables on the dictionary, constructs on the Atlas
│   ├── abcd_inputs.py       ← input resolver: PDF / directory / DOI table / DOI,
│   │                          fetching open-access PDFs (no paywall, no proxy)
│   ├── abcd_extract.py      ← driver: one argument, auto-detected. --prepare/--payload
│   │                          when you are the model; --llm-model when a framework calls
│   ├── abcd_synthesize.py   ← cross-paper consensus/divergence + role consistency
│   └── abcd_export.py       ← JSON + Markdown tables + Turtle (PROV-O) writers
├── data/
│   └── dictionaries/        ← bundled ABCD/HBCD dictionaries, all 7 releases,
│                              539,781 variables in 8.6 MB gzipped (self-contained)
├── requirements.txt         ← core deps; -llm / -ner / -dev for the optional paths
└── examples/                ← worked end-to-end examples
    ├── ttl/                 ← a real open-access paper (Hu et al. 2026, CC BY 4.0) end to end
    │                          to a validated hu2026.ttl — run.sh, curated reference, text
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
| **Pi** (CLI, pi.dev) | [connecting/pi-dev.md](connecting/pi-dev.md) — drop into `~/.pi/agent/skills/`, `~/.agents/skills/`, or `.pi/skills/`. Native Agent Skills support; runs the pipeline via its built-in `bash` tool. |
| **Claude Desktop** | [connecting/claude-desktop.md](connecting/claude-desktop.md) — split execution model; bridge `localhost` services via MCP. |
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

### 3. Map, judge, represent

| Stage | How |
|---|---|
| Ontology mapping | `python -m scripts.concept_mapping map <result.json>` — trusted ontologies in `priority.md` order, then local hybrid, then BioPortal (tool-only; no IRI from model knowledge) |
| Relations (in extraction) | the extractor emits per-mention `relations` / `broader` and document `causal_relations`; `scripts/relations.py` resolves them to extracted entities (see [Relations, hierarchy and causal claims](#relations-hierarchy-and-causal-claims-09)) |
| KG plan (NER) | `prompts/kg-plan.md` → `kg_plan.json`: coreference keys, finer classes, cross-sentence relations and chains extraction could not state |
| Judge ensemble | `scripts/judge_prepare.py` → one judge at a time per `prompts/judge-*.md` → `scripts/judge_combine.py` (→ `prompts/judge-combiner.md` only for disagreements) |
| Turtle | `python -m scripts.json_to_ttl <result.json> --kg-plan kg_plan.json --source paper.pdf` → `python -m scripts.validate_ttl <stem>.ttl` (must report 0 violations) |
| Human review | `prompts/humanfeedback.md` (escalations from the combiner) |

The whole chain on a real paper: `examples/ttl/run.sh`.

---

## Usage with Claude Code

### As a project skill

```bash
cp -r structsense/ /path/to/your/project/.claude/skills/structsense/
```

Claude Code will discover it automatically from the `name` + `description` in `SKILL.md`'s frontmatter. Invoke implicitly by mentioning the task ("extract entities from this paper") or explicitly ("use the structsense skill").

### As a user-global skill (auto-loaded on every project)

```bash
cp -r structsense/ ~/.claude/skills/structsense/
```

### Verify it loaded

In Claude Code:

```
/skills
```

You should see `structsense` in the list with its one-line description.

---

## Usage with the Anthropic SDK

The skill files are plain Markdown / JSON / Python — there's no SDK-specific format. Load `SKILL.md` (or a specific prompt file) as a system prompt:

```python
from pathlib import Path
from anthropic import Anthropic

SKILL_DIR = Path("structsense")
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

SKILL_DIR = Path("structsense")
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
    system=Path("structsense/prompts/extractor-ner-neuroscience.md").read_text(),
    user=f"INPUT TEXT:\n<<<\n{paper_text}\n>>>",
    base_url="http://localhost:11434",
    json_mode=True,
    temperature=0,
)
```

For vLLM (OpenAI-compatible API), use `model="openai/<your-model>"` and set `base_url` to your vLLM server.

Small local models often produce malformed JSON; pair them with `scripts/json_repair.py` and consider constrained decoding (Outlines, llguidance, vLLM's `guided_json`).

---

## ABCD / HBCD extraction

Extracts what an ABCD or HBCD study **used** (variables, constructs, models) and
**found** (findings with direction and role) from the publication itself, then
compares across papers: where constructs reach consensus, where they diverge, and
whether particular variables are consistently mediators or moderators.

The paper is the only source of what a study used. The data dictionary and the
Cognitive Atlas verify and join; neither is enumerated into the output.

```bash
pip install -r requirements.txt

# one argument, auto-detected: a PDF, a directory, a CSV/TSV/XLSX of DOIs, or a DOI
python -m scripts.abcd_extract paper.pdf --prepare          # you are the model
python -m scripts.abcd_extract paper.pdf --payload paper.payload.json
python -m scripts.abcd_extract ./papers --llm-model MODEL   # a framework calls an API
```

Every run writes `<stem>_abcd.json`, `.md` (tables) and `.ttl` (PROV-O triples) into
`<input>/abcd_results/`, never beside the papers — plus `abcd_synthesis.{json,md,ttl}`
when there is more than one paper:

```
papers/                                  <- your PDFs, untouched
└── abcd_results/                        <- --out-dir to put it elsewhere
    ├── <stem>_abcd.{json,md,ttl}        one set per paper
    ├── <stem>_abcd.codebook.tsv         --formats ...,codebook (opt-in)
    ├── abcd_synthesis.{json,md,ttl}     the cross-paper pass
    ├── text/<stem>.txt                  --prepare writes extracted text here
    └── payloads/<stem>.payload.json     ...and expects your payloads here
```

`abcd_results/` is excluded from input scanning, so a rerun does not read its own
`text/` copies back in as papers. With payloads in place, `--payload` can be omitted
entirely — the run finds them.

**Dictionaries ship with the skill** — `data/dictionaries/` holds all seven releases
(ABCD `nda-legacy` for 2.0/3.0/4.0/5.x, `6.0`, `6.1`, `7.0`; HBCD `1.0`, `1.1`,
`2.0`), 570,150 variables in 9.1 MB gzipped. No workbook, no network, no R:

```bash
python -m scripts.abcd_dictionary info
python -m scripts.abcd_dictionary lookup nihtbx_flanker_uncorrected
```

That lookup matters: ABCD 6.x renamed variables wholesale, so a 2021 paper's
`nihtbx_flanker_uncorrected` is `nc_y_nihtb__flnkr__uncor_score` in 6.1. The
catalog's NDA / DEAP / short / Stata names are all indexed, and the match method
records which naming the paper used, so a name resolves across every era.

### Mapping a paper's wording, not just its variable names

Most papers never print a variable name. They write "youth-reported family
conflict", "the Flanker Inhibitory Control and Attention Test", "fractional
anisotropy" — and a name-only resolver returns nothing for all three, leaving
`nda_or_nbdc_table` and `nbdc_domain` empty, which is the point of the mapping. So
`scripts/abcd_context.py` matches the paper's phrasing against dictionary *labels*,
disambiguated by what the paper itself said:

| Signal from the paper | What it decides |
|---|---|
| `instrument` — "Child Behavior Checklist" | Scopes candidates to one table. `externalizing` exists in the CBCL, ABCL, YSR and BPM; naming the instrument settles it. |
| `respondent` — "children completed the FES" | `fes_y_ss_fc`, not `fes_p_ss_fc`. A different measure, not a near miss — so mismatches are filtered out, not merely penalised. |
| `metric` — "fully corrected T-scores" | `nihtbx_cryst_fc` over `nihtbx_cryst_rawscore`, and any metric cue outranks the administrative siblings (Version, Language, ItmCnt) that share every content word with the measure. |
| `data_release` — "Release 5.0" | Which snapshot is eligible at all. Matching a 5.0 paper against 6.1 turns one clear measure into rival candidates in two tables. For NDA 2.0/3.0/4.0 the check is per-row: each row records which releases its structure shipped in, so a 3.0 paper is held to 3.0's 87,682 variables rather than the 116,353-variable union. |

On a three-paper sample this moved 1 of 57 variables carrying a dictionary table to
29 of 57. What it will not do is name a variable the paper did not name:

- `context_variable` — one variable, with the margin over its rivals recorded
- `context_family` — several variables in one table fit equally well (68
  Desikan-Killiany ROIs for "cortical thickness"): table and domain reported,
  variable `null`, family prefix and candidates attached
- `context_domain` — tables disagree, the domain does not
- `instrument_table` — the paper named an instrument, not a variable
- `ambiguous` / `not_a_variable_name` — an honest gap, with the candidates it
  rejected and why

Every one of those carries `context_mapping`: the cues that fired, the ranked
candidates with scores, and the thresholds applied.

`scripts/abcd_nda_api.py` adds NDA's live element API — confirming a printed name
that no bundled release contains (`verified_via_nda_api`), and full-text searching
element descriptions. Search hits are recorded as `nda_api_suggestions`, never as
mappings: NDA ranks across the whole archive and offered an *Adult* Behavior
Checklist score for "internalizing behaviors".

### What makes the output trustworthy

Full detail in [`references/abcd-extraction.md`](references/abcd-extraction.md):

1. **Strict verification.** Every item carries a verbatim quote (≥25 chars) that must
   be findable in *that* paper; failures land in `rejected[]` with a reason instead of
   vanishing. A variable name must appear literally in its quote; a construct need not
   (it is a reading of the prose) and `label_in_quote` records which case it was.
2. **Only what this study did.** Introductions and discussions are largely other
   people's work. A variable counts only if this paper measured it, a finding only if
   this paper's analysis produced it; the rest is rejected as
   `finding_attributed_to_cited_work` / `measure_only_mentioned_in_cited_work`.
   Without that gate, paper A's summary of paper B arrives in the synthesis as
   independent evidence.
3. **Complete provenance**, including position: `section`, `page`, char offsets into
   the original text, `used_context` (the surrounding sentences), plus every
   dictionary snapshot consulted and the Atlas vocabulary versions.
4. **A coverage audit.** Anything named in a model or finding but never declared in
   `variables[]` is listed in `coverage.referenced_but_not_declared` — a hole in the
   extraction, not a detail, since those rows reach the synthesis with no evidence,
   no table and no domain.
5. **One paper per distinct file, one entry per variable.** Inputs are deduplicated by
   content checksum before any PDF is parsed (`paper.pdf` and `paper(1).pdf` are one
   paper — otherwise a duplicate doubles that study's weight in every
   paper-counted consensus), and duplicate wordings of the same variable inside a
   paper are merged with every quote kept.
6. **Single or bulk, same guarantees.** One failing paper does not abort a corpus, and
   per-paper evidence stays inspectable — the synthesis is never the only record.

### The synthesis

Counts **by paper, not by finding**, so one verbose paper cannot outvote several
others. Beyond that it reports:

- **`claims[]`** — what the corpus supports, then the evidence paper by paper with a
  strength rating (sample band, design, whether an effect size was printed,
  subgroup-only), then the **contradictions** separately, then caveats such as
  "these papers report the same sample size, so their agreement is not independent".
- **constructs** — the papers behind *each direction*, agreement over paper-direction
  claims (papers as the denominator let a row print 1.00 agreement next to a
  `divergent` verdict), and the variables that measured the construct — declared
  measures kept apart from variables that merely appear in its findings.
- **variables** — per paper: the wording used, instrument, respondent, metric, roles,
  timepoints, what it resolved to, its table, its domain and the **dictionary release
  that mapping holds in**, plus the quotes. Two papers count as the same variable only
  when they resolve to the same dictionary variable, share a declared alias, or share
  a normalised mention — never on similarity, and a `mapping_disagreement` is reported
  rather than resolved.
- **papers** — the dataset each analysed: release, sample, analytic sample, design,
  waves, cohort, site count, source.

`divergent` means opposing signs, not differing magnitudes. A contested
mediator/moderator role is reported as contested rather than resolved by majority —
and consistency requires `role_exclusivity` too, because a variable that is a
mediator in every paper *and* an outcome in every paper is contested.

---

## Turtle output (0.9)

One validated `<stem>.ttl` per paper — N papers, N files. Everything is an instance of
`default_ontology/named_entity_ontology.owl` (v2.4) under `kb:<uuid5>` IRIs:

- **Entities** keyed by `normalizedEntityKey` (`entity|<key>`), **concepts** by IRI and
  **ontology hubs** by acronym share one IRI across papers, so loading several files
  into one store merges them by plain union; everything paper-specific (mentions,
  reviews, runs) is scoped by DOI. The scheme reproduces the BrainKB graph's IRIs.
- **Grounding**: every mention with verbatim surface, character offsets, sentence and
  section; the model that surfaced it.
- **Alignment**: only tool-verified mappings become IRIs, at an honest skos tier, each
  with its decision record (candidate, rank, confidence, the source and method that
  decided it).
- **Claims**: paper-stated RO/BFO edges (incl. has_phenotype), in-paper hierarchies
  (`skos:broader`: subtype → type → class), and causal relations with versions,
  evidence basis and effect estimates — stated by the extractor itself for every
  NER domain (plus kg_plan), resolved to entities and reviewed by the claims judge.
- **Provenance**: run start/end (recorded, never inferred), agent versions, a
  configuration hash, and the judges as data — one activity per judge with its model
  version and prompt hash, every verdict as a `ReviewDecision` (dimension, confidence),
  an ensemble-combined decision derived from them, and a `ChangeRecord` for every
  relabel, tier change, key rename, demoted mapping and dropped item.

`scripts/validate_ttl.py` gates each file: declared OWL vocabulary and domain/range,
the SHACL shapes (`default_ontology/named_entity_shapes.ttl`), config policy, prefix
consistency, labels and one connected component. See
[references/ttl-representation.md](references/ttl-representation.md).

## Relations, hierarchy and causal claims (0.9)

Entities are half of what a paper says; the extractor states the other half with
them, for every NER domain:

- **per mention** — `relations`: `{predicate, target}` with the target another
  extracted mention as written, predicate from the closed list in
  `default_ontology/ttl_config.json` (`part_of`, `located_in`, `expresses`,
  `has_phenotype` RO:0002200, `in_taxon`, `capable_of`, `develops_from`, …); and
  `broader`: the in-paper hierarchy (CellSubtype → CellType → CellClass, region →
  larger region, drug → drug class);
- **cns-cells** — the existing `cell_context` becomes edges: marker → `expresses`,
  region/layer → `located_in`, species → `in_taxon`, ephys → `has_phenotype`;
- **per document** — `causal_relations`: cause → effect (genotype / intervention /
  drug → phenotype or measurement), with mediators, polarity, evidence basis,
  `hypothetical` (false only for this paper's own intervention) and effect size.

`scripts/relations.py` resolves each target to an extracted mention of the same
paper (unresolvable ones are reported, never invented); the claims judge reviews
every claim; survivors become RO/BFO edges, `skos:broader`, and the causal module
in the Turtle. SHACL requires both ends to be extracted entities, no self-loops, and
acyclic hierarchies; the validator rejects predicates outside the config and warns
when a target is the wrong kind (`relation_range_hints`).

## Trusted ontologies (0.9)

The ontology files in `trusted_ontologes/` are consulted before any remote mapper.
`trusted_ontologes/priority.md` is the only place that says which files are used and in
what order (1 first; `off` disables a row); `trusted_ontologes/README.md` lists them.

```bash
python -m scripts.concept_mapping index      # once, ~2 min, no network: TSV lexicon per ontology + SQLite
python -m scripts.concept_mapping show       # effective order, routing, index state
python -m scripts.concept_mapping lookup "SST interneuron" --label CellType
python -m scripts.prefixes check             # must report 0 conflicts after adding an ontology
python -m scripts.concept_mapping readme     # regenerate the ontology list
```

Matching is exact on a normalised form (never fuzzy), routed by label (a filter, never
a reordering), guarded against bare abbreviations, and ambiguity is reported rather
than resolved. A term keeps the prefix of its namespace whichever file it came from.
Keys come from the ontologies too: without a kg_plan key, an entity's
`normalizedEntityKey` is the preferred label of the one class its text denotes. See
[references/ontology-mapping.md](references/ontology-mapping.md) and
[references/key-normalization.md](references/key-normalization.md).

---

## Running the reference pipeline end-to-end

`scripts/pipeline.py` wires together: (optional) HF NER ensemble + LLM extraction → ontology mapping (trusted ontologies → local → BioPortal) → KG plan → judge ensemble → Turtle + gate. Standalone, no framework:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...

python -m scripts.pipeline \
    --task ner \
    --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --ner-domain neuroscience \
    --ner-profile biomedical_broad \
    --chunk-size 2000 --max-workers 8
# writes paper.ttl (validated) and prints stats to stderr; --format json for the old
# paper_final.json, --keep-json to keep the working JSON under .structsense/.
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--task` | `ner` | `ner` / `resource` / `structured`. Picks the matching `extractor-*.md` prompt. |
| `--ner-domain` | `general` | NER prompt: `general` / `neuroscience` / `cns-cells` (`prompts/extractor-ner-<domain>.md`). |
| `--input` | (required) | Path to a `.txt` input. |
| `--extractor` | (required) | Model string for LLM extraction (e.g. `openrouter/anthropic/claude-sonnet-4-6`). |
| `--judge` | none = auto-approve | Default judge model. With `--judge-mode ensemble` (default) every panel member uses it unless `--judge-models` says otherwise. |
| `--judge-mode` | `ensemble` | `ensemble` (independent judges + deterministic combine) or `single` (legacy one-score judge). |
| `--judge-models` | none | Per-judge models, e.g. `mapping=openrouter/x,claims=openrouter/y`. |
| `--combiner` | `--judge` | Model for `prompts/judge-combiner.md` (disagreements only). |
| `--kg-plan-model` | `--judge` | Model that writes `kg_plan.json`; `none` to skip. |
| `--format` | `ttl` | `ttl`: one validated `<stem>.ttl` per input (a failing file is renamed `.invalid.ttl`); `json`: the legacy `<stem>_final.json`. |
| `--keep-json` | off | With `--format ttl`, keep the working JSON under `<out-dir>/.structsense/`. |
| `--out-dir` | beside each input | Where results go. |
| `--mapper` | `config` | `config`: the `concept_mapping.json` cascade (trusted ontologies → local hybrid → BioPortal → ask user). `local` / `bioportal` / `ols` / `none`: the older single-backend paths. |
| `--mapper-url` | `http://localhost:8000` | Local hybrid service URL. Verify it's up at `/docs`. |
| `--non-interactive` | off | Disable the user prompt when the mapper cascade fails. Fail fast instead. |
| `--ner-profile` | none | Enable the HF ensemble: `biomedical_broad` / `cns_cells` / `pharmacology` / `genetic` / `clinical` / `minimal` / `all`. Requires `pip install transformers torch`. |
| `--ner-models` | none | Comma-separated explicit list of HF model IDs (overrides `--ner-profile`). |
| `--ner-device` | `-1` (CPU) | CUDA device index for HF models. |
| `--chunk-size` | `2000` | Characters per chunk for parallel extraction. |
| `--max-workers` | `8` | Parallel workers. |
| `--out` | `<input_stem>.ttl` | Output path for a single input. |

### What you get in the output

The deliverable is `<input_stem>.ttl` ([Turtle output](#turtle-output-09)). The working
JSON the stages exchange (kept with `--keep-json`, or written with `--format json`) is
`<input_stem>_final.json`:

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

Declared in requirements files, split by who needs them:

```bash
pip install -r requirements.txt          # core: HTTP, JSON repair + validation, PDF, xlsx, rdflib + pyshacl (TTL)
pip install -r requirements-llm.txt      # provider SDKs — only when a framework calls an API
pip install -r requirements-ner.txt      # HuggingFace NER ensemble (heavy: torch)
pip install -r requirements-dev.txt      # pandas, for inspecting output while developing
```

`requirements.txt` alone runs every mode with the calling agent as the model,
including ABCD/HBCD. A missing PDF backend is the most common first-run failure
("all PDF extractors failed"), and it is in that file.

What each piece is for, if you prefer installing by hand:

```bash
# Always useful
pip install requests

# For json_repair.py Tier-3 fallback (highly recommended)
pip install json-repair

# For repair_to_schema()
pip install jsonschema

# Text extraction, stage 1 — and the only backend for DOCX/PPTX/XLSX/HTML/images
# or a SCANNED PDF (its pipeline OCRs). Also the best tables. Downloads models on
# first use; skip it with --no-docling.
pip install docling

# PDF-only fallbacks for when docling is absent or fails — input_loader then
# tries GROBID -> pymupdf4llm -> PyMuPDF -> pdfminer.six, whichever is present
pip install pymupdf pymupdf4llm pdfminer.six

# Excel: the NBDC variable catalog workbook, and .xlsx DOI lists
pip install openpyxl

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
9. **The deliverable is `<input_stem>.ttl`**, one per paper, validated (0 violations). The JSON between stages is working state. Honor `--out` only when the user provides one.
10. **Mapping cascade:** trusted ontologies (`trusted_ontologes/priority.md` order) → local hybrid (`http://localhost:8000`, verify at `/docs`) → BioPortal → **ask the user** for an alternative URL. All of it is `concept_mapping.json`; don't hardcode URLs or ontologies.
11. **Document metadata at top, not per entity.** `paper_title` / `doi` / `source_path` go ONCE in `source_metadata`. `paper_location` stays per-entity because it varies.
12. **Always emit both `entities[]` (raw) and `entities_grouped[]` (per-entity index).** The raw list is one-per-occurrence; the grouped list collapses by canonical (entity, label) with merged sentences + every contributing `source_model`.
13. **Judge with the ensemble.** Independent judges, one dimension each; critical judges (grounding, claims) are gates, not votes; the combiner only chooses among judges' suggestions.
14. **One prefix means one namespace**, everywhere (`scripts/prefixes.py`), and **identity is deterministic** (UUIDv5 from key / IRI / acronym).
15. **ABCD/HBCD mode: verification is the feature.** The paper is the only source of what a study used; a quote must be findable in that paper or the item is rejected with a reason; a string is only called a variable when a real dictionary release contains it; construct ids come from a Cognitive Atlas lookup, never from the model; and provenance records where in the paper each claim came from. See rule 16 in `SKILL.md`.

---

## Licence

Apache 2.0 (matches `structsense`'s licence).
