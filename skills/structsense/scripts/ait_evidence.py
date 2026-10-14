"""Pass 2a of the AIT mapping mode: lexical verification of every evidence sentence.

This is the trust check, and it is code, not model judgement. For each evidence
sentence in `entities.csv` and `methods.csv`:

1. normalise the quote and the source text identically — Unicode NFKC, quote and
   dash folding, soft hyphens removed, line-break hyphenation joined
   (`inter-\\nneuron` -> `interneuron`), whitespace runs collapsed;
2. test containment at the recorded `evidence_char_start`/`evidence_char_end`
   -> `exact`;
3. else search the whole document -> `exact_offset_corrected` (offsets rewritten to
   the occurrence nearest the recorded one);
4. else best fuzzy alignment -> `fuzzy` when similarity >= 0.95 (offsets point at the
   aligned span), otherwise `not_found`. The similarity is recorded in both cases.

`methods.csv` carries no offsets, so a verbatim hit there is simply `exact`.

Matching is case-sensitive: the quote has to be the paper's characters, in order.
Folding is limited to artefacts no reader would call a change of wording.

An entity whose every evidence sentence is `not_found` is quarantined
(`extraction_flag = unverified_evidence`): it stays in the output and must never
become a mapping edge. A `fuzzy` sentence may support an edge, but never reaches
`review_sheet.csv`, and the run report counts it separately.

Offsets index the exact text file the Pass 1a retrieval index was built on (main text
plus captions, tables and supplements, concatenated). Pass that file as `--text`.

    python -m scripts.ait_evidence out/ --text paper.txt
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scripts import ait_tables as tables

FUZZY_THRESHOLD = 0.95
MAX_CANDIDATES = 200

_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-", "﹣": "-", "－": "-",
}
_DROP = {"­", "​", "‌", "‍", "﻿"}


class Normalized:
    """Normalised text plus a map from every normalised character to its raw offset,
    so a hit in normalised space reports offsets a reader can slice the source with."""

    def __init__(self, raw: str):
        self.raw = raw or ""
        chars: List[Tuple[str, int]] = []
        for i, ch in enumerate(self.raw):
            if ch in _DROP:
                continue
            for c in unicodedata.normalize("NFKC", ch):
                if c in _DROP:
                    continue
                chars.append((_FOLD.get(c, c), i))
        chars = _dehyphenate(chars)
        out: List[str] = []
        idx: List[int] = []
        for c, i in chars:
            if c.isspace():
                if not out or out[-1] == " ":
                    continue
                c = " "
            out.append(c)
            idx.append(i)
        while out and out[-1] == " ":
            out.pop()
            idx.pop()
        self.text = "".join(out)
        self.index = idx

    def raw_span(self, start: int, end: int) -> Tuple[int, int]:
        """Raw [start, end) covering normalised [start, end)."""
        if not self.index or start >= end:
            return 0, 0
        return self.index[start], self.index[min(end, len(self.index)) - 1] + 1


def _dehyphenate(chars: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Drop `-` + whitespace-containing-a-newline between a letter and a lowercase letter."""
    out: List[Tuple[str, int]] = []
    i, n = 0, len(chars)
    while i < n:
        c = chars[i][0]
        if c == "-" and out and out[-1][0].isalpha():
            j = i + 1
            saw_newline = False
            while j < n and chars[j][0].isspace():
                saw_newline = saw_newline or chars[j][0] in "\n\r"
                j += 1
            if saw_newline and j < n and chars[j][0].islower():
                i = j
                continue
        out.append(chars[i])
        i += 1
    return out


def normalize(s: str) -> str:
    return Normalized(s).text


def _all_occurrences(haystack: str, needle: str) -> List[int]:
    out, pos = [], haystack.find(needle)
    while pos >= 0:
        out.append(pos)
        pos = haystack.find(needle, pos + 1)
    return out


def best_alignment(quote: str, doc: Normalized) -> Tuple[float, int, int]:
    """(similarity, norm_start, norm_end) of the best fuzzy alignment of a normalised quote.

    Candidate windows are anchored on exact word n-grams of the quote, so the cost is
    proportional to the number of anchors rather than the document length."""
    words = quote.split(" ")
    if not quote or not doc.text:
        return 0.0, 0, 0
    k = 3 if len(words) >= 6 else 1
    starts = set()
    offset = 0
    for w in range(len(words) - k + 1):
        gram = " ".join(words[w:w + k])
        for pos in _all_occurrences(doc.text, gram):
            starts.add(max(0, pos - offset))
            if len(starts) >= MAX_CANDIDATES:
                break
        offset += len(words[w]) + 1
        if len(starts) >= MAX_CANDIDATES:
            break
    best = (0.0, 0, 0)
    slack = len(quote) // 10 + 5
    for s in sorted(starts):
        lo = max(0, s - slack)
        window = doc.text[lo:s + len(quote) + slack]
        blocks = [b for b in SequenceMatcher(None, quote, window, autojunk=False).get_matching_blocks() if b.size]
        if not blocks:
            continue
        a_start, a_end = lo + blocks[0].b, lo + blocks[-1].b + blocks[-1].size
        ratio = SequenceMatcher(None, quote, doc.text[a_start:a_end], autojunk=False).ratio()
        if ratio > best[0]:
            best = (ratio, a_start, a_end)
    return best


def verify_sentence(quote: str, doc: Normalized, start: Optional[int] = None,
                    end: Optional[int] = None, *, has_offsets: bool = True,
                    threshold: float = FUZZY_THRESHOLD) -> Dict[str, object]:
    """{status, similarity, start, end} for one evidence sentence."""
    q = normalize(quote)
    if not q:
        return {"status": "not_found", "similarity": 0.0, "start": start, "end": end}
    if start is not None and end is not None and 0 <= start < end <= len(doc.raw):
        if normalize(doc.raw[start:end]) == q:
            return {"status": "exact", "similarity": 1.0, "start": start, "end": end}
    hits = _all_occurrences(doc.text, q)
    if hits:
        if start is not None:
            hits.sort(key=lambda p: abs(doc.raw_span(p, p + len(q))[0] - start))
        rs, re_ = doc.raw_span(hits[0], hits[0] + len(q))
        status = "exact_offset_corrected" if has_offsets else "exact"
        return {"status": status, "similarity": 1.0, "start": rs, "end": re_}
    ratio, ns, ne = best_alignment(q, doc)
    if ratio >= threshold:
        rs, re_ = doc.raw_span(ns, ne)
        return {"status": "fuzzy", "similarity": round(ratio, 4), "start": rs, "end": re_}
    return {"status": "not_found", "similarity": round(ratio, 4), "start": start, "end": end}


def _int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verify_row(row: Dict[str, str], doc: Normalized, *, has_offsets: bool,
               threshold: float = FUZZY_THRESHOLD) -> List[Dict[str, object]]:
    """Verify every aligned evidence sentence in a row, writing results back in place."""
    sentences = tables.split_multi(row.get("evidence_sentence", ""))
    starts = tables.split_multi(row.get("evidence_char_start", "")) if has_offsets else []
    ends = tables.split_multi(row.get("evidence_char_end", "")) if has_offsets else []
    results = []
    for i, s in enumerate(sentences):
        start = _int(starts[i]) if i < len(starts) else None
        end = _int(ends[i]) if i < len(ends) else None
        results.append(verify_sentence(s, doc, start, end, has_offsets=has_offsets, threshold=threshold))
    if not sentences:
        return results
    row["evidence_verified"] = tables.join_multi(r["status"] for r in results)
    if has_offsets:
        row["evidence_char_start"] = tables.join_multi("" if r["start"] is None else r["start"] for r in results)
        row["evidence_char_end"] = tables.join_multi("" if r["end"] is None else r["end"] for r in results)
        row["evidence_similarity"] = tables.join_multi(r["similarity"] for r in results)
    if all(r["status"] == "not_found" for r in results):
        row["extraction_flag"] = "unverified_evidence"
    if "field_provenance" in row:
        row["field_provenance"] = tables.add_provenance(row.get("field_provenance", ""),
                                                        "evidence_sentence", "code:lexical_verify")
    return results


def verify_dir(out_dir: Path, text: str, *, threshold: float = FUZZY_THRESHOLD) -> dict:
    """Verify entities.csv and methods.csv in place. Returns the report dict."""
    doc = Normalized(text)
    report: dict = {"threshold": threshold, "tables": {}, "quarantined": []}
    for table, has_offsets in (("entities.csv", True), ("methods.csv", False)):
        path = Path(out_dir) / table
        if not path.exists():
            continue
        _, rows = tables.read_table(path)
        counts = {"exact": 0, "exact_offset_corrected": 0, "fuzzy": 0, "not_found": 0}
        for row in rows:
            for r in verify_row(row, doc, has_offsets=has_offsets, threshold=threshold):
                counts[r["status"]] += 1
            if table == "entities.csv" and tables.is_quarantined(row):
                report["quarantined"].append({"mention_id": row.get("mention_id"),
                                              "label_verbatim": row.get("label_verbatim")})
        tables.write_table(path, table, rows)
        total = sum(counts.values())
        report["tables"][table] = {
            "rows": len(rows), "sentences": total, **counts,
            "evidence_integrity": round((counts["exact"] + counts["exact_offset_corrected"]) / total, 4) if total else None,
        }
    n_entities = report["tables"].get("entities.csv", {}).get("rows", 0)
    report["quarantine_rate"] = round(len(report["quarantined"]) / n_entities, 4) if n_entities else None
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path, help="directory holding entities.csv / methods.csv")
    ap.add_argument("--text", type=Path, required=True,
                    help="the exact text file the Pass 1a index (and its offsets) was built on")
    ap.add_argument("--threshold", type=float, default=FUZZY_THRESHOLD)
    ap.add_argument("--report", type=Path, help="default: <out_dir>/evidence_report.json")
    args = ap.parse_args(argv)

    text = args.text.read_text(encoding="utf-8")
    report = verify_dir(args.out_dir, text, threshold=args.threshold)
    report_path = args.report or args.out_dir / "evidence_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for table, c in report["tables"].items():
        print(f"{table}: {c['sentences']} sentences — exact {c['exact']}, offset-corrected "
              f"{c['exact_offset_corrected']}, fuzzy {c['fuzzy']}, not_found {c['not_found']}; "
              f"evidence integrity {c['evidence_integrity']}")
    print(f"quarantined entities: {len(report['quarantined'])} (rate {report['quarantine_rate']})")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
