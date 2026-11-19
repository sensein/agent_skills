"""Full-document text windowing for the per-article LLM stages.

Before this module, every per-article agent saw a hard prefix of the article
body (``full_text[:2000]``–``[:4000]``) — roughly the title page and
introduction. Results, Discussion and Conclusion, where a paper actually
reports what it found, were never in the prompt. Two primitives fix that:

* :func:`chunk_text` — sequential, slightly overlapping chunks that cover the
  **whole** document. Used by evidence extraction, which must see every
  section: one agent call per chunk, spans merged afterwards.
* :func:`window_text` — a single budgeted excerpt that keeps the **head and
  the tail** of the document (plus an explicit omission marker) for stages
  that get one prompt per article (risk of bias, data extraction, charting).

Both split on paragraph boundaries when one is available near the cut, so a
chunk rarely starts or ends mid-sentence.
"""

from __future__ import annotations

# Defaults; overridable per run via ReviewProtocol.
DEFAULT_CHUNK_CHARS = 12000
DEFAULT_CHUNK_OVERLAP = 400
DEFAULT_ARTICLE_BUDGET = 16000

# How far back from a target cut point to look for a paragraph break.
_BOUNDARY_LOOKBACK = 0.2


def _boundary_before(text: str, end: int) -> int:
    """Nearest paragraph/sentence break at or before *end*, else *end*."""
    if end >= len(text):
        return len(text)
    window = max(1, int(end * _BOUNDARY_LOOKBACK))
    start = max(0, end - window)
    for sep in ("\n\n", "\n", ". "):
        idx = text.rfind(sep, start, end)
        if idx > 0:
            return idx + len(sep)
    return end


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_chars: int = 0,
) -> list[str]:
    """Split *text* into overlapping chunks covering the whole document.

    Args:
        text:        the document body (empty input yields an empty list).
        chunk_chars: target characters per chunk.
        overlap:     characters of the previous chunk repeated at the start of
                     the next, so a finding spanning a cut is still readable.
        max_chars:   optional hard cap on how much of *text* is considered at
                     all (0 = the entire document).

    Returns:
        Chunks in document order. A document shorter than *chunk_chars*
        returns a single chunk.
    """
    body = (text or "").strip()
    if not body:
        return []
    if max_chars > 0:
        body = body[:max_chars]
    chunk_chars = max(1000, int(chunk_chars))
    overlap = max(0, min(int(overlap), chunk_chars // 2))

    chunks: list[str] = []
    pos = 0
    n = len(body)
    while pos < n:
        end = _boundary_before(body, min(pos + chunk_chars, n))
        if end <= pos:                      # pathological boundary — hard cut
            end = min(pos + chunk_chars, n)
        chunks.append(body[pos:end].strip())
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return [c for c in chunks if c]


def window_text(text: str, budget: int = DEFAULT_ARTICLE_BUDGET) -> str:
    """Return at most *budget* characters of *text*, keeping head **and** tail.

    A plain prefix drops exactly the sections that report findings. This keeps
    the opening (aims, methods) and the closing (results, discussion,
    conclusion) and states plainly how much was omitted, so the model knows
    the body is not contiguous.
    """
    body = (text or "").strip()
    budget = max(1000, int(budget))
    if len(body) <= budget:
        return body

    head_len = int(budget * 0.6)
    tail_len = budget - head_len
    head = body[:_boundary_before(body, head_len)]
    tail = body[len(body) - tail_len:]
    # Start the tail at a paragraph break so it doesn't begin mid-sentence.
    offset = _boundary_before(tail, min(len(tail), int(tail_len * 0.15)))
    tail = tail[offset:]
    omitted = len(body) - len(head) - len(tail)
    return f"{head}\n\n[… {omitted} characters omitted from the middle …]\n\n{tail}"
