"""Validate NER spans against the source text.

After extraction (and especially after merging chunk results), every entity's
start/end should satisfy: text[start:end] == entity. This module checks that,
and offers a best-effort repair that re-finds the entity in its sentence.
"""
from __future__ import annotations

from typing import Iterable, Tuple


def validate(text: str, item: dict) -> bool:
    """True if start/end correctly anchor `entity` (or `term`) in `text`."""
    surface = item.get("entity") or item.get("term")
    start, end = item.get("start"), item.get("end")
    sentence = item.get("sentence", "")
    if surface is None or start is None or end is None:
        return False
    if text[start:end] != surface:
        return False
    if sentence and sentence not in text:
        return False
    if sentence and surface not in sentence:
        return False
    return True


def repair_span(text: str, item: dict) -> dict | None:
    """Try to re-locate `entity` in the source text.

    Strategy: search for `entity` inside `sentence`, then for `sentence` inside
    `text`. Combine offsets. Returns a repaired item, or None if not found.
    """
    surface = item.get("entity") or item.get("term")
    sentence = item.get("sentence", "")
    if not surface:
        return None

    if sentence and sentence in text and surface in sentence:
        sent_offset = text.index(sentence)
        local = sentence.index(surface)
        new = dict(item)
        new["start"] = sent_offset + local
        new["end"] = new["start"] + len(surface)
        return new

    # No sentence — fall back to first occurrence in text.
    if surface in text:
        new = dict(item)
        new["start"] = text.index(surface)
        new["end"] = new["start"] + len(surface)
        return new

    return None


def validate_all(text: str, items: Iterable[dict]) -> Tuple[list[dict], list[dict]]:
    """Partition items into (valid, dropped).

    Repaired items are placed in `valid`. Items that can't be repaired are
    in `dropped` for the caller to log.
    """
    valid, dropped = [], []
    for it in items:
        if validate(text, it):
            valid.append(it)
            continue
        repaired = repair_span(text, it)
        if repaired is not None and validate(text, repaired):
            valid.append(repaired)
        else:
            dropped.append(it)
    return valid, dropped


if __name__ == "__main__":
    text = "We recorded from CA1 pyramidal cells in the hippocampus."
    good = {
        "entity": "hippocampus",
        "sentence": text,
        "start": text.index("hippocampus"),
        "end": text.index("hippocampus") + len("hippocampus"),
    }
    bad = {"entity": "hippocampus", "sentence": text, "start": 0, "end": 5}
    print("good ok?", validate(text, good))
    print("bad ok?", validate(text, bad))
    print("repaired:", repair_span(text, bad))
