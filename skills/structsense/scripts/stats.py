"""Compute a stats block for a pipeline result.

Pure function — no LLM calls. Embed the returned dict under `result["stats"]`
and print the human summary to stderr.

The stats answer the question "did the run do what it was supposed to?":

- total mentions emitted (NER yield)
- mentions per unique surface form (sanity check for exhaustive extraction;
  should be > 1 on most papers — if = 1, you're likely deduplicating by
  surface form somewhere upstream)
- alignment provenance breakdown (tool / llm_knowledge / unmapped / skipped)
- judge score distribution
- per-stage elapsed times
- which mapper backend was used (for debugging the cascade)
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Optional


def _iter_items(result: Mapping[str, Any]) -> Iterable[tuple[str, dict]]:
    """Yield (container_key, item) for every extracted item across all
    container shapes (NER, key_terms, resource)."""
    for key in ("entities", "key_terms"):
        for it in result.get(key, []) or []:
            yield key, it
    res = result.get("extracted_resources") or result.get("aligned_resources") \
        or result.get("judge_resource") or {}
    if isinstance(res, dict):
        for bucket, items in res.items():
            for it in items or []:
                yield "resources", it


def _bucket_score(s: Optional[float]) -> str:
    if s is None:
        return "missing"
    s = float(s)
    if s >= 1.0:
        return "1.00"
    if s >= 0.85:
        return "0.85-0.99"
    if s >= 0.65:
        return "0.65-0.84"
    if s >= 0.40:
        return "0.40-0.64"
    return "<0.40"


def compute_stats(
    result: Mapping[str, Any],
    *,
    timings: Optional[Mapping[str, float]] = None,
    input_meta: Optional[Mapping[str, Any]] = None,
    alignment_meta: Optional[Mapping[str, Any]] = None,
    judge_meta: Optional[Mapping[str, Any]] = None,
    dropped_invalid_span: int = 0,
) -> dict:
    """Build a `stats` dict from a pipeline result + sidecar metadata.

    Args:
        result: the full pipeline result (extractor + alignment + judge merged).
        timings: {"extraction": float, "mask_recall": float, "alignment": float,
                  "judge": float, "total": float}.  All optional.
        input_meta: {"char_count": int, "chunk_count": int,
                     "chunk_size_chars": int, "input_path": str?}.
        alignment_meta: {"mapper_used": str, "mapper_url": str,
                         "fallback_triggered": bool, "cascade_history": [str, …]}.
        judge_meta: {"method": "llm" | "auto_approved", "model": str?,
                     "max_workers": int?}.
        dropped_invalid_span: number of items dropped during span validation.
    """
    # ---- entity / key_term counts ----
    entities = list(result.get("entities") or [])
    key_terms = list(result.get("key_terms") or [])

    ent_labels = Counter((it.get("label") or "Unknown") for it in entities)
    ent_sources = Counter((it.get("source_model") or "unknown") for it in entities)

    # Prefer the grouped view's count when it's present (case-insensitive + label-aware).
    # Fall back to a simple lowercase set of surface forms if grouping wasn't run.
    grouped = result.get("entities_grouped") or []
    if grouped:
        ent_unique = len(grouped)
    else:
        ent_unique = len({(it.get("entity") or "").lower() for it in entities
                          if it.get("entity")})

    kt_grouped = result.get("key_terms_grouped") or []
    if kt_grouped:
        kt_unique = len(kt_grouped)
    else:
        kt_unique = len({(it.get("term") or "").lower() for it in key_terms
                         if it.get("term")})

    # ---- resource counts (if present) ----
    resources = []
    res_container = result.get("extracted_resources") or result.get("aligned_resources") \
        or result.get("judge_resource") or {}
    if isinstance(res_container, dict):
        for items in res_container.values():
            resources.extend(items or [])

    # ---- alignment provenance ----
    prov = Counter()
    method = Counter()
    for _, it in _iter_items(result):
        prov[it.get("concept_mapping_provenance") or "missing"] += 1
        method[it.get("alignment_method") or "missing"] += 1

    # ---- judge score distribution ----
    score_buckets = Counter()
    for _, it in _iter_items(result):
        score_buckets[_bucket_score(it.get("judge_score"))] += 1

    # ---- task_type: prefer the result, otherwise infer from what's present ----
    task_type = result.get("task_type")
    if not task_type:
        if entities or key_terms:
            task_type = "ner"
        elif resources:
            task_type = "resource"
        elif result.get("activity") or result.get("items"):
            task_type = "structured_extraction"

    # ---- prominent totals at the top of the stats block ----
    total_items = (len(entities) + len(key_terms) + len(resources))

    # ---- assemble ----
    stats: dict[str, Any] = {
        "task_type": task_type,
        "totals": {
            "total_entity_mentions":  len(entities),
            "total_key_term_mentions": len(key_terms),
            "total_resources":         len(resources),
            "total_items":             total_items,
            "unique_entities":         ent_unique,
            "unique_key_terms":        kt_unique,
        },
        "elapsed_seconds": dict(timings or {}),
        "input": dict(input_meta or {}),
        "entities": {
            "total_mentions": len(entities),
            "unique_surface_forms": ent_unique,
            "mentions_per_unique": round(len(entities) / ent_unique, 2) if ent_unique else 0,
            "by_label": dict(ent_labels.most_common()),
            "by_source_model": dict(ent_sources.most_common()),
            "dropped_invalid_span": dropped_invalid_span,
        },
        "key_terms": {
            "total_mentions": len(key_terms),
            "unique_surface_forms": kt_unique,
        },
        "resources": {
            "total": len(resources),
        },
        "alignment": {
            "by_provenance": dict(prov.most_common()),
            "by_method": dict(method.most_common()),
            **dict(alignment_meta or {}),
        },
        "judge": {
            **dict(judge_meta or {}),
            "score_buckets": dict(sorted(score_buckets.items(),
                                          key=lambda kv: ("1.00", "0.85-0.99",
                                          "0.65-0.84", "0.40-0.64", "<0.40",
                                          "missing").index(kv[0]) if kv[0] in
                                          ("1.00","0.85-0.99","0.65-0.84","0.40-0.64","<0.40","missing")
                                          else 99)),
        },
    }
    return stats


def format_summary(stats: Mapping[str, Any]) -> str:
    """Human-readable one-screen summary. Print this to stderr after a run."""
    lines = []
    ent = stats.get("entities", {})
    kt = stats.get("key_terms", {})
    res = stats.get("resources", {})
    al = stats.get("alignment", {})
    jud = stats.get("judge", {})
    inp = stats.get("input", {})
    t = stats.get("elapsed_seconds", {})

    lines.append("================ STRUCTSENSE-SKILLS RUN SUMMARY ================")
    if stats.get("task_type"):
        lines.append(f"task_type:           {stats['task_type']}")
    totals = stats.get("totals") or {}
    if totals:
        lines.append(
            f"TOTAL ITEMS:         {totals.get('total_items', 0):,}  "
            f"(entities={totals.get('total_entity_mentions',0):,}  "
            f"key_terms={totals.get('total_key_term_mentions',0):,}  "
            f"resources={totals.get('total_resources',0):,})"
        )
        lines.append(
            f"UNIQUE:              entities={totals.get('unique_entities',0):,}  "
            f"key_terms={totals.get('unique_key_terms',0):,}"
        )
    if inp:
        lines.append(f"input:               {inp.get('input_path','<text>')} "
                     f"({inp.get('char_count','?')} chars, "
                     f"{inp.get('chunk_count','?')} chunks of "
                     f"{inp.get('chunk_size_chars','?')} chars)")
    if t:
        lines.append(f"elapsed:             total={t.get('total','?')}s  "
                     f"extract={t.get('extraction','?')}s  "
                     f"mask_recall={t.get('mask_recall','?')}s  "
                     f"align={t.get('alignment','?')}s  "
                     f"judge={t.get('judge','?')}s")
    lines.append("")
    lines.append(f"ENTITIES:            {ent.get('total_mentions',0)} mentions, "
                 f"{ent.get('unique_surface_forms',0)} unique  "
                 f"({ent.get('mentions_per_unique',0):.2f}x avg) "
                 f"[dropped: {ent.get('dropped_invalid_span',0)}]")
    by_label = ent.get("by_label") or {}
    if by_label:
        head = list(by_label.items())[:8]
        rest = sum(v for _, v in list(by_label.items())[8:])
        line = "  by_label:            " + ", ".join(f"{k}={v}" for k, v in head)
        if rest:
            line += f", …+{rest} more"
        lines.append(line)
    by_src = ent.get("by_source_model") or {}
    if by_src:
        head = list(by_src.items())[:8]
        rest = sum(v for _, v in list(by_src.items())[8:])
        line = "  by_source_model:     " + ", ".join(f"{k}={v}" for k, v in head)
        if rest:
            line += f", …+{rest} more"
        lines.append(line)
    lines.append(f"KEY_TERMS:           {kt.get('total_mentions',0)} mentions, "
                 f"{kt.get('unique_surface_forms',0)} unique")
    if res.get("total"):
        lines.append(f"RESOURCES:           {res['total']}")
    lines.append("")
    lines.append(f"ALIGNMENT:           "
                 f"mapper={al.get('mapper_used','?')} "
                 f"url={al.get('mapper_url','?')} "
                 f"fallback={al.get('fallback_triggered', False)}")
    if al.get("by_provenance"):
        lines.append(f"  by_provenance:     " + ", ".join(f"{k}={v}" for k, v in al["by_provenance"].items()))
    lines.append("")
    lines.append(f"JUDGE:               method={jud.get('method','?')} "
                 f"model={jud.get('model','?')}")
    if jud.get("score_buckets"):
        lines.append(f"  score_buckets:     " + ", ".join(f"{k}={v}" for k, v in jud["score_buckets"].items()))
    lines.append("================================================================")
    return "\n".join(lines)


if __name__ == "__main__":
    demo_result = {
        "task_type": "ner",
        "entities": [
            {"entity": "BDNF", "label": "Gene", "judge_score": 1.0,
             "concept_mapping_provenance": "tool", "alignment_method": "direct_tool_call"},
            {"entity": "BDNF", "label": "Gene", "judge_score": 1.0,
             "concept_mapping_provenance": "tool", "alignment_method": "direct_tool_call"},
            {"entity": "hippocampus", "label": "BrainRegion", "judge_score": 0.9,
             "concept_mapping_provenance": "tool", "alignment_method": "direct_tool_call"},
            {"entity": "fast-spiking", "label": "Phenomenon", "judge_score": 0.4,
             "concept_mapping_provenance": "unmapped", "alignment_method": "skipped"},
        ],
        "key_terms": [],
    }
    stats = compute_stats(
        demo_result,
        timings={"extraction": 5.2, "mask_recall": 2.1, "alignment": 1.4,
                 "judge": 3.0, "total": 11.7},
        input_meta={"char_count": 12340, "chunk_count": 6, "chunk_size_chars": 2000,
                    "input_path": "paper.txt"},
        alignment_meta={"mapper_used": "local_hybrid",
                        "mapper_url": "http://localhost:8000",
                        "fallback_triggered": False},
        judge_meta={"method": "llm", "model": "openrouter/openai/gpt-4o-mini"},
    )
    print(format_summary(stats))
