"""Multi-tier JSON repair for LLM output.

Tier 1: strict json.loads
Tier 2: strip markdown fences, then strict json.loads
Tier 3: json-repair library (handles trailing commas, single quotes,
        Python None/True/False, truncated trailing braces)
Tier 4: truncate to last balanced brace/bracket

Combined with schema-aware repair via repair_to_schema(), which sends an
LLM the validator error and asks for a fixed JSON.

Adapted from the structsense json_repair_tool.py.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Callable

try:
    from json_repair import repair_json as _jr_repair
except ImportError:  # pragma: no cover
    _jr_repair = None

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def strip_fences(s: str) -> str:
    """Remove a surrounding ```json ... ``` fence if present."""
    s = s.strip()
    m = _FENCE_RE.match(s)
    return m.group(1).strip() if m else s


def truncate_to_last_balanced(s: str) -> str:
    """Walk the string, tracking brace/bracket depth (ignoring strings).
    Return the substring up to and including the last position where depth
    returned to 0. Useful for token-limit truncations.
    """
    depth = 0
    last_balanced = -1
    in_string = False
    escape = False
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                last_balanced = i
    return s[: last_balanced + 1] if last_balanced > 0 else s


def parse_or_repair(raw: str) -> Optional[Any]:
    """Run the four-tier repair pipeline. Returns parsed object or None."""
    if not raw or not raw.strip():
        return None

    # Tier 1: strict
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Tier 2: strip fences
    stripped = strip_fences(raw)
    if stripped != raw:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Tier 3: json-repair library
    if _jr_repair is not None:
        try:
            return _jr_repair(stripped, return_objects=True)
        except Exception:  # pragma: no cover
            pass

    # Tier 4: truncate to last balanced and try again
    truncated = truncate_to_last_balanced(stripped)
    if truncated and truncated != stripped:
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            if _jr_repair is not None:
                try:
                    return _jr_repair(truncated, return_objects=True)
                except Exception:
                    pass

    return None


def repair_to_schema(
    parsed: Any,
    schema: dict,
    llm_call: Callable[[str], str],
    max_attempts: int = 2,
):
    """Validate `parsed` against `schema`. If invalid, ask the LLM to repair
    using the validator's error path. Returns (parsed, errors).

    `llm_call(prompt: str) -> str` should call your model and return raw text
    (we'll re-run parse_or_repair on the result).
    """
    try:
        import jsonschema
    except ImportError as e:
        raise RuntimeError(
            "repair_to_schema requires the jsonschema package: pip install jsonschema"
        ) from e

    validator = jsonschema.Draft202012Validator(schema)
    errors_history: list[str] = []
    current = parsed

    for _ in range(max_attempts + 1):
        errs = sorted(validator.iter_errors(current), key=lambda e: list(e.absolute_path))
        if not errs:
            return current, []
        first = errs[0]
        path = ".".join(str(p) for p in first.absolute_path)
        msg = f"{first.message} at path: {path or '<root>'}"
        errors_history.append(msg)

        prompt = (
            "The JSON below is INVALID. Fix it to match the JSON Schema. "
            "Output ONLY the fixed JSON — no prose, no markdown fences.\n\n"
            f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
            f"ERROR:\n{msg}\n\n"
            f"INVALID JSON:\n{json.dumps(current, indent=2)}"
        )
        repaired_raw = llm_call(prompt)
        repaired = parse_or_repair(repaired_raw)
        if repaired is None:
            return current, errors_history + ["repair attempt returned unparseable text"]
        current = repaired

    return current, errors_history


if __name__ == "__main__":
    bad = """```json
    {
        "entities": [
            {'entity': 'BDNF', 'label': 'Gene',},
            {"entity": "hippocampus", "label": "BrainRegion",}
        ],
        "key_terms": [None, True, False]
    """
    obj = parse_or_repair(bad)
    print(json.dumps(obj, indent=2))
