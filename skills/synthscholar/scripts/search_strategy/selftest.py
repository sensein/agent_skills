#!/usr/bin/env python
"""
Self-test for the deterministic layer. No network, no LLM, no pytest.

Pins the rules that cross-model agreement depends on, so a later change to
canonical.py / candidates.py cannot quietly undo them.

    python scripts/search_strategy/selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # scripts/
sys.path.insert(0, str(ROOT))

from search_strategy import canonical, pipeline, query_builder            # noqa: E402
from search_strategy.candidates import matched_spans, mesh_only_blocks    # noqa: E402
from search_strategy.mesh_index import get_index                          # noqa: E402

Q1 = ("Does optogenetic stimulation of the hippocampus improve memory "
      "consolidation in rodent models?")
Q2 = "single-cell RNA sequencing of microglia in Alzheimer's disease"
Q3 = "CRISPR-Cas9 off-target effects detection methods"

failures: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}   {detail}")
        failures.append(name)


def main() -> int:
    ix = get_index()

    print("MeSH index")
    check("english label for D002493",
          ix.get("D002493").label == "Central Nervous System Diseases",
          repr(ix.get("D002493").label))
    check("no non-ASCII descriptor labels leak into headings",
          all(canonical.preferred_label(ix, d).isascii()
              for d in ("D002493", "D006624", "D009474")))

    print("\nexact-only resolution")
    check("real heading resolves", canonical.resolve_strict(ix, "Hippocampus") is not None)
    check("entry term resolves", canonical.resolve_strict(ix, "RNA Sequencing") is not None)
    check("hallucination is dropped, not fuzzy-matched",
          canonical.resolve_strict(ix, "Hippocampal Memory Consolidation Circuitry") is None)
    # "Hippocamp" is a substring of a real heading, so the old ladder's LIKE tier
    # would have auto-accepted "Hippocampus" for it; exact-only must not.
    check("fuzzy substring is NOT accepted",
          canonical.resolve_strict(ix, "Hippocamp") is None
          and any(d.label == "Hippocampus" for d in ix.search("Hippocamp", limit=8)))

    print("\nsubsumption pruning")
    hipp = [d.dui for d in (canonical.resolve_strict(ix, h) for h in
                            ("Hippocampus", "CA1 Region, Hippocampal",
                             "CA3 Region, Hippocampal", "Dentate Gyrus")) if d]
    kept = canonical.prune_subsumed(ix, hipp)
    check("descendants of Hippocampus are pruned", kept == [hipp[0]],
          f"kept={[canonical.preferred_label(ix, d) for d in kept]}")
    rodents = [d.dui for d in (canonical.resolve_strict(ix, h) for h in
                               ("Rodentia", "Mice", "Rats")) if d]
    check("Mice/Rats collapse into Rodentia",
          canonical.prune_subsumed(ix, rodents) == [rodents[0]])
    check("unrelated headings both survive",
          len(canonical.prune_subsumed(ix, [d.dui for d in (
              canonical.resolve_strict(ix, "Microglia"),
              canonical.resolve_strict(ix, "Alzheimer Disease"))])) == 2)

    print("\ncanonical ordering (same content, different proposal order -> same query)")
    a = [{"name": "pop", "slot": "population", "mesh": ["Rodentia"], "freetext": ["mouse"]},
         {"name": "int", "slot": "intervention", "mesh": ["Optogenetics"], "freetext": []}]
    b = [{"name": "intervention", "slot": "intervention", "mesh": ["Optogenetics"], "freetext": []},
         {"name": "animals", "slot": "population", "mesh": ["Rodentia", "Mice"], "freetext": ["Mouse"]}]
    qa = query_builder.compile_search(
        pipeline.finalize(canonical.canonicalize_blocks(a, ix)["blocks"], strict=True, ix=ix), {})
    qb = query_builder.compile_search(
        pipeline.finalize(canonical.canonicalize_blocks(b, ix)["blocks"], strict=True, ix=ix), {})
    check("two orderings compile to the same hash", qa["hash"] == qb["hash"],
          f"{qa['hash']} vs {qb['hash']}")

    print("\nspan matching (mesh_only)")
    spans = {s["span"].lower(): s["label"] for s in matched_spans(Q1, ix)}
    check("longest match wins ('memory consolidation', not 'memory')",
          spans.get("memory consolidation") == "Memory Consolidation", str(spans))
    check("plural/singular ('rodent' -> Rodentia)", spans.get("rodent") == "Rodentia")
    check("generic words never become blocks",
          not {"Disease", "Models, Theoretical"} & {b["mesh"][0] for b in mesh_only_blocks(Q3, ix)})

    print("\nno model prose reaches a strict query")
    a = [{"name": "sequencing", "slot": "method", "mesh_candidates": ["RNA Sequencing"],
          "freetext": ["scRNA-seq", "single cell transcriptomics"]}]
    b = [{"name": "rna-seq assay", "slot": "context", "mesh_candidates": ["RNA Sequencing"],
          "freetext": []}]
    def compile_llm(concepts):
        res = pipeline._assemble(pipeline._blocks_from_concepts(concepts), ["bioinformatics"],
                                 strict=True, ix=ix)
        return query_builder.compile_search(res["concepts"], {})["hash"]
    check("differing block names/free-text do not change the query (llm mode)",
          compile_llm(a) == compile_llm(b), f"{compile_llm(a)} vs {compile_llm(b)}")

    print("\nmesh_only determinism")
    h = {query_builder.compile_search(
        pipeline.build(Q1, mode="mesh_only")["concepts"], {})["hash"] for _ in range(3)}
    check("three builds -> one hash", len(h) == 1, str(h))

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
