"""
Deterministic canonicalization of concept blocks.

This is the layer that makes two DIFFERENT models (or two runs of one model)
collapse onto the same PubMed query whenever their proposals are *semantically*
the same. The evaluation showed within-model reproducibility was fine but
cross-model agreement was ~0.09 — every model produced a distinct query. The
divergence was mostly cosmetic or redundant, not substantive:

  1. block ORDER differed (AND is commutative, the string is not)
  2. redundant NARROWER headings ("Hippocampus" + "CA1 Region, Hippocampal" +
     "Dentate Gyrus"): `[MeSH Terms]` already auto-explodes, so a descendant of
     another heading in the same OR block adds nothing
  3. duplicate headings / unsorted heading lists
  4. FUZZY resolution let a hallucinated heading become a real (wrong) one:
     `_resolve_mesh` fell back to a substring LIKE match and auto-selected the
     first hit, so e.g. "CNS disease" resolved to the descriptor whose stored
     label happened to be the Dutch "Ziekte, centraalzenuwstelsel-" — a tag
     PubMed cannot match at all.

Everything here is a pure function of (proposal, MeSH index): no LLM, no clock,
no randomness. Blocks in, canonical blocks out.

Block shape (same as query_builder / /api/compile):
    {"name": str, "slot": str, "mesh": [heading, ...],
     "freetext": [term, ...], "explode": bool}
"""
from __future__ import annotations

from .mesh_index import MeshIndex

# Canonical facet vocabulary. Models are asked to label each block with one of
# these (PICO + the two facets biomedical questions actually add). Used for
# display, and to keep block names from being free-form
# prose that no two models ever spell the same way.
SLOTS = (
    "population",     # who/what is studied (species, patients, cells, cohort)
    "intervention",   # exposure, technique applied, treatment, manipulation
    "comparator",     # control condition (rarely searchable; usually omitted)
    "outcome",        # measured effect / phenomenon of interest
    "method",         # assay/measurement the question is specifically about
    "context",        # disease, setting, anatomical site when not the outcome
    "other",
)
SLOT_RANK = {s: i for i, s in enumerate(SLOTS)}

# Free-form block names -> canonical slot (only used when a model ignores the
# enum, or for blocks typed by a human in the UI).
_SLOT_HINTS: tuple[tuple[str, str], ...] = (
    ("population", "population"), ("participant", "population"),
    ("subject", "population"), ("species", "population"),
    ("animal", "population"), ("patient", "population"),
    ("cohort", "population"), ("organism", "population"),
    ("intervention", "intervention"), ("exposure", "intervention"),
    ("treatment", "intervention"), ("therap", "intervention"),
    ("stimulat", "intervention"), ("manipulat", "intervention"),
    ("drug", "intervention"),
    ("comparator", "comparator"), ("control", "comparator"),
    ("comparison", "comparator"), ("placebo", "comparator"),
    ("outcome", "outcome"), ("endpoint", "outcome"), ("effect", "outcome"),
    ("method", "method"), ("technique", "method"), ("assay", "method"),
    ("measurement", "method"), ("sequencing", "method"), ("imaging", "method"),
    ("context", "context"), ("setting", "context"), ("disease", "context"),
    ("condition", "context"), ("site", "context"),
)


def normalize_slot(raw: str) -> str:
    """Map a model- or human-supplied block label onto the canonical enum."""
    s = " ".join(str(raw or "").lower().split())
    if s in SLOT_RANK:
        return s
    for hint, slot in _SLOT_HINTS:
        if hint in s:
            return slot
    return "other"


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


def sort_terms(terms) -> list[str]:
    """Case-insensitive, case-tiebroken, de-duplicated stable term order."""
    seen: dict[str, str] = {}
    for t in terms:
        t = str(t).strip()
        if t and _norm(t) not in seen:
            seen[_norm(t)] = t
    return sorted(seen.values(), key=lambda s: (s.lower(), s))


# ---------------------------------------------------------------- resolution

def resolve_strict(ix: MeshIndex, candidate: str):
    """
    Resolve a proposed heading to exactly one descriptor, or None.

    Exact heading match first, then exact entry-term (synonym) match. NO
    substring/fuzzy fallback: a fuzzy hit is a guess, and a guess that silently
    enters the query is both irreproducible across models and unauditable. When
    several descriptors share a normalized string, the lowest DUI wins (stable).
    """
    cand = str(candidate or "").strip()
    if not cand:
        return None
    hits = ix.exact(cand) or ix.by_entry_term(cand)
    if not hits:
        return None
    return sorted(hits, key=lambda d: d.dui)[0]


def preferred_label(ix: MeshIndex, dui: str) -> str:
    """
    The heading string to actually put in the query.

    `descriptor.label` comes from rdfs:label, which in the 2025 dump has one
    non-English outlier (D002493 -> "Ziekte, centraalzenuwstelsel-"). PubMed
    only matches English headings, so a label that is not among the descriptor's
    entry terms in English form would silently retrieve nothing. Prefer the
    stored label, but fall back to the shortest ASCII entry term if the label is
    not ASCII.
    """
    d = ix.get(dui)
    if d is None:
        return ""
    if d.label.isascii():
        return d.label
    ascii_terms = [t for t in ix.entry_terms(dui) if t.isascii()]
    return ascii_terms[0] if ascii_terms else d.label


# ---------------------------------------------------------------- subsumption

def is_descendant(ix: MeshIndex, child: str, parent: str) -> bool:
    """True if every place `child` sits in the MeSH tree is under `parent`."""
    if child == parent:
        return False
    ct, pt = ix.trees(child), ix.trees(parent)
    if not ct or not pt:
        return False
    return all(any(c == p or c.startswith(p + ".") for p in pt) for c in ct)


def prune_subsumed(ix: MeshIndex, duis) -> list[str]:
    """
    Drop descendants of other members of the same OR block.

    `"Hippocampus"[MeSH Terms]` already retrieves CA1/CA3/Dentate Gyrus records
    server-side, so listing them adds zero recall and is a pure source of
    cross-model string divergence. Only safe when the block is exploded.
    """
    uniq = sorted(set(duis))
    return [d for d in uniq if not any(is_descendant(ix, d, o) for o in uniq if o != d)]


# ---------------------------------------------------------------- blocks

def _content_key(block: dict) -> tuple:
    """
    Canonical sort key for a block: its content, NOT its slot or name.

    Ordering by slot would reintroduce divergence whenever two models file the
    same content under different slots (they frequently do — "Hippocampus" is
    'context' to one model and 'population' to another). Content is the thing
    both models agree on.
    """
    mesh = [m.lower() for m in block.get("mesh", [])]
    ft = [t.lower() for t in block.get("freetext", [])]
    return (0 if mesh else 1, mesh[0] if mesh else (ft[0] if ft else ""), tuple(mesh), tuple(ft))


def canonicalize_blocks(blocks: list[dict], ix: MeshIndex, *,
                        prune: bool = True) -> dict:
    """
    Canonicalize proposed blocks into a deterministic, model-independent form.

    Steps (all deterministic):
      1. resolve every proposed heading EXACTLY (unresolvable ones are dropped
         and reported, never fuzzy-matched into the query)
      2. drop headings subsumed by a broader heading in the same block
      3. sort headings and free-text; drop empty blocks; drop blocks whose
         content duplicates another block
      4. sort blocks by content

    Returns {"blocks": [...], "dropped": [{"block","candidate"}...]}.
    """
    dropped: list[dict] = []
    staged: list[dict] = []

    for raw in blocks:
        name = str(raw.get("name", "")).strip()
        slot = normalize_slot(raw.get("slot") or name)
        explode = bool(raw.get("explode", True))

        duis: list[str] = []
        for cand in raw.get("mesh", []):
            d = resolve_strict(ix, cand)
            if d is None:
                dropped.append({"block": name, "candidate": str(cand), "reason": "no exact MeSH match"})
                continue
            duis.append(d.dui)
        if prune and explode and len(duis) > 1:
            kept = prune_subsumed(ix, duis)
            for d in sorted(set(duis) - set(kept)):
                dropped.append({"block": name, "candidate": preferred_label(ix, d),
                                "reason": "subsumed by a broader heading (auto-exploded)"})
            duis = kept

        headings = sort_terms(preferred_label(ix, d) for d in sorted(set(duis)))
        freetext = sort_terms(raw.get("freetext", []))
        if not headings and not freetext:
            continue
        staged.append({
            "name": name or (headings[0] if headings else freetext[0]),
            "slot": slot,
            "mesh": headings,
            "freetext": freetext,
            "explode": explode,
            "duis": sorted(set(duis)),
        })

    seen: set[tuple] = set()
    out: list[dict] = []
    for b in sorted(staged, key=_content_key):
        key = (tuple(b["mesh"]), tuple(b["freetext"]), b["explode"])
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return {"blocks": out, "dropped": dropped}
