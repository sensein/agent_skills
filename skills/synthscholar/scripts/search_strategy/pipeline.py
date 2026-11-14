"""
The question -> concept-blocks pipeline, in three modes.

One entry point shared by the CLI (generate_search_strategy.py) and the
self-test, so what is tested is exactly what runs.

  closure    (default) The LLM never sees MeSH at all. It only marks WHERE each
             PICO-style concept sits in the question text, sampled k times and
             kept only where a majority of samples agree (self-consistency
             voting -- facets.py). Vocabulary is then resolved afterward as a
             pure function of (span, MeSH index) -- closure.py -- so the
             model's output space never includes a MeSH heading at all.
             Measured in search-strategy-generator's eval/ (within-model,
             cross-model, heading Jaccard, PMID Jaccard): 0.97 / 0.97 / 1.00 /
             0.84 with Claude Haiku 4.5 + Sonnet 5 on 6 questions x 3 runs.
             It replaces that project's `hybrid` mode (0.90 / 0.76 / 0.95 /
             0.68 on the same metrics), which is not carried over.
  llm        The LLM proposes blocks and headings freely (prompt v2). Widest
             output space; kept for comparison, not recommended.
  mesh_only  No LLM at all: every maximal MeSH match in the question becomes a
             block. Trivially model-independent; the baseline and the fallback.

Whatever the mode, the output passes through canonical.canonicalize_blocks
(exact-only resolution, subsumption pruning, canonical ordering) before it can
reach the query builder.
"""
from __future__ import annotations

from . import canonical, facet_pipeline
from .candidates import mesh_only_blocks
from .domain_vocab import get_vocab
from .mesh_index import get_index
from .openrouter_client import map_question, parse_proposal

MODES = ("closure", "llm", "mesh_only")


def _check_mode(mode: str) -> str:
    mode = (mode or "closure").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return mode


# ---------------------------------------------------------------- shaping

def _blocks_from_concepts(concepts: list[dict]) -> list[dict]:
    """LLM proposal (free-form) -> canonicaliser input."""
    return [{
        "name": c.get("name", ""),
        "slot": c.get("slot", "") or c.get("name", ""),
        "mesh": c.get("mesh_candidates", []) or c.get("mesh", []),
        "freetext": c.get("freetext", []),
        "explode": True,
        "rationale": c.get("rationale", ""),
    } for c in concepts]


def _blocks_from_closure(concepts: list[dict]) -> list[dict]:
    """
    closure mode (facet_pipeline.build) -> canonicaliser input.

    facet_pipeline's concepts already carry resolved MeSH rows (each with its
    own single, exactly-matched descriptor -- see closure.py), so this is a
    reshape, not a resolution step: pull the label out of each matched row and
    drop the ones the frontend/UI-review shape carries that canonicalize_blocks
    doesn't need (pico_role, unmatched, dropped_subsumed). Re-running these
    through canonicalize_blocks below is redundant with what closure.py already
    did (both do exact-match resolution + subsumption pruning) but harmless --
    it keeps `closure` mode on the exact same _assemble()/finalize() path as
    the other modes instead of a special-cased one.
    """
    out = []
    for c in concepts:
        mesh_labels = [m["options"][0]["label"] for m in c.get("mesh", [])
                       if m.get("matched") and m.get("options")]
        out.append({
            "name": c.get("name", ""),
            "slot": c.get("pico_role", "") or c.get("name", ""),
            "mesh": mesh_labels,
            "freetext": c.get("freetext", []),
            "explode": bool(c.get("explode", True)),
            "rationale": c.get("rationale", ""),
        })
    return out


def _vocab_terms(block: dict, domains: list[str] | None, *,
                 use_model_text: bool = False) -> list[str]:
    """
    Domain-vocabulary synonyms that apply to a block (deterministic, file-driven).

    Kept separate from the model's free-text so strict mode can drop the model's
    prose while keeping this — it is as reproducible as the MeSH index is.

    use_model_text=False (strict mode) matches only against the block's MeSH
    headings. Matching against the model's block NAME and free-text was a real
    determinism leak: two models that selected identical headings still compiled
    different queries (153 vs 145 terms) because their differing prose fired
    different vocabulary clusters.
    """
    parts = list(block.get("mesh", []))
    if use_model_text:
        parts += [block.get("name", "")] + list(block.get("freetext", []))
    hay = " ".join(parts).lower()
    out: list[str] = []
    for cl in get_vocab().clusters(domains or None):
        if cl.concept.lower() in hay or any(s.lower() in hay for s in cl.synonyms):
            out.extend(cl.synonyms)
    return canonical.sort_terms(out)


def finalize(blocks: list[dict], *, strict: bool = True, ix=None) -> list[dict]:
    """
    Blocks -> compile-shape concepts for query_builder.

    strict=True derives every free-text term for a block WITH MeSH headings from
    the MeSH index itself (deterministic), keeping only the domain-vocabulary
    additions from the original free-text. Blocks with no resolvable heading keep
    their free-text — that is how non-MeSH jargon survives.
    """
    ix = ix or get_index()
    out = []
    for b in blocks:
        duis = list(b.get("duis") or [])
        if not duis:                     # UI-supplied blocks carry labels only
            for h in b.get("mesh", []):
                d = canonical.resolve_strict(ix, h)
                if d is not None:
                    duis.append(d.dui)
        freetext = list(b.get("freetext", []))
        keep = list(b.get("vocab_freetext", []))
        if strict and duis:
            freetext = canonical.sort_terms(
                ix.strict_terms(sorted(set(duis)), explode=bool(b.get("explode", True))) + keep)
        else:
            freetext = canonical.sort_terms(freetext + keep)
        out.append({
            "name": b.get("name", ""),
            "slot": b.get("slot", "other"),
            "mesh": list(b.get("mesh", [])),
            "freetext": freetext,
            "explode": bool(b.get("explode", True)),
        })
    return out


def _assemble(blocks: list[dict], domains: list[str] | None, *, strict: bool, ix) -> dict:
    can = canonical.canonicalize_blocks(blocks, ix, prune=True)
    for b in can["blocks"]:
        b["vocab_freetext"] = _vocab_terms(b, domains, use_model_text=not strict)
    return {
        "blocks": can["blocks"],
        "dropped": can["dropped"],
        "concepts": finalize(can["blocks"], strict=strict, ix=ix),
    }


# ---------------------------------------------------------------- entry points

def build(question: str, *, domains: list[str] | None = None, mode: str = "closure",
          model: str | None = None, api_key: str | None = None,
          extra_context: str = "", seed: int | None = None,
          strict: bool = True,
          facet_runs: int = 3, facet_samples: list[str] | None = None,
          llm_response: str | None = None) -> dict:
    """Question -> canonical concept blocks (CLI / self-test).

    facet_samples (closure) / llm_response (llm): the model's answers when the
    host agent produced them itself; then no model is called here.
    """
    mode = _check_mode(mode)
    ix = get_index()
    raw: dict = {}
    notes = ""
    seg: dict = {}

    if mode == "mesh_only":
        blocks = mesh_only_blocks(question, ix)
    elif mode == "llm":
        if llm_response is not None:
            raw = parse_proposal(llm_response, model or "")
        else:
            raw = map_question(question, domains=domains, model=model, api_key=api_key,
                               extra_context=extra_context, seed=seed)
        notes = raw.get("notes", "")
        blocks = _blocks_from_concepts(raw["concepts"])
    else:
        fp = facet_pipeline.build(question, ix, model=model, api_key=api_key, k=facet_runs,
                                  samples=facet_samples)
        notes = fp["notes"]
        seg = fp["segmentation"]
        blocks = _blocks_from_closure(fp["concepts"])

    res = _assemble(blocks, domains, strict=strict, ix=ix)
    return {**res, "mode": mode, "model": raw.get("model") or model or "",
            "prompt_version": "v2" if mode == "llm" else mode,
            "notes": notes, "raw": raw, "segmentation": seg}
