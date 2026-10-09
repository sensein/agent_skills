---
name: structsense
metadata:
  version: "0.12.0"
description: Extract named entities and source-stated relations from unstructured text, notes, messages, web pages and papers. Resolve repeated mentions and aliases to stable entities with source provenance, optional tool-backed ontology mapping, and validated Turtle plus entity-focused JSON. Also supports resource extraction, target JSON schemas, ABCD/HBCD study extraction, and mapping a paper's cell types to Allen Institute (AIT) cell type taxonomies with code-verified evidence and SKOS relations.
license: Apache-2.0
---

# StructSense — structured information extraction

Use this skill to extract entities, research resources, schema-defined records,
ABCD/HBCD study results, or cell-type mappings from source documents. Follow the
relevant prompt and reference for the selected mode; do not load every guide,
prompt, and schema for a task.

The detailed workflow retained from the previous entry point is in
[`EXTENDED-GUIDE.md`](EXTENDED-GUIDE.md). Prefer the
mode-specific references below when possible so that only relevant guidance
enters context.

## Choose a workflow

| Request | Read first | Prompt or output contract |
|---|---|---|
| Named entities and key terms | [`references/ner-extraction.md`](references/ner-extraction.md) | Choose `prompts/extractor-ner-{general,neuroscience,cns-cells}.md` |
| Research resources and their applicability | [`references/resource-extraction.md`](references/resource-extraction.md) | `prompts/extractor-resource.md`; `schemas/bkr-resource-extraction.schema.json` |
| User-supplied JSON schema | [`references/structured-extraction.md`](references/structured-extraction.md) | `prompts/extractor-structured.md` |
| ABCD/HBCD variables, constructs, models, findings, or synthesis | [`references/abcd-extraction.md`](references/abcd-extraction.md) | `prompts/extractor-abcd.md` |
| Cell types mapped to Allen Institute taxonomies | `prompts/extractor-cell-type-ait-mapping.md` | `schemas/ait-mapping-columns.json` |

For cell annotation scored against human gold data, also read
[`references/cell-annotation-conventions.md`](references/cell-annotation-conventions.md).
For long inputs, use [`references/chunking-strategy.md`](references/chunking-strategy.md).
For mapping, review, or output, use
[`references/ontology-mapping.md`](references/ontology-mapping.md),
[`references/review-loop.md`](references/review-loop.md), and
[`references/ttl-representation.md`](references/ttl-representation.md) as
applicable.

## Operating workflow

1. Identify the extraction mode and read only its relevant reference and prompt.
2. Extract source-grounded records using the mode's required schema. For NER,
   preserve distinct occurrences and their evidence; use the mask-recall pass
   when exhaustive recall is needed.
3. Map concepts with the configured tools. For NER, use
   `python -m scripts.concept_mapping map <result.json>`.
4. Review with the mode's judge workflow and apply corrections through
   `scripts/review_loop.py` or `scripts/human_feedback.py`, not untracked edits.
5. Write the mode's deliverables. For NER and resource extraction, generate and
   validate Turtle:

   ```bash
   python -m scripts.json_to_ttl <result.json> --source <source-file>
   python -m scripts.validate_ttl <output.ttl>
   ```

   Hand back a Turtle file only when validation succeeds. ABCD/HBCD and AIT have
   their own output and validation contracts; follow their references.

For multiple papers, use `scripts/batch.py` for NER or the bulk workflow in
`references/abcd-extraction.md` for ABCD/HBCD. Keep each paper's evidence and
results distinct; use the mode's synthesis step for cross-paper conclusions.

## Model and tool use

When an agent is reading this skill, it is normally the extractor and judge
itself (host-model mode); do not ask for an LLM API key or pass an API model
unless the user explicitly requests a separate framework/model run. The
tool-backed mapping services may have their own credentials; ask only for the
specific service key that is actually missing.

Concept mapping is tool-only: trusted ontologies first, then configured
backends. Never invent an ontology IRI or claim a mapping succeeded without
tool evidence. Record mapping provenance, and tell the user when a required
mapping backend is unavailable rather than silently skipping mapping.

## Non-negotiable quality rules

- Keep extracted facts grounded in the supplied source. Preserve verbatim
  evidence and offsets where the schema requires them; do not infer missing
  facts from domain conventions or cited work.
- Keep entity identity separate from mentions: repeated mentions retain their
  own source evidence, while only resolved identities share a canonical key.
- Produce strict JSON matching the selected schema before deterministic
  processing. Validate intermediate and final outputs with the supplied tools.
- Use the configured mapping cascade and judge gates. A low-quality mapping
  may be demoted; an unsupported mapping must not be retained to make output
  appear complete.
- Do not skip failed or pending work silently. Report validation failures,
  rejected records, and unavailable tools with their reason.
- For long documents, follow the chunking guide and process bounded chunks;
  do not assume the whole source fits in model context.

## Supporting guides

- [`references/ner-models.md`](references/ner-models.md) — optional biomedical
  Hugging Face ensemble.
- [`references/judge-ensemble.md`](references/judge-ensemble.md) — judge
  dimensions, gates, and combination.
- [`references/key-normalization.md`](references/key-normalization.md) — stable
  entity keys across documents.
- [`references/json-output-discipline.md`](references/json-output-discipline.md) —
  strict JSON generation and repair.
- [`references/model-selection.md`](references/model-selection.md) — model
  choices for framework/API runs.
- `cqs/` — competency questions for exported graphs.
- `connecting/` — platform-specific setup; [`connecting/mcp-server.md`](connecting/mcp-server.md)
  documents the MCP bridge.

Core mental model: extract only what the source supports, map with tools, review
with explicit operations, and validate the requested deliverable before returning it.
