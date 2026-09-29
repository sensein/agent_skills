"""Identity basis: what an occurrence is AND why it is that (references/identity-basis.md).

    Cell-type mapping = f(name, hierarchy, defining characteristics, context)

The extractor records, per occurrence, an `identity_basis`: a canonical candidate
("Pvalb GABAergic interneuron") and the features the text gives (PVALB+, GABA, layer 5,
motor cortex, ...), each with a role (defining / supporting / contextual / excluding)
and a source (explicit_text / surrounding_context / ontology_inference /
naming_convention). This module, deterministically:

  normalize(ib)             accepts the list form or the keyed form
                            ({"base_cell_type": ..., "molecular_marker": [...], ...})
  ground(item, text)        keeps a text-sourced feature only if its quote (else its
                            value or target) occurs in the occurrence's sentence or the
                            nearby text; the rest are dropped and counted, never guessed
  assess(item, concept)     which features the mapped concept supports (its label, or
                            its CL hierarchy via scripts/class_anchors.py) and which
                            contradict it (a GABA feature against a glutamatergic term)
  refine_mappings(...)      when the name maps nowhere, or to a term a defining/
                            supporting feature contradicts, tries the canonical
                            candidate, then compositions of defining features with the
                            base type, through the same mapping cascade; records the
                            query, the features that justify the choice and those that
                            contradict it as item["identity_mapping"]
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

KINDS = ("hierarchy", "molecular_marker", "neurotransmitter", "transcriptomic", "anatomical",
         "morphological", "electrophysiological", "connectivity", "developmental", "functional",
         "species", "state")
ROLES = ("defining", "supporting", "contextual", "excluding")
SOURCES = ("explicit_text", "surrounding_context", "ontology_inference", "naming_convention")
TEXT_SOURCES = ("explicit_text", "surrounding_context")
CELL_LABELS = {"CellType", "CellClass", "CellSubtype", "CellPopulation", "Neuron", "Interneuron",
               "GlialCell", "NeuralCellType"}

# keyed form (the user-facing JSON) -> kind
_KEYED = {"base_cell_type": "hierarchy", "base_type": "hierarchy", "hierarchy": "hierarchy",
          "molecular_marker": "molecular_marker", "molecular_markers": "molecular_marker",
          "marker": "molecular_marker", "markers": "molecular_marker",
          "neurotransmitter": "neurotransmitter", "transcriptomic": "transcriptomic",
          "anatomical_location": "anatomical", "anatomical": "anatomical", "location": "anatomical",
          "morphology": "morphological", "morphological": "morphological",
          "electrophysiology": "electrophysiological", "electrophysiological": "electrophysiological",
          "firing_pattern": "electrophysiological", "connectivity": "connectivity",
          "projection": "connectivity", "developmental": "developmental",
          "developmental_origin": "developmental", "functional": "functional", "function": "functional",
          "species": "species", "state": "state", "cell_state": "state"}
_SUPPORT = {"explicit": "explicit_text", "text": "explicit_text", "context": "surrounding_context",
            "contextual": "surrounding_context", "ontology": "ontology_inference",
            "inferred": "ontology_inference", "name": "naming_convention", "naming": "naming_convention"}

# neurotransmitter value -> the NER class a concept must (not) be under
_TRANSMITTER = {"gaba": ("InhibitoryNeuron", "ExcitatoryNeuron"),
                "gabaergic": ("InhibitoryNeuron", "ExcitatoryNeuron"),
                "glutamate": ("ExcitatoryNeuron", "InhibitoryNeuron"),
                "glutamatergic": ("ExcitatoryNeuron", "InhibitoryNeuron")}
_HIERARCHY = {"neuron": ("Neuron", "GlialCell"), "neurons": ("Neuron", "GlialCell"),
              "interneuron": ("Interneuron", "GlialCell"), "glia": ("GlialCell", "Neuron"),
              "glial cell": ("GlialCell", "Neuron"), "astrocyte": ("Astrocyte", "Neuron"),
              "microglia": ("Microglia", "Neuron"), "oligodendrocyte": ("Oligodendrocyte", "Neuron")}
_WORDS = {"gaba": "gabaergic", "glutamate": "glutamatergic", "pvalb": "parvalbumin", "sst": "somatostatin",
          "vip": "vasoactive intestinal", "cck": "cholecystokinin", "npy": "neuropeptide y"}


def _norm(s: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", str(s or "").lower()).split())


def normalize(ib: Any) -> Optional[dict]:
    """The list form, whatever the extractor wrote. Unknown kinds/roles/sources are
    dropped rather than guessed."""
    if not isinstance(ib, dict):
        return None
    out = {k: ib.get(k) for k in ("canonical_candidate", "hierarchy_level", "name_derivation",
                                  "stability", "state_evidence") if ib.get(k)}
    if isinstance(ib.get("canonical_mapping"), dict) and not out.get("canonical_candidate"):
        out["canonical_candidate"] = ib["canonical_mapping"].get("label")
    feats = list(ib.get("features") or [])
    basis = ib.get("identity_basis") if isinstance(ib.get("identity_basis"), dict) else ib
    for key, kind in _KEYED.items():
        vals = basis.get(key)
        if vals is None or key == "features":
            continue
        for v in (vals if isinstance(vals, list) else [vals]):
            v = v if isinstance(v, dict) else {"value": v}
            feats.append({**v, "kind": kind})
    clean = []
    for f in feats:
        if not isinstance(f, dict) or not str(f.get("value") or "").strip():
            continue
        f = dict(f)
        src = f.get("source") or _SUPPORT.get(str(f.get("support") or "").lower()) or "explicit_text"
        f["source"] = src if src in SOURCES else _SUPPORT.get(str(src).lower())
        f["role"] = f.get("role") if f.get("role") in ROLES else None
        if f.get("kind") not in KINDS or not f["source"]:
            continue
        if f["role"] is None:  # no role stated: contextual for place/species, else supporting
            f["role"] = "contextual" if f["kind"] in ("anatomical", "species") else "supporting"
        f.pop("support", None)
        clean.append(f)
    out["features"] = clean
    return out if (clean or out.get("canonical_candidate")) else None


def ground(item: dict, text: Optional[str], window: int = 800) -> tuple[Optional[dict], int]:
    """(identity_basis, n_dropped): text-sourced features whose quote (or value, or
    target) is not in the occurrence's sentence / nearby text are dropped."""
    ib = normalize(item.get("identity_basis"))
    if not ib:
        return None, 0
    hay = [str(item.get("sentence") or "")]
    a, b = item.get("start"), item.get("end")
    if text and isinstance(a, int) and isinstance(b, int):
        hay.append(text[max(0, a - window):b + window])
    hay_n = _norm(" ".join(hay))
    kept, dropped = [], 0
    for f in ib["features"]:
        if f["source"] in TEXT_SOURCES:
            probes = [f.get("quote"), f.get("target"), f.get("value")]
            if not any(p and _norm(p) and _norm(p) in hay_n for p in probes):
                dropped += 1
                continue
        kept.append(f)
    ib["features"] = kept
    return ib, dropped


def grounded_mask(item: dict, text: Optional[str], window: int = 800) -> tuple[Optional[dict], list[bool]]:
    """(normalized identity_basis, per-feature grounded flags) — indices stay those of
    normalize(), which assess() and identity_mapping use."""
    ib = normalize(item.get("identity_basis"))
    if not ib:
        return None, []
    hay = [str(item.get("sentence") or "")]
    a, b = item.get("start"), item.get("end")
    if text and isinstance(a, int) and isinstance(b, int):
        hay.append(text[max(0, a - window):b + window])
    hay_n = _norm(" ".join(hay))
    mask = []
    for f in ib["features"]:
        if f["source"] not in TEXT_SOURCES:
            mask.append(True)
            continue
        probes = [f.get("quote"), f.get("target"), f.get("value")]
        mask.append(any(p and _norm(p) and _norm(p) in hay_n for p in probes))
    return ib, mask


def assess(item: dict, concept_label: Optional[str], concept_classes: list[str]) -> dict:
    """Which features the concept supports / contradicts. Support: the feature's value or
    target (or its common expansion) is in the concept label, or the concept sits under
    the class the feature implies. Contradiction: the concept sits under the class the
    feature rules out (GABA vs ExcitatoryNeuron, neuron vs GlialCell)."""
    ib = normalize(item.get("identity_basis")) or {"features": []}
    label = _norm(concept_label)
    classes = set(concept_classes or [])
    justified, contradicted = [], []
    for i, f in enumerate(ib["features"]):
        if f["role"] == "contextual" or f["kind"] in ("species", "state"):
            continue
        v = _norm(f.get("value"))
        forms = {v, _norm(f.get("target")), _WORDS.get(v, "")} - {""}
        table = _TRANSMITTER if f["kind"] == "neurotransmitter" else _HIERARCHY if f["kind"] == "hierarchy" else {}
        must, not_ = table.get(v, (None, None))
        if not_ and not_ in classes:
            contradicted.append(i)
        elif (must and must in classes) or any(x and re.search(rf"\b{re.escape(x)}", label) for x in forms):
            justified.append(i)
    return {"justified_by": justified, "contradicted_by": contradicted}


def candidate_queries(item: dict, surface: str) -> list[str]:
    """Identity-derived lookups, most specific first: the canonical candidate, then
    defining/supporting markers and transmitter composed with the base type."""
    ib = normalize(item.get("identity_basis")) or {}
    feats = ib.get("features") or []
    base = next((f["value"] for f in feats if f["kind"] == "hierarchy"), None)
    out = []
    if ib.get("canonical_candidate"):
        out.append(ib["canonical_candidate"])
    if base:
        marks = [f["value"] for f in feats if f["kind"] == "molecular_marker"
                 and f["role"] in ("defining", "supporting") and f.get("polarity") in (None, "positive", "high", "enriched")]
        tx = [f["value"] for f in feats if f["kind"] == "neurotransmitter" and f["role"] != "contextual"]
        txw = [_WORDS.get(_norm(t), t) for t in tx]
        for m in marks:
            for t in txw:
                out.append(f"{m} {t} {base}")
            out.append(f"{m} {base}")
            if _WORDS.get(_norm(m)):
                out.append(f"{_WORDS[_norm(m)]} {base}")
        for t in txw:
            out.append(f"{t} {base}")
    seen, uniq = {_norm(surface)}, []
    for q in out:
        if _norm(q) not in seen:
            seen.add(_norm(q))
            uniq.append(q)
    return uniq


def refine_mappings(items: list[dict], map_one: Callable[[dict], dict],
                    classes_of: Callable[[str], list[str]], surface_key: str = "entity") -> dict:
    """For cell items that carry an identity basis: keep a name mapping the features do
    not contradict; otherwise try identity-derived queries through `map_one` (the
    mapping cascade for one item copy) and take the first candidate no defining/
    supporting feature contradicts, preferring the one most features justify."""
    stats = {"assessed": 0, "remapped": 0, "rejected_contradicted": 0}
    for it in items:
        if it.get("label") not in CELL_LABELS or not normalize(it.get("identity_basis")):
            continue
        stats["assessed"] += 1
        cur = None
        if it.get("concept_mapping_provenance") == "tool" and it.get("ontology_id"):
            cur = assess(it, it.get("ontology_label"), classes_of(it["ontology_id"]))
            if not cur["contradicted_by"]:
                it["identity_mapping"] = {"query": it.get("mapping_query") or it.get(surface_key),
                                          "basis": "name", **cur}
                continue
            stats["rejected_contradicted"] += 1
        best = None
        for q in candidate_queries(it, str(it.get(surface_key) or "")):
            trial = {k: v for k, v in it.items() if k not in (
                "ontology_id", "ontology_label", "ontology", "concept_mapping_provenance",
                "alignment_method", "mapping_source", "ontology_match_type", "match_tier",
                "trusted_ambiguous", "identity_mapping")}
            trial["mapping_query"] = q
            m = map_one(trial)
            if m.get("concept_mapping_provenance") != "tool" or not m.get("ontology_id"):
                continue
            a = assess(it, m.get("ontology_label"), classes_of(m["ontology_id"]))
            if a["contradicted_by"]:
                continue
            score = len(a["justified_by"])
            if best is None or score > best[0]:
                best = (score, q, m, a)
        if best:
            _, q, m, a = best
            for k in ("ontology_id", "ontology_label", "ontology", "concept_mapping_provenance",
                      "alignment_method", "mapping_source", "ontology_match_type", "match_tier"):
                if m.get(k) is not None:
                    it[k] = m[k]
            it["mapping_query"] = q
            it["identity_mapping"] = {"query": q, "basis": "identity", **a}
            stats["remapped"] += 1
        elif cur is not None:  # the name mapping was contradicted and nothing better exists
            it["identity_mapping"] = {"query": it.get(surface_key), "basis": "name", **cur,
                                      "rejected": it.get("ontology_id")}
            for f in ("ontology_id", "ontology_label", "ontology"):
                it[f] = None
            it["concept_mapping_provenance"] = "unmapped"
            it["alignment_method"] = "identity_contradicted"
    return stats
