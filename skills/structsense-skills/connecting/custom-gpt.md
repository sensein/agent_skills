# Connecting structsense-skills to ChatGPT (Custom GPT)

OpenAI doesn't yet have a first-class "skills" concept the way Claude does, but a **Custom GPT** with the right Instructions + Knowledge files + (optionally) an Action gives you a comparable connection. This guide walks through it.

## What you'll build

A Custom GPT that:
- Knows the structsense-skills pattern (extract → align → judge → group) from its Instructions.
- Has the relevant prompt files + JSON Schemas as Knowledge files.
- (Optional) Calls a hosted `pipeline.py` HTTP service via an Action when the user wants full end-to-end runs.

## 1. Create the GPT

1. In ChatGPT, click "Create a GPT".
2. Name it "**structsense-skills**" (or similar).
3. Set the description to the same text as the `description:` field in [SKILL.md](../SKILL.md). This is what users will see in the GPT picker.

## 2. Instructions

Paste the contents of [SKILL.md](../SKILL.md) into the **Instructions** field. Then append:

```
When the user asks for NER on biomedical text, use the
`extractor-ner-neuroscience.md` prompt verbatim as the system message,
followed by the user's input under a `INPUT TEXT:\n<<<\n…\n>>>` block.

For CNS cell-typing text, use `extractor-ner-cns-cells.md`.
For general-domain text, use `extractor-ner-general.md`.

After pass-1, run mask-recall (prompts/mask-recall-pass.md) for biomedical
papers — typical recovery is +30-80% mentions.

Always emit the canonical shape:
- source_metadata at the TOP LEVEL (paper_title, doi, source_path).
- entities[] one item per occurrence (exhaustive).
- entities_grouped[] per-entity index.
- stats embedded.
- Each entity has source_model = "llm_ner:gpt-4.1" (or whichever model).
- DO NOT put paper_title or doi on every entity.

If asked to call the pipeline end-to-end (with HF ensemble, ontology mapping,
judge stage), use the `run_pipeline` Action.
```

This nudges GPT to follow the same conventions the SKILL.md describes.

## 3. Knowledge files

Upload the following from the `structsense-skills/` folder:

| File | Why upload |
|---|---|
| `SKILL.md` | Discovery + rules. |
| `prompts/extractor-ner-general.md` | General NER prompt. |
| `prompts/extractor-ner-neuroscience.md` | Neuroscience NER prompt. |
| `prompts/extractor-ner-cns-cells.md` | CNS-cell NER prompt. |
| `prompts/mask-recall-pass.md` | Pass-2 recall booster. |
| `prompts/mask-verify-pass.md` | Per-item label sanity. |
| `prompts/extractor-resource.md` | Resource extraction. |
| `prompts/extractor-structured.md` | Schema-driven extraction. |
| `prompts/alignment.md` | Ontology alignment. |
| `prompts/judge.md` | Quality scoring. |
| `schemas/ner-output.schema.json` | Output validation. |
| `schemas/resource-output.schema.json` | Resource output validation. |
| `references/ner-extraction.md` | NER methodology. |
| `references/ner-models.md` | HF ensemble docs. |
| `references/ontology-mapping.md` | Mapper backends + cascade. |
| `references/json-output-discipline.md` | Strict-JSON + repair patterns. |

GPT will retrieve from these when relevant. Don't upload all of `scripts/` — Custom GPTs don't execute Python natively.

## 4. Enable JSON-schema-mode output

In **Capabilities**, ensure "Code Interpreter" is enabled if you want GPT to run Python on the user's behalf. For best schema compliance:

- Tell GPT in Instructions: "When emitting JSON, use the `response_format: json_object` capability."
- For full structured outputs (the strongest guarantee), use the OpenAI API directly with `response_format={"type": "json_schema", "json_schema": {...}}` and `schemas/ner-output.schema.json` — Custom GPTs in the chat UI don't expose this option, but the model still tends to comply when shown the schema in Instructions.

## 5. Optional: Action that calls the pipeline end-to-end

If you have a server running `scripts/pipeline.py` as an HTTP service (FastAPI wrapper — see `connecting/mcp-server.md` for one option), add a Custom GPT **Action** so the GPT can invoke the full pipeline (HF ensemble + ontology mapping + judge + grouped + stats) without doing it inline.

### Minimal OpenAPI spec for the Action

```yaml
openapi: 3.0.0
info:
  title: structsense-skills pipeline
  version: 0.3.0
servers:
  - url: https://your-server.example.com   # replace
paths:
  /extract:
    post:
      operationId: run_pipeline
      summary: Run the full structsense-skills pipeline on a text input
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [text, task, extractor_model]
              properties:
                text:            { type: string, description: Source text. }
                task:            { type: string, enum: [ner, resource, structured] }
                extractor_model: { type: string, description: Model string e.g. openrouter/anthropic/claude-sonnet-4-6 }
                judge_model:     { type: string, nullable: true }
                mapper:          { type: string, enum: [local, bioportal, ols, none], default: local }
                mapper_url:      { type: string, default: http://localhost:8000 }
                ner_profile:     { type: string, nullable: true, enum: [biomedical_broad, cns_cells, pharmacology, genetic, clinical, minimal, all] }
                chunk_size:      { type: integer, default: 2000 }
                max_workers:     { type: integer, default: 8 }
                paper_title:     { type: string, nullable: true }
                doi:             { type: string, nullable: true }
      responses:
        '200':
          description: Pipeline result in the canonical structsense-skills shape.
          content:
            application/json:
              schema:
                type: object
                properties:
                  source_metadata:   { type: object }
                  entities:          { type: array }
                  entities_grouped:  { type: array }
                  key_terms:         { type: array }
                  key_terms_grouped: { type: array }
                  stats:             { type: object }
                  ensemble_models:   { type: array }
```

Save this as `connecting/openapi-pipeline.yaml`, paste it into the Custom GPT's Action editor, and set the auth (Bearer token recommended).

Behind the URL you need a small FastAPI service that wraps `pipeline.run()`. See `connecting/mcp-server.md` for a working example you can copy.

## 6. API keys

Custom GPTs run inside ChatGPT's infrastructure — they don't have access to your shell env. Two ways to provide keys:

- **In the Action's auth config**: use Bearer token; the server reads `OPENROUTER_API_KEY` etc. from its own env.
- **For inline use (no Action)**: the GPT can ask the user to paste a key, but you should NOT do this — keys end up in the chat log.

The right setup is a server-side Action whose env has the keys.

## 7. Test

In the GPT, try:

> "Extract entities from this paper: [paste 2-3 pages of biomedical text]. Title: 'Foo bar atlas'. DOI: 10.1234/foo."

Expected output:
- Top-level `source_metadata: { paper_title: "Foo bar atlas", doi: "10.1234/foo" }`.
- `entities[]` — many items, each with `source_model: "llm_ner:gpt-4.1"` (or your default model), `start`/`end`/`sentence`/`paper_location`.
- No `paper_title` or `doi` on individual entities.
- `entities_grouped[]` — entities consolidated by `(surface, label)`.
- `stats` block at the bottom.

If the GPT regresses to the legacy shape on long inputs (per-entity `paper_title`/`doi`), re-emphasize the WRONG/RIGHT block in the Instructions, or run the output through `scripts/normalize_result.py` server-side.

## 8. What this gives you vs the Claude Code path

| | Claude Code | Custom GPT |
|---|---|---|
| Auto-loads SKILL.md | ✅ | Via Instructions paste |
| Progressive file loading | ✅ native | Knowledge retrieval (similar) |
| Calls `scripts/pipeline.py` | ✅ via Bash | ✅ via Action (server) |
| Strong JSON-schema mode | ✅ Anthropic structured outputs | ✅ via API; weaker in chat UI |
| Local file IO (writes `paper_final.json`) | ✅ | Only via server Action |

For research/notebook workflows, Claude Code is the simpler path. For team-facing tools where users come through ChatGPT, the Custom GPT + Action setup is the right answer.
