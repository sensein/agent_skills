"""Normalize a pipeline result to the canonical skill shape — idempotent.

Some models (and older runs) emit the legacy shape:
  - paper_title / doi repeated on every entity
  - no source_metadata block
  - no source_model field
  - no entities_grouped / stats

This module rewrites such results into the canonical shape. It is safe to run
on already-canonical results (idempotent).

Always run this in the pipeline before writing the *_final.json. It is also
exposed as a CLI so users can repair legacy files in place:

    python -m scripts.normalize_result paper_final.json \\
        --input paper.txt \\
        --llm-model openrouter/anthropic/claude-sonnet-4-6

What it does (each step is a no-op if already done):
1. Lift `paper_title` + `doi` off every entity/key_term into top-level
   `source_metadata` (deduplicating by most-common value).
2. Strip per-entity `paper_title` / `doi` keys.
3. If an item is missing `source_model`, tag it `llm_ner:<llm_model>` (or
   `llm_ner:unknown` if the model isn't known).
4. Attach `entities_grouped` + `key_terms_grouped` via group_by_entity.
5. Compute and embed `stats` via stats.compute_stats.
6. Add `source_path` to `source_metadata` if `input_path` is provided.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Step 1+2: source_metadata lifting
# ---------------------------------------------------------------------------

_DOC_LEVEL_KEYS = ("paper_title", "doi", "source_path")


def lift_doc_metadata(result: dict, *, input_path: Optional[str] = None) -> dict:
    """Move paper_title/doi off every item to top-level source_metadata.

    Deduplicates: the value most commonly seen across items wins. If items
    disagree, the others are stored under
    ``source_metadata.duplicate_values_seen`` for audit.
    """
    sm = dict(result.get("source_metadata") or {})

    for field in _DOC_LEVEL_KEYS:
        if sm.get(field):
            continue
        seen: Counter = Counter()
        for key in ("entities", "key_terms"):
            for it in result.get(key, []) or []:
                v = it.get(field)
                if v not in (None, ""):
                    seen[v] += 1
        if not seen:
            continue
        top, _ = seen.most_common(1)[0]
        sm[field] = top
        # audit if disagreement
        if len(seen) > 1:
            dup = sm.setdefault("duplicate_values_seen", {})
            dup[field] = [v for v, _ in seen.most_common()][1:]

    if input_path and not sm.get("source_path"):
        sm["source_path"] = input_path

    # strip per-item paper_title/doi/source_path
    for key in ("entities", "key_terms"):
        for it in result.get(key, []) or []:
            for field in _DOC_LEVEL_KEYS:
                it.pop(field, None)

    if sm:
        result["source_metadata"] = sm
    return result


# ---------------------------------------------------------------------------
# Step 3: tag source_model
# ---------------------------------------------------------------------------

def tag_missing_source_model(result: dict, llm_model: Optional[str]) -> dict:
    """Tag any item missing `source_model` as `llm_ner:<llm_model>`.

    Items that already have source_model are left alone.
    """
    label = f"llm_ner:{llm_model}" if llm_model else "llm_ner:unknown"
    for key in ("entities", "key_terms"):
        for it in result.get(key, []) or []:
            if not it.get("source_model"):
                it["source_model"] = label
    return result


# ---------------------------------------------------------------------------
# alignment_method inference
# ---------------------------------------------------------------------------

# When the alignment was clearly done (concept_mapping_provenance is set) but
# the per-item alignment_method field is missing, infer the most likely
# method so downstream consumers see a consistent provenance trail.
_PROVENANCE_TO_METHOD = {
    "tool":          "direct_tool_call",   # batched HTTP call to mapping service
    "llm_knowledge": "llm_agent",          # LLM filled it in from prior knowledge
    "unmapped":      "direct_tool_call",   # service responded but had no hit
    "skipped":       "skipped",            # alignment stage was explicitly disabled
}


def tag_missing_alignment_method(result: dict) -> dict:
    """For any item with `concept_mapping_provenance` set but missing
    `alignment_method`, derive the method from the provenance.

    This fixes the common case where a user (or a previous pipeline) tagged
    only the provenance field, leaving downstream consumers with a
    `by_method: {"missing": N}` row in stats.
    """
    for key in ("entities", "key_terms"):
        for it in result.get(key, []) or []:
            if it.get("alignment_method"):
                continue
            prov = it.get("concept_mapping_provenance")
            if prov in _PROVENANCE_TO_METHOD:
                it["alignment_method"] = _PROVENANCE_TO_METHOD[prov]
    return result


# ---------------------------------------------------------------------------
# task_type inference
# ---------------------------------------------------------------------------

def infer_task_type(result: dict) -> dict:
    """Set `result["task_type"]` if it's missing, by looking at what shape
    the result actually contains.
    """
    if result.get("task_type"):
        return result
    if result.get("entities") or result.get("key_terms"):
        result["task_type"] = "ner"
    elif result.get("extracted_resources") or result.get("aligned_resources") \
            or result.get("judge_resource"):
        result["task_type"] = "resource"
    elif result.get("activity") or result.get("items"):
        result["task_type"] = "structured_extraction"
    return result


# ---------------------------------------------------------------------------
# Step 4+5: grouped views + stats (delegated)
# ---------------------------------------------------------------------------

def attach_grouped(result: dict) -> dict:
    try:
        from group_by_entity import attach_grouped_views
    except ImportError:
        # When called from outside the scripts/ dir
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from group_by_entity import attach_grouped_views   # type: ignore
    attach_grouped_views(result)
    return result


def attach_stats(result: dict, *,
                 input_path: Optional[str] = None,
                 input_text_chars: Optional[int] = None,
                 chunk_size_chars: Optional[int] = None,
                 chunk_count: Optional[int] = None) -> dict:
    try:
        from stats import compute_stats
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from stats import compute_stats   # type: ignore

    # If the result already has timings, keep them; otherwise start fresh.
    existing = (result.get("stats") or {}).get("elapsed_seconds") or {}

    input_meta: dict[str, Any] = {"input_path": input_path}
    if input_text_chars is not None:
        input_meta["char_count"] = input_text_chars
    if chunk_size_chars is not None:
        input_meta["chunk_size_chars"] = chunk_size_chars
    if chunk_count is not None:
        input_meta["chunk_count"] = chunk_count

    result["stats"] = compute_stats(
        result,
        timings=existing,
        input_meta=input_meta,
        alignment_meta=(result.get("stats") or {}).get("alignment") or {},
        judge_meta=(result.get("stats") or {}).get("judge") or {},
    )
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _validate_iris(result: dict, *, strict: bool = True) -> dict:
    """Run strict IRI validation and demote llm_knowledge / hallucinated mappings."""
    try:
        from iri_validation import validate_all
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from iri_validation import validate_all   # type: ignore
    validate_all(result, strict=strict)
    return result


def normalize(result: dict, *,
              llm_model: Optional[str] = None,
              input_path: Optional[str] = None,
              input_text_chars: Optional[int] = None,
              chunk_size_chars: Optional[int] = None,
              chunk_count: Optional[int] = None,
              strict_iri_validation: bool = True) -> dict:
    """Run all normalization steps in order. Idempotent.

    Pipeline:
      1. lift paper_title/doi → source_metadata
      2. tag missing source_model
      3. tag missing alignment_method (from concept_mapping_provenance)
      4. infer task_type from result shape
      5. **strict IRI validation** — reject llm_knowledge provenance and
         malformed/hallucinated IRIs. Failed items get ontology fields nulled
         and `concept_mapping_provenance: "unmapped"`,
         `alignment_method: "validation_failed"`.
      6. unify ontology across mentions of the same (entity, label)
      7. attach entities_grouped + key_terms_grouped
      8. compute stats (totals, by_label, by_source_model, by_provenance, etc.)
    """
    lift_doc_metadata(result, input_path=input_path)
    tag_missing_source_model(result, llm_model)
    tag_missing_alignment_method(result)
    infer_task_type(result)
    _validate_iris(result, strict=strict_iri_validation)
    attach_grouped(result)
    attach_stats(
        result,
        input_path=input_path,
        input_text_chars=input_text_chars,
        chunk_size_chars=chunk_size_chars,
        chunk_count=chunk_count,
    )
    return result


# ---------------------------------------------------------------------------
# CLI: fix a legacy result file in place
# ---------------------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(
        description="Normalize a legacy pipeline result file to the canonical "
                    "skill shape. Idempotent; safe to run on already-canonical "
                    "outputs."
    )
    ap.add_argument("path", help="Path to a result JSON file. Modified in place "
                                  "unless --out is given.")
    ap.add_argument("--out", default=None,
                    help="Write to this path instead of overwriting.")
    ap.add_argument("--input", default=None,
                    help="Path to the original source text file (for source_path "
                         "and char_count in stats).")
    ap.add_argument("--llm-model", default=None,
                    help="LLM model string used for the run (for tagging items "
                         "missing source_model). e.g. "
                         "openrouter/anthropic/claude-sonnet-4-6")
    ap.add_argument("--no-strict-iri", action="store_true",
                    help="Disable strict IRI validation. By default, items "
                         "with concept_mapping_provenance=llm_knowledge or "
                         "malformed/hallucinated IRIs are demoted to "
                         "unmapped. This flag preserves them as-is (useful "
                         "only for debugging or for inputs that pre-date the "
                         "strict policy).")
    args = ap.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text())

    chars = None
    if args.input:
        try:
            chars = len(Path(args.input).read_text())
        except OSError:
            chars = None

    normalize(
        data,
        llm_model=args.llm_model,
        input_path=args.input,
        input_text_chars=chars,
        strict_iri_validation=not args.no_strict_iri,
    )

    out = Path(args.out) if args.out else path
    out.write_text(json.dumps(data, indent=2, default=str))

    try:
        from stats import format_summary  # type: ignore
        print(format_summary(data["stats"]), file=sys.stderr)
    except Exception:
        pass
    print(f"normalized {path} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    _main()
