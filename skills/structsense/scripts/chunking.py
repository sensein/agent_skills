"""Sentence-aligned chunking for long-document extraction.

Splits text on sentence boundaries, accumulating sentences until the chunk
would exceed `max_chars`. Returns chunks with their character offset in the
original text so per-chunk extraction results can be re-anchored to global
offsets after merging.

Two backends:
- spaCy (preferred — better sentence segmentation on scientific text)
- regex fallback (no extra dependency)

Adapted from the structsense pipeline pattern.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple, Dict, Any

# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

# Very basic English sentence splitter. Good enough for chunking; do not use
# for linguistic analysis.
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'\[])")

def _sentences_regex(text: str) -> List[Tuple[int, int, str]]:
    """Return [(start, end, sentence_text)] from the start of `text`."""
    spans: List[Tuple[int, int, str]] = []
    cursor = 0
    for match in _SENT_RE.finditer(text):
        end = match.start()
        if end > cursor:
            spans.append((cursor, end, text[cursor:end]))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text), text[cursor:]))
    return spans


def _sentences_spacy(text: str, nlp) -> List[Tuple[int, int, str]]:
    doc = nlp(text)
    return [(s.start_char, s.end_char, s.text) for s in doc.sents]


def sentence_spans(text: str, nlp=None) -> List[Tuple[int, int, str]]:
    """Split text into sentence spans. Pass an initialized spaCy `nlp` to use
    spaCy; otherwise falls back to a regex splitter.
    """
    if nlp is not None:
        return _sentences_spacy(text, nlp)
    return _sentences_regex(text)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_by_sentences(text: str, max_chars: int = 2000, nlp=None) -> List[Dict[str, Any]]:
    """Greedy sentence-aligned chunking.

    Args:
        text: source text.
        max_chars: maximum characters per chunk.
        nlp: optional spaCy `Language` object for sentence splitting.

    Returns:
        List of {"text": str, "start": int}. `start` is the chunk's character
        offset in the original `text`. The concatenation of all chunk texts
        (with inter-sentence whitespace) reconstructs `text`.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [{"text": text, "start": 0}]

    spans = sentence_spans(text, nlp=nlp)
    if not spans:
        return [{"text": text, "start": 0}]

    chunks: List[Dict[str, Any]] = []
    cur_start: int | None = None
    cur_end: int | None = None

    for s_start, s_end, _ in spans:
        if cur_start is None:
            cur_start, cur_end = s_start, s_end
            continue
        if s_end - cur_start <= max_chars:
            cur_end = s_end
        else:
            chunks.append({"text": text[cur_start:cur_end], "start": cur_start})
            cur_start, cur_end = s_start, s_end

    if cur_start is not None:
        chunks.append({"text": text[cur_start:cur_end], "start": cur_start})

    return chunks


def reanchor_items(items: Iterable[dict], chunk_start: int,
                   start_key: str = "start", end_key: str = "end") -> List[dict]:
    """Add `chunk_start` to every item's start/end so they reference the
    original full text instead of the chunk.
    """
    out = []
    for it in items:
        it = dict(it)
        if start_key in it:
            it[start_key] = it[start_key] + chunk_start
        if end_key in it:
            it[end_key] = it[end_key] + chunk_start
        out.append(it)
    return out


def dedupe(items: Iterable[dict], key_fields=("entity", "start", "end")) -> List[dict]:
    """Deduplicate by tuple of field values. Preserves first occurrence."""
    seen = set()
    out = []
    for it in items:
        k = tuple(it.get(f) for f in key_fields)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


if __name__ == "__main__":
    sample = (
        "The hippocampus is a brain region. Studies in mice show its role in memory. "
        "BDNF is upregulated. CA1 pyramidal cells fire in response to novelty."
    ) * 50
    chunks = chunk_by_sentences(sample, max_chars=400)
    print(f"{len(chunks)} chunks")
    for i, c in enumerate(chunks):
        print(f"[{i}] start={c['start']} chars={len(c['text'])}: {c['text'][:60]}...")
