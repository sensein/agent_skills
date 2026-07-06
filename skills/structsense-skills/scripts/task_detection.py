"""LLM-based task-type detection from a task description.

Given a free-text extractor description (e.g. the `description:` field of
a CrewAI/structsense task config, or the user's natural-language ask), this
returns a canonical task type the pipeline can route on:

    ner | resource | structured_extraction | keyphrase_extraction |
    relation_extraction | event_extraction | classification | …

Adapted from structsense `task_detection.py` but uses the skill's
provider-agnostic ``llm_client.call`` instead of importing the OpenAI SDK
directly, so it works with Claude / OpenRouter / Ollama / Gemini.

When the user is in Claude Code (or another runtime where Claude is the
caller), the LLM can simply *read* the taxonomy below and pick the right
type from the conversation — no extra round-trip needed. This module is
useful when the pipeline driver needs to *programmatically* pick the type
(e.g. inside `pipeline.run()` when invoked from a Python script).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("task_detection")


# ---------------------------------------------------------------------------
# Canonical taxonomy — keep aligned with structsense for cross-tool consistency.
# ---------------------------------------------------------------------------

DEFAULT_TAXONOMY: Dict[str, str] = {
    "ner": (
        "Named Entity Recognition: detect and extract spans that refer to "
        "real-world or domain-specific entities. Common types: PERSON, ORG, "
        "LOCATION, DATE, PRODUCT, EVENT, DISEASE, DRUG, GENE, PROTEIN, "
        "CELL_TYPE, BRAIN_REGION, NEUROTRANSMITTER, ASSAY, DATASET, etc. "
        "Output is entity spans with type labels."
    ),
    "keyphrase_extraction": (
        "Extract a concise set of important phrases that capture the main "
        "topics, mechanisms, or claims. Broader than NER. Output is a "
        "ranked list of key terms."
    ),
    "resource": (
        "Extract one primary research resource (Model, Dataset, Tool, "
        "Benchmark, Leaderboard, Paper) plus its mentions of secondary "
        "resources. Output is a resource object with name, description, "
        "type, category, target, url, mentions."
    ),
    "structured_extraction": (
        "Extract information into a predefined JSON schema with strict "
        "fields and types. Use when the user supplies a target schema "
        "(e.g. ReproSchema, Croissant, schema.org Dataset, a custom schema)."
    ),
    "relation_extraction": (
        "Extract typed relationships between entities (often from NER "
        "output). Output is (subject, relation, object) triples."
    ),
    "event_extraction": (
        "Extract events and their arguments/roles (who did what to whom, "
        "when, where, how)."
    ),
    "classification": (
        "Assign exactly one category/label to an input (single-label)."
    ),
    "multi_label_classification": (
        "Assign multiple applicable labels/tags to the same input "
        "(not mutually exclusive)."
    ),
    "summarization": (
        "Produce a shorter version that preserves key information."
    ),
    "question_answering": (
        "Answer a question directly (extractive or abstractive)."
    ),
    "other": (
        "None of the above; fallback when the user's request doesn't "
        "fit a canonical type."
    ),
}


# Map canonical task types → which extractor prompt to load.
TASK_TYPE_TO_PROMPT: Dict[str, str] = {
    "ner":                       "prompts/extractor-ner-neuroscience.md",  # default; override per-domain
    "keyphrase_extraction":      "prompts/extractor-ner-neuroscience.md",
    "resource":                  "prompts/extractor-resource.md",
    "structured_extraction":     "prompts/extractor-structured.md",
    "relation_extraction":       "prompts/extractor-structured.md",
    "event_extraction":          "prompts/extractor-structured.md",
}


@dataclass
class TaskDetection:
    task_type: str             # canonical type
    confidence: float          # 0.0 – 1.0
    labels: List[str] = field(default_factory=list)  # optional sublabels
    rationale: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Heuristic detection (no LLM call) — fast first-pass
# ---------------------------------------------------------------------------

_NER_PATTERNS = re.compile(
    r"\b(named[- ]entity|\bNER\b|entities? and key[- ]terms?|"
    r"\bextract entities|extract spans?|extract mentions?)",
    re.IGNORECASE,
)
_KEYPHRASE_PATTERNS = re.compile(
    r"\b(key[- ]?phrases?|key[- ]?terms?|main topics|important phrases)\b",
    re.IGNORECASE,
)
_RESOURCE_PATTERNS = re.compile(
    r"\b(resources?\b|"
    r"(tools?|datasets?|models?|benchmarks?)([,\s]+(and\s+)?(tools?|datasets?|models?|benchmarks?))+|"
    r"model card|catalog of (tools|datasets|models|benchmarks))",
    re.IGNORECASE,
)
_STRUCTURED_PATTERNS = re.compile(
    r"\b(JSON[- ]?[Ss]chema|structured (extraction|output)|reproschema|"
    r"croissant|target schema)",
    re.IGNORECASE,
)
_RELATION_PATTERNS = re.compile(
    r"\b(relation(ship)?s?|triples?|\(subject,? *relation,? *object\))",
    re.IGNORECASE,
)


def detect_heuristic(text: str) -> Optional[TaskDetection]:
    """Quickly pick a task type from common phrasing patterns, no LLM call.

    Returns None when no pattern matches confidently — call `detect_with_llm`
    next in that case.
    """
    if _STRUCTURED_PATTERNS.search(text):
        return TaskDetection("structured_extraction", 0.85,
                             ["heuristic"], "matched schema/structured pattern")
    if _RESOURCE_PATTERNS.search(text):
        return TaskDetection("resource", 0.8,
                             ["heuristic"], "matched resource-extraction pattern")
    if _RELATION_PATTERNS.search(text):
        return TaskDetection("relation_extraction", 0.75,
                             ["heuristic"], "matched relation-extraction pattern")
    if _NER_PATTERNS.search(text):
        return TaskDetection("ner", 0.85,
                             ["heuristic"], "matched NER pattern")
    if _KEYPHRASE_PATTERNS.search(text):
        return TaskDetection("keyphrase_extraction", 0.7,
                             ["heuristic"], "matched keyphrase pattern")
    return None


# ---------------------------------------------------------------------------
# LLM-based detection (provider-agnostic via llm_client)
# ---------------------------------------------------------------------------

_DETECT_PROMPT_SYSTEM = (
    "You classify a task description into ONE canonical task type from a "
    "fixed taxonomy. Output strict JSON, no prose, no markdown. Schema:\n"
    '{ "task_type": "<one of the listed types>",\n'
    '  "confidence": <float 0-1>,\n'
    '  "labels": ["<optional sublabels, e.g. multi_label, few_shot>"],\n'
    '  "rationale": "<one short sentence>" }\n\n'
    "If nothing fits, return task_type=\"other\" with confidence ≤ 0.3."
)


def _user_prompt(task_description: str, taxonomy: Dict[str, str]) -> str:
    lines = ["TAXONOMY (pick ONE task_type from these keys):"]
    for k, v in taxonomy.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("TASK DESCRIPTION TO CLASSIFY:")
    lines.append(task_description)
    return "\n".join(lines)


def detect_with_llm(
    task_description: str,
    *,
    model: str,
    taxonomy: Optional[Dict[str, str]] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> TaskDetection:
    """Run an LLM round-trip to classify the task type. Falls back to 'other'
    on parse error. Uses the provider-agnostic `llm_client.call`.
    """
    try:
        from llm_client import call as llm_call   # local import for cycle safety
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call as llm_call   # type: ignore

    tax = taxonomy or DEFAULT_TAXONOMY
    user = _user_prompt(task_description, tax)

    raw = llm_call(
        model=model,
        system=_DETECT_PROMPT_SYSTEM,
        user=user,
        temperature=0,
        json_mode=True,
        api_key=api_key,
        base_url=base_url,
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # try the repair helper
        try:
            from json_repair import parse_or_repair
        except ImportError:
            parse_or_repair = None
        data = parse_or_repair(raw) if parse_or_repair else None
        if data is None:
            logger.warning("task_detection: LLM output not parseable: %r", raw[:200])
            return TaskDetection("other", 0.0, ["parse_error"],
                                 "LLM output not parseable", {"llm_text": raw})

    task_type = str(data.get("task_type", "other")).strip().lower()
    if task_type not in tax:
        logger.warning("task_detection: out-of-taxonomy %r → other", task_type)
        return TaskDetection("other", min(float(data.get("confidence", 0)), 0.3),
                             ["out_of_taxonomy"], str(data.get("rationale", "")), data)

    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    labels = data.get("labels") or []
    if not isinstance(labels, list):
        labels = [str(labels)]
    rationale = str(data.get("rationale", "")).strip()

    return TaskDetection(task_type, confidence, [str(x) for x in labels],
                         rationale, data)


def detect(
    task_description: str,
    *,
    model: Optional[str] = None,
    taxonomy: Optional[Dict[str, str]] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> TaskDetection:
    """High-level entry point: try heuristic first, fall back to LLM.

    Set ``model=None`` to disable the LLM fallback — useful when you only
    want the cheap heuristic.
    """
    hit = detect_heuristic(task_description)
    if hit is not None and hit.confidence >= 0.75:
        return hit
    if model is None:
        # No LLM available — return whatever the heuristic gave (even low-confidence),
        # or 'other'.
        return hit or TaskDetection("other", 0.0, ["no_llm"],
                                    "No heuristic match and LLM disabled")
    return detect_with_llm(task_description, model=model, taxonomy=taxonomy,
                            api_key=api_key, base_url=base_url)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("description", help="Task description / extractor agent prompt text")
    ap.add_argument("--model", default=None,
                    help="LLM model for fallback (e.g. openrouter/openai/gpt-4o-mini). "
                         "Omit to use only the heuristic.")
    args = ap.parse_args()
    result = detect(args.description, model=args.model)
    print(json.dumps(result.asdict(), indent=2))
