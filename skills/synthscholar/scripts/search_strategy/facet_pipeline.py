"""
Wires facets.py (self-consistency span voting) + closure.py (deterministic
MeSH closure) into the same {"concepts", "notes", "model"} shape
openrouter_client.map_question already returns, so the CLI's
`resolve_concepts`/frontend contract needs no change to support a new mode.

Mode name: "closure". Kept alongside the existing single-shot "llm" mode
(openrouter_client.py) for side-by-side comparison -- see search-strategy-generator's eval/.
"""
from __future__ import annotations

from . import closure, facets
from .mesh_index import MeshIndex

# Words this small on their own don't carry search-relevant meaning; used
# only to decide whether an uncovered stretch of the question is worth
# surfacing as a coverage gap, not to filter what closure.py resolves.
_SKIP = closure.STOPWORDS | closure.GENERIC_TERMS


def coverage_gaps(question: str, voted_facets: list[dict]) -> list[dict]:
    """
    Find contiguous runs of question tokens that fall OUTSIDE every voted
    facet span -- i.e. text closure.py never got a chance to look at,
    because facets.py's segmentation (heuristic or self-consistency voting)
    dropped it. This is the fix for the gap described in
    closure.py's module docstring: "closure is exhaustive within a span it
    is given, but the pipeline is only as exhaustive as the union of spans
    it is given" -- previously that union was never checked.

    Returns one {"start", "end"} range per contiguous uncovered run that
    contains at least one token worth searching on (not a stopword/generic
    term) -- pure text-range detection, no MeSH lookup here.
    """
    tokens = closure.tokenize(question)
    n = len(tokens)
    covered = [False] * n
    for f in voted_facets:
        for i in range(max(0, f["start"]), min(n - 1, f["end"]) + 1):
            covered[i] = True

    gaps: list[dict] = []
    i = 0
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j < n and not covered[j]:
            j += 1
        if any(tokens[t].lower() not in _SKIP and len(tokens[t]) >= 3 for t in range(i, j)):
            gaps.append({"start": i, "end": j - 1})
        i = j
    return gaps


def resolve_coverage_gaps(ix: MeshIndex, question: str, voted_facets: list[dict],
                          *, gaps: list[dict] | None = None, explode: bool = True,
                          max_freetext: int | None = None) -> list[dict]:
    """
    For every coverage gap, run the SAME deterministic closure engine used
    for voted facets over the gap's token range, and tag the resulting block
    so a reviewer can see it was never chosen by segmentation. Unlike
    silently dropping this text, an unreviewed gap block defaults to
    role="required" (visible, not opted out of the query) -- the reviewer
    can demote or remove it in the UI same as any other block, but it starts
    counted rather than starting lost.

    Pass `gaps` if the caller already computed coverage_gaps(question,
    voted_facets) -- e.g. _finish() below needs the gap count for its notes
    string regardless, so it computes gaps once and hands them here instead
    of this function silently recomputing the same (cheap, but still
    redundant) scan.
    """
    if gaps is None:
        gaps = coverage_gaps(question, voted_facets)
    out = []
    for gap in gaps:
        facet = {"name": None, "role": "other", **gap}
        block = closure.build_facet(ix, question, facet, explode=explode,
                                    max_freetext=max_freetext)
        block["rationale"] = "[coverage gap: outside every voted facet span] " + block["rationale"]
        out.append(block)
    return out


def concepts_for_compile(concepts: list[dict]) -> list[dict]:
    """
    Convert the UI-facing concept shape (mesh: list of {matched, options,
    selected_dui, ...}) into the plain {name, mesh: [label,...], freetext}
    shape query_builder.compile_search expects -- the same transform
    frontend/app.js:collectConcepts() does before calling /api/compile.
    Shared by the eval harness and any other non-UI caller so this contract
    is defined in exactly one place.
    """
    out = []
    for c in concepts:
        mesh_labels = [m["options"][0]["label"] for m in c.get("mesh", [])
                       if m.get("matched") and m.get("options")]
        oc = {"name": c.get("name", ""), "explode": c.get("explode", True),
              "mesh": mesh_labels, "freetext": c.get("freetext", [])}
        if oc["mesh"] or oc["freetext"]:
            out.append(oc)
    return out


def _finish(question: str, ix: MeshIndex, seg: dict, model: str | None, *,
           min_agreement: float, explode: bool, max_freetext: int | None) -> dict:
    concepts = closure.build_concepts(ix, question, seg["facets"], explode=explode,
                                      max_freetext=max_freetext)
    gaps = coverage_gaps(question, seg["facets"])
    gap_blocks = resolve_coverage_gaps(ix, question, seg["facets"], gaps=gaps,
                                       explode=explode, max_freetext=max_freetext) if gaps else []
    concepts.extend(gap_blocks)
    notes = (f"facet segmentation mode={seg['mode']} runs={seg['runs']}"
             + (f" agreement>={min_agreement}" if seg["mode"] == "llm_voted" else "")
             + (f"; {len(gaps)} coverage gap(s) outside every voted facet -- "
                f"resolved separately and appended as extra block(s)" if gaps else ""))
    return {"concepts": concepts, "notes": notes,
            "model": model or "closure/heuristic", "segmentation": seg,
            "coverage_gaps": gaps}


def build(question: str, ix: MeshIndex, *, model: str | None = None,
         api_key: str | None = None, k: int = 3, use_llm: bool = True,
         min_agreement: float = 0.5, explode: bool = True,
         max_freetext: int | None = None,
         samples: list[str] | None = None) -> dict:
    """samples: the k segmentation answers, when the host agent produced them
    (facets.segment_from_samples); otherwise the model is called here."""
    if samples is not None:
        seg = facets.segment_from_samples(question, samples, min_agreement)
    else:
        seg = facets.segment(question, model=model, api_key=api_key, k=k,
                             use_llm=use_llm, min_agreement=min_agreement)
    return _finish(question, ix, seg, model, min_agreement=min_agreement,
                   explode=explode, max_freetext=max_freetext)
