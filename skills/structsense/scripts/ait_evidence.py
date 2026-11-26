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

Two more checks run in the same pass, because both need the verified offsets:

* **Where** (`--layout paper.layout.json`, built by `scripts/doc_layout.py`): each
  found sentence gets `evidence_section`, `evidence_page`, `evidence_locator_type`
  and `evidence_locator` from the layout map (provenance `code:layout_lookup`), in
  `entities.csv` and `methods.csv`. A `not_found` sentence has no location (`NA`).
  Without a layout the location columns are left as they are, and `validate` warns.
* **Junk** (`scripts/text_quality.py`): a quote can be verbatim and still be parse
  garbage. Each entity's label and evidence are checked; the codes go to
  `quality_issues`, and an entity with a hard issue (garbled label, a label that
  occurs nowhere in the paper, or only junk evidence) is held back as
  `extraction_flag = parse_artifact`. A held-back entity whose issues have since
  been fixed is reset to `uncertain`, for a curator to confirm.

    python -m scripts.ait_evidence out/ --text paper.txt --layout paper.layout.json
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scripts import ait_tables as tables
from scripts import doc_layout, text_quality

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


def normalize_chars(raw: str) -> str:
    """NFKC + quote/dash folding + invisible characters dropped; no dehyphenation and
    no whitespace change (callers matching URLs and identifiers need every hyphen)."""
    out: List[str] = []
    for ch in raw or "":
        for c in unicodedata.normalize("NFKC", ch):
            if c not in _DROP:
                out.append(_FOLD.get(c, c))
    return "".join(out)


# A manuscript line number at the start of a line: digits followed by two or more
# spaces, or alone on the line ("1947    Figure 1 ...", "306\n"). Prose that starts a
# line with a number ("12 donors were ...") has one space, so it does not match.
_LINE_NUMBER = re.compile(r"(?m)^[ \t]*(\d{1,5})(?:[ \t]{2,}|[ \t]*$)")
MIN_LINE_NUMBERS = 50


def line_number_spans(raw: str, *, numbered: Optional[bool] = None) -> List[Tuple[int, int]]:
    """Raw [start, end) of every manuscript line number (preprints print one on each
    line). They are dropped before matching, so a correctly copied quote that
    crosses a line break still verifies, while offsets keep indexing the raw text.

    `numbered=None` decides from the text: at least MIN_LINE_NUMBERS candidates whose
    values mostly increase. Only numbers that continue the running sequence are
    taken, so a table value at the start of a line ("30    28    57") is kept."""
    cands = [(m.start(), m.end(), int(m.group(1))) for m in _LINE_NUMBER.finditer(raw or "")]
    if numbered is None:
        if len(cands) < MIN_LINE_NUMBERS:
            return []
        rising = sum(1 for a, b in zip(cands, cands[1:]) if b[2] > a[2])
        if rising < 0.8 * (len(cands) - 1):
            return []
    elif not numbered:
        return []
    spans, last = [], None
    for start, end, n in cands:
        if last is None or last < n <= last + 200:
            spans.append((start, end))
            last = n
    return spans


class Normalized:
    """Normalised text plus a map from every normalised character to its raw offset,
    so a hit in normalised space reports offsets a reader can slice the source with.

    Manuscript line numbers are dropped (see `line_number_spans`) when the text is
    line-numbered; `numbered` forces the decision (a quote or a slice of a numbered
    document is too short to decide for itself)."""

    def __init__(self, raw: str, *, numbered: Optional[bool] = None):
        self.raw = raw or ""
        spans = line_number_spans(self.raw, numbered=numbered)
        self.line_numbered = bool(spans) if numbered is None else bool(numbered)
        drop = set()
        for a, b in spans:
            drop.update(range(a, b))
        chars: List[Tuple[str, int]] = []
        for i, ch in enumerate(self.raw):
            if ch in _DROP or i in drop:
                if i in drop and (not chars or not chars[-1][0].isspace()):
                    chars.append((" ", i))  # the number separated words; keep them apart
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
    q = Normalized(quote, numbered=doc.line_numbered).text
    if not q:
        return {"status": "not_found", "similarity": 0.0, "start": start, "end": end}
    if start is not None and end is not None and 0 <= start < end <= len(doc.raw):
        if Normalized(doc.raw[start:end], numbered=doc.line_numbered).text == q:
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


LOCATION_COLUMNS = ("evidence_section", "evidence_page", "evidence_locator_type", "evidence_locator")


def locate_row(row: Dict[str, str], results: List[Dict[str, object]], layout: dict) -> None:
    """Write the aligned location columns for one row from the layout map."""
    sections = tables.split_multi(row.get("evidence_section", ""))
    cols: Dict[str, List[str]] = {c: [] for c in LOCATION_COLUMNS}
    replaced_section = False
    for i, r in enumerate(results):
        found = r["status"] != "not_found"
        loc = doc_layout.locate(layout, r["start"] if found else None, r["end"] if found else None)
        old = sections[i] if i < len(sections) else ""
        if loc["section"] != "NA":
            replaced_section = replaced_section or loc["section"] != old
            cols["evidence_section"].append(loc["section"])
        else:
            cols["evidence_section"].append(old or "NA")
        for c, key in (("evidence_page", "page"), ("evidence_locator_type", "locator_type"),
                       ("evidence_locator", "locator")):
            cols[c].append(loc[key])
    for c, vals in cols.items():
        row[c] = tables.join_multi(vals)
    prov = row.get("field_provenance", "")
    for c in LOCATION_COLUMNS[1:] + ((LOCATION_COLUMNS[0],) if replaced_section else ()):
        prov = tables.add_provenance(prov, c, "code:layout_lookup")
    row["field_provenance"] = prov


def judge_quality(row: Dict[str, str], results: List[Dict[str, object]], *, junk_spans, boiler_spans,
                  doc_folded: str) -> Dict[str, object]:
    """Fill quality_issues and set/reset the parse_artifact hold-back for an entity row."""
    verdict = text_quality.entity_quality(
        label=row.get("label_verbatim", ""), aliases=tables.split_multi(row.get("aliases", "")),
        entity_type=row.get("entity_type", ""),
        sentences=tables.split_multi(row.get("evidence_sentence", "")),
        spans=[(r["start"], r["end"]) for r in results], statuses=[r["status"] for r in results],
        locator_types=tables.split_multi(row.get("evidence_locator_type", "")),
        junk_spans=junk_spans, boiler_spans=boiler_spans, doc_folded=doc_folded)
    row["quality_issues"] = tables.join_multi(verdict["issues"])
    flag = row.get("extraction_flag", "")
    if verdict["hold_back"] and flag != "unverified_evidence":
        row["extraction_flag"] = "parse_artifact"
    elif not verdict["hold_back"] and flag == "parse_artifact":
        row["extraction_flag"] = "uncertain"  # was junk; fixed since — a curator confirms
    if row["extraction_flag"] != flag or verdict["issues"]:
        row["field_provenance"] = tables.add_provenance(row.get("field_provenance", ""),
                                                        "quality_issues", "code:text_quality")
    return verdict


def verify_dir(out_dir: Path, text: str, *, threshold: float = FUZZY_THRESHOLD,
               layout: Optional[dict] = None) -> dict:
    """Verify entities.csv and methods.csv in place: evidence, location, junk. Returns the report dict."""
    doc = Normalized(text)
    quality = text_quality.document_report(text, layout)
    junk_spans = [tuple(s) for s in quality["junk_spans"]]
    boiler_spans = [tuple(sp) for b in quality["boilerplate"] for sp in b["spans"]]
    doc_folded = text_quality.fold_separators(doc.text.casefold())
    report: dict = {"threshold": threshold, "tables": {}, "quarantined": [], "parse_artifacts": [],
                    "layout": None if layout is None else {
                        "page_method": layout.get("page_method"), "pages": len(layout.get("pages") or []),
                        "pages_unaligned": layout.get("pages_unaligned") or [],
                        "sections": [s["label"] for s in layout.get("sections") or []]},
                    "text_quality": {k: quality[k] for k in ("verdict", "junk_fraction", "advice")},
                    "quality_issue_counts": {}}
    for table, has_offsets in (("entities.csv", True), ("methods.csv", False)):
        path = Path(out_dir) / table
        if not path.exists():
            continue
        _, rows = tables.read_table(path)
        counts = {"exact": 0, "exact_offset_corrected": 0, "fuzzy": 0, "not_found": 0}
        for row in rows:
            results = verify_row(row, doc, has_offsets=has_offsets, threshold=threshold)
            for r in results:
                counts[r["status"]] += 1
            if layout is not None and results:
                locate_row(row, results, layout)
            if table != "entities.csv":
                continue
            verdict = judge_quality(row, results, junk_spans=junk_spans, boiler_spans=boiler_spans,
                                    doc_folded=doc_folded)
            for code in verdict["issues"]:
                report["quality_issue_counts"][code] = report["quality_issue_counts"].get(code, 0) + 1
            entry = {"mention_id": row.get("mention_id"), "label_verbatim": row.get("label_verbatim")}
            if row.get("extraction_flag") == "unverified_evidence":
                report["quarantined"].append(entry)
            elif row.get("extraction_flag") == "parse_artifact":
                report["parse_artifacts"].append({**entry, "quality_issues": verdict["issues"]})
        tables.write_table(path, table, rows)
        total = sum(counts.values())
        report["tables"][table] = {
            "rows": len(rows), "sentences": total, **counts,
            "evidence_integrity": round((counts["exact"] + counts["exact_offset_corrected"]) / total, 4) if total else None,
        }
    n_entities = report["tables"].get("entities.csv", {}).get("rows", 0)
    report["quarantine_rate"] = round(len(report["quarantined"]) / n_entities, 4) if n_entities else None
    report["parse_artifact_rate"] = round(len(report["parse_artifacts"]) / n_entities, 4) if n_entities else None
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path, help="directory holding entities.csv / methods.csv")
    ap.add_argument("--text", type=Path, required=True,
                    help="the exact text file the Pass 1a index (and its offsets) was built on")
    ap.add_argument("--layout", type=Path,
                    help="paper.layout.json from scripts/doc_layout.py (default: <text stem>.layout.json "
                         "next to --text, when it exists)")
    ap.add_argument("--threshold", type=float, default=FUZZY_THRESHOLD)
    ap.add_argument("--report", type=Path, help="default: <out_dir>/evidence_report.json")
    args = ap.parse_args(argv)

    text = args.text.read_text(encoding="utf-8")
    layout_path = args.layout or args.text.with_name(args.text.stem + ".layout.json")
    layout = None
    if layout_path.exists():
        layout = doc_layout.load_layout(layout_path, text)
    elif args.layout:
        ap.error(f"--layout {args.layout} does not exist")
    report = verify_dir(args.out_dir, text, threshold=args.threshold, layout=layout)
    report_path = args.report or args.out_dir / "evidence_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for table, c in report["tables"].items():
        print(f"{table}: {c['sentences']} sentences — exact {c['exact']}, offset-corrected "
              f"{c['exact_offset_corrected']}, fuzzy {c['fuzzy']}, not_found {c['not_found']}; "
              f"evidence integrity {c['evidence_integrity']}")
    print(f"quarantined entities: {len(report['quarantined'])} (rate {report['quarantine_rate']})")
    print(f"parse artifacts held back: {len(report['parse_artifacts'])} (rate {report['parse_artifact_rate']})")
    tq = report["text_quality"]
    print(f"source text quality: {tq['verdict']} (junk fraction {tq['junk_fraction']})")
    for a in tq["advice"]:
        print(f"  {a}")
    print(f"evidence location: {'layout ' + str(layout_path) if layout else 'not computed (no layout; run scripts.doc_layout)'}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
