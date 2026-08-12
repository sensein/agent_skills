# Worked example — PDF questionnaire → ReproSchema

Converts a paper-based questionnaire PDF into structured JSON matching a simplified ReproSchema-style activity + items schema. Uses the **structured extraction** variant.

## The target schema (locked up front)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["activity", "items"],
  "properties": {
    "activity": {
      "type": "object",
      "required": ["id", "prefLabel", "description"],
      "properties": {
        "id":          { "type": "string", "pattern": "^[A-Z][A-Za-z0-9_-]*$" },
        "prefLabel":   { "type": "string" },
        "description": { "type": ["string", "null"] },
        "preamble":    { "type": ["string", "null"] },
        "citation":    { "type": ["string", "null"] }
      }
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "question", "inputType", "valueRequired"],
        "properties": {
          "id":            { "type": "string", "pattern": "^Q[0-9]+$" },
          "question":      { "type": "string" },
          "prefLabel":     { "type": ["string", "null"] },
          "inputType":     { "type": "string",
                             "enum": ["radio", "text", "number", "slider"] },
          "valueRequired": { "type": "boolean" },
          "responseOptions": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["value", "name"],
              "properties": {
                "value": { "type": "integer" },
                "name":  { "type": "string" }
              }
            }
          },
          "scoring": {
            "type": ["object", "null"],
            "properties": {
              "type":    { "type": "string", "enum": ["sum", "mean", "custom"] },
              "weights": { "type": "array", "items": { "type": "number" } }
            }
          }
        }
      }
    }
  }
}
```

Save it as `schemas/reproschema-questionnaire.schema.json`.

## Source (excerpt from a 12-page Mood and Feelings Questionnaire PDF)

```
MOOD AND FEELINGS QUESTIONNAIRE: Long Version

Below is a list of feelings or behaviors that may apply to you.
For each one, please mark how often it was true for you in the last two
weeks: 0 = not true, 1 = sometimes true, 2 = true.

  1. I felt miserable or unhappy.        [0] [1] [2]
  2. I didn't enjoy anything at all.     [0] [1] [2]
  3. I felt so tired I just sat around.  [0] [1] [2]
  ...
```

## Stage 1 — extractor (schema-driven)

System prompt: `prompts/extractor-structured.md`, with the schema inline.

```python
import json
from pathlib import Path
from scripts.llm_client import call
from scripts.json_repair import parse_or_repair

schema_text = Path("schemas/reproschema-questionnaire.schema.json").read_text()
system = Path("prompts/extractor-structured.md").read_text()  # the file's ## System block
# (or use scripts.pipeline._load_prompt("extractor-structured"))
system = system.format(json_schema=schema_text, field_hints="""
- activity.id: short identifier built from title initials, ignoring stopwords
              ('AND', 'THE'). e.g. 'MOOD AND FEELINGS QUESTIONNAIRE' -> 'MFQ-long'.
- items[].inputType: one of [radio | text | number | slider].
- items[].responseOptions[].value: integer score, lowest first.
- preamble: the instruction paragraph before the first question.
""")

raw = call(
    model="openrouter/anthropic/claude-sonnet-4-6",
    system=system,
    user=f"SOURCE DOCUMENT:\n<<<\n{pdf_text}\n>>>\n\nADDITIONAL CONTEXT:\n{{}}",
    json_mode=True,
    temperature=0,
)
data = parse_or_repair(raw)
```

Expected output:

```jsonc
{
  "activity": {
    "id": "MFQ-long",
    "prefLabel": "Mood and Feelings Questionnaire — Long Version",
    "description": null,
    "preamble": "Below is a list of feelings or behaviors that may apply to you. For each one, please mark how often it was true for you in the last two weeks: 0 = not true, 1 = sometimes true, 2 = true.",
    "citation": null
  },
  "items": [
    {
      "id": "Q1",
      "question": "I felt miserable or unhappy.",
      "prefLabel": "miserable_unhappy",
      "inputType": "radio",
      "valueRequired": true,
      "responseOptions": [
        { "value": 0, "name": "not true" },
        { "value": 1, "name": "sometimes true" },
        { "value": 2, "name": "true" }
      ],
      "scoring": null
    },
    {
      "id": "Q2",
      "question": "I didn't enjoy anything at all.",
      "prefLabel": "no_enjoyment",
      "inputType": "radio",
      "valueRequired": true,
      "responseOptions": [
        { "value": 0, "name": "not true" },
        { "value": 1, "name": "sometimes true" },
        { "value": 2, "name": "true" }
      ],
      "scoring": null
    }
    // ... 31 more items
  ]
}
```

## Stage 2 — validate + repair

```python
import jsonschema
from scripts.json_repair import repair_to_schema

schema = json.loads(schema_text)
fixed, errors = repair_to_schema(
    data, schema,
    llm_call=lambda prompt: call(
        model="openrouter/openai/gpt-4o-mini",
        system="You output strict JSON only.",
        user=prompt, json_mode=True, temperature=0,
    ),
    max_attempts=2,
)
if errors:
    print("Repair history:", errors)
jsonschema.validate(fixed, schema)
```

Typical repair errors caught here:

- `items[2].responseOptions[1].value: expected integer, got string "1"` → `value: 1` (int).
- `items[5].inputType: 'Radio' is not one of ['radio','text','number','slider']` → `inputType: "radio"`.
- `activity.id: 'MFQ_long' does not match '^[A-Z][A-Za-z0-9_-]*$'` (underscore not in pattern? this one **does** match — included as a reminder to always re-test).

## Stage 3 — judge (faithfulness + schema validity)

Two judgments:

1. `schema_valid`: did the final JSON validate? (deterministic)
2. `field_judgments`: per-field LLM faithfulness check.

```jsonc
{
  "data": { "...": "..." },
  "schema_valid": true,
  "field_judgments": {
    "activity.prefLabel":  {"score": 1.0,  "remarks": "Title taken verbatim from source."},
    "activity.preamble":   {"score": 0.85, "remarks": "Slightly compressed whitespace; semantics preserved."},
    "items":               {"score": 1.0,  "remarks": "Counted 33 items matching the questionnaire structure."},
    "items[*].responseOptions": {"score": 1.0, "remarks": "All items share the 0/1/2 scale shown in the preamble."}
  }
}
```

If any `score < 0.6`, surface that field in the human-feedback stage for review.

## Running it end-to-end

```python
# Pseudocode — wire your own driver around scripts/pipeline.py and the repair helpers
from scripts import pipeline
result = pipeline.run(
    text=pdf_text,
    task="structured",                # uses extractor-structured.md
    extractor_model="openrouter/anthropic/claude-sonnet-4-6",
    mapper_backend=None,              # no ontology mapping for this schema
    judge_model="openrouter/openai/gpt-4o-mini",
    chunk_size=8000,                  # questionnaires fit in larger chunks
    max_workers=4,
    skip_judge=False,
)
```

## Common gotchas

- **Tables in PDFs.** A questionnaire's repeated response columns get rendered weirdly by PDF text extractors. Preprocess: collapse "[0] [1] [2]" patterns into the structured shape before running the extractor, or include an example of the malformed PDF table in the field hints.
- **Numbering schemes.** Some questionnaires number items "Q1, Q2…", others "1., 2.…", others by domain ("D1.1, D1.2…"). Pin the format in `field_hints`.
- **Subscales.** A questionnaire often has computed subscales (e.g. items 1, 4, 7 → "anhedonia"). The schema above doesn't model that; add a `computedScores` field if you need it.
- **Conditional logic.** Some items only appear if a previous answer matched a condition. Add `conditionalLogic` to the schema and have the extractor capture the rule as a parseable expression.
