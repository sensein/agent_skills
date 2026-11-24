"""Detect text-extraction junk, in the document and in extracted values.

The lexical evidence check (`scripts/ait_evidence.py`) proves a quote is in the
source text. It cannot tell whether that text is readable: when a PDF parse emits
`(cid:12)` glyph codes, `â€™` mojibake, letter-spaced words (`c e l l`), columns
run together one word per line, or words fused without spaces, the garbage is
copied verbatim and verifies as `exact`. This module catches that, at two levels.

**Document** (`document_report`, the Pass 0 gate). The text is scored per
paragraph (long paragraphs in fixed windows), so one garbled table does not taint
the clean prose around it. A region is *junk* when its
glyph/mojibake/control density, letter-spacing, letter-to-symbol ratio, one-word-line
ratio or fused-word ratio crosses a threshold below. The verdict is `ok`,
`degraded` (some junk: extract, but expect parse artifacts there) or `unusable`
(re-extract with another backend before extracting anything). Repeated short lines
(running headers and footers) are reported as boilerplate.

**Values** (`label_issues`, `evidence_issues`, `entity_quality`), used by Pass 2a.
Each check returns codes from the closed `quality_issue` vocabulary in
`schemas/ait-mapping-columns.json`. *Hard* codes mean the value is a parse
artifact: the entity is held back (`extraction_flag = parse_artifact`), kept and
visible but never a mapping edge or a graph node. *Soft* codes are recorded for the
curator and do not hold anything back.

The thresholds are heuristics chosen to be conservative on clean scientific text
(numbers-heavy tables included); every metric is reported, so they can be tuned
against real failures.

    python -m scripts.text_quality paper.txt --layout paper.layout.json   # exit 1 if unusable
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #

# (cid:NN) is what pdfminer emits for a glyph with no Unicode mapping; U+FFFD is a
# decoder's replacement character; private-use code points are unmapped font glyphs.
GLYPH_RE = re.compile(r"\(cid:\d+\)|\ufffd|[\ue000-\uf8ff]")
# Control characters other than tab, newline, carriage return and form feed.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
# UTF-8 decoded as Windows-1252/Latin-1: "Ã©" for é, "â€™" for ’, "Â " for NBSP, "ï¬" for ligatures.
MOJIBAKE_RE = re.compile(r"\u00c3[\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u203a\u20ac\u2122]"
                         r"|\u00e2\u20ac|\u00c2[\u00a0-\u00bf]|\u00ef\u00ac")
# Five or more single letters separated by single spaces: "c e l l s", "P v a l b".
LETTER_SPACED_RE = re.compile(r"(?<![^\s(\[])(?:[A-Za-z] ){4,}[A-Za-z](?![^\s.,;:)\]])")
# A run of 30+ letters with no space: words fused by a lost space ("thecellsweresorted...").
FUSED_RE = re.compile(r"[A-Za-z]{30,}")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# --------------------------------------------------------------------------- #
# thresholds (per region)
# --------------------------------------------------------------------------- #

WINDOW_CHARS = 2000          # longest region; longer paragraphs are cut into windows
ARTIFACTS_PER_1K = 2.0       # glyph + mojibake + control characters per 1,000 chars
LETTER_SPACED_PER_1K = 1.0   # letter-spaced runs per 1,000 chars
MIN_LETTER_RATIO = 0.5       # letters / (letters + symbols); digits and table pipes ignored
MAX_SHORT_LINE_RATIO = 0.6   # lines of <= 3 chars / non-empty lines, with >= 20 lines
FUSED_PER_1K = 0.5           # fused-word runs per 1,000 chars
DEGRADED_FRACTION = 0.02     # junk chars / all chars above this -> degraded
UNUSABLE_FRACTION = 0.25     # ... above this -> unusable

# --------------------------------------------------------------------------- #
# value checks
# --------------------------------------------------------------------------- #

MIN_EVIDENCE_CHARS = 20
MAX_LABEL_CHARS = 100
MAX_LABEL_WORDS = 12
GENE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-./@_']{0,24}[+\-]?$")

HARD_LABEL = ("label_glyph_artifact", "label_mojibake", "label_letter_spaced", "label_low_alpha",
              "label_sentence_like", "label_too_long", "label_unbalanced_brackets",
              "label_not_gene_symbol", "label_not_in_text", "label_not_in_evidence")
JUNK_EVIDENCE = ("evidence_glyph_artifact", "evidence_mojibake", "evidence_letter_spaced",
                 "evidence_low_alpha", "evidence_fused_words", "evidence_in_junk_region")
# The returned evidence must contain the extracted phrase: a sentence that does not
# name the entity is not evidence for it (it is dropped from the review sheet, the
# cards and the Turtle), and an entity none of whose found sentences names it is
# held back (label_not_in_evidence, in HARD_LABEL). Some sentences missing it is
# recorded only.
SOFT_EVIDENCE = ("evidence_without_label", "evidence_too_short", "evidence_boilerplate",
                 "evidence_in_reference_list")
QUALITY_ISSUES = HARD_LABEL + JUNK_EVIDENCE + SOFT_EVIDENCE + ("all_evidence_junk",)
# Holding back: any hard label issue, or every usable evidence sentence is junk.
HOLD_BACK = set(HARD_LABEL) | {"all_evidence_junk"}


def alpha_ratio(s: str) -> float:
    """letters / non-space characters (for labels, which must be mostly letters)."""
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _LETTER.match(c)) / len(chars)


def letter_symbol_ratio(s: str) -> float:
    """letters / (letters + symbols), ignoring digits, whitespace and table pipes.

    Digits are legitimate in bulk (count tables, statistics), so they say nothing
    about readability. What a broken font map produces is symbol soup
    (`!"#$%&'()*+`), and that is what this ratio drops on. 1.0 for text with
    neither letters nor symbols (a row of numbers)."""
    letters = symbols = 0
    for c in s:
        if c.isspace() or c.isdigit() or c == "|":
            continue
        if _LETTER.match(c):
            letters += 1
        else:
            symbols += 1
    return letters / (letters + symbols) if (letters + symbols) else 1.0


def _prose_lines(s: str) -> str:
    """`s` without digit-heavy lines (table rows of counts and statistics), which are
    legitimately low in letters and would otherwise read as junk."""
    keep = []
    for line in s.splitlines() or [s]:
        chars = [c for c in line if not c.isspace()]
        if chars and sum(c.isdigit() for c in chars) / len(chars) >= 0.4:
            continue
        keep.append(line)
    return "\n".join(keep)


def _artifact_codes(s: str, prefix: str) -> List[str]:
    out = []
    if GLYPH_RE.search(s) or CONTROL_RE.search(s):
        out.append(f"{prefix}_glyph_artifact")
    if MOJIBAKE_RE.search(s):
        out.append(f"{prefix}_mojibake")
    if LETTER_SPACED_RE.search(s):
        out.append(f"{prefix}_letter_spaced")
    return out


def _balanced(s: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[str] = []
    for c in s:
        if c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return False
            stack.pop()
    return not stack


def label_issues(label: str, entity_type: str = "") -> List[str]:
    """Codes for a label that is not a readable name."""
    s = (label or "").strip()
    if not s:
        return ["label_low_alpha"]
    out = _artifact_codes(s, "label")
    # a short label made of spaced single letters ("P v a l b") is letter-spaced too
    if "label_letter_spaced" not in out and re.fullmatch(r"(?:[A-Za-z] ){2,}[A-Za-z]", s):
        out.append("label_letter_spaced")
    if alpha_ratio(s) < 0.3:
        out.append("label_low_alpha")
    if len(s) > MAX_LABEL_CHARS or len(s.split()) > MAX_LABEL_WORDS:
        out.append("label_too_long")
    if re.search(r"[.!?]\s+[A-Z]", s) or (s.endswith(".") and len(s.split()) > 6):
        out.append("label_sentence_like")
    if not _balanced(s):
        out.append("label_unbalanced_brackets")
    if entity_type == "gene" and not GENE_SYMBOL_RE.match(s):
        out.append("label_not_gene_symbol")
    return out


def evidence_issues(sentence: str) -> List[str]:
    """Codes for an evidence sentence that reads as parse junk (independent of where it is)."""
    s = sentence or ""
    out = _artifact_codes(s, "evidence")
    if len(s.strip()) >= MIN_EVIDENCE_CHARS and letter_symbol_ratio(_prose_lines(s)) < MIN_LETTER_RATIO:
        out.append("evidence_low_alpha")
    if FUSED_RE.search(s):
        out.append("evidence_fused_words")
    if len(" ".join(s.split())) < MIN_EVIDENCE_CHARS:
        out.append("evidence_too_short")
    return out


# Separators that do not change which entity is named: "Sst Chodl", "Sst-Chodl" and
# "Sst/Chodl" are one cell type. A comma does change it ("Sst, Chodl" is two genes),
# so it is not folded.
_SEPARATORS = re.compile(r"[\s\-/]+")


def fold_separators(s: str) -> str:
    """Runs of spaces, hyphens and slashes -> one space. Expects normalized text
    (ait_evidence.normalize already maps every dash variant to '-')."""
    return _SEPARATORS.sub(" ", s)


def _fold(s: str) -> str:
    from scripts.ait_evidence import normalize
    return fold_separators(normalize(s).casefold()).strip()


def label_in_sentence(labels: Iterable[str], sentence: str) -> bool:
    """True if any of the labels occurs in this one sentence (case, typography and
    space/hyphen/slash separators folded)."""
    return label_in_evidence(labels, [sentence])


def label_in_evidence(labels: Iterable[str], sentences: Iterable[str]) -> bool:
    """True if any of the labels occurs (case-insensitively, typography folded) in any sentence."""
    folded = [_fold(s) for s in sentences if s]
    for lab in labels:
        f = _fold(lab or "")
        if f and any(f in s for s in folded):
            return True
    return False


def label_in_text(labels: Iterable[str], doc_folded: str) -> bool:
    """True if any of the labels occurs anywhere in the source (`doc_folded` is
    `fold(text)`, computed once). A label that occurs nowhere was not copied from
    the paper: it was repaired, invented, or read from text that is not there."""
    return any(f and f in doc_folded for f in (_fold(lab or "") for lab in labels))


def fold(text: str) -> str:
    """The folding every phrase comparison uses: ait_evidence normalisation, casefold,
    and space/hyphen/slash treated as one separator."""
    return _fold(text)


def overlaps(spans: Sequence[Tuple[int, int]], start: Optional[int], end: Optional[int]) -> bool:
    if start is None or end is None:
        return False
    return any(s < end and start < e for s, e in spans)


# --------------------------------------------------------------------------- #
# document report
# --------------------------------------------------------------------------- #

def _regions(text: str, layout: Optional[dict]) -> List[Tuple[str, int, int]]:
    """Paragraphs (split on blank lines), long ones cut into WINDOW_CHARS windows.

    Paragraphs, not pages: one garbled table must not taint the clean prose on the
    same page, or every entity on that page would be held back with it. Text that
    lost its paragraph breaks (one word per line, columns run together) is one long
    paragraph, and the windows keep its metrics comparable."""
    pages = (layout or {}).get("pages") or []
    starts = [int(p["start"]) for p in pages]

    def name(a: int, b: int) -> str:
        if not pages:
            return f"chars {a}-{b}"
        import bisect
        return f"page {pages[max(0, bisect.bisect_right(starts, a) - 1)]['page']}, chars {a}-{b}"

    out = []
    pos = 0
    for m in list(re.finditer(r"\n[ \t]*\n", text)) + [None]:
        end = m.start() if m else len(text)
        if text[pos:end].strip():
            for a in range(pos, end, WINDOW_CHARS):
                b = min(end, a + WINDOW_CHARS)
                if end - b < WINDOW_CHARS // 4:  # no runt window at the end
                    b = end
                out.append((name(a, b), a, b))
                if b == end:
                    break
        pos = m.end() if m else len(text)
    return out


def region_metrics(chunk: str) -> Dict[str, object]:
    n = max(1, len(chunk))
    per_1k = 1000.0 / n
    artifacts = len(GLYPH_RE.findall(chunk)) + len(CONTROL_RE.findall(chunk)) + len(MOJIBAKE_RE.findall(chunk))
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    short = sum(1 for ln in lines if len(ln) <= 3)
    m = {
        "chars": len(chunk),
        "artifacts_per_1k": round(artifacts * per_1k, 3),
        "letter_spaced_per_1k": round(len(LETTER_SPACED_RE.findall(chunk)) * per_1k, 3),
        "letter_ratio": round(letter_symbol_ratio(_prose_lines(chunk)), 3),
        "short_line_ratio": round(short / len(lines), 3) if lines else 0.0,
        "lines": len(lines),
        "fused_per_1k": round(len(FUSED_RE.findall(chunk)) * per_1k, 3),
    }
    reasons = []
    if m["artifacts_per_1k"] >= ARTIFACTS_PER_1K:
        reasons.append("glyph_or_mojibake")
    if m["letter_spaced_per_1k"] >= LETTER_SPACED_PER_1K:
        reasons.append("letter_spaced")
    if len(chunk.strip()) >= 200 and m["letter_ratio"] < MIN_LETTER_RATIO:
        reasons.append("low_alpha")
    if m["lines"] >= 20 and m["short_line_ratio"] > MAX_SHORT_LINE_RATIO:
        reasons.append("fragmented_lines")
    if m["fused_per_1k"] >= FUSED_PER_1K:
        reasons.append("fused_words")
    m["junk_reasons"] = reasons
    return m


def boilerplate_lines(text: str, *, min_repeats: int = 3) -> List[Dict[str, object]]:
    """Running headers/footers: the same short line repeated >= min_repeats times."""
    positions: Dict[str, List[int]] = {}
    at = 0
    for line in text.split("\n"):
        key = line.strip()
        if 15 <= len(key) <= 120 and alpha_ratio(key) >= 0.5 and not key.startswith("|"):
            positions.setdefault(key, []).append(at + (len(line) - len(line.lstrip())))
        at += len(line) + 1
    return [{"line": k, "count": len(v), "spans": [[p, p + len(k)] for p in v]}
            for k, v in positions.items() if len(v) >= min_repeats]


def document_report(text: str, layout: Optional[dict] = None) -> Dict[str, object]:
    regions = []
    junk_spans: List[List[int]] = []
    junk_chars = 0
    n_regions = 0
    reasons_all: Counter = Counter()
    for name, start, end in _regions(text, layout):
        n_regions += 1
        m = region_metrics(text[start:end])
        if m["junk_reasons"]:
            m.update({"region": name, "start": start, "end": end,
                      "excerpt": " ".join(text[start:end].split())[:160]})
            regions.append(m)
            reasons_all.update(m["junk_reasons"])
            junk_spans.append([start, end])
            junk_chars += end - start
    content = sum(len(text[a:b]) for _, a, b in _regions(text, layout)) if text else 0
    frac = junk_chars / content if content else 1.0
    verdict = "unusable" if (not text.strip() or frac > UNUSABLE_FRACTION) else \
        "degraded" if frac > DEGRADED_FRACTION else "ok"
    try:
        from scripts.input_loader import caption_coverage
        captions = caption_coverage(text)
    except Exception:  # the loader's optional imports are not needed for this check
        captions = None
    boiler = boilerplate_lines(text)
    advice = []
    if verdict != "ok":
        advice.append(f"{verdict}: {round(100 * frac, 1)}% of the text is in junk regions "
                      f"({', '.join(f'{k} x{v}' for k, v in reasons_all.most_common())}).")
        advice.append("Re-extract with another backend before extracting entities: open access -> "
                      "`python -m scripts.fetch_fulltext <PMCID>`; otherwise GROBID, Docling (OCRs scanned "
                      "pages), or pymupdf4llm via `scripts/input_loader.py`. Then rebuild the layout.")
    if captions and captions.get("looks_empty") and captions.get("chars", 0) > 2000:
        advice.append("No figure/table captions found: this extraction path probably dropped them.")
    return {
        "verdict": verdict,
        "junk_fraction": round(frac, 4),
        "junk_spans": junk_spans,
        "n_regions": n_regions,
        "junk_regions": regions,
        "boilerplate": boiler,
        "captions": captions,
        "thresholds": {
            "window_chars": WINDOW_CHARS, "artifacts_per_1k": ARTIFACTS_PER_1K,
            "letter_spaced_per_1k": LETTER_SPACED_PER_1K, "min_letter_ratio": MIN_LETTER_RATIO,
            "max_short_line_ratio": MAX_SHORT_LINE_RATIO, "fused_per_1k": FUSED_PER_1K,
            "degraded_fraction": DEGRADED_FRACTION, "unusable_fraction": UNUSABLE_FRACTION,
        },
        "advice": advice,
    }


# --------------------------------------------------------------------------- #
# per-entity verdict (used by ait_evidence)
# --------------------------------------------------------------------------- #

def entity_quality(*, label: str, aliases: Sequence[str], entity_type: str,
                   sentences: Sequence[str], spans: Sequence[Tuple[Optional[int], Optional[int]]],
                   statuses: Sequence[str], locator_types: Sequence[str],
                   junk_spans: Sequence[Tuple[int, int]],
                   boiler_spans: Sequence[Tuple[int, int]],
                   doc_folded: Optional[str] = None) -> Dict[str, object]:
    """{issues, hold_back} for one entity. Only sentences found in the source
    (status other than not_found) are judged; a not_found sentence is the evidence
    check's business, not this one's."""
    issues: List[str] = list(label_issues(label, entity_type))
    if doc_folded is not None and not label_in_text([label, *aliases], doc_folded):
        issues.append("label_not_in_text")
    usable = [i for i, st in enumerate(statuses) if st != "not_found" and i < len(sentences)]
    named = [i for i in usable if label_in_sentence([label, *aliases], sentences[i])]
    if usable and not named:
        issues.append("label_not_in_evidence")
    elif len(named) < len(usable):
        issues.append("evidence_without_label")
    junk_flags = []
    for i in usable:
        codes = evidence_issues(sentences[i])
        start, end = spans[i] if i < len(spans) else (None, None)
        if overlaps(junk_spans, start, end):
            codes.append("evidence_in_junk_region")
        if overlaps(boiler_spans, start, end):
            codes.append("evidence_boilerplate")
        if i < len(locator_types) and locator_types[i] == "reference_list":
            codes.append("evidence_in_reference_list")
        issues.extend(codes)
        junk_flags.append(any(c in JUNK_EVIDENCE for c in codes))
    if junk_flags and all(junk_flags):
        issues.append("all_evidence_junk")
    issues = [c for c in QUALITY_ISSUES if c in set(issues)]  # vocabulary order, deduplicated
    return {"issues": issues, "hold_back": bool(HOLD_BACK & set(issues))}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("text", type=Path)
    ap.add_argument("--layout", type=Path, help="paper.layout.json, to score per PDF page")
    ap.add_argument("--out", type=Path, help="default: <text stem>.quality.json next to the text")
    args = ap.parse_args(argv)
    text = args.text.read_text(encoding="utf-8")
    layout = None
    if args.layout:
        from scripts.doc_layout import load_layout
        layout = load_layout(args.layout, text)
    report = document_report(text, layout)
    out = args.out or args.text.with_name(args.text.stem + ".quality.json")
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    bad = report["junk_regions"]
    print(f"verdict: {report['verdict']} (junk fraction {report['junk_fraction']}; "
          f"{len(bad)}/{report['n_regions']} regions)")
    for r in bad[:20]:
        print(f"  {r['region']}: {', '.join(r['junk_reasons'])} — {r['excerpt'][:80]!r}")
    if report["boilerplate"]:
        print(f"boilerplate lines: {len(report['boilerplate'])}")
    for a in report["advice"]:
        print(a)
    print(f"report: {out}")
    return 1 if report["verdict"] == "unusable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
