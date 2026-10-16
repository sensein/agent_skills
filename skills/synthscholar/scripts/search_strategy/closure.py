"""
Deterministic MeSH closure over a facet span.

Given a facet's token span (a contiguous slice of the research question) and
NOTHING else, return every MeSH descriptor that exactly matches a phrase
inside that span, then prune descriptors subsumed by a broader descriptor
already found in the SAME facet (a narrower heading's "[MeSH Terms]" hits are
a strict subset of its broader ancestor's -- PubMed auto-explodes -- so
keeping both only inflates the query without adding recall).

No model ever sees this step: it is a pure function of
(question, facet span, MeSH index), so two runs over the SAME facet span
always produce the SAME headings. The only source of variance left anywhere
in this pipeline is facets.py deciding WHERE the spans are, not what they
resolve to -- which is the whole point of moving the model's job from
"choose vocabulary" to "mark a boundary".

Unlike a first-match strategy, every exact match inside the span is kept
(subject to subsumption pruning): this is deliberately recall-maximizing. A
facet spanning "RNA sequencing in the hippocampus" keeps both "Hippocampus"
and "Sequence Analysis, RNA" if both resolve, rather than picking one "best"
phrase and discarding the other.
"""
from __future__ import annotations

import re

from .mesh_index import Descriptor, MeshIndex

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-+]*")
MAX_NGRAM = 6

STOPWORDS = frozenset("""
a an the of to on at by is are was were be been being this that these those
their its his her our your my as it in with for and or vs versus what
""".split())

# Real MeSH descriptors that are nonetheless too generic to usefully OR into a
# facet on their own: matching "Patients" inside a population facet like
# "Alzheimer Disease patients" would OR "Patients"[MeSH Terms] into that
# block, and since nearly every clinical article mentions patients, that
# turns an AND-filtering block into a near-no-op. Only excluded as a
# STANDALONE (size==1) match; a multi-word phrase containing one of these
# words can still match normally.
GENERIC_TERMS = frozenset("""
patients patient subjects subject participants participant humans human
study studies research analysis analyses method methods effect effects
outcome outcomes result results group groups population populations
disease diseases condition conditions data sample samples
incidence prevalence frequency rate rates risk severity
""".split())


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _is_descendant(child: Descriptor, parent: Descriptor) -> bool:
    if child.dui == parent.dui or not child.trees or not parent.trees:
        return False
    return all(any(c == p or c.startswith(p + ".") for p in parent.trees)
               for c in child.trees)


def prune_subsumed(descs: list[Descriptor]) -> list[Descriptor]:
    """Drop any descriptor that is a MeSH-tree descendant of another
    descriptor already in the list (broader heading's explosion already
    covers it server-side, at zero query-length cost)."""
    keep = []
    for d in descs:
        if any(_is_descendant(d, other) for other in descs if other.dui != d.dui):
            continue
        keep.append(d)
    return keep


def resolve_span(ix: MeshIndex, tokens: list[str], lo: int, hi: int) -> dict:
    """
    Exhaustive resolution of every phrase inside tokens[lo:hi+1].

    Scans window sizes MAX_NGRAM..1, longest first; a window is only tried if
    it has at least one token not yet covered by a longer match, so a single
    dominant phrase ("magnetic resonance imaging") does not also spawn
    matches for each of its sub-words once it has already matched, but a
    facet made of two independent concepts glued into one span ("RNA
    sequencing in hippocampal neurons") still yields both.
    """
    n = hi - lo + 1
    covered = [False] * n
    found: dict[str, Descriptor] = {}
    matched_via: dict[str, str] = {}
    unmatched_words: set[int] = set()

    for size in range(min(MAX_NGRAM, n), 0, -1):
        for start in range(0, n - size + 1):
            window = range(start, start + size)
            if all(covered[i] for i in window):
                continue
            words = tokens[lo + start: lo + start + size]
            phrase = " ".join(words)
            low = phrase.lower()
            if size == 1 and (len(low) < 3 or low in STOPWORDS or low in GENERIC_TERMS):
                continue
            if words[0].lower() in STOPWORDS or words[-1].lower() in STOPWORDS:
                continue
            hits = ix.exact(phrase) or ix.by_entry_term(phrase)
            if not hits:
                continue
            for i in window:
                covered[i] = True
            for d in hits:
                found.setdefault(d.dui, d)
                matched_via.setdefault(d.dui, phrase)

    for i in range(n):
        if not covered[i] and tokens[lo + i].lower() not in STOPWORDS and len(tokens[lo + i]) >= 3:
            unmatched_words.add(lo + i)

    return {
        "descriptors": list(found.values()),
        "matched_via": matched_via,
        "unmatched_tokens": sorted(unmatched_words),
    }


def build_facet(ix: MeshIndex, question: str, facet: dict, *,
                explode: bool = True, max_freetext: int | None = None) -> dict:
    """
    facet: {"name", "role", "start", "end"} (token indices, inclusive) as
    produced by facets.segment(). Returns a concept block in the same shape
    the frontend already renders (mesh: list of {query, matched, options,
    selected_dui}, freetext: list[str]) so no UI changes are required.
    """
    tokens = tokenize(question)
    lo, hi = facet["start"], facet["end"]
    lo, hi = max(0, lo), min(len(tokens) - 1, hi)

    resolved = resolve_span(ix, tokens, lo, hi)
    kept = prune_subsumed(resolved["descriptors"])
    dropped_subsumed = [d for d in resolved["descriptors"] if d not in kept]
    kept.sort(key=lambda d: (d.label.lower(), d.dui))

    mesh_rows = [{
        "query": resolved["matched_via"].get(d.dui, d.label),
        "matched": True,
        "options": [d.to_dict()],
        "selected_dui": d.dui,
    } for d in kept]

    freetext = ix.strict_terms([d.dui for d in kept], explode=explode,
                               max_total=max_freetext) if kept else []

    unmatched_phrase = " ".join(tokens[i] for i in resolved["unmatched_tokens"]
                                 if lo <= i <= hi)
    span_phrase = " ".join(tokens[lo:hi + 1])
    pico_role = facet.get("role", "other")

    return {
        "name": facet.get("name") or span_phrase,
        # NOTE: "role" here is the PORTFOLIO role the frontend/app._portfolio()
        # already understands (required|optional|contextual) -- every facet
        # starts required, matching prior behaviour; the PICO classification
        # (population/intervention/...) lives in "pico_role" instead, so it
        # can never silently drop a concept out of portfolio compilation.
        "role": "required",
        "pico_role": pico_role,
        "rationale": (f'[{pico_role}] deterministic closure over span "{span_phrase}"'
                      + (f"; unresolved words: {unmatched_phrase}" if unmatched_phrase else "")
                      + (f"; {len(dropped_subsumed)} subsumed heading(s) pruned"
                         if dropped_subsumed else "")),
        "explode": explode,
        "mesh": mesh_rows,
        "freetext": freetext,
        "unmatched": unmatched_phrase,
        "dropped_subsumed": [d.label for d in dropped_subsumed],
    }


def build_concepts(ix: MeshIndex, question: str, facets: list[dict], *,
                   explode: bool = True, max_freetext: int | None = None) -> list[dict]:
    return [build_facet(ix, question, f, explode=explode, max_freetext=max_freetext)
            for f in facets]
