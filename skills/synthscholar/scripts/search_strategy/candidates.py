"""
Deterministic MeSH span matching over the question text (`mesh_only` mode).

Given the raw question we find, with no LLM involved, every MAXIMAL word span
that exactly matches a MeSH heading or entry term (synonym). Each match
becomes its own ANDed block, so the result is a pure function of
(question, MeSH index): the model-free baseline, and the fallback when no
model is available.
"""
from __future__ import annotations

import re

from .mesh_index import MeshIndex

# Words that are never a search facet on their own. Several are real MeSH
# headings ("Models, Theoretical" has the entry term "model"), so without this
# list a question like "...detection methods" would AND a useless block and
# throttle recall to near zero.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been
before being below between both but by can cannot could did do does doing down during
each few for from further had has have having he her here hers him his how i if in
into is it its itself just me more most my no nor not of off on once only or other
our out over own same she should so some such than that the their them then there
these they this those through to too under until up very was we were what when where
which while who whom why will with would you your
""".split())

# Generic research/discourse nouns: they DO resolve to MeSH descriptors but are
# almost never the facet the author means.
GENERIC_TERMS = frozenset("""
analysis approach approaches assessment characterization comparison control data
detection development effect effects evaluation evidence experiment experiments
factor factors finding findings function group groups identification impact
improvement influence level levels measure measurement measurements mechanism
mechanisms method methods model models outcome outcomes pattern patterns
performance population populations problem process processes protocol protocols
research response responses result results review reviews role sample samples
strategy strategies study studies system systems technique techniques test tests
treatment type types use validation variation
""".split())

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-+]*")
MAX_NGRAM = 6


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _variants(phrase: str) -> list[str]:
    """
    Surface variants to try against the index, in a fixed order.

    MeSH stores headings in singular, inverted form ("Memory, Short-Term"), but
    entry terms carry the natural-order and plural forms, so a light
    singular/plural pass is enough and stays deterministic.
    """
    out = [phrase]
    low = phrase.lower()
    if "-" in phrase:            # "Alzheimer's-disease" style joins, "RNA-Seq"
        out.append(phrase.replace("-", " "))
    if low.endswith("ies") and len(low) > 4:
        out.append(phrase[:-3] + "y")
    if low.endswith("ses") and len(low) > 4:
        out.append(phrase[:-2])
    if low.endswith("s") and not low.endswith("ss"):
        out.append(phrase[:-1])
    else:
        out.append(phrase + "s")
    seen, uniq = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def _lookup(ix: MeshIndex, phrase: str):
    """Exact heading or exact entry-term hit for a phrase (or one of its
    singular/plural variants). Lowest DUI wins so ties are stable."""
    for v in _variants(phrase):
        hits = ix.exact(v) or ix.by_entry_term(v)
        if hits:
            return sorted(hits, key=lambda d: d.dui)[0], v
    # Technical compounds ("CRISPR-Cas9", "AAV-PHP.eB") are one token that MeSH
    # never carries verbatim, but a hyphen part usually is a heading ("CRISPR").
    # Restricted to compounds with a digit or an internal capital so ordinary
    # hyphenations ("single-cell" -> "single") are left alone.
    if "-" in phrase and " " not in phrase and (
        any(ch.isdigit() for ch in phrase) or any(ch.isupper() for ch in phrase[1:])
    ):
        for part in sorted(phrase.split("-"), key=lambda p: (-len(p), p)):
            if len(part) < 4 or part.lower() in STOPWORDS or part.lower() in GENERIC_TERMS:
                continue
            hits = ix.exact(part) or ix.by_entry_term(part)
            if hits:
                return sorted(hits, key=lambda d: d.dui)[0], part
    return None, ""


def matched_spans(question: str, ix: MeshIndex) -> list[dict]:
    """
    Every MAXIMAL word span of the question that resolves to a descriptor.

    Longest-match-first, left to right: once "memory consolidation" matches, the
    contained spans "memory" and "consolidation" are not considered again. Order
    of the returned list follows the question, which keeps the result readable.
    """
    toks = _tokens(question)
    n = len(toks)
    taken = [False] * n
    spans: list[dict] = []
    for size in range(min(MAX_NGRAM, n), 0, -1):
        for i in range(0, n - size + 1):
            if any(taken[i:i + size]):
                continue
            words = toks[i:i + size]
            phrase = " ".join(words)
            low = phrase.lower()
            if size == 1:
                if len(low) < 4 or low in STOPWORDS or low in GENERIC_TERMS:
                    continue
            elif all(w.lower() in STOPWORDS for w in words):
                continue
            if words[0].lower() in STOPWORDS or words[-1].lower() in STOPWORDS:
                continue
            desc, variant = _lookup(ix, phrase)
            if desc is None:
                continue
            for k in range(i, i + size):
                taken[k] = True
            spans.append({"span": phrase, "matched_as": variant, "start": i,
                          "size": size, "dui": desc.dui, "label": desc.label})
    spans.sort(key=lambda s: s["start"])
    return spans


def mesh_only_blocks(question: str, ix: MeshIndex) -> list[dict]:
    """
    LLM-free baseline: one ANDed block per maximal matched span.

    Fully deterministic and model-independent by construction (cross-model
    agreement is 1.0 trivially), at the cost of the judgement an LLM adds — it
    cannot drop an irrelevant match or add a facet the question only implies.
    """
    spans = matched_spans(question, ix)
    blocks: list[dict] = []
    for s in spans:
        blocks.append({
            "name": s["span"],
            "slot": "other",
            "mesh": [s["label"]],
            "freetext": [],
            "explode": True,
        })
    return blocks
