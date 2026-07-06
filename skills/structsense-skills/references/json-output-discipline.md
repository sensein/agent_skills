# JSON output discipline

LLMs produce unreliable JSON unless you constrain them. This document is the playbook for getting clean, parseable, schema-conformant JSON every time.

## Tier 1 — preventive prompting

The cheapest fix is a strict system prompt. Every extractor / alignment / judge prompt must include:

```
You output STRICT JSON ONLY.
- No prose before or after.
- No markdown code fences.
- No comments inside the JSON.
- All strings use double quotes.
- All keys are strings; no trailing commas.
- If a field is unknown, set it to null. Never invent values.
If you cannot comply, output {"error": "<one-line reason>"} and nothing else.
```

Set `temperature: 0` (or `0.1` for tiebreakers). Set `top_p: 1`.

If your model API supports it, use **JSON mode**:

| Provider | How to enable |
|---|---|
| OpenAI / OpenRouter (GPT-4o, GPT-4.1) | `response_format={"type": "json_object"}` |
| OpenAI (structured outputs) | `response_format={"type": "json_schema", "json_schema": {...}}` — guarantees schema-valid output |
| Anthropic Claude | Add `"Respond with only valid JSON."` to system prompt; consider tool use with a JSON-typed tool input |
| Gemini | `response_mime_type="application/json"` |
| Ollama (most models) | Add `"format": "json"` to the request body |

Structured outputs (OpenAI's JSON-schema mode, or constrained decoding for local models via Outlines/Guidance) make the model **physically incapable** of producing invalid JSON. Use them when available.

## Tier 2 — parse + repair

After receiving the response:

```python
import json
from json_repair import repair_json

def parse_or_repair(raw: str) -> dict | None:
    # Try strict parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences if present
    stripped = strip_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # json-repair handles trailing commas, single quotes, Python None/True/False, truncated trailing braces
    try:
        return repair_json(stripped, return_objects=True)
    except Exception:
        return None
```

```python
import re
FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)
def strip_fences(s: str) -> str:
    m = FENCE.match(s.strip())
    return m.group(1) if m else s
```

Install: `pip install json-repair`.

See `scripts/json_repair.py` for the full helper.

## Tier 3 — schema-driven re-prompt

If Tier 2 still fails (or the result is parseable but schema-invalid):

```python
import jsonschema

try:
    jsonschema.validate(parsed, schema)
except jsonschema.ValidationError as e:
    repair_prompt = f"""
The JSON below is INVALID. Fix it to match this schema. Output ONLY the fixed JSON, no prose.

SCHEMA:
{json.dumps(schema, indent=2)}

ERROR:
{e.message} at path: {'.'.join(str(p) for p in e.absolute_path)}

JSON TO FIX:
{json.dumps(parsed, indent=2)}
"""
    parsed = call_llm(repair_prompt)
    jsonschema.validate(parsed, schema)
```

Cap retries at 2. If still failing, return whatever's parseable plus `"errors": [...]`.

## Tier 4 — last-resort partial recovery

When the model truncated mid-output (token-limit exceeded) and even json-repair can't recover:

1. Find the last balanced `}` or `]`.
2. Truncate the string there and re-parse.
3. If a list is at the top level, you'll lose the last (probably incomplete) item. That's fine — log a warning, don't fail.

```python
def truncate_to_last_balanced(s: str) -> str:
    depth, last_balanced = 0, -1
    in_string = False
    escape = False
    for i, ch in enumerate(s):
        if escape: escape = False; continue
        if ch == "\\": escape = True; continue
        if ch == '"': in_string = not in_string; continue
        if in_string: continue
        if ch in "{[": depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                last_balanced = i
    return s[: last_balanced + 1] if last_balanced > 0 else s
```

## Common malformations and what works

| Malformation | Tier that fixes it |
|---|---|
| Wrapped in markdown fences | Tier 2 (strip_fences) |
| Trailing comma after last list item | Tier 2 (json-repair) |
| Python `True` / `False` / `None` | Tier 2 (json-repair) |
| Single quotes around keys/values | Tier 2 (json-repair) |
| Truncated by token limit | Tier 4 (last balanced) |
| Hallucinated extra fields | Tier 3 (schema re-prompt) |
| Required field missing | Tier 3 (schema re-prompt) |
| Wrong enum value | Tier 3 (schema re-prompt) |
| Float where int expected (or vice versa) | Tier 3 (schema re-prompt) |

## Validation after parsing — beyond the schema

JSON Schema can't check everything. After parsing, run domain-specific validators:

```python
def validate_ner(parsed: dict, original_text: str) -> list[str]:
    errors = []
    for i, e in enumerate(parsed.get("entities", [])):
        # Span integrity
        if original_text[e["start"]:e["end"]] != e["entity"]:
            errors.append(f"entities[{i}]: span mismatch")
        # Sentence containment
        if e["entity"] not in e["sentence"]:
            errors.append(f"entities[{i}]: entity not in sentence")
        if e["sentence"] not in original_text:
            errors.append(f"entities[{i}]: sentence not in source")
    return errors
```

Drop items that fail; never silently keep broken spans.

## Test your prompts with a torture set

Maintain a small "torture test" of inputs known to break JSON:

- Quotes inside string values
- Backslashes (Windows paths, LaTeX)
- Unicode emoji and right-to-left text
- Very long single-line outputs
- Inputs containing the exact tokens `{`, `}`, `:`, `,`, `"` in problematic positions

Run your prompt against the torture set whenever you change it. A small CI script saves hours of production debugging.
