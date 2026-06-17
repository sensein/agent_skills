"""Group raw NER mentions into a per-entity index.

The extractor emits one item per occurrence (exhaustive). For downstream
consumers ("show me everything about BDNF in this paper") it's often
cleaner to also have a view where mentions are grouped by canonical entity,
with all containing sentences and paper locations merged.

This module is pure post-processing — no LLM calls. It runs after the judge
stage and writes a sibling `entities_grouped` list to the pipeline result.

Output shape per group:

    {
      "entity":         "BDNF",                        # canonical surface form
      "label":          "Gene",
      "mention_count":  17,
      "ontology_id":    "http://identifiers.org/hgnc/1033",
      "ontology_label": "BDNF",
      "ontology":       "HGNC",
      "concept_mapping_provenance": "tool",
      "alignment_method":           "direct_tool_call",
      "judge_score_max":  1.0,
      "judge_score_avg":  0.92,
      "judge_score_min":  0.7,
      "mentions": [
        {"start": 12, "end": 16, "sentence": "...", "paper_location": "Methods"},
        {"start": 348, "end": 352, "sentence": "...", "paper_location": "Results"},
        ...
      ],
      "sentences": [
        {"text": "...", "paper_locations": ["Methods", "Discussion"]},
        {"text": "...", "paper_locations": ["Results"]},
        ...
      ]
    }
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Iterable, Optional


def _canonical_key(entity: str, label: Optional[str]) -> tuple[str, str]:
    """Group key: case-insensitive entity + label.

    Case-sensitive variants (BDNF vs bdnf) collapse into one group; the
    canonical surface form is picked as the most common casing seen.
    Different labels (e.g. 'Pvalb' as Gene vs as LineageMarker) intentionally
    do NOT collapse — they're semantically different.
    """
    return ((entity or "").strip().lower(), (label or "").strip())


def _pick_canonical_surface(surfaces: list[str]) -> str:
    """Return the most common surface form (preserving original casing)."""
    counts: dict[str, int] = defaultdict(int)
    for s in surfaces:
        counts[s] += 1
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _safe(v):
    return None if v is None else v


def _summarize_judge(scores: list[float]) -> dict[str, Optional[float]]:
    finite = [s for s in scores if isinstance(s, (int, float))]
    if not finite:
        return {"judge_score_max": None, "judge_score_avg": None,
                "judge_score_min": None}
    return {
        "judge_score_max": round(max(finite), 3),
        "judge_score_avg": round(mean(finite), 3),
        "judge_score_min": round(min(finite), 3),
    }


def _pick_best_alignment(mentions: list[dict]) -> dict:
    """Of all mentions for this entity, return the one with the highest
    judge_score AND a non-null ontology_id. Falls back to any non-null
    mapping, then to the first mention.
    """
    candidates = [m for m in mentions if m.get("ontology_id")]
    if candidates:
        return max(candidates, key=lambda m: m.get("judge_score") or 0.0)
    return mentions[0]


def group_mentions_by_entity(
    items: Iterable[dict],
    *,
    surface_key: str = "entity",
    label_key: str = "label",
) -> list[dict]:
    """Group raw mention items by canonical (entity.lower(), label).

    Returns a list of grouped dicts (see module docstring for shape).
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for it in items:
        if not it.get(surface_key):
            continue
        k = _canonical_key(it[surface_key], it.get(label_key))
        buckets[k].append(it)

    out: list[dict] = []
    for (lower_form, label), mentions in buckets.items():
        canonical = _pick_canonical_surface([m[surface_key] for m in mentions])
        best = _pick_best_alignment(mentions)

        # merge sentences by exact text; aggregate the locations they appear in.
        sent_to_locs: dict[str, list[str]] = defaultdict(list)
        for m in mentions:
            sent = m.get("sentence")
            loc = m.get("paper_location")
            if not sent:
                continue
            if loc and loc not in sent_to_locs[sent]:
                sent_to_locs[sent].append(loc)
            elif not loc and not sent_to_locs[sent]:
                sent_to_locs[sent] = []
        sentences_merged = [
            {"text": s, "paper_locations": locs}
            for s, locs in sent_to_locs.items()
        ]

        mentions_view = [
            {
                "start": m.get("start"),
                "end":   m.get("end"),
                "sentence": m.get("sentence"),
                "paper_location": m.get("paper_location"),
                "source_model":   m.get("source_model"),
                "source_score":   m.get("source_score"),
                "judge_score":    m.get("judge_score"),
                "concept_mapping_provenance": m.get("concept_mapping_provenance"),
                "alignment_method":           m.get("alignment_method"),
            }
            for m in mentions
        ]

        # Which models surfaced this entity (preserves provenance across the
        # whole ensemble + LLM). consensus_count = how many distinct models
        # found it; useful as a confidence proxy.
        source_models = sorted({m.get("source_model") for m in mentions
                                if m.get("source_model")})
        source_counts: dict[str, int] = defaultdict(int)
        for m in mentions:
            if m.get("source_model"):
                source_counts[m["source_model"]] += 1

        out.append({
            surface_key:    canonical,
            label_key:      label or None,
            "mention_count": len(mentions),

            "source_models":   source_models,
            "source_model_counts": dict(source_counts),
            "consensus_count": len(source_models),

            "ontology_id":    _safe(best.get("ontology_id")),
            "ontology_label": _safe(best.get("ontology_label")),
            "ontology":       _safe(best.get("ontology")),
            "concept_mapping_provenance": _safe(best.get("concept_mapping_provenance")),
            "alignment_method":           _safe(best.get("alignment_method")),

            **_summarize_judge([m.get("judge_score") for m in mentions]),

            "sentences": sentences_merged,
            "mentions":  mentions_view,
        })

    # sort: most-mentioned first; ties broken by entity name.
    out.sort(key=lambda g: (-g["mention_count"], g.get(surface_key) or ""))
    return out


# ---------------------------------------------------------------------------
# Unify ontology across all mentions of the same entity
# ---------------------------------------------------------------------------

_ONTOLOGY_FIELDS = (
    "ontology_id", "ontology_label", "ontology", "concept_mapping_provenance",
)


def _ontology_score(ent: dict) -> int:
    """Score an entity's ontology mapping quality. Higher = better.

    Tool-backed mapping (provenance == 'tool') beats LLM knowledge beats
    nothing. A real IRI beats placeholders like 'N/A' / 'none'.
    """
    s = 0
    if ent.get("concept_mapping_provenance") == "tool":
        s += 100
    elif ent.get("concept_mapping_provenance") == "llm_knowledge":
        s += 25
    oid = str(ent.get("ontology_id") or "").strip().lower()
    if oid and oid not in ("n/a", "none", "null", ""):
        s += 50
    if str(ent.get("ontology_label") or "").strip().lower() not in ("n/a", "none", "null", ""):
        s += 10
    return s


def unify_ontology_across_entities(entities: list[dict],
                                   surface_key: str = "entity",
                                   ) -> list[dict]:
    """Make every occurrence of the same (entity, label) share ONE ontology mapping.

    When the same surface form is processed in different parallel chunks (or
    by different NER models in the ensemble), each may produce a different
    ontology ID. This pass picks the *best* mapping per (entity, label) and
    applies it back to every occurrence.

    The "best" is determined by `_ontology_score`:
      tool-mapped > llm_knowledge-mapped > unmapped
      with a real IRI > with placeholders.

    Individual mentions are preserved — nothing is dropped or deduplicated.
    Only the four `_ONTOLOGY_FIELDS` are normalized.

    Mutates the list in place and returns it.
    """
    best: dict[tuple, dict] = {}

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        surface = (ent.get(surface_key) or ent.get("term")
                   or ent.get("name") or "")
        key = (str(surface).lower().strip(),
               str(ent.get("label") or "").lower().strip())
        if not key[0]:
            continue
        s = _ontology_score(ent)
        if key not in best or s > _ontology_score(best[key]):
            best[key] = {f: ent[f] for f in _ONTOLOGY_FIELDS if f in ent}

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        surface = (ent.get(surface_key) or ent.get("term")
                   or ent.get("name") or "")
        key = (str(surface).lower().strip(),
               str(ent.get("label") or "").lower().strip())
        if key in best:
            ent.update(best[key])

    return entities


def attach_grouped_views(result: dict, *, unify_ontology: bool = True) -> dict:
    """Mutate ``result`` to add `entities_grouped` and `key_terms_grouped`.

    The original `entities` / `key_terms` lists (raw, one-per-mention) are
    preserved as the authoritative record. When ``unify_ontology=True`` (the
    default), we first normalize ontology fields across mentions of the same
    (entity, label) so all occurrences share one consistent mapping (best one
    wins — tool-mapped beats LLM, real IRI beats placeholders).
    """
    if "entities" in result:
        if unify_ontology:
            unify_ontology_across_entities(result["entities"] or [],
                                            surface_key="entity")
        result["entities_grouped"] = group_mentions_by_entity(
            result.get("entities") or [],
            surface_key="entity", label_key="label",
        )
    if "key_terms" in result:
        if unify_ontology:
            unify_ontology_across_entities(result["key_terms"] or [],
                                            surface_key="term")
        result["key_terms_grouped"] = group_mentions_by_entity(
            result.get("key_terms") or [],
            surface_key="term", label_key="label",
        )
    return result


if __name__ == "__main__":
    import json
    sample = [
        {"entity": "BDNF", "label": "Gene", "sentence": "BDNF is upregulated.",
         "start": 0, "end": 4, "paper_location": "Results", "judge_score": 1.0,
         "ontology_id": "HGNC:1033", "ontology_label": "BDNF", "ontology": "HGNC",
         "concept_mapping_provenance": "tool"},
        {"entity": "BDNF", "label": "Gene", "sentence": "BDNF protein levels increase.",
         "start": 52, "end": 56, "paper_location": "Discussion", "judge_score": 0.95,
         "ontology_id": "HGNC:1033", "ontology_label": "BDNF", "ontology": "HGNC",
         "concept_mapping_provenance": "tool"},
        {"entity": "bdnf", "label": "Gene", "sentence": "BDNF is upregulated.",
         "start": 200, "end": 204, "paper_location": "Methods", "judge_score": 0.85,
         "concept_mapping_provenance": "tool"},
        {"entity": "hippocampus", "label": "BrainRegion",
         "sentence": "We recorded in the hippocampus.",
         "start": 25, "end": 36, "paper_location": "Methods", "judge_score": 0.9,
         "ontology_id": "UBERON_0002421", "ontology_label": "hippocampal formation",
         "ontology": "UBERON", "concept_mapping_provenance": "tool"},
    ]
    print(json.dumps(group_mentions_by_entity(sample), indent=2))
