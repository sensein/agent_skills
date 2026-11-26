"""Where in the paper a character offset sits: section, page, figure, table.

Evidence offsets index one text file (`paper.txt`). This module builds a layout
map of that same file — `paper.layout.json` — and answers, for any offset range,
which section, PDF page, figure caption, table or supplementary file it falls in.
Provenance then comes from code instead of from what the model says it read.

The text file is never modified, so offsets computed against it stay valid. The
layout records the text's sha256; `locate` refuses a layout built on other text.

What is detected, and how:

* **Sections** — Markdown headings (`# Results`, Docling / pymupdf4llm output),
  BioC/JATS section tags (`[RESULTS]`, `scripts/fetch_fulltext.py` output), and
  standard heading lines on their own (`Results`, `2. Materials and Methods`,
  `STAR Methods`, `References`, ...). The top-level heading is the section label.
* **Figure and table captions** — a line opening with `Figure 3` / `Fig. 3` /
  `Table 2` / `Supplementary Table S1` / `Extended Data Fig. 2`, or a `[FIG]` /
  `[TABLE]` tag, through the end of its paragraph. A table's block also takes the
  Markdown table rows (`| ... |`) that follow its caption; a Markdown table with
  no caption is still a `table` block.
* **Reference list** — the References / Bibliography section.
* **Figure pages** — with `--pdf`, a page that is a figure or other graphic rather
  than running text (almost no text, or labels scattered over the page with almost
  no sentences). Its text is located as `figure` in section `Figure page`: axis and
  column labels read out as a "sentence" are figure content, not prose.
* **Supplementary material** — a `Supplementary ...` section, or a marker line
  `[SUPPLEMENTARY FILE: <name>]` written when a supplement is appended to the
  text (the AIT prompt asks for one per appended file).
* **Pages** — physical PDF pages, 1-based (not the journal's printed page
  numbers). Taken from form feeds (`\\f`) in the text when present; otherwise from
  the PDF (`--pdf`), by finding each page's own lines in the text. Pages whose
  lines cannot be found unambiguously are listed in `pages_unaligned`, and an
  offset in a gap is attributed to the nearest aligned page before it.

    python -m scripts.doc_layout paper.txt --pdf paper.pdf      # -> paper.layout.json
    python -m scripts.doc_layout paper.txt                      # no pages without a PDF or \\f
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

LAYOUT_VERSION = 1

LOCATOR_TYPES = ("body", "figure_caption", "figure", "table", "supplementary", "reference_list")

# Top-level section headings, canonical label per pattern. Order matters only for
# the label: the first pattern that matches names the section.
_SECTION_NAMES = [
    (r"abstract|summary", "Abstract"),
    (r"introduction|background", "Introduction"),
    (r"results(?:\s+and\s+discussion)?", "Results"),
    (r"discussion", "Discussion"),
    (r"conclusions?", "Conclusion"),
    (r"(?:materials?\s+and\s+methods|methods?\s+and\s+materials?|methods|online\s+methods|"
     r"star\s*\W?\s*methods|experimental\s+procedures|method\s+details)", "Methods"),
    (r"key\s+resources\s+table", "Methods"),
    (r"(?:references|bibliography|literature\s+cited|references\s+and\s+notes)", "References"),
    (r"acknowledge?ments?", "Acknowledgments"),
    (r"(?:data|code|data\s+and\s+code|resource)\s+availability", "Data availability"),
    (r"author\s+contributions?", "Author contributions"),
    (r"(?:competing|declaration\s+of)\s+interests?", "Competing interests"),
    (r"funding", "Funding"),
    (r"figure\s+legends?", "Figure legends"),
    (r"(?:supplementary|supplemental)\s+(?:information|materials?|data|figures|tables|methods)", "Supplementary"),
    (r"extended\s+data", "Supplementary"),
]
_SECTION_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*)?(?:(?:\d+(?:\.\d+)*|[IVX]+)\.?[ \t]+)?"
    r"(?P<name>" + "|".join(p for p, _ in _SECTION_NAMES) + r")[ \t]*:?(?:\*\*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_MD_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>\S.*?)[ \t]*#*[ \t]*$", re.MULTILINE)

# BioC / JATS passage tags written by scripts/fetch_fulltext.py.
_BIOC_SECTIONS = {
    "TITLE": "Title", "ABSTRACT": "Abstract", "INTRO": "Introduction", "METHODS": "Methods",
    "RESULTS": "Results", "DISCUSS": "Discussion", "CONCL": "Conclusion", "REF": "References",
    "SUPPL": "Supplementary", "ACK_FUND": "Acknowledgments", "AUTH_CONT": "Author contributions",
    "COMP_INT": "Competing interests", "CASE": "Results", "APPENDIX": "Supplementary",
}
_BIOC_TAG_RE = re.compile(r"^\[(?P<tag>[A-Z_]+)\][ \t]*$", re.MULTILINE)

_SUPPL_FILE_RE = re.compile(r"^\[SUPPLEMENT(?:ARY)?(?:\s+FILE)?:\s*(?P<name>[^\]\n]+?)\s*\][ \t]*$",
                            re.IGNORECASE | re.MULTILINE)

_CAPTION_RE = re.compile(
    # an optional manuscript line number first ("1947    Figure 1 ...")
    r"^[ \t]*(?:\d{1,5}[ \t]{2,})?(?:#{1,6}[ \t]+)?(?:\*\*)?(?P<label>"
    r"(?P<prefix>Supplementary|Supplemental|Extended\s+Data)?\s*"
    r"(?P<kind>Figure|Fig\.?|Table)\s*(?P<num>S?\d+[A-Za-z]?|[IVX]+)\b)"
    # a caption's number is followed by caption punctuation, the end of the line, or a
    # capitalised title word ("Figure 1 Single-cell ..."); a wrapped body line
    # ("Fig. 3b, Pvalb cells ...", "Figure 3 shows ...", "Figure 2 A-C") is not
    r"(?=[ \t]*(?:\*\*)?[ \t]*(?:[.:|\u2014\u2013]|$)|[ \t]+(?-i:[A-Z][a-z]))",
    re.IGNORECASE | re.MULTILINE,
)
_MD_TABLE_LINE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_PARA_BREAK = re.compile(r"\n[ \t]*\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def _canonical_section(name: str) -> Optional[str]:
    for pattern, label in _SECTION_NAMES:
        if re.fullmatch(pattern, name.strip(), re.IGNORECASE):
            return label
    return None


def find_headings(text: str) -> List[Dict[str, object]]:
    """Top-level section headings, in document order: {start, label, source}."""
    found: Dict[int, Dict[str, object]] = {}
    for m in _SECTION_RE.finditer(text):
        label = _canonical_section(m.group("name"))
        if label:
            found.setdefault(m.start(), {"start": m.start(), "label": label, "source": "heading"})
    for m in _BIOC_TAG_RE.finditer(text):
        label = _BIOC_SECTIONS.get(m.group("tag"))
        if label:
            found.setdefault(m.start(), {"start": m.start(), "label": label, "source": "bioc_tag"})
    # Markdown headings that are not a standard name still start a section when they
    # are level 1-2 and nothing else claims the spot (e.g. "## Single-nucleus RNA-seq
    # identifies ..." inside Results is a subsection, so only level 1 counts).
    for m in _MD_HEADING_RE.finditer(text):
        if m.start() in found:
            continue
        title = m.group("title").strip("* ").strip()
        label = _canonical_section(re.sub(r"^(?:\d+(?:\.\d+)*|[IVX]+)\.?\s+", "", title))
        if label:
            found[m.start()] = {"start": m.start(), "label": label, "source": "markdown"}
    for m in _SUPPL_FILE_RE.finditer(text):
        found[m.start()] = {"start": m.start(), "label": f"Supplementary: {m.group('name')}",
                            "source": "supplementary_marker"}
    return [found[k] for k in sorted(found)]


def build_sections(text: str) -> List[Dict[str, object]]:
    heads = find_headings(text)
    # consecutive duplicates (a BioC tag followed by the printed heading) collapse
    merged: List[Dict[str, object]] = []
    for h in heads:
        if merged and merged[-1]["label"] == h["label"]:
            continue
        merged.append(h)
    out = []
    if merged and merged[0]["start"] > 0 and text[: merged[0]["start"]].strip():
        out.append({"label": "Front matter", "start": 0, "end": merged[0]["start"], "source": "implicit"})
    for i, h in enumerate(merged):
        end = merged[i + 1]["start"] if i + 1 < len(merged) else len(text)
        out.append({"label": h["label"], "start": h["start"], "end": end, "source": h["source"]})
    return out


# --------------------------------------------------------------------------- #
# captions, tables, reference list, supplements
# --------------------------------------------------------------------------- #

def caption_label(prefix: Optional[str], kind: str, num: str) -> str:
    """'Supplementary Figure S2' -> 'Fig. S2'; 'Extended Data Fig. 2' -> 'Extended Data Fig. 2'."""
    k = "Table" if kind.lower().startswith("tab") else "Fig."
    n = num
    if prefix and prefix.lower().startswith("suppl") and not n.upper().startswith("S"):
        n = "S" + n
    lead = "Extended Data " if prefix and prefix.lower().startswith("extended") else ""
    return f"{lead}{k} {n}"


def _paragraph_end(text: str, pos: int) -> int:
    m = _PARA_BREAK.search(text, pos)
    return m.start() if m else len(text)


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _table_rows_end(text: str, pos: int) -> Optional[int]:
    """If Markdown table rows start within the next paragraph after `pos`, their end."""
    m = re.compile(r"\n[ \t]*\n?").match(text, pos)
    at = m.end() if m else pos
    lines_end = None
    while at < len(text):
        nl = text.find("\n", at)
        line = text[at: nl if nl >= 0 else len(text)]
        if _MD_TABLE_LINE.match(line):
            lines_end = nl if nl >= 0 else len(text)
            at = (nl + 1) if nl >= 0 else len(text)
            continue
        break
    return lines_end


def build_blocks(text: str, sections: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    blocks: List[Dict[str, object]] = []
    claimed: List[tuple] = []

    def add(kind: str, label: str, start: int, end: int, source: str) -> None:
        if end <= start:
            return
        blocks.append({"type": kind, "label": label, "start": start, "end": end, "source": source})
        claimed.append((start, end))

    caps = list(_CAPTION_RE.finditer(text))
    for k, m in enumerate(caps):
        start = _line_start(text, m.start("label"))
        kind = "table" if m.group("kind").lower().startswith("tab") else "figure_caption"
        end = _paragraph_end(text, m.end())
        if k + 1 < len(caps):  # legends printed back to back, with no blank line between
            end = min(end, _line_start(text, caps[k + 1].start("label")))
        if kind == "table":
            rows_end = _table_rows_end(text, end)
            end = rows_end or end
        add(kind, caption_label(m.group("prefix"), m.group("kind"), m.group("num")), start, end, "caption")

    for m in _BIOC_TAG_RE.finditer(text):
        tag = m.group("tag")
        if tag not in ("FIG", "TABLE"):
            continue
        body_start = m.end() + 1 if m.end() < len(text) else m.end()
        if any(s <= body_start < e for s, e in claimed):
            continue
        end = _paragraph_end(text, body_start)
        cap = _CAPTION_RE.match(text, body_start)
        label = caption_label(cap.group("prefix"), cap.group("kind"), cap.group("num")) if cap else ""
        add("table" if tag == "TABLE" else "figure_caption", label, m.start(), end, "bioc_tag")

    # Markdown tables nobody's caption claimed
    pos = 0
    while pos < len(text):
        nl = text.find("\n", pos)
        line_end = nl if nl >= 0 else len(text)
        if _MD_TABLE_LINE.match(text[pos:line_end]) and not any(s <= pos < e for s, e in claimed):
            end = line_end
            nxt = line_end + 1
            while nxt < len(text):
                n2 = text.find("\n", nxt)
                le = n2 if n2 >= 0 else len(text)
                if not _MD_TABLE_LINE.match(text[nxt:le]):
                    break
                end, nxt = le, le + 1
            add("table", "", pos, end, "markdown_table")
            pos = end + 1
            continue
        pos = line_end + 1

    for s in sections:
        if s["label"] == "References":
            add("reference_list", "", int(s["start"]), int(s["end"]), "section")
        elif str(s["label"]).startswith("Supplementary"):
            name = str(s["label"]).split(": ", 1)[1] if ": " in str(s["label"]) else ""
            add("supplementary", name, int(s["start"]), int(s["end"]), "section")
    blocks.sort(key=lambda b: (b["start"], -b["end"]))
    return blocks


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

def pages_from_form_feeds(text: str) -> List[Dict[str, int]]:
    out, start = [], 0
    for i, part in enumerate(text.split("\f"), 1):
        out.append({"page": i, "start": start, "end": start + len(part)})
        start += len(part) + 1
    return out


def align_pages(text: str, page_texts: Sequence[str], *, min_probe: int = 25) -> Dict[str, object]:
    """Attribute character ranges of `text` to PDF pages by locating each page's own
    lines in it. Only lines that occur exactly once in the text are used as probes,
    so a repeated header or a boilerplate sentence cannot pin a page to the wrong
    place. Returns {pages, unaligned}."""
    from scripts.ait_evidence import Normalized  # one normaliser for every match

    doc = Normalized(text)
    # probes are normalised like the document: line numbers dropped when it has them
    normalize = lambda line: Normalized(line, numbered=doc.line_numbered).text  # noqa: E731
    anchors = []  # (page, norm_min, norm_max_end)
    unaligned = []
    for p, ptext in enumerate(page_texts, 1):
        hits = []
        for line in (ptext or "").splitlines():
            probe = normalize(line)
            if len(probe) < min_probe:
                continue
            first = doc.text.find(probe)
            if first < 0 or doc.text.find(probe, first + 1) >= 0:
                continue
            hits.append((first, first + len(probe)))
        if not hits:
            unaligned.append(p)
            continue
        anchors.append((p, min(h[0] for h in hits), max(h[1] for h in hits)))

    # keep pages in reading order: a page whose anchor starts before the previous
    # page's is mis-ordered text, not a page boundary
    ordered = []
    for a in anchors:
        if ordered and a[1] < ordered[-1][1]:
            unaligned.append(a[0])
            continue
        ordered.append(a)

    pages = []
    for i, (p, lo, hi) in enumerate(ordered):
        if i == 0:
            n_start = 0
        else:
            prev_hi = ordered[i - 1][2]
            n_start = prev_hi + max(0, lo - prev_hi) // 2 if lo >= prev_hi else lo
        n_end = len(doc.text)
        if i + 1 < len(ordered):
            nxt_lo = ordered[i + 1][1]
            n_end = hi + max(0, nxt_lo - hi) // 2 if nxt_lo >= hi else nxt_lo
        raw_start = 0 if i == 0 else (doc.index[n_start] if n_start < len(doc.index) else len(text))
        raw_end = len(text) if i + 1 == len(ordered) else (doc.index[n_end] if n_end < len(doc.index) else len(text))
        pages.append({"page": p, "start": raw_start, "end": raw_end})
    for i in range(1, len(pages)):  # contiguous, no gaps or overlaps
        pages[i]["start"] = pages[i - 1]["end"]
    return {"pages": pages, "unaligned": sorted(set(unaligned))}


FIGURE_PAGE_MAX_CHARS = 400        # an image page with less text than this is a picture (plus a watermark)
FIGURE_PAGE_WHITESPACE = 0.6       # ... or text scattered over the page (labels, axes) ...
FIGURE_PAGE_SENTENCES = 1.0        # ... with fewer sentence endings than this per 100 words


def is_figure_page(page_text: str, images: int = 0) -> bool:
    """A PDF page that is a figure or other graphic, not running text.

    Running text, title pages and reference lists alike are dense and full of
    sentence endings. A figure page is either an image with almost no text (often
    only the preprint watermark over it) or its text is labels scattered across the
    page: mostly whitespace and almost no sentences. A short page with no image (the
    last lines of a reference list) is not a figure."""
    stripped = (page_text or "").strip()
    if not stripped:
        return False
    if len(stripped) < FIGURE_PAGE_MAX_CHARS:
        return images > 0
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", page_text)
    if not words:
        return True
    whitespace = sum(c.isspace() for c in page_text) / len(page_text)
    sentences = len(re.findall(r"[a-z]{2}[.;:](?:\s|$)", page_text)) * 100 / len(words)
    return whitespace > FIGURE_PAGE_WHITESPACE and sentences < FIGURE_PAGE_SENTENCES


def pdf_page_texts(pdf: Path) -> List[str]:
    """Per-page text from a PDF, PyMuPDF first, pdfminer as fallback."""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(str(pdf))
        try:
            out = []
            for page in doc:
                try:
                    out.append(page.get_text("text", sort=True))
                except TypeError:
                    out.append(page.get_text())
            return out
        finally:
            doc.close()
    except ImportError:
        pass
    from pdfminer.high_level import extract_text  # raises ImportError if neither is installed
    from pdfminer.pdfpage import PDFPage
    with open(pdf, "rb") as fh:
        n = sum(1 for _ in PDFPage.get_pages(fh))
    return [extract_text(str(pdf), page_numbers=[i]) for i in range(n)]


# --------------------------------------------------------------------------- #
# build / load / locate
# --------------------------------------------------------------------------- #

def pdf_page_images(pdf: Path) -> List[int]:
    """Images per PDF page (PyMuPDF); zeros when it is not installed."""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except ImportError:
        return []
    doc = fitz.open(str(pdf))
    try:
        return [len(page.get_images()) for page in doc]
    finally:
        doc.close()


def build_layout(text: str, *, page_texts: Optional[Sequence[str]] = None,
                 page_images: Optional[Sequence[int]] = None,
                 source: Optional[str] = None) -> Dict[str, object]:
    sections = build_sections(text)
    layout: Dict[str, object] = {
        "layout_version": LAYOUT_VERSION,
        "text_sha256": sha256_text(text),
        "text_chars": len(text),
        "source": source,
        "sections": sections,
        "blocks": build_blocks(text, sections),
        "pages": [],
        "page_method": "none",
        "pages_unaligned": [],
    }
    if "\f" in text:
        layout["pages"], layout["page_method"] = pages_from_form_feeds(text), "form_feed"
    elif page_texts:
        aligned = align_pages(text, page_texts)
        layout["pages"], layout["pages_unaligned"] = aligned["pages"], aligned["unaligned"]
        layout["page_method"] = "pdf_line_alignment"
        layout["pdf_pages"] = len(page_texts)
    if page_texts:
        images = list(page_images or [])
        figure_pages = [n for n, t in enumerate(page_texts, 1)
                        if is_figure_page(t, images[n - 1] if n - 1 < len(images) else 0)]
        layout["figure_pages"] = figure_pages
        for p in layout["pages"]:
            if p["page"] in figure_pages:
                layout["blocks"].append({"type": "figure", "label": "", "start": p["start"], "end": p["end"],
                                         "source": "page_profile"})
        layout["blocks"].sort(key=lambda b: (b["start"], -b["end"]))
    return layout


def load_layout(path: Path, text: Optional[str] = None) -> Dict[str, object]:
    layout = json.loads(Path(path).read_text(encoding="utf-8"))
    if text is not None and layout.get("text_sha256") != sha256_text(text):
        raise ValueError(f"{path} was built on different text (sha256 mismatch); rebuild it with "
                         f"`python -m scripts.doc_layout <paper.txt>` so its offsets match")
    return layout


def _containing(items: Iterable[Dict[str, object]], start: int) -> List[Dict[str, object]]:
    return [it for it in items if int(it["start"]) <= start < int(it["end"])]


def locate(layout: Dict[str, object], start: Optional[int], end: Optional[int]) -> Dict[str, str]:
    """{section, page, locator_type, locator} for raw [start, end); 'NA' when unknown."""
    na = {"section": "NA", "page": "NA", "locator_type": "NA", "locator": "NA"}
    if start is None or end is None or start < 0 or end <= start:
        return na
    out = dict(na)
    sec = _containing(layout.get("sections") or [], start)
    if sec:
        out["section"] = str(sec[-1]["label"])
    pages = layout.get("pages") or []
    if pages:
        starts = [int(p["start"]) for p in pages]
        i = max(0, bisect.bisect_right(starts, start) - 1)
        j = max(0, bisect.bisect_right(starts, max(start, end - 1)) - 1)
        a, b = pages[i]["page"], pages[j]["page"]
        out["page"] = str(a) if a == b else f"{a}-{b}"
    blocks = _containing(layout.get("blocks") or [], start)
    # the innermost block wins: a figure caption inside a supplement is a figure caption,
    # and a legend printed on a figure page is a caption, not figure text
    rank = {"figure_caption": 0, "table": 0, "figure": 0, "reference_list": 1, "supplementary": 2}
    if blocks:
        best = sorted(blocks, key=lambda b: (rank.get(str(b["type"]), 3), int(b["end"]) - int(b["start"])))[0]
        out["locator_type"] = str(best["type"])
        out["locator"] = str(best["label"]) if best.get("label") else "NA"
        if best["type"] == "figure":
            # figure pages often follow the reference list; they are not part of it
            out["section"] = "Figure page"
    else:
        out["locator_type"] = "body"
    return out


def format_source(section: str, locator: str, page: str) -> str:
    """'Results; Fig. 3; p. 4' — the review sheet's `source_in_paper`. Unknown parts are left out."""
    parts = []
    if section and section != "NA":
        parts.append(section)
    if locator and locator != "NA":
        parts.append(locator)
    if page and page != "NA":
        parts.append(("pp. " if "-" in page else "p. ") + page)
    return "; ".join(parts) if parts else "NA"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("text", type=Path, help="the exact text file evidence offsets index (paper.txt)")
    ap.add_argument("--pdf", type=Path, help="the PDF the text was extracted from, for page numbers")
    ap.add_argument("--out", type=Path, help="default: <text stem>.layout.json next to the text")
    args = ap.parse_args(argv)

    text = args.text.read_text(encoding="utf-8")
    page_texts = pdf_page_texts(args.pdf) if args.pdf else None
    page_images = pdf_page_images(args.pdf) if args.pdf else None
    layout = build_layout(text, page_texts=page_texts, page_images=page_images, source=str(args.pdf or args.text))
    out = args.out or args.text.with_name(args.text.stem + ".layout.json")
    out.write_text(json.dumps(layout, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    kinds: Dict[str, int] = {}
    for b in layout["blocks"]:
        kinds[b["type"]] = kinds.get(b["type"], 0) + 1
    print(f"sections: {', '.join(s['label'] for s in layout['sections']) or 'none found'}")
    print(f"blocks: {kinds or 'none'}")
    if layout.get("figure_pages"):
        print(f"figure pages: {layout['figure_pages']}")
    print(f"pages: {len(layout['pages'])} ({layout['page_method']})"
          + (f"; unaligned {layout['pages_unaligned']}" if layout["pages_unaligned"] else ""))
    if not layout["pages"]:
        print("no page numbers: pass --pdf, or keep form feeds between pages in the text")
    print(f"layout: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
