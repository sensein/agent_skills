"""Mask-mode passes for NER recall and verification.

Two functions:

- mask_for_recall(text, items)
    Replace every extracted span in `text` with a placeholder of the form
    [E<i>], padded to preserve character offsets. Use the resulting masked
    text as input to the mask-recall-pass.md prompt. Returns (masked_text,
    placeholder_map) where placeholder_map records what each [En] replaced.

- mask_one_for_verify(text, item, context_chars=300)
    Replace just `text[item.start:item.end]` with `[MASK]`, padded to keep
    offsets. Return the masked sentence + surrounding context for use with
    mask-verify-pass.md.

- map_masked_offsets_to_original(masked_text, items, placeholder_map)
    After the mask-recall pass returns new items with offsets in the
    MASKED text, translate those offsets back to the ORIGINAL text.

These are pure functions — they do not call any LLM. Plug them into
your own driver alongside scripts/llm_client.py and scripts/json_repair.py.
"""
from __future__ import annotations

from typing import Iterable


def _placeholder(i: int, width: int) -> str:
    """Return a placeholder string [E<i>] padded with spaces to `width` chars."""
    raw = f"[E{i}]"
    if len(raw) > width:
        # Span is shorter than the placeholder; we can't preserve offsets in
        # this case. Caller should detect and treat masked_text offsets as
        # approximate.
        return raw
    pad = width - len(raw)
    left = pad // 2
    right = pad - left
    return " " * left + raw + " " * right


def mask_for_recall(text: str, items: list[dict],
                    span_keys: tuple[str, str, str] = ("entity", "start", "end")
                    ) -> tuple[str, list[dict]]:
    """Replace every (start, end) span with a padded placeholder.

    Items can carry either 'entity' or 'term' as their surface field; pass
    the relevant span_keys triple.

    Returns:
        masked_text: original-length string with spans replaced.
        placeholder_map: list of {"placeholder", "entity", "label",
                                  "orig_start", "orig_end", "masked_start",
                                  "masked_end"}.
    """
    surface_key, start_key, end_key = span_keys

    # Sort spans by start ascending; resolve overlaps by keeping the longest.
    spans = sorted(
        ((it[start_key], it[end_key], it) for it in items
         if it.get(start_key) is not None and it.get(end_key) is not None),
        key=lambda s: (s[0], -(s[1] - s[0])),
    )
    chosen: list[tuple[int, int, dict]] = []
    last_end = -1
    for s, e, it in spans:
        if s < last_end:
            continue  # overlap with already-chosen longer span
        chosen.append((s, e, it))
        last_end = e

    # Build the masked text in one pass.
    masked: list[str] = []
    pmap: list[dict] = []
    cursor = 0
    for i, (s, e, it) in enumerate(chosen):
        masked.append(text[cursor:s])
        ph = _placeholder(i, e - s)
        masked.append(ph)
        # masked_text offset == original offset because length is preserved
        # when the placeholder fits; otherwise we record both.
        masked_start = sum(len(p) for p in masked) - len(ph)
        masked_end = masked_start + len(ph)
        pmap.append({
            "placeholder": ph.strip(),
            "entity":      it.get(surface_key),
            "label":       it.get("label"),
            "orig_start":  s,
            "orig_end":    e,
            "masked_start": masked_start,
            "masked_end":   masked_end,
        })
        cursor = e
    masked.append(text[cursor:])

    return "".join(masked), pmap


def map_masked_offsets_to_original(masked_text: str,
                                   new_items: Iterable[dict],
                                   placeholder_map: list[dict],
                                   original_text: str,
                                   start_key: str = "start",
                                   end_key: str = "end",
                                   surface_key: str = "entity",
                                   ) -> list[dict]:
    """Translate offsets reported in `masked_text` back to `original_text`.

    Because placeholders are padded to the same length as the spans they
    replace, character offsets in masked_text equal those in original_text
    — UNLESS a span was shorter than its placeholder text. The function
    re-verifies each item against `original_text[start:end]` and, on
    mismatch, searches for the surface form in the original text.
    """
    from .span_validator import repair_span  # local import to avoid cycle
    out = []
    for it in new_items:
        s = it.get(start_key)
        e = it.get(end_key)
        surface = it.get(surface_key)
        if s is None or e is None or surface is None:
            continue
        if 0 <= s < e <= len(original_text) and original_text[s:e] == surface:
            out.append(it)
            continue
        repaired = repair_span(original_text, it)
        if repaired is not None:
            out.append(repaired)
    return out


def mask_one_for_verify(text: str, item: dict,
                        context_chars: int = 300,
                        start_key: str = "start", end_key: str = "end",
                        surface_key: str = "entity",
                        ) -> dict:
    """Replace just one entity with [MASK] (padded) and return the local
    context for the mask-verify prompt.

    Returns:
        {
          "masked_sentence":     the sentence with [MASK] in place of the span,
          "masked_paragraph":    a +/- context_chars window with [MASK] in place,
          "placeholder":         "[MASK]"
        }
    """
    s = item[start_key]
    e = item[end_key]
    ph = _placeholder_mask(e - s)
    sent = item.get("sentence")
    surface = item.get(surface_key)
    if sent and surface:
        local_idx = sent.find(surface)
        if local_idx >= 0:
            masked_sentence = sent[:local_idx] + ph + sent[local_idx + len(surface):]
        else:
            masked_sentence = sent
    else:
        masked_sentence = ph

    # paragraph window
    w_start = max(0, s - context_chars)
    w_end = min(len(text), e + context_chars)
    window = text[w_start:s] + ph + text[e:w_end]
    return {
        "masked_sentence": masked_sentence,
        "masked_paragraph": window,
        "placeholder": "[MASK]",
    }


def _placeholder_mask(width: int) -> str:
    raw = "[MASK]"
    if len(raw) > width:
        return raw
    pad = width - len(raw)
    left = pad // 2
    right = pad - left
    return " " * left + raw + " " * right


if __name__ == "__main__":
    text = (
        "BDNF is upregulated in the hippocampus of adult mice. "
        "BDNF protein levels increase following exercise."
    )
    items = [
        {"entity": "BDNF", "label": "Gene",
         "sentence": "BDNF is upregulated in the hippocampus of adult mice.",
         "start": 0, "end": 4},
        {"entity": "hippocampus", "label": "BrainRegion",
         "sentence": "BDNF is upregulated in the hippocampus of adult mice.",
         "start": 25, "end": 36},
        {"entity": "mice", "label": "Species",
         "sentence": "BDNF is upregulated in the hippocampus of adult mice.",
         "start": 46, "end": 50},
        {"entity": "BDNF", "label": "Gene",
         "sentence": "BDNF protein levels increase following exercise.",
         "start": 52, "end": 56},
    ]
    masked, pmap = mask_for_recall(text, items)
    print("=== masked text ===")
    print(masked)
    print("\n=== placeholder map ===")
    for p in pmap:
        print(p)
    print("\n=== mask-one for verify (first item) ===")
    print(mask_one_for_verify(text, items[1]))
